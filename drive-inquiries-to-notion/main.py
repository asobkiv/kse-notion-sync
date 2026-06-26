"""
Google Drive (Inquiries) → Notion: Requests Dashboard sync.
Runs via GitHub Actions on a daily schedule.

WHAT IT DOES
  Watches a Drive folder with two subfolders, Requests/ and Responses/.

  Requests/  → one Notion page per file in the "📩 Requests Dashboard" database.
               File attached to the "Files & media request" property.
               docx text (and pdf text) extracted into the page body so Notion's
               AI Autofill / Database Agent can classify it. Status = "Received".

  Responses/ → a response belongs on its request's page ("Response files"), but
               filename matching is unreliable. So each response becomes a STUB
               page: file in "Response files", Status = "Triage / qualification",
               title "⚠️ RESPONSE — link to request: <file>", plus the extracted
               text and the TOP-3 candidate request pages (by content similarity)
               as links for one-click manual linking.
               Optional INQUIRIES_AUTO_ATTACH=true: on a single confident match,
               the file is attached straight to that request's "Response files"
               instead of creating a stub. Off by default (no wrong-merge risk).

  Dedup is by filename: each run scans both subfolders and skips any file whose
  name already appears attached in Notion ("Files & media request" for requests,
  "Response files" for responses). No Drive files are moved or modified.

Required env vars (GitHub Secrets):
  INQUIRIES_NOTION_TOKEN       — Notion integration secret (access to the DB)
  INQUIRIES_DB_ID              — Requests Dashboard database ID
  INQUIRIES_DRIVE_FOLDER_ID    — parent Drive folder (Inquiries_TM)
  GOOGLE_SERVICE_ACCOUNT_JSON  — Google service account JSON (Drive read/write)

Optional env vars:
  INQUIRIES_AUTO_ATTACH  — "true" to auto-attach confident response matches
  INQUIRIES_DRY_RUN      — "true" to log actions without writing anything
  INQUIRIES_MAX_CREATES  — cap pages created per run (0 = unlimited)
"""

import io
import json
import logging
import os
import re
import time

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

import docx          # python-docx
from pypdf import PdfReader

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

# ── CONFIG (via GitHub Secrets) ───────────────────────────────

DB_ID            = os.environ.get("INQUIRIES_DB_ID", "")
DRIVE_FOLDER_ID  = os.environ.get("INQUIRIES_DRIVE_FOLDER_ID", "")
AUTO_ATTACH      = bool(os.environ.get("INQUIRIES_AUTO_ATTACH"))
DRY_RUN          = bool(os.environ.get("INQUIRIES_DRY_RUN"))
_max             = os.environ.get("INQUIRIES_MAX_CREATES", "")
MAX_CREATES      = int(_max) if _max.strip().isdigit() else 0

# Notion property names (from the Requests Dashboard schema)
P_TITLE          = "Request(case)"
P_REQUEST_FILES  = "Files & media request"
P_RESPONSE_FILES = "Response files"
P_STATUS         = "Status"
STATUS_REQUEST   = "Received"
STATUS_RESPONSE  = "Triage / qualification"

# Properties read to build the candidate-matching profile of each request
P_SUMMARY        = "Summary"
P_CONTACT        = "Contact person"
P_ORG            = "Organization"

NOTION_VERSION   = "2026-03-11"   # required for the file-upload API + data sources
NOTION_API       = "https://api.notion.com/v1"

MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MIME_PDF  = "application/pdf"

# ── MAIN ──────────────────────────────────────────────────────

def main():
    token   = os.environ["INQUIRIES_NOTION_TOKEN"]
    sa_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    if not DB_ID:           raise RuntimeError("Set INQUIRIES_DB_ID")
    if not DRIVE_FOLDER_ID: raise RuntimeError("Set INQUIRIES_DRIVE_FOLDER_ID")

    drive = build_drive_service(sa_json)
    ds_id = get_data_source_id(token)
    log.info(f"Data source: {ds_id}")

    req_folder  = find_subfolder(drive, DRIVE_FOLDER_ID, "Requests")
    resp_folder = find_subfolder(drive, DRIVE_FOLDER_ID, "Responses")
    if not req_folder:  raise RuntimeError("No 'Requests' subfolder in the Drive folder")
    if not resp_folder: log.info("No 'Responses' subfolder — responses skipped this run")

    requests_in  = list_files(drive, req_folder)
    responses_in = list_files(drive, resp_folder) if resp_folder else []
    log.info(f"{len(requests_in)} request file(s), {len(responses_in)} response file(s)")

    if DRY_RUN:
        log.info("DRY RUN — nothing will be created or moved in Notion/Drive")

    existing, req_file_names, resp_file_names = load_existing(token, ds_id)
    log.info(f"{len(existing)} existing pages | {len(req_file_names)} request file(s), "
             f"{len(resp_file_names)} response file(s) already in Notion")

    created = stubs = attached = skipped = errors = 0

    # ── Requests ──────────────────────────────────────────────
    for f in requests_in:
        try:
            # Dedup by attached filename — no Drive files are moved
            if f["name"] in req_file_names:
                skipped += 1
                continue
            if hit_cap(created + stubs):
                break

            title = clean_title(f["name"])
            text = extract_text(drive, f)
            if DRY_RUN:
                log.info(f"  WOULD CREATE request page: {title}")
                created += 1
                continue

            page_id = create_request_page(token, ds_id, title, text)
            attach_file(token, drive, f, page_id, P_REQUEST_FILES)
            req_file_names.add(f["name"])
            created += 1
            log.info(f"  Created request: {title}")
            time.sleep(0.35)
        except Exception as e:
            log.info(f"  Error on request '{f['name']}': {e}")
            errors += 1

    # ── Responses ─────────────────────────────────────────────
    for f in responses_in:
        try:
            if f["name"] in resp_file_names:
                skipped += 1
                continue
            if hit_cap(created + stubs):
                break

            text = extract_text(drive, f)
            candidates = rank_candidates(existing, text, f["name"])

            if AUTO_ATTACH and is_confident(candidates):
                target = candidates[0]
                if DRY_RUN:
                    log.info(f"  WOULD ATTACH response '{f['name']}' → {target['title']}")
                    attached += 1
                    continue
                append_file(token, drive, f, target["id"], P_RESPONSE_FILES)
                resp_file_names.add(f["name"])
                attached += 1
                log.info(f"  Attached response '{f['name']}' → {target['title']}")
                time.sleep(0.35)
                continue

            # Default: stub page with candidates for manual/AI linking
            if DRY_RUN:
                cand = ", ".join(c["title"] for c in candidates[:3]) or "(none)"
                log.info(f"  WOULD STUB response '{f['name']}' | candidates: {cand}")
                stubs += 1
                continue

            page_id = create_response_stub(token, ds_id, f["name"], text, candidates)
            attach_file(token, drive, f, page_id, P_RESPONSE_FILES)
            resp_file_names.add(f["name"])
            stubs += 1
            log.info(f"  Stub for response: {f['name']}")
            time.sleep(0.35)
        except Exception as e:
            log.info(f"  Error on response '{f['name']}': {e}")
            errors += 1

    log.info(f"Done. Requests created: {created} | Response stubs: {stubs} | "
             f"Auto-attached: {attached} | Skipped: {skipped} | Errors: {errors}")


def hit_cap(n):
    if MAX_CREATES and n >= MAX_CREATES:
        log.info(f"Hit MAX_CREATES={MAX_CREATES} — stopping. Re-run to continue.")
        return True
    return False


# ── GOOGLE DRIVE ──────────────────────────────────────────────

def build_drive_service(sa_json_str):
    creds = service_account.Credentials.from_service_account_info(
        json.loads(sa_json_str),
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_subfolder(drive, parent_id, name):
    safe = name.replace("'", "\\'")
    res = drive.files().list(
        q=(f"name='{safe}' and '{parent_id}' in parents and trashed=false "
           f"and mimeType='application/vnd.google-apps.folder'"),
        fields="files(id)", pageSize=1,
    ).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def list_files(drive, folder_id):
    items, page = [], None
    while True:
        res = drive.files().list(
            q=f"'{folder_id}' in parents and trashed=false "
              f"and mimeType != 'application/vnd.google-apps.folder'",
            fields="nextPageToken, files(id, name, mimeType)",
            pageSize=100, pageToken=page,
        ).execute()
        items.extend(res.get("files", []))
        page = res.get("nextPageToken")
        if not page:
            break
    return items


def download_bytes(drive, file_id):
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, drive.files().get_media(fileId=file_id))
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


# ── TEXT EXTRACTION ───────────────────────────────────────────

def extract_text(drive, f):
    data = download_bytes(drive, f["id"])
    mime = f.get("mimeType", "")
    try:
        if mime == MIME_DOCX or f["name"].lower().endswith(".docx"):
            d = docx.Document(io.BytesIO(data))
            return "\n".join(p.text for p in d.paragraphs if p.text.strip())
        if mime == MIME_PDF or f["name"].lower().endswith(".pdf"):
            reader = PdfReader(io.BytesIO(data))
            return "\n".join((pg.extract_text() or "") for pg in reader.pages).strip()
    except Exception as e:
        log.info(f"  Could not extract text from {f['name']}: {e}")
    return ""


# ── CANDIDATE MATCHING (responses → requests) ─────────────────

def tokenize(text):
    return {w for w in re.findall(r"\w{4,}", (text or "").lower())}


def rank_candidates(existing, resp_text, resp_filename):
    resp_blob = f"{resp_filename}\n{resp_text}".lower()
    resp_tokens = tokenize(resp_blob)

    scored = []
    for r in existing:
        s = 0
        if r["org"] and r["org"].lower() in resp_blob:
            s += 3
        if r["contact"] and r["contact"].lower() in resp_blob:
            s += 3
        s += len(tokenize(r["title"]) & resp_tokens)
        s += len(tokenize(r["summary"]) & resp_tokens)
        if s > 0:
            scored.append({**r, "score": s})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:5]


def is_confident(candidates):
    if not candidates:
        return False
    top = candidates[0]["score"]
    second = candidates[1]["score"] if len(candidates) > 1 else 0
    return top >= 4 and (top - second) >= 3


# ── NOTION ────────────────────────────────────────────────────

def notion_headers(token, json_body=True):
    h = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def get_data_source_id(token):
    resp = requests.get(f"{NOTION_API}/databases/{DB_ID}",
                        headers=notion_headers(token), timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Get database → HTTP {resp.status_code}: {resp.text[:300]}")
    sources = resp.json().get("data_sources", [])
    if not sources:
        raise RuntimeError("Database has no data sources")
    return sources[0]["id"]


def load_existing(token, ds_id):
    """Return (request profiles, set of request filenames, set of response
    filenames) already present in Notion — used for candidate matching and for
    name-based dedup (so no Drive files need to be moved)."""
    out, cursor = [], None
    req_files, resp_files = set(), set()
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(f"{NOTION_API}/data_sources/{ds_id}/query",
                             headers=notion_headers(token), json=body, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"Query → HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        for page in data["results"]:
            props = page["properties"]
            out.append({
                "id":      page["id"],
                "url":     page.get("url", ""),
                "title":   plain_title(props.get(P_TITLE)),
                "summary": plain_text(props.get(P_SUMMARY)),
                "contact": plain_text(props.get(P_CONTACT)),
                "org":     plain_text(props.get(P_ORG)),
            })
            req_files.update(file_names(props.get(P_REQUEST_FILES)))
            resp_files.update(file_names(props.get(P_RESPONSE_FILES)))
        if data.get("has_more"):
            cursor = data["next_cursor"]
            time.sleep(0.3)
        else:
            break
    return out, req_files, resp_files


def file_names(prop):
    return [it.get("name", "") for it in (prop or {}).get("files", []) if it.get("name")]


def plain_title(prop):
    if prop and prop.get("title"):
        return "".join(t["plain_text"] for t in prop["title"])
    return ""


def plain_text(prop):
    if prop and prop.get("rich_text"):
        return "".join(t["plain_text"] for t in prop["rich_text"])
    return ""


def create_request_page(token, ds_id, title, text):
    children = text_to_blocks(text)
    body = {
        "parent": {"type": "data_source_id", "data_source_id": ds_id},
        "properties": {
            P_TITLE:  {"title": [{"text": {"content": title[:2000]}}]},
            P_STATUS: {"status": {"name": STATUS_REQUEST}},
        },
        "children": children,
    }
    return _create_page(token, body)


def create_response_stub(token, ds_id, filename, text, candidates):
    children = []
    children.append(callout(
        "RESPONSE document. Link it to the matching request below, then move this "
        "file into that request's “Response files”.", "⚠️"))
    if candidates:
        children.append(heading("Candidate requests"))
        for c in candidates[:3]:
            children.append(bookmark_or_link(c))
    else:
        children.append(paragraph("No candidate requests found by content match."))
    children.append(heading("Extracted text"))
    children.extend(text_to_blocks(text))

    body = {
        "parent": {"type": "data_source_id", "data_source_id": ds_id},
        "properties": {
            P_TITLE:  {"title": [{"text": {"content": f"⚠️ RESPONSE — link to request: {filename}"[:2000]}}]},
            P_STATUS: {"status": {"name": STATUS_RESPONSE}},
        },
        "children": children[:90],
    }
    return _create_page(token, body)


def _create_page(token, body):
    resp = requests.post(f"{NOTION_API}/pages", headers=notion_headers(token),
                         json=body, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Create page → HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()["id"]


# ── NOTION FILE UPLOAD + ATTACH ───────────────────────────────

def upload_file(token, drive, f):
    """Upload a Drive file's bytes to Notion, return the file_upload id."""
    data = download_bytes(drive, f["id"])
    content_type = f.get("mimeType") or "application/octet-stream"

    create = requests.post(f"{NOTION_API}/file_uploads",
                           headers=notion_headers(token),
                           json={"filename": f["name"], "content_type": content_type},
                           timeout=30)
    if create.status_code != 200:
        raise RuntimeError(f"File upload create → HTTP {create.status_code}: {create.text[:300]}")
    upload_id = create.json()["id"]

    send = requests.post(f"{NOTION_API}/file_uploads/{upload_id}/send",
                         headers=notion_headers(token, json_body=False),
                         files={"file": (f["name"], data, content_type)},
                         timeout=120)
    if send.status_code != 200:
        raise RuntimeError(f"File upload send → HTTP {send.status_code}: {send.text[:300]}")
    return upload_id


def attach_file(token, drive, f, page_id, prop_name):
    """Set a file property to a single newly-uploaded file (new pages)."""
    upload_id = upload_file(token, drive, f)
    _patch_files(token, page_id, prop_name,
                 [{"type": "file_upload", "file_upload": {"id": upload_id}, "name": f["name"]}])


def append_file(token, drive, f, page_id, prop_name):
    """Append a file to an existing page's file property (preserves existing)."""
    existing = _read_files(token, page_id, prop_name)
    upload_id = upload_file(token, drive, f)
    existing.append({"type": "file_upload", "file_upload": {"id": upload_id}, "name": f["name"]})
    _patch_files(token, page_id, prop_name, existing)


def _read_files(token, page_id, prop_name):
    resp = requests.get(f"{NOTION_API}/pages/{page_id}", headers=notion_headers(token), timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Read page → HTTP {resp.status_code}: {resp.text[:200]}")
    files = resp.json()["properties"].get(prop_name, {}).get("files", [])
    keep = []
    for item in files:
        # Re-send existing external files as-is; previously-uploaded files come
        # back as type "file" and can be re-referenced by their existing name only.
        if item.get("type") == "external":
            keep.append({"type": "external", "name": item.get("name", "file"),
                         "external": {"url": item["external"]["url"]}})
    return keep


def _patch_files(token, page_id, prop_name, files_value):
    resp = requests.patch(f"{NOTION_API}/pages/{page_id}", headers=notion_headers(token),
                          json={"properties": {prop_name: {"files": files_value}}}, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Attach file → HTTP {resp.status_code}: {resp.text[:300]}")


# ── NOTION BLOCK HELPERS ──────────────────────────────────────

def text_to_blocks(text, max_blocks=70):
    if not text:
        return [paragraph("(no text extracted)")]
    chunks, blocks = [], []
    for para in text.split("\n"):
        para = para.strip()
        while len(para) > 1900:
            chunks.append(para[:1900]); para = para[1900:]
        if para:
            chunks.append(para)
    for c in chunks[:max_blocks]:
        blocks.append(paragraph(c))
    if len(chunks) > max_blocks:
        blocks.append(paragraph("… (text truncated)"))
    return blocks


def paragraph(text):
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}}


def heading(text):
    return {"object": "block", "type": "heading_3",
            "heading_3": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}}


def callout(text, emoji):
    return {"object": "block", "type": "callout",
            "callout": {"icon": {"type": "emoji", "emoji": emoji},
                        "rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}}


def bookmark_or_link(candidate):
    label = f"{candidate['title']}  (score {candidate['score']})"
    rich = [{"type": "text", "text": {"content": label,
             "link": {"url": candidate["url"]} if candidate.get("url") else None}}]
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": rich}}


# ── HELPERS ───────────────────────────────────────────────────

def clean_title(filename):
    base = re.sub(r"\.[^.]+$", "", filename)        # drop extension
    base = re.sub(r"[_]+", " ", base).strip()
    return base or filename


if __name__ == "__main__":
    main()
