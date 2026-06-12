# KSE Notion Sync — Google Apps Script automations

Three independent Apps Script projects that pull content from external sources into a Notion **⏱️ Artifacts** database.

| Script | Source | What it creates |
|---|---|---|
| `moodle-to-notion/` | KSE Moodle (teaching.kse.org.ua) | Teaching evidence — slides, documents, YouTube recordings |
| `rada-tsk-to-notion/` | Rada TSK stenographic records (rada.gov.ua) | Document entries linked to the Honcharenko Crisis Topic |
| `sheets-to-notion/` | Google Sheets (Ukrainian media mentions) | Media mention pages |

---

## How these scripts work

All three scripts run on a time-based trigger in Google Apps Script and write to the same Notion database. No servers, no hosting — everything runs inside Google's infrastructure for free.

**Credentials** are stored only in Apps Script's Script Properties (never in the code). The code itself is safe to publish publicly.

---

## Setup — moodle-to-notion

### 1. Create the Apps Script project

1. Go to [script.google.com](https://script.google.com) → **New project**
2. Rename it to `moodle-to-notion`
3. Delete the placeholder `Code.gs` content
4. Paste the contents of `moodle-to-notion/main.gs`
5. Open `appsscript.json` (View → Show manifest file) and paste the contents of `moodle-to-notion/appsscript.json`

### 2. Enable YouTube Data API v3

Services (left sidebar) → **+** → YouTube Data API v3 → Add

### 3. Set Script Properties

Project Settings (gear icon) → Script Properties → Add:

| Property | Value |
|---|---|
| `NOTION_TOKEN` | Your Notion integration secret |
| `MOODLE_USERNAME` | Your KSE Moodle email |
| `MOODLE_PASSWORD` | Your KSE Moodle password |

### 4. Configure the script

Edit the constants at the top of `main.gs`:

- `DRIVE_FOLDER_ID` — Google Drive folder ID where files are uploaded
- `COURSE_ID_FILTER` — Moodle course IDs to sync (empty = all enrolled courses)
- `TEACHER_NAME_MAP` — add entries if a teacher's English name doesn't transliterate correctly to Ukrainian
- `COURSE_MAP` — mapping from Moodle course names to Notion Course options

### 5. Connect the Notion integration

The integration needs access to both databases:

- **⏱️ Artifacts** database → `...` → Connections → add your integration
- **Crisis Topics** database (teachers) → `...` → Connections → add your integration

### 6. Test and schedule

- Run `syncMoodleToNotion()` once manually
- Then run `createDailyTrigger()` once to set up the daily schedule (runs at 07:00)

---

## Setup — rada-tsk-to-notion

### 1. Create the Apps Script project

1. Go to [script.google.com](https://script.google.com) → **New project**
2. Rename it to `rada-tsk-to-notion`
3. Paste the contents of `rada-tsk-to-notion/main.gs`
4. Open manifest and paste `rada-tsk-to-notion/appsscript.json`

### 2. Set Script Properties

| Property | Value |
|---|---|
| `NOTION_TOKEN` | Your Notion integration secret |

### 3. Configure the script

- `DRIVE_FOLDER_ID` — Google Drive folder ID for downloaded PDFs
- `HONCHARENKO_PAGE_ID` — Notion page ID of the Honcharenko Crisis Topic (from its URL)

### 4. Connect Notion integration

- **⏱️ Artifacts** database → `...` → Connections → add integration
- **Crisis Topics** database → `...` → Connections → add integration

### 5. Test and schedule

- Run `syncRadaTskToNotion()` once manually
- Then run `createWeeklyTrigger()` once to set up the weekly schedule (runs every Monday at 08:00)

---

## Setup — sheets-to-notion

This script is **bound to a specific Google Sheet** (not a standalone project).

### 1. Open the Google Sheet

Extensions → Apps Script → paste the contents of `sheets-to-notion/main.gs`

### 2. Set Script Properties

| Property | Value |
|---|---|
| `NOTION_TOKEN` | Your Notion integration secret |

### 3. Connect Notion integration

Open the target Notion database → `...` → Connections → add your integration

### 4. Test and schedule

- Run `syncToNotion()` once manually
- Then run `createDailyTrigger()` once to set up the daily schedule (runs at 06:00)

---

## Security

- **No secrets in code.** All tokens and passwords are stored in Apps Script Script Properties, which are encrypted and private to each project.
- Notion database IDs and Drive folder IDs are in the code — these are not secrets (they require authentication to access), but replace them if you fork this for a different workspace.
- The `DRIVE_FOLDER_ID` in `moodle-to-notion` is set for KSE's Drive folder. Replace it if you set up your own folder.

---

## Syncing code changes (clasp)

To push code updates without copy-pasting:

```bash
npm install -g @google/clasp
clasp login
cd moodle-to-notion
clasp clone <scriptId>   # first time: get scriptId from Apps Script → Project Settings
clasp push               # push local changes to Apps Script
```

---

## Adding new courses (moodle-to-notion)

1. Find the course ID in the Moodle URL: `.../course/view.php?id=XXXX`
2. Add it to `COURSE_ID_FILTER`
3. Add a mapping to `COURSE_MAP` if the course name isn't already covered
4. Push the updated script
