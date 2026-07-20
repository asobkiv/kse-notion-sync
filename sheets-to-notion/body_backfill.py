"""
One-off companion to main.py: move a sheet column's text into Notion page
BODIES instead of a (2000-char-capped) rich_text property.

For every page in the target database, finds the sheet row whose KEY_COLUMN
matches the page's title, and appends the full BODY_COLUMN text to the page
content as paragraph blocks. Pages that already have body content are skipped,
so the script is idempotent and safe to re-run.

Required env vars:
  NOTION_TOKEN                 — Notion integration secret
  GOOGLE_SERVICE_ACCOUNT_JSON  — Google service account JSON (Sheets read)
  SHEETS_SPREADSHEET_ID        — Google Sheets spreadsheet ID
  SHEETS_DB_ID                 — Notion database ID

Optional env vars:
  SHEETS_TAB_NAME — sheet tab name (default "Sheet1")
  BODY_KEY_COLUMN — sheet column matched against the page title (default: first column)
  BODY_COLUMN     — sheet column whose text goes into the page body (default "Текст")
  BODY_DRY_RUN    — set to any value to log what would happen without writing
"""

import json
import logging
import os
import time

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

SPREADSHEET_ID     = os.environ.get("SHEETS_SPREADSHEET_ID", "")
NOTION_DATABASE_ID = os.environ.get("SHEETS_DB_ID", "")
SHEET_NAME         = os.environ.get("SHEETS_TAB_NAME") or "Sheet1"
KEY_COLUMN         = os.environ.get("BODY_KEY_COLUMN", "")   # "" = first column
BODY_COLUMN        = os.environ.get("BODY_COLUMN") or "Текст"
DRY_RUN            = bool(os.environ.get("BODY_DRY_RUN"))

NOTION_VERSION = "2022-06-28"

# Notion caps rich_text content at 2000 UTF-16 code units; stay under it.
CHUNK_LIMIT = 1900


def main():
    token   = os.environ["NOTION_TOKEN"]
    sa_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

    if not SPREADSHEET_ID:
        raise RuntimeError("Set SHEETS_SPREADSHEET_ID")
    if not NOTION_DATABASE_ID:
        raise RuntimeError("Set SHEETS_DB_ID")

    bodies = read_bodies(sa_json)
    log.info(f"Sheet: {len(bodies)} rows with a non-empty '{BODY_COLUMN}'")

    pages = fetch_pages(token)
    log.info(f"Notion: {len(pages)} pages in the database")
    if DRY_RUN:
        log.info("DRY RUN — nothing will be written")

    done = skipped_filled = skipped_nokey = errors = 0
    for page_id, title in pages:
        body = bodies.get(title.strip())
        if not body:
            skipped_nokey += 1
            log.info(f"  no sheet match for: {title[:80]}")
            continue
        try:
            if page_has_children(token, page_id):
                skipped_filled += 1
                continue
            if DRY_RUN:
                log.info(f"  WOULD FILL {title[:80]} ({len(body)} chars)")
                done += 1
                continue
            append_body(token, page_id, body)
            done += 1
            time.sleep(0.35)
        except Exception as e:
            log.info(f"  Error on {title[:80]}: {e}")
            errors += 1

    verb = "Would fill" if DRY_RUN else "Filled"
    log.info(f"Finished. {verb} {done}, already had content {skipped_filled}, "
             f"no sheet match {skipped_nokey}, errors {errors}.")


# ── SHEET ─────────────────────────────────────────────────────

def read_bodies(sa_json_str):
    creds = service_account.Credentials.from_service_account_info(
        json.loads(sa_json_str),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    values = sheets.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=SHEET_NAME,
    ).execute().get("values", [])
    if not values:
        return {}
    headers = [str(h).strip() for h in values[0]]
    key_idx  = headers.index(KEY_COLUMN) if KEY_COLUMN else 0
    body_idx = headers.index(BODY_COLUMN)
    out = {}
    for row in values[1:]:
        key  = row[key_idx].strip()  if key_idx  < len(row) else ""
        body = row[body_idx].strip() if body_idx < len(row) else ""
        if key and body:
            out[key] = body
    return out


# ── NOTION ────────────────────────────────────────────────────

def fetch_pages(token):
    """Return [(page_id, title_plain_text)] for every page in the database."""
    pages, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query",
            headers=headers(token), json=body, timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for page in data["results"]:
            for pv in page["properties"].values():
                if pv["type"] == "title":
                    title = "".join(x["plain_text"] for x in pv["title"])
                    pages.append((page["id"], title))
                    break
        if data.get("has_more"):
            cursor = data["next_cursor"]
            time.sleep(0.3)
        else:
            return pages


def page_has_children(token, page_id):
    resp = requests.get(
        f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=1",
        headers=headers(token), timeout=30,
    )
    resp.raise_for_status()
    return bool(resp.json()["results"])


def append_body(token, page_id, body):
    blocks = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [
                {"type": "text", "text": {"content": c}} for c in chunks(line)
            ]},
        })
    for i in range(0, len(blocks), 90):  # API cap: 100 blocks per append
        resp = requests.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=headers(token), json={"children": blocks[i:i + 90]}, timeout=60,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        time.sleep(0.35)


def chunks(s, limit=CHUNK_LIMIT):
    """Split into pieces of ≤limit UTF-16 code units (Notion's unit of length),
    never splitting a surrogate pair."""
    out, cur, cur_len = [], [], 0
    for ch in s:
        units = len(ch.encode("utf-16-le")) // 2
        if cur_len + units > limit:
            out.append("".join(cur))
            cur, cur_len = [], 0
        cur.append(ch)
        cur_len += units
    if cur:
        out.append("".join(cur))
    return out


def headers(token):
    return {
        "Authorization":  f"Bearer {token}",
        "Content-Type":   "application/json",
        "Notion-Version": NOTION_VERSION,
    }


if __name__ == "__main__":
    main()
