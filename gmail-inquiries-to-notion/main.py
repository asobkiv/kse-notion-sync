"""
Gmail → Notion: Requests Dashboard sync.
Runs via GitHub Actions daily at 10:00 Kyiv.

Reads a dedicated inquiries mailbox and creates one Notion page per email in the
"📩 Requests Dashboard" database:
  Request(case)        = email subject
  Contact person       = sender name
  Organization         = sender email domain
  Received date        = email date
  Email ID             = Gmail message id (used for dedup)
  Classification status = Queued   (so the classification agent picks it up)
  Status               = Received  (workflow status)
  page body            = email text (so Notion AI can classify)
  attachments          → "Files & media request" (uploaded to Notion)

Dedup is by Gmail message id stored in the "Email ID" property — each run rescans
the mailbox and skips messages already imported. The mailbox is never modified
(gmail.readonly).

Required env vars (GitHub Secrets):
  GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN  — OAuth credentials
  INQUIRIES_NOTION_TOKEN   — Notion integration secret (access to the DB)
  INQUIRIES_DB_ID          — Requests Dashboard database ID

Optional env vars:
  GMAIL_QUERY        — Gmail search filter (default "in:inbox")
  GMAIL_DRY_RUN      — "true" to log without writing anything
  GMAIL_MAX_CREATES  — cap pages created per run (0 = unlimited)
"""

import base64
import logging
import os
import re
import time
from email.utils import parsedate_to_datetime

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

# ── CONFIG (via GitHub Secrets) ───────────────────────────────

DB_ID       = os.environ.get("INQUIRIES_DB_ID", "")
GMAIL_QUERY = os.environ.get("GMAIL_QUERY") or "in:inbox"
DRY_RUN     = bool(os.environ.get("GMAIL_DRY_RUN"))
_max        = os.environ.get("GMAIL_MAX_CREATES", "")
MAX_CREATES = int(_max) if _max.strip().isdigit() else 0

# Notion property names (Requests Dashboard schema)
P_TITLE   = "Request(case)"
P_FILES   = "Files & media request"
P_EMAILID = "Email ID"
P_CONTACT = "Contact person"
P_ORG     = "Organization"
P_RECV    = "Received date"
P_CLASS   = "Classification status"
P_STATUS  = "Status"

NOTION_VERSION = "2026-03-11"
NOTION_API     = "https://api.notion.com/v1"
GMAIL_SCOPES   = ["https://www.googleapis.com/auth/gmail.readonly"]

# ── MAIN ──────────────────────────────────────────────────────

def main():
    token = os.environ["INQUIRIES_NOTION_TOKEN"]
    if not DB_ID:
        raise RuntimeError("Set INQUIRIES_DB_ID")

    gmail = build_gmail()
    ds_id = get_data_source_id(token)
    log.info(f"Data source: {ds_id}")

    seen = load_existing_email_ids(token, ds_id)
    log.info(f"{len(seen)} emails already in Notion")

    msg_ids = list_message_ids(gmail, GMAIL_QUERY)
    log.info(f"{len(msg_ids)} message(s) matched query '{GMAIL_QUERY}'")
    if DRY_RUN:
        log.info("DRY RUN — nothing will be created in Notion")

    created = skipped = errors = 0

    for mid in msg_ids:
        try:
            if mid in seen:
                skipped += 1
                continue
            if MAX_CREATES and created >= MAX_CREATES:
                log.info(f"Hit GMAIL_MAX_CREATES={MAX_CREATES} — stopping. Re-run to continue.")
                break

            msg = gmail.users().messages().get(userId="me", id=mid, format="full").execute()
            payload = msg.get("payload", {})
            subject = header(payload, "Subject") or "(no subject)"
            contact, _email, org = parse_from(header(payload, "From"))
            recv = parse_date(header(payload, "Date"))
            body_text = extract_body(payload)

            if DRY_RUN:
                log.info(f"  WOULD CREATE: {subject}  | from {contact} <{org}>")
                created += 1
                continue

            page_id = create_request_page(token, ds_id, subject, body_text, contact, org, recv, mid)

            atts = extract_attachments(gmail, mid, payload)
            if atts:
                uploaded = [(upload_to_notion(token, a["name"], a["mime"], a["data"]), a["name"]) for a in atts]
                attach_files(token, page_id, P_FILES, uploaded)

            seen.add(mid)
            created += 1
            log.info(f"  Created: {subject}  ({len(atts)} attachment(s))")
            time.sleep(0.35)
        except Exception as e:
            log.info(f"  Error on message {mid}: {e}")
            errors += 1

    log.info(f"Done. Created: {created} | Skipped: {skipped} | Errors: {errors}")


# ── GMAIL ─────────────────────────────────────────────────────

def build_gmail():
    creds = Credentials(
        None,
        refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        client_id=os.environ["GMAIL_CLIENT_ID"],
        client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=GMAIL_SCOPES,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def list_message_ids(gmail, query):
    ids, page = [], None
    while True:
        r = gmail.users().messages().list(
            userId="me", q=query, pageSize=100, pageToken=page).execute()
        ids.extend(m["id"] for m in r.get("messages", []))
        page = r.get("nextPageToken")
        if not page:
            break
    return ids


def header(payload, name):
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def parse_from(value):
    m = re.match(r'^\s*"?([^"<]*)"?\s*<([^>]+)>', value or "")
    if m:
        name, email = m.group(1).strip(), m.group(2).strip()
    else:
        name, email = "", (value or "").strip()
    domain = email.split("@")[-1] if "@" in email else ""
    return (name or email), email, domain


def parse_date(value):
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except Exception:
        return None


def _b64(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def extract_body(payload):
    text = _walk_text(payload, "text/plain")
    if not text:
        html = _walk_text(payload, "text/html")
        text = re.sub(r"<[^>]+>", " ", html) if html else ""
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def _walk_text(part, mime):
    if part.get("mimeType") == mime and part.get("body", {}).get("data"):
        try:
            return _b64(part["body"]["data"]).decode("utf-8", "replace")
        except Exception:
            return ""
    out = ""
    for p in part.get("parts", []) or []:
        out += _walk_text(p, mime)
    return out


def extract_attachments(gmail, mid, payload):
    out = []

    def walk(part):
        fn = part.get("filename")
        body = part.get("body", {})
        if fn and body.get("attachmentId"):
            att = gmail.users().messages().attachments().get(
                userId="me", messageId=mid, id=body["attachmentId"]).execute()
            out.append({"name": fn,
                        "mime": part.get("mimeType", "application/octet-stream"),
                        "data": _b64(att["data"])})
        for p in part.get("parts", []) or []:
            walk(p)

    walk(payload)
    return out


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


def load_existing_email_ids(token, ds_id):
    ids, cursor = set(), None
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
            v = plain_text(page["properties"].get(P_EMAILID))
            if v:
                ids.add(v)
        if data.get("has_more"):
            cursor = data["next_cursor"]
            time.sleep(0.3)
        else:
            break
    return ids


def plain_text(prop):
    if prop and prop.get("rich_text"):
        return "".join(t["plain_text"] for t in prop["rich_text"])
    return ""


def create_request_page(token, ds_id, subject, body_text, contact, org, recv_date, email_id):
    props = {
        P_TITLE:   {"title": [{"text": {"content": subject[:2000]}}]},
        P_CLASS:   {"status": {"name": "Queued"}},
        P_STATUS:  {"status": {"name": "Received"}},
        P_EMAILID: {"rich_text": [{"text": {"content": email_id[:2000]}}]},
    }
    if contact:
        props[P_CONTACT] = {"rich_text": [{"text": {"content": contact[:2000]}}]}
    if org:
        props[P_ORG] = {"rich_text": [{"text": {"content": org[:2000]}}]}
    if recv_date:
        props[P_RECV] = {"date": {"start": recv_date}}

    body = {
        "parent": {"type": "data_source_id", "data_source_id": ds_id},
        "properties": props,
        "children": text_to_blocks(body_text),
    }
    resp = requests.post(f"{NOTION_API}/pages", headers=notion_headers(token), json=body, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Create page → HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()["id"]


def upload_to_notion(token, name, mime, data):
    create = requests.post(f"{NOTION_API}/file_uploads", headers=notion_headers(token),
                           json={"filename": name, "content_type": mime}, timeout=30)
    if create.status_code != 200:
        raise RuntimeError(f"File upload create → HTTP {create.status_code}: {create.text[:300]}")
    upload_id = create.json()["id"]

    send = requests.post(f"{NOTION_API}/file_uploads/{upload_id}/send",
                         headers=notion_headers(token, json_body=False),
                         files={"file": (name, data, mime)}, timeout=120)
    if send.status_code != 200:
        raise RuntimeError(f"File upload send → HTTP {send.status_code}: {send.text[:300]}")
    return upload_id


def attach_files(token, page_id, prop_name, uploaded):
    files = [{"type": "file_upload", "file_upload": {"id": uid}, "name": name}
             for uid, name in uploaded]
    resp = requests.patch(f"{NOTION_API}/pages/{page_id}", headers=notion_headers(token),
                          json={"properties": {prop_name: {"files": files}}}, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Attach files → HTTP {resp.status_code}: {resp.text[:300]}")


def text_to_blocks(text, max_blocks=70):
    if not text:
        return [paragraph("(no email body)")]
    chunks = []
    for para in text.split("\n"):
        para = para.strip()
        while len(para) > 1900:
            chunks.append(para[:1900]); para = para[1900:]
        if para:
            chunks.append(para)
    blocks = [paragraph(c) for c in chunks[:max_blocks]]
    if len(chunks) > max_blocks:
        blocks.append(paragraph("… (text truncated)"))
    return blocks or [paragraph("(no email body)")]


def paragraph(text):
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}}


if __name__ == "__main__":
    main()
