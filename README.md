# kse-notion-sync

Automated pipelines that pull content from external sources into a Notion database. Run on GitHub Actions — no servers, no manual work.

| Workflow | Source | Schedule | Creates in Notion |
|---|---|---|---|
| `moodle-to-notion` | Moodle LMS | daily 09:00 Kyiv | Teaching evidence — slides, docs, YouTube recordings |
| `rada-tsk-to-notion` | Rada TSK stenographic records | every Monday 10:00 Kyiv | Documents linked to a Crisis Topic |
| `sheets-to-notion` | Google Sheets | daily 08:00 Kyiv | One Notion page per row (schema-driven) |
| `drive-inquiries-to-notion` | Google Drive (Requests/Responses folders) | daily 09:00 Kyiv | One page per request file; stub page per response file |

The scripts are independent — fork all of them or just the one you need.

---

## Fork & Deploy

### 1. Fork this repo

GitHub → **Fork** → create under your account or organization.

### 2. Create a Google Cloud project

1. [console.cloud.google.com](https://console.cloud.google.com) → New project
2. **APIs & Services → Library** → enable the APIs you need:
   - **Google Drive API** (moodle, rada)
   - **Google Sheets API** (sheets)
   - **YouTube Data API v3** (moodle, optional — for video duration)
3. **IAM → Service Accounts → Create** → **Keys → Add Key → JSON** → save the file
4. The JSON content goes into the `GOOGLE_SERVICE_ACCOUNT_JSON` secret
5. Share each Drive folder / Sheet with the service account email (`...@....iam.gserviceaccount.com`) as **Editor**

### 3. Create a Notion integration

1. [notion.so/my-integrations](https://www.notion.so/my-integrations) → New integration → copy the secret
2. Open each Notion database → **...** → **Connections** → add your integration
3. Create the database properties as described in the **Notion schema** section below

### 4. Add GitHub Secrets

Repo → **Settings → Secrets and variables → Actions** → add the secrets from the [reference table](#secrets-reference).

### 5. Run manually to test

**Actions → pick a workflow → Run workflow** → check the logs.

---

## Notion database schema

Each scraper writes a fixed set of properties. **Create a Notion database with these property names and types.** If a property is missing in your database, the script simply skips it (no error), so you can start minimal and add more later.

> ⚠️ `status`-type properties are special: Notion does **not** let the API create new status options. So for any `status` property, manually add the option values the script uses (e.g. `Queued`) before the first run. `select` / `multi_select` options are created automatically.

### moodle-to-notion

| Property | Type | Notes |
|---|---|---|
| `Name` | Title | **required** — the resource name |
| `Source URL` | URL | Drive link (or Moodle/YouTube link) |
| `Evidence format` | Select | `Document` / `Slides` / `Video` / `Other` |
| `Artifact Type` | Select | always `Teaching evidence` |
| `Course` | Multi-select | mapped course name |
| `Classification status` | Status | set to `Queued` — **add this option manually** |
| `Teaching hours` | Number | only for YouTube videos |
| `Relation` | Relation | → your "teachers" database (optional) |

### rada-tsk-to-notion

| Property | Type | Notes |
|---|---|---|
| `Name` | Title | **required** — the document name |
| `Source URL` | URL | Drive link (or Rada link) |
| `Artifact Type` | Select | always `Document` |
| `Evidence format` | Select | always `Document` |
| `Classification status` | Status | set to `Queued` — **add this option manually** |
| `Tags` | Multi-select | `Evidence`, `Timeline` |
| `Relation` | Relation | → the topic page set via `HONCHARENKO_PAGE_ID` (optional) |
| `Date` | Date | parsed from the document name |

### sheets-to-notion

This one is **schema-driven** — it adapts to whatever database you point it at. See the next section.

### drive-inquiries-to-notion

Database with these properties (the script writes the title, status, and a file
property; everything else is meant to be auto-filled by Notion AI):

| Property | Type | Notes |
|---|---|---|
| `Request(case)` | Title | **required** — working title (derived from filename) |
| `Files & media request` | Files | request file uploaded here |
| `Response files` | Files | response file uploaded here |
| `Status` | Status | `Received` (requests) / `Triage / qualification` (response stubs) — **add these options** |
| `Summary`, `Primary topic`, `Requester type`, `Relevance`, `Contact person`, `Organization`, … | any | left for Notion **AI Autofill / Database Agent** to fill from the page content |

Classification here = enabling **AI Autofill** (now "Database Agent") on the
analytical properties. The script only puts content on the page (docx/pdf text in
the body, file attached); Notion fills the rest automatically.

---

## How `drive-inquiries-to-notion` works

Watches a parent Drive folder with two subfolders:

- **`Requests/`** → one page per file. File → `Files & media request`, extracted
  docx/pdf text → page body, `Status = Received`.
- **`Responses/`** → a response belongs on its request's page, but filename
  matching is unreliable, so each response becomes a **stub page**: file →
  `Response files`, `Status = Triage / qualification`, title `⚠️ RESPONSE — link
  to request: <file>`, plus the extracted text and the **top-3 candidate requests**
  (ranked by content similarity) as links for one-click manual linking.

**Dedup is by filename** — each run rescans both folders and skips any file whose
name already appears attached in Notion. No Drive files are moved or modified.

**`INQUIRIES_AUTO_ATTACH=true`** (optional, off by default): on a single confident
content match, the response file is attached straight to that request's
`Response files` instead of creating a stub. Keep off until you trust the matching
on real data. Notion AI agents cannot move file attachments between pages, so the
scraper is the only reliable way to place the file — hence the stub-by-default.

Use `INQUIRIES_DRY_RUN=true` to preview (`WOULD CREATE` / `WOULD STUB`) with no writes.

---

## How `sheets-to-notion` maps columns

The script reads your Notion database schema, then for each **sheet column** it looks for a **Notion property with the same name** (case-insensitive). If found, the cell value is written there, formatted automatically based on the property's type:

| Notion property type | Expected sheet cell |
|---|---|
| Title / Text | any text (truncated to Notion's 2000-unit limit, counted in UTF-16 — emoji count double) |
| URL / Email / Phone | the raw value |
| Number | `123` or `12,5` |
| Checkbox | `true` / `1` / `yes` / `так` / `x` |
| Select / Status | the option name (Select auto-creates; Status must pre-exist). Commas are replaced with `‚` and whitespace collapsed — Notion forbids commas in option names |
| Multi-select | comma-separated, e.g. `Evidence, Timeline` |
| Date | `2026-05-15` or `15.05.2026` |

**Rules:**
- Name your sheet columns exactly like your Notion properties → they map automatically.
- Columns with no matching property are ignored.
- Duplicates are detected via the title property (override with `SHEETS_DEDUP_PROPERTY`).
- If sheet headers and Notion property names differ (e.g. different languages), map them with `SHEETS_COLUMN_MAP`, e.g. `Ссылка=Лінк,Время=Час`.
- A `Notion Synced` column is added to the sheet to track progress; synced rows are skipped on re-runs.

**Recommendation for a clean setup:** create a Notion database where the property names match your sheet headers 1:1. Make one of them the Title, and pick a column with unique values for dedup.

### Project-specific rules (advanced)

The KSE media-mentions sheet has logic the generic engine can't infer (title generated from a link, a carry-forward date written to a custom-named property, and a status remap). That logic lives in one clearly-marked block in `sheets-to-notion/main.py`, activated only when `SHEETS_CUSTOM_RULES=kse_media`. Generic forks leave that secret unset and the block never runs. Use it as a template if you need your own custom rules.

---

## Secrets reference

| Secret | Required by | Description |
|---|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | all | Google service account JSON |
| `MOODLE_NOTION_TOKEN` | moodle | Notion integration secret |
| `MOODLE_USERNAME` | moodle | Moodle login email |
| `MOODLE_PASSWORD` | moodle | Moodle password |
| `YOUTUBE_API_KEY` | moodle | YouTube Data API v3 key (optional) |
| `NOTION_DB_ID` | moodle, rada | Notion artifacts database ID |
| `CRISIS_TOPICS_DB_ID` | moodle | Teachers database ID (optional) |
| `MOODLE_DRIVE_FOLDER_ID` | moodle | Drive folder for uploaded files |
| `MOODLE_BASE_URL` | moodle | e.g. `https://teaching.kse.org.ua` |
| `MOODLE_COURSE_IDS` | moodle | Comma-separated, e.g. `3261,3508` (empty = all) |
| `RADA_NOTION_TOKEN` | rada | Notion integration secret |
| `HONCHARENKO_PAGE_ID` | rada | Topic page to link documents to (optional) |
| `RADA_DRIVE_FOLDER_ID` | rada | Drive folder for downloaded PDFs |
| `SHEETS_NOTION_TOKEN` | sheets | Notion integration secret |
| `SHEETS_SPREADSHEET_ID` | sheets | Spreadsheet ID (from URL) |
| `SHEETS_DB_ID` | sheets | Notion database ID |
| `SHEETS_TAB_NAME` | sheets | Sheet tab name (default `Sheet1`) |
| `SHEETS_SYNCED_COLUMN` | sheets | Tracking column name (default `Notion Synced`) |
| `SHEETS_DEDUP_PROPERTY` | sheets | Dedup property (default: the title property) |
| `SHEETS_COLUMN_MAP` | sheets | Optional sheet-header → property renames, `A=B,C=D` |
| `SHEETS_CUSTOM_RULES` | sheets | Set to `kse_media` to enable KSE rules; else leave unset |
| `SHEETS_DRY_RUN` | sheets | Set to `true` to preview without creating anything (no credits) |
| `SHEETS_MAX_CREATES` | sheets | Cap pages created per run (e.g. `50`; unset = unlimited) |
| `INQUIRIES_NOTION_TOKEN` | inquiries | Notion integration secret |
| `INQUIRIES_DB_ID` | inquiries | Requests Dashboard database ID |
| `INQUIRIES_DRIVE_FOLDER_ID` | inquiries | Parent Drive folder (with `Requests/` + `Responses/` subfolders) |
| `INQUIRIES_AUTO_ATTACH` | inquiries | `true` to auto-attach confident response matches (default off) |
| `INQUIRIES_DRY_RUN` | inquiries | `true` to preview without writing anything |
| `INQUIRIES_MAX_CREATES` | inquiries | Cap pages created per run (0/unset = unlimited) |

**How to find IDs:**
- Notion database ID → open database → 32-char string in the URL
- Drive folder ID → open folder → URL part after `/folders/`
- Spreadsheet ID → URL part after `/spreadsheets/d/`

---

## Customization

- **Moodle courses** → set the `MOODLE_COURSE_IDS` secret; update `COURSE_MAP` in `moodle-to-notion/main.py` for the course-name → Notion-option mapping.
- **Teacher name overrides** → `TEACHER_NAME_MAP` in `moodle-to-notion/main.py` (for names that don't transliterate cleanly).
- **Rada crawl target** → `RADA_INDEX_URL` and the regex in `fetch_sub_page_links()`.

---

## How deduplication works

All scripts load existing Notion entries before writing. An entry is skipped if it already exists (by name for Moodle/Rada, by the dedup property for Sheets, by attached filename for Inquiries). Safe to run repeatedly.

For `sheets-to-notion`, a row whose **dedup property is empty** (e.g. a row with no link) is skipped entirely — it can't be deduplicated, so creating it would spawn a junk page that reappears on every run. It stays unmarked, so if you fill in the value later it syncs then.

---

## Safe testing & credit control (sheets)

Creating Notion pages can trigger downstream automation (e.g. an AI classifier) that costs credits. Two controls prevent surprises:

- **`SHEETS_DRY_RUN=true`** — the script logs exactly what it *would* create (`WOULD CREATE row N: dedup=...`) and creates nothing. Use this after any change to verify before going live. `Would create 0` means nothing new to sync.
- **`SHEETS_MAX_CREATES=N`** — hard cap on pages created per run. Even if something is misconfigured, a run can't flood. Re-run to continue through a backlog in safe batches.

Recommended first run on a new setup: set `SHEETS_DRY_RUN=true`, run, read the log, then remove it.

---

## Security

No credentials or IDs in the code — everything lives in GitHub Secrets (encrypted, masked in logs). The `.gs` files are the original Google Apps Script versions, kept as reference.
