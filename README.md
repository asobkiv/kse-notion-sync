# kse-notion-sync

Automated pipelines that pull content from external sources into a Notion **⏱️ Artifacts** database. Run on GitHub Actions — no servers, no manual work.

| Workflow | Source | Schedule | Creates in Notion |
|---|---|---|---|
| `moodle-to-notion` | Moodle LMS | daily 09:00 Kyiv | Teaching evidence — slides, docs, YouTube recordings |
| `rada-tsk-to-notion` | Rada TSK stenographic records | every Monday 10:00 Kyiv | Documents linked to a Crisis Topic |
| `sheets-to-notion` | Google Sheets | daily 08:00 Kyiv | Media mention pages |

---

## Fork & Deploy

### 1. Fork this repo

GitHub → **Fork** → create under your account or organization.

### 2. Create a Google Cloud project

1. [console.cloud.google.com](https://console.cloud.google.com) → New project
2. **APIs & Services → Library** → enable:
   - **Google Drive API**
   - **Google Sheets API**
   - **YouTube Data API v3**
3. **IAM → Service Accounts → Create** → name it anything → **Keys → Add Key → JSON** → save the file
4. Copy the full JSON content — you'll need it as `GOOGLE_SERVICE_ACCOUNT_JSON` secret

### 3. Create a Notion integration

1. [notion.so/my-integrations](https://www.notion.so/my-integrations) → New integration
2. Copy the **Internal Integration Secret** — you'll need it as a Notion token secret
3. Open each Notion database → **...** → **Connections** → add your integration

### 4. Add GitHub Secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**

Add all secrets from the table below.

### 5. Run manually to test

**Actions → pick a workflow → Run workflow**

Check the logs — if everything is green, the scheduled runs will take care of the rest.

---

## Secrets reference

| Secret | Required by | Description |
|---|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | all | Google service account JSON (Drive + Sheets access) |
| `MOODLE_NOTION_TOKEN` | moodle | Notion integration secret |
| `MOODLE_USERNAME` | moodle | Moodle login email |
| `MOODLE_PASSWORD` | moodle | Moodle password |
| `YOUTUBE_API_KEY` | moodle | YouTube Data API v3 key |
| `NOTION_DB_ID` | moodle, rada | Notion artifacts database ID |
| `CRISIS_TOPICS_DB_ID` | moodle | Notion Crisis Topics database ID |
| `MOODLE_DRIVE_FOLDER_ID` | moodle | Drive folder ID for uploaded Moodle files |
| `MOODLE_BASE_URL` | moodle | Moodle URL, e.g. `https://teaching.kse.org.ua` |
| `MOODLE_COURSE_IDS` | moodle | Comma-separated course IDs, e.g. `3261,3508` (empty = all) |
| `RADA_NOTION_TOKEN` | rada | Notion integration secret |
| `HONCHARENKO_PAGE_ID` | rada | Notion page ID of the Crisis Topic to link documents to |
| `RADA_DRIVE_FOLDER_ID` | rada | Drive folder ID for downloaded Rada PDFs |
| `SHEETS_NOTION_TOKEN` | sheets | Notion integration secret |
| `SHEETS_SPREADSHEET_ID` | sheets | Google Sheets spreadsheet ID (from URL) |
| `SHEETS_DB_ID` | sheets | Notion database ID for media mentions |
| `SHEETS_TAB_NAME` | sheets | Sheet tab name, e.g. `Лист1` or `Sheet1` |

**How to find IDs:**
- Notion database ID → open database → copy from URL (32-char string after the last `/`)
- Drive folder ID → open folder → copy from URL (`/folders/THIS_PART`)
- Spreadsheet ID → open sheet → copy from URL (`/spreadsheets/d/THIS_PART/edit`)

---

## Customization

### Adding Moodle courses

Add course IDs to the `MOODLE_COURSE_IDS` secret (comma-separated). Find the ID in the Moodle course URL: `.../course/view.php?id=XXXX`.

Update `COURSE_MAP` in `moodle-to-notion/main.py` if the course name isn't mapped yet.

### Teacher name overrides

If a teacher's English name on Moodle doesn't transliterate correctly to Ukrainian, add an entry to `TEACHER_NAME_MAP` in `moodle-to-notion/main.py`:

```python
TEACHER_NAME_MAP = {
    "sobolev":  "соболєв",
    "yourname": "вашеімʼя",
}
```

### Changing Rada crawl target

Edit `RADA_INDEX_URL` and the regex in `fetch_sub_page_links()` in `rada-tsk-to-notion/main.py`.

---

## Running manually

Actions → pick a workflow → **Run workflow**

---

## How deduplication works

All three scripts load existing Notion entries before writing anything. An entry is skipped if its name (Moodle/Rada) or URL (Sheets) already exists in the database. Safe to run multiple times.

---

## Security

No credentials or IDs in the code — everything is in GitHub Secrets (encrypted, never visible in logs). The `.gs` files are the original Google Apps Script versions kept as reference.
