"""
Rada TSK → Notion: Document Sync
Runs via GitHub Actions on a weekly schedule.

Required env vars (set as GitHub Secrets):
  NOTION_TOKEN                 — Notion integration secret
  GOOGLE_SERVICE_ACCOUNT_JSON  — Google service account JSON (for Drive uploads)
  NOTION_DB_ID                 — Notion artifacts database ID
  HONCHARENKO_PAGE_ID          — Notion page ID of the Honcharenko Crisis Topic
  RADA_DRIVE_FOLDER_ID         — Google Drive folder ID for downloaded PDFs
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
from googleapiclient.http import MediaIoBaseUpload

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

# ── CONFIGURE THESE ───────────────────────────────────────────

# Loaded from GitHub Secrets at runtime
DRIVE_FOLDER_ID     = os.environ.get("RADA_DRIVE_FOLDER_ID", "")
NOTION_DB_ID        = os.environ.get("NOTION_DB_ID", "")
HONCHARENKO_PAGE_ID = os.environ.get("HONCHARENKO_PAGE_ID", "")

# ── INTERNALS ─────────────────────────────────────────────────

RADA_BASE_URL  = "https://www.rada.gov.ua"
RADA_INDEX_URL = RADA_BASE_URL + "/documents/tskVRU/tskzakon/dijal_tskzakon"
NOTION_VERSION = "2022-06-28"

# ── MAIN ──────────────────────────────────────────────────────

def main():
    notion_token = os.environ["NOTION_TOKEN"]
    sa_json      = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

    if not DRIVE_FOLDER_ID:
        raise RuntimeError("Set DRIVE_FOLDER_ID in the script config")

    drive_service = build_drive_service(sa_json) if sa_json else None
    if not drive_service:
        log.info("No GOOGLE_SERVICE_ACCOUNT_JSON — Drive uploads disabled, using Rada URLs directly")

    sub_pages = fetch_sub_page_links()
    log.info(f"Found {len(sub_pages)} sub-page(s)")

    existing = fetch_existing_names(notion_token)
    log.info(f"{len(existing)} existing entries in Notion")

    # Read the target DB schema so we only send properties that actually exist —
    # lets others reuse this script with a differently-structured Notion table.
    schema = fetch_notion_schema(notion_token)

    created = skipped = errors = 0

    for page_url in sub_pages:
        log.info(f"Processing: {page_url}")

        try:
            files = fetch_file_links(page_url)
        except Exception as e:
            log.info(f"  Could not fetch sub-page: {e}")
            continue

        log.info(f"  Found {len(files)} file(s)")

        for f in files:
            try:
                if f["name"] in existing:
                    skipped += 1
                    continue

                source_url = f["rada_url"]
                if drive_service:
                    drive_url = upload_file_to_drive(drive_service, f["rada_url"], f["filename"])
                    if drive_url:
                        source_url = drive_url

                create_notion_document(notion_token, f, source_url, schema)
                existing.add(f["name"])
                created += 1
                time.sleep(0.35)
            except Exception as e:
                log.info(f"  Error on \"{f['name']}\": {e}")
                errors += 1

    log.info(f"Done. Created: {created} | Skipped: {skipped} | Errors: {errors}")


# ── RADA PARSING ──────────────────────────────────────────────

def fetch_sub_page_links():
    resp = requests.get(RADA_INDEX_URL, timeout=30)
    resp.raise_for_status()

    links = []
    seen  = set()
    for m in re.finditer(r'href="(/documents/tskVRU/tskzakon/dijal_tskzakon/\d+\.html)"', resp.text):
        url = RADA_BASE_URL + m.group(1)
        if url not in seen:
            seen.add(url)
            links.append(url)
    return links


def fetch_file_links(sub_page_url):
    resp = requests.get(sub_page_url, timeout=30)
    resp.raise_for_status()

    files = []
    seen  = set()

    # Matches: <a class="attachment-list__name" href="/uploads/documents/…">
    #            <i class="fa fa-paperclip"></i>
    #            Стенограма 15.05.2026
    #          </a>
    pattern = re.compile(
        r'<a[^>]*class="attachment-list__name"[^>]*href="(/uploads/documents/[^"]+)"[^>]*>([\s\S]*?)</a>',
        re.IGNORECASE,
    )

    for m in pattern.finditer(resp.text):
        href = m.group(1)
        name = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        name = re.sub(r"\s+", " ", name)
        name = decode_html_entities(name)

        rada_url = RADA_BASE_URL + href
        if not name or rada_url in seen:
            continue
        seen.add(rada_url)

        ext      = href.rsplit(".", 1)[-1].lower()
        filename = f"{name}.{ext}"

        files.append({
            "name":     name,
            "filename": filename,
            "rada_url": rada_url,
            "date":     extract_date(name),
        })

    return files


def extract_date(text):
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def decode_html_entities(s):
    return (s
        .replace("&amp;",  "&")
        .replace("&lt;",   "<")
        .replace("&gt;",   ">")
        .replace("&quot;", '"')
        .replace("&#039;", "'")
        .replace("&nbsp;", " ")
        .strip())


# ── GOOGLE DRIVE ──────────────────────────────────────────────

def build_drive_service(sa_json_str):
    sa_info = json.loads(sa_json_str)
    creds   = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_file_in_drive(drive_service, filename):
    escaped = filename.replace("'", "\\'")
    results = drive_service.files().list(
        q=f"name='{escaped}' and '{DRIVE_FOLDER_ID}' in parents and trashed=false",
        fields="files(id)",
        pageSize=1,
    ).execute()
    files = results.get("files", [])
    return f"https://drive.google.com/file/d/{files[0]['id']}/view" if files else None


def upload_file_to_drive(drive_service, file_url, filename):
    existing = find_file_in_drive(drive_service, filename)
    if existing:
        log.info(f"  Drive: already exists — {filename}")
        return existing

    try:
        resp = requests.get(file_url, timeout=60)
        if resp.status_code != 200:
            log.info(f"  Could not download: {filename} (HTTP {resp.status_code})")
            return None

        mime    = resp.headers.get("Content-Type", "application/octet-stream").split(";")[0]
        created = drive_service.files().create(
            body={"name": filename, "parents": [DRIVE_FOLDER_ID]},
            media_body=MediaIoBaseUpload(io.BytesIO(resp.content), mimetype=mime),
            fields="id",
        ).execute()

        file_id = created["id"]
        drive_service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
        ).execute()

        log.info(f"  Drive: uploaded — {filename}")
        return f"https://drive.google.com/file/d/{file_id}/view"
    except Exception as e:
        log.info(f"  Drive upload failed for {filename}: {e}")
        return None


# ── NOTION ────────────────────────────────────────────────────

def fetch_existing_names(token):
    names  = set()
    cursor = None

    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor

        resp = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query",
            headers=notion_headers(token),
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        for page in data["results"]:
            prop = page["properties"].get("Name")
            if prop and prop.get("title"):
                names.add(prop["title"][0]["plain_text"])

        if data.get("has_more"):
            cursor = data["next_cursor"]
            time.sleep(0.3)
        else:
            break

    return names


def create_notion_document(token, f, source_url, schema):
    properties = {
        "Name":                  {"title": [{"text": {"content": f["name"][:2000]}}]},
        "Source URL":            {"url": source_url},
        "Artifact Type":         {"select": {"name": "Document"}},
        "Evidence format":       {"select": {"name": "Document"}},
        "Classification status": {"status": {"name": "Queued"}},
        "Tags":                  {"multi_select": [{"name": "Evidence"}, {"name": "Timeline"}]},
    }

    # Relation only if a target page is configured
    if HONCHARENKO_PAGE_ID:
        properties["Relation"] = {"relation": [{"id": HONCHARENKO_PAGE_ID}]}

    if f.get("date"):
        properties["Date"] = {"date": {"start": f["date"]}}

    # Keep only properties that exist in the target database (skip the rest)
    properties = {k: v for k, v in properties.items() if k in schema}

    resp = requests.post(
        "https://api.notion.com/v1/pages",
        headers=notion_headers(token),
        json={"parent": {"database_id": NOTION_DB_ID}, "properties": properties},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Notion create → HTTP {resp.status_code}: {resp.text[:300]}")


def fetch_notion_schema(token):
    resp = requests.get(
        f"https://api.notion.com/v1/databases/{NOTION_DB_ID}",
        headers=notion_headers(token),
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Could not read Notion schema → HTTP {resp.status_code}: {resp.text[:300]}")
    return {name: meta["type"] for name, meta in resp.json()["properties"].items()}


def notion_headers(token):
    return {
        "Authorization":  f"Bearer {token}",
        "Content-Type":   "application/json",
        "Notion-Version": NOTION_VERSION,
    }


if __name__ == "__main__":
    main()
