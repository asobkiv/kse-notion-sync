"""
Google Sheets → Notion: Ukrainian Media Mentions Sync
Runs via GitHub Actions on a daily schedule.

Required env vars (set as GitHub Secrets):
  NOTION_TOKEN                 — Notion integration secret
  GOOGLE_SERVICE_ACCOUNT_JSON  — Google service account JSON (for Sheets read/write)

Setup:
  1. Share your Google Sheet with the service account email (Editor access)
  2. Set SPREADSHEET_ID below
"""

import json
import logging
import os
import re
import time

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

# ── CONFIGURE THESE ───────────────────────────────────────────

# Google Sheets spreadsheet ID (from the URL: /spreadsheets/d/THIS_PART/edit)
SPREADSHEET_ID = "1ysF3g9lPtwGrL1-8HGRl-BV1F7Hl33Iznp5rHhA3mJI"

# Name of the sheet tab (bottom tab name, usually "Sheet1" or "Аркуш1")
SHEET_NAME = "Лист1"

# Notion database for Ukrainian media mentions
NOTION_DATABASE_ID = "342d1fbd7688805f80a7e70bf32f4762"

# ── INTERNALS ─────────────────────────────────────────────────

NOTION_VERSION = "2022-06-28"

# ── MAIN ──────────────────────────────────────────────────────

def main():
    notion_token = os.environ["NOTION_TOKEN"]
    sa_json      = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

    if not SPREADSHEET_ID:
        raise RuntimeError("Set SPREADSHEET_ID in the script config")

    sheets = build_sheets_service(sa_json)

    data, headers = read_sheet(sheets)
    if not data:
        log.info("Sheet is empty")
        return

    idx = {
        "date":   headers.index("Дата")          if "Дата"          in headers else -1,
        "status": headers.index("Статус")        if "Статус"        in headers else -1,
        "screen": headers.index("Скрін")         if "Скрін"         in headers else -1,
        "link":   headers.index("Лінк")          if "Лінк"          in headers else -1,
        "synced": headers.index("Notion Synced") if "Notion Synced" in headers else -1,
    }

    # Add "Notion Synced" column if missing
    if idx["synced"] == -1:
        idx["synced"] = len(headers)
        update_cell(sheets, 1, idx["synced"] + 1, "Notion Synced")

    log.info("Loading existing Notion links...")
    existing_links = fetch_all_notion_links(notion_token)
    log.info(f"Loaded {len(existing_links)} existing links from Notion")

    last_known_date = None
    synced = skipped = errors = 0

    for row_idx, row in enumerate(data, start=2):  # row_idx = 1-based sheet row
        # Carry date forward
        raw_date = safe_get(row, idx["date"])
        parsed   = parse_date(raw_date)
        if parsed:
            last_known_date = parsed

        synced_val = safe_get(row, idx["synced"])
        if synced_val:
            continue

        link = safe_get(row, idx["link"]).strip()
        if not link:
            continue

        title  = extract_title(link)
        status = safe_get(row, idx["status"]).strip()
        screen = safe_get(row, idx["screen"]).strip()

        try:
            if link in existing_links:
                update_cell(sheets, row_idx, idx["synced"] + 1, "exists")
                skipped += 1
            else:
                create_notion_page(notion_token, title, link, screen, last_known_date, status)
                update_cell(sheets, row_idx, idx["synced"] + 1, time.strftime("%Y-%m-%dT%H:%M:%SZ"))
                existing_links.add(link)
                synced += 1
                time.sleep(0.35)
        except Exception as e:
            log.info(f"  Row {row_idx} error: {e}")
            errors += 1

    log.info(f"Finished. Synced {synced}, skipped {skipped}, errors {errors}.")


# ── GOOGLE SHEETS ─────────────────────────────────────────────

def build_sheets_service(sa_json_str):
    sa_info = json.loads(sa_json_str)
    creds   = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_sheet(sheets):
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=SHEET_NAME,
    ).execute()

    values = result.get("values", [])
    if not values:
        return [], []

    headers = [str(h).strip() for h in values[0]]
    data    = values[1:]
    return data, headers


def update_cell(sheets, row, col, value):
    col_letter = col_to_letter(col)
    sheets.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!{col_letter}{row}",
        valueInputOption="RAW",
        body={"values": [[value]]},
    ).execute()


def col_to_letter(col):
    result = ""
    while col > 0:
        col, remainder = divmod(col - 1, 26)
        result = chr(65 + remainder) + result
    return result


def safe_get(row, idx):
    if idx < 0 or idx >= len(row):
        return ""
    return str(row[idx])


# ── NOTION ────────────────────────────────────────────────────

def create_notion_page(token, title, link, screen, iso_date, status):
    classification = "Approved" if status == "Чисто" else "Queued"
    props = {
        "What":                  {"title": [{"text": {"content": title}}]},
        "Classification status": {"status": {"name": classification}},
    }
    if link:    props["Лінк"]  = {"url": link}
    if screen:  props["Скрін"] = {"url": screen}
    if iso_date: props["Дата_YYYY_MM_DD_inferred"] = {"date": {"start": iso_date}}
    if status in ("Подія", "Чисто"):
        props["Статус"] = {"select": {"name": status}}

    resp = requests.post(
        "https://api.notion.com/v1/pages",
        headers=notion_headers(token),
        json={"parent": {"database_id": NOTION_DATABASE_ID}, "properties": props},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")


def fetch_all_notion_links(token):
    links  = set()
    cursor = None

    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor

        resp = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query",
            headers=notion_headers(token),
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        for page in data["results"]:
            prop = page["properties"].get("Лінк")
            if prop and prop.get("url"):
                links.add(prop["url"])

        if data.get("has_more"):
            cursor = data["next_cursor"]
            time.sleep(0.3)
        else:
            break

    return links


def notion_headers(token):
    return {
        "Authorization":  f"Bearer {token}",
        "Content-Type":   "application/json",
        "Notion-Version": NOTION_VERSION,
    }


# ── HELPERS ───────────────────────────────────────────────────

def parse_date(raw):
    if not raw:
        return None
    raw = str(raw).strip()
    if not raw:
        return None

    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw

    m = re.match(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", raw)
    if m:
        day   = m.group(1).zfill(2)
        month = m.group(2).zfill(2)
        year  = m.group(3) or str(infer_year(int(m.group(2))))
        return f"{year}-{month}-{day}"

    return None


def infer_year(month):
    now = time.gmtime()
    return now.tm_year - 1 if month > now.tm_mon + 1 else now.tm_year


def extract_title(link):
    if not link:
        return "Untitled"
    m = re.match(r"^https?://([^/]+)(/.*)?$", link)
    if not m:
        return link[:100]

    host  = re.sub(r"^www\.", "", m.group(1))
    path  = (m.group(2) or "").rstrip("/")
    parts = [p for p in path.split("/") if p]

    if host == "t.me" and len(parts) >= 2: return f"@{parts[0]} · {parts[1]}"
    if host == "t.me" and len(parts) == 1: return f"@{parts[0]}"

    last = parts[-1] if parts else ""
    return f"{host} / {last}" if last else host


if __name__ == "__main__":
    main()
