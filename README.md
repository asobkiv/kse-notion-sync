# kse-notion-sync

Automated pipelines that pull content from external sources into a Notion **⏱️ Artifacts** database. Run on GitHub Actions — no servers, no manual work.

| Workflow | Source | Schedule | Creates in Notion |
|---|---|---|---|
| `moodle-to-notion` | KSE Moodle | daily 09:00 Kyiv | Teaching evidence — slides, docs, YouTube recordings |
| `rada-tsk-to-notion` | Rada TSK stenographic records | every Monday 10:00 Kyiv | Documents linked to Honcharenko Crisis Topic |
| `sheets-to-notion` | Google Sheets (media mentions) | daily 08:00 Kyiv | Media mention pages |

---

## Running manually

GitHub → **Actions** → pick a workflow → **Run workflow**

---

## GitHub Secrets

Settings → Secrets and variables → Actions

| Secret | Used by |
|---|---|
| `MOODLE_NOTION_TOKEN` | moodle-to-notion |
| `MOODLE_USERNAME` | moodle-to-notion |
| `MOODLE_PASSWORD` | moodle-to-notion |
| `YOUTUBE_API_KEY` | moodle-to-notion |
| `RADA_NOTION_TOKEN` | rada-tsk-to-notion |
| `SHEETS_NOTION_TOKEN` | sheets-to-notion |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | all three (Drive + Sheets access) |

---

## Configuration

Each script has a `# ── CONFIGURE THESE ──` section at the top with constants you can edit directly in the file (Drive folder IDs, course filters, course name mappings, etc.). Edit → commit → push — the next run picks up the changes automatically.

### Adding a new Moodle course

1. Find the course ID in Moodle URL: `.../course/view.php?id=XXXX`
2. Add it to `COURSE_ID_FILTER` in `moodle-to-notion/main.py`
3. Add a mapping to `COURSE_MAP` if the course name isn't covered
4. Commit and push

### Adding a new teacher name override

If a teacher's English name on Moodle doesn't transliterate correctly to Ukrainian (e.g. Russified surnames), add an entry to `TEACHER_NAME_MAP` in `moodle-to-notion/main.py`:
```python
TEACHER_NAME_MAP = {
    "sobolev":  "соболєв",
    "yourname": "вашеімʼя",
}
```

---

## Google Service Account setup (one-time)

The service account lets GitHub Actions write to Google Drive and read/write Google Sheets without your personal credentials.

1. [console.cloud.google.com](https://console.cloud.google.com) → IAM → Service Accounts → Create
2. Keys → Add Key → JSON → save the file
3. Enable APIs: **Google Drive API** and **Google Sheets API** in the same project
4. Add the JSON contents as `GOOGLE_SERVICE_ACCOUNT_JSON` secret on GitHub
5. Share each Drive folder and the Sheets spreadsheet with the service account email (`...@....iam.gserviceaccount.com`) as Editor

---

## Security

No credentials in code. All tokens, passwords, and keys are stored as GitHub Actions Secrets — encrypted, never visible in logs.

The `.gs` files in each folder are the original Google Apps Script versions kept as reference backup.
