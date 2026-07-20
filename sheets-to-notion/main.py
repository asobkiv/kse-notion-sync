"""
Google Sheets → Notion: generic, schema-driven sync.
Runs via GitHub Actions on a daily schedule.

HOW IT WORKS
  1. Reads your Notion database schema (property names + types) via the API.
  2. Reads your Google Sheet (header row + data rows).
  3. Maps each sheet column to the Notion property with the SAME NAME
     (case-insensitive). The value is formatted automatically according to
     the Notion property's type (date / url / select / number / text / ...).
  4. Creates one Notion page per new row. Tracks progress in a "Notion Synced"
     column written back to the sheet, so re-runs skip already-synced rows.

  => To map a sheet column into Notion, just name it exactly like the Notion
     property. Columns without a matching property are ignored.

Required env vars (GitHub Secrets):
  NOTION_TOKEN                 — Notion integration secret
  GOOGLE_SERVICE_ACCOUNT_JSON  — Google service account JSON (Sheets read/write)
  SHEETS_SPREADSHEET_ID        — Google Sheets spreadsheet ID
  SHEETS_DB_ID                 — Notion database ID

Optional env vars:
  SHEETS_TAB_NAME      — sheet tab name (default "Sheet1")
  SHEETS_SYNCED_COLUMN — tracking column name (default "Notion Synced")
  SHEETS_DEDUP_PROPERTY — Notion property used to detect duplicates
                          (default: the database's title property)
  SHEETS_COLUMN_MAP    — rename sheet columns before matching, when headers
                          and Notion properties differ. Comma-separated pairs:
                          "SheetHeader=NotionProperty,Another=Other"
  SHEETS_CUSTOM_RULES  — enables a project-specific rules block; leave unset
                          for generic behavior (see CUSTOM RULES section below)
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

# ── CONFIGURE (via GitHub Secrets) ────────────────────────────

SPREADSHEET_ID     = os.environ.get("SHEETS_SPREADSHEET_ID", "")
NOTION_DATABASE_ID = os.environ.get("SHEETS_DB_ID", "")
SHEET_NAME         = os.environ.get("SHEETS_TAB_NAME") or "Sheet1"
SYNCED_COLUMN      = os.environ.get("SHEETS_SYNCED_COLUMN") or "Notion Synced"
DEDUP_PROPERTY     = os.environ.get("SHEETS_DEDUP_PROPERTY", "")  # "" = title prop
CUSTOM_RULES       = os.environ.get("SHEETS_CUSTOM_RULES", "")    # "" = generic

# Optional sheet-header → Notion-property renames ("Ссылка=Лінк,Время=Час")
COLUMN_MAP = {}
for _pair in (os.environ.get("SHEETS_COLUMN_MAP") or "").split(","):
    if "=" in _pair:
        _src, _dst = _pair.split("=", 1)
        COLUMN_MAP[_src.strip().lower()] = _dst.strip()

# Safety: when set (any non-empty value), the script only LOGS what it would
# create — it creates nothing in Notion, so no classification credits are spent.
DRY_RUN            = bool(os.environ.get("SHEETS_DRY_RUN"))

# Safety: stop after creating this many pages in a single run (0 = unlimited).
_max = os.environ.get("SHEETS_MAX_CREATES", "")
MAX_CREATES        = int(_max) if _max.strip().isdigit() else 0

NOTION_VERSION = "2022-06-28"

# ── MAIN ──────────────────────────────────────────────────────

def main():
    token   = os.environ["NOTION_TOKEN"]
    sa_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

    if not SPREADSHEET_ID:
        raise RuntimeError("Set SHEETS_SPREADSHEET_ID")
    if not NOTION_DATABASE_ID:
        raise RuntimeError("Set SHEETS_DB_ID")

    sheets = build_sheets_service(sa_json)

    schema     = fetch_notion_schema(token)
    title_prop = next((n for n, t in schema.items() if t == "title"), None)
    if not title_prop:
        raise RuntimeError("Notion database has no title property")

    data, headers = read_sheet(sheets)
    if not data:
        log.info("Sheet is empty")
        return

    # Map sheet columns → Notion properties by matching name (case-insensitive)
    name_to_prop = {n.lower(): n for n in schema}
    col_map    = {}   # sheet column index → (notion property name, notion type)
    synced_idx = None
    for i, h in enumerate(headers):
        hn = str(h).strip()
        if hn.lower() == SYNCED_COLUMN.lower():
            synced_idx = i
            continue
        hn = COLUMN_MAP.get(hn.lower(), hn)
        prop = name_to_prop.get(hn.lower())
        if prop:
            col_map[i] = (prop, schema[prop])

    log.info("Mapped columns → Notion: " +
             (", ".join(f"{headers[i]}→{p}" for i, (p, _) in col_map.items()) or "(none)"))

    # Add a tracking column if the sheet doesn't have one yet
    if synced_idx is None:
        synced_idx = len(headers)
        update_cell(sheets, 1, synced_idx + 1, SYNCED_COLUMN)

    dedup_prop = DEDUP_PROPERTY or title_prop
    existing   = fetch_existing_values(token, dedup_prop)
    log.info(f"Loaded {len(existing)} existing '{dedup_prop}' values from Notion")

    # Project-specific precomputation (see CUSTOM RULES section)
    kse_dates = compute_kse_dates(data, headers) if CUSTOM_RULES == "kse_media" else None

    unsynced = sum(1 for row in data if not safe_get(row, synced_idx))
    log.info(f"{len(data)} data rows | {unsynced} without a '{SYNCED_COLUMN}' mark")
    if DRY_RUN:
        log.info("DRY RUN — nothing will be created in Notion, no credits spent")

    synced = skipped = errors = no_key = 0

    for offset, row in enumerate(data):
        row_num = offset + 2  # 1-based sheet row (row 1 = headers)

        if safe_get(row, synced_idx):
            continue

        # Build Notion properties from mapped columns
        props = {}
        for cidx, (pname, ptype) in col_map.items():
            val = safe_get(row, cidx).strip()
            if not val:
                continue
            formatted = format_value(ptype, val)
            if formatted is not None:
                props[pname] = formatted

        # Apply project-specific rules (no-op unless SHEETS_CUSTOM_RULES set)
        if CUSTOM_RULES == "kse_media":
            apply_kse_media_rules(props, title_prop, schema, kse_dates[offset])

        # Deduplicate against what already exists in Notion
        dval = dedup_value(props, dedup_prop)

        # No dedup key (e.g. empty link) → skip. Such a row can't be deduplicated,
        # so creating it would spawn a junk page that reappears on every run.
        # Left unmarked: if a link is added later, it will sync then.
        if not dval:
            no_key += 1
            continue

        if dval in existing:
            if not DRY_RUN:
                update_cell(sheets, row_num, synced_idx + 1, "exists")
            skipped += 1
            continue

        # Dry run: show exactly what WOULD be created, create nothing
        if DRY_RUN:
            log.info(f"  WOULD CREATE row {row_num}: dedup={dval!r}")
            synced += 1
            continue

        # Safety cap: never flood Notion (and the classifier) in one run
        if MAX_CREATES and synced >= MAX_CREATES:
            log.info(f"Hit MAX_CREATES={MAX_CREATES} — stopping. Re-run to continue.")
            break

        try:
            create_page(token, props)
            update_cell(sheets, row_num, synced_idx + 1, time.strftime("%Y-%m-%dT%H:%M:%SZ"))
            if dval:
                existing.add(dval)
            synced += 1
            time.sleep(0.35)
        except Exception as e:
            log.info(f"  Row {row_num} error: {e}")
            errors += 1

    verb = "Would create" if DRY_RUN else "Created"
    log.info(f"Finished. {verb} {synced}, skipped (exist) {skipped}, "
             f"skipped (no '{dedup_prop}') {no_key}, errors {errors}.")


# ── NOTION VALUE FORMATTING (generic) ─────────────────────────

def clip(val, limit=2000):
    """Truncate to a Notion length limit. Notion counts UTF-16 code units,
    not Python characters — emoji count as 2 — so a plain val[:2000] can
    still be rejected with "length should be ≤ 2000"."""
    enc = val.encode("utf-16-le")[: limit * 2]
    return enc.decode("utf-16-le", errors="ignore")


def option_name(val):
    """Sanitize a select/status option name: Notion forbids commas in option
    names, and stray newlines in sheet cells produce unusable options."""
    return re.sub(r"\s+", " ", val).replace(",", "‚").strip()[:100]


def format_value(ptype, val):
    """Format a plain string into a Notion property payload for the given type.
    Returns None for empty/unsupported values."""
    if ptype == "title":
        return {"title": [{"text": {"content": clip(val)}}]}
    if ptype == "rich_text":
        return {"rich_text": [{"text": {"content": clip(val)}}]}
    if ptype == "url":
        return {"url": val}
    if ptype == "email":
        return {"email": val}
    if ptype == "phone_number":
        return {"phone_number": val}
    if ptype == "number":
        try:
            return {"number": float(val.replace(",", "."))}
        except ValueError:
            return None
    if ptype == "checkbox":
        return {"checkbox": val.strip().lower() in ("true", "1", "yes", "так", "x", "✓")}
    if ptype == "select":
        return {"select": {"name": option_name(val)}}
    if ptype == "status":
        return {"status": {"name": option_name(val)}}
    if ptype == "multi_select":
        parts = [option_name(p) for p in val.split(",") if p.strip()]
        return {"multi_select": [{"name": p} for p in parts]} if parts else None
    if ptype == "date":
        d = parse_date(val)
        return {"date": {"start": d}} if d else None
    return None  # people / relation / files / formula / rollup → not auto-mapped


def dedup_value(props, dedup_prop):
    """Extract a comparable plain value from a built property payload."""
    pv = props.get(dedup_prop)
    if not pv:
        return None
    if "url" in pv:        return pv["url"]
    if "email" in pv:      return pv["email"]
    if "title" in pv:      return "".join(x["text"]["content"] for x in pv["title"]) or None
    if "rich_text" in pv:  return "".join(x["text"]["content"] for x in pv["rich_text"]) or None
    if "select" in pv:     return (pv["select"] or {}).get("name")
    if "status" in pv:     return (pv["status"] or {}).get("name")
    if "number" in pv:     return str(pv["number"])
    return None


# ── CUSTOM RULES (project-specific, opt-in) ───────────────────
# Everything below activates ONLY when SHEETS_CUSTOM_RULES="kse_media".
# Generic forks leave that secret unset and ignore this block entirely.
#
# KSE media-mentions sheet needs three things the generic engine can't infer:
#   1. Title (the "What" property) generated from the link's domain/path,
#      because the sheet has no dedicated title column.
#   2. A date that "carries forward": dates appear only on the first row of
#      each day's group, and blank rows below inherit the last seen date.
#      It is written to a custom-named property "Дата_YYYY_MM_DD_inferred".
#   3. "Classification status" = Approved when Статус is "Чисто", else Queued.

def compute_kse_dates(data, headers):
    date_idx = next((i for i, h in enumerate(headers) if str(h).strip().lower() == "дата"), None)
    dates, last = [], None
    for row in data:
        p = parse_date(safe_get(row, date_idx)) if date_idx is not None else None
        if p:
            last = p
        dates.append(last)
    return dates


def apply_kse_media_rules(props, title_prop, schema, row_date):
    # 1. Title from the Лінк url
    link = (props.get("Лінк") or {}).get("url")
    if title_prop not in props and link:
        props[title_prop] = {"title": [{"text": {"content": extract_title(link)}}]}

    # 2. Inferred (carry-forward) date → custom-named property
    if row_date and "Дата_YYYY_MM_DD_inferred" in schema:
        props["Дата_YYYY_MM_DD_inferred"] = {"date": {"start": row_date}}

    # 3. Classification status from Статус; ignore Статус values outside the set
    status = (props.get("Статус") or {}).get("select", {}).get("name")
    if status not in ("Подія", "Чисто"):
        props.pop("Статус", None)
    if "Classification status" in schema:
        props["Classification status"] = {
            "status": {"name": "Approved" if status == "Чисто" else "Queued"}
        }


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
    return values[1:], headers


def update_cell(sheets, row, col, value):
    # Sheets API caps write requests at 60/min per user; a large backlog of
    # per-row write-backs blows through that and 429s without this throttle.
    time.sleep(1.1)
    sheets.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!{col_to_letter(col)}{row}",
        valueInputOption="RAW",
        body={"values": [[value]]},
    ).execute()


def col_to_letter(col):
    result = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        result = chr(65 + rem) + result
    return result


def safe_get(row, idx):
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    return str(row[idx])


# ── NOTION API ────────────────────────────────────────────────

def fetch_notion_schema(token):
    resp = requests.get(
        f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}",
        headers=notion_headers(token),
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Could not read Notion schema → HTTP {resp.status_code}: {resp.text[:300]}")
    return {name: meta["type"] for name, meta in resp.json()["properties"].items()}


def fetch_existing_values(token, prop_name):
    values = set()
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
            v = extract_plain(page["properties"].get(prop_name))
            if v:
                values.add(v)
        if data.get("has_more"):
            cursor = data["next_cursor"]
            time.sleep(0.3)
        else:
            break
    return values


def extract_plain(pv):
    if not pv:
        return None
    t = pv.get("type")
    if t == "title":      return "".join(x["plain_text"] for x in pv["title"]) or None
    if t == "rich_text":  return "".join(x["plain_text"] for x in pv["rich_text"]) or None
    if t == "url":        return pv.get("url")
    if t == "email":      return pv.get("email")
    if t == "select":     return (pv.get("select") or {}).get("name")
    if t == "status":     return (pv.get("status") or {}).get("name")
    if t == "number":     return str(pv.get("number")) if pv.get("number") is not None else None
    if t == "date":       return (pv.get("date") or {}).get("start")
    return None


def create_page(token, props):
    resp = requests.post(
        "https://api.notion.com/v1/pages",
        headers=notion_headers(token),
        json={"parent": {"database_id": NOTION_DATABASE_ID}, "properties": props},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")


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
