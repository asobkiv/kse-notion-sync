"""
Moodle → Notion: Course Content Sync
Runs via GitHub Actions on a daily schedule.

Required env vars (set as GitHub Secrets):
  NOTION_TOKEN                 — Notion integration secret
  MOODLE_USERNAME              — KSE Moodle email
  MOODLE_PASSWORD              — KSE Moodle password
  YOUTUBE_API_KEY              — YouTube Data API v3 key (for video duration)
  GOOGLE_SERVICE_ACCOUNT_JSON  — Google service account JSON (for Drive uploads)
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

DRIVE_FOLDER_ID     = "19igBYz95z5LJl1dxx51M7RQqzdjiIvTa"
MOODLE_BASE_URL     = "https://teaching.kse.org.ua"
NOTION_DB_ID        = "a32758a7dea44e319d1acf59760ad6a6"
CRISIS_TOPICS_DB_ID = "ca860c20-0776-4862-b19d-ecd1d1370297"
NOTION_VERSION      = "2022-06-28"
COURSE_ID_FILTER    = [3261, 3508, 3942, 2688, 3563]  # empty list = all enrolled
EXT_SLIDES          = {"ppt", "pptx", "key", "odp"}

TEACHER_OVERRIDES = {
    # "3261": "https://www.notion.so/...",
}

TEACHER_NAME_MAP = {
    "sobolev":  "соболєв",
    "soboliev": "соболєв",
}

COURSE_MAP = {
    "macroeconomics":                   "Макроекономіка",
    "макроекономіка":                   "Макроекономіка",
    "strategic management":             "Cтратегічний менеджмент",
    "стратегічний менеджмент":          "Cтратегічний менеджмент",
    "geopolitics":                      "Geopolitics and Global Markets",
    "business and global stakeholders": "Business and Global Stakeholders",
    "трансформація економіки":          "Трансформація економіки України",
    "govtech":                          "Сертифікаційна програма GovTech",
    "energoatom":                       "Energoatom — навчальні семінари",
    "партнерство в gr":                 "Партнерство в GR",
    "взаємодії з владою":               "Фахівець з питань взаємодії з владою",
    "повоєнна відбудова":               "Економіка повоєнної відбудови",
    "відбудова":                        "Економіка повоєнної відбудови",
    "macroclub":                        "Macroclub",
}

NOTION_COURSE_OPTIONS = [
    "Macroclub", "Трансформація економіки України", "Energoatom — навчальні семінари",
    "Other", "Партнерство в GR", "Сертифікаційна програма GovTech",
    "Geopolitics and Global Markets", "Фахівець з питань взаємодії з владою",
    "Економіка повоєнної відбудови", "Макроекономіка", "Cтратегічний менеджмент",
    "Business and Global Stakeholders",
]

# ── MAIN ──────────────────────────────────────────────────────

def main():
    notion_token = os.environ["NOTION_TOKEN"]
    username     = os.environ["MOODLE_USERNAME"]
    password     = os.environ["MOODLE_PASSWORD"]
    yt_api_key   = os.environ.get("YOUTUBE_API_KEY", "")
    sa_json      = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

    drive_service = build_drive_service(sa_json) if sa_json else None
    if not drive_service:
        log.info("No GOOGLE_SERVICE_ACCOUNT_JSON — Drive uploads disabled, using Moodle URLs directly")

    auth = moodle_auth(username, password)
    log.info(f"Auth method: {auth['type']}")

    courses = get_enrolled_courses(auth)
    log.info(f"Courses to sync: {len(courses)}")

    existing = fetch_existing_names(notion_token)
    log.info(f"{len(existing)} existing entries in Notion")

    created = skipped = errors = 0

    for course in courses:
        log.info(f"Processing: {course['fullname']} (id={course['id']})")

        try:
            contents = get_course_contents(auth, course["id"])
        except Exception as e:
            log.info(f"  Could not fetch contents: {e}")
            continue

        teacher   = get_course_teacher(auth, notion_token, course["id"])
        resources = extract_resources(contents, course["fullname"], auth, yt_api_key)
        log.info(f"  Found {len(resources)} resource(s)")

        for res in resources:
            res["teacher_page_id"] = teacher["page_id"]
            res["teacher_name"]    = teacher["name"]

            try:
                if res["name"] in existing:
                    skipped += 1
                    continue

                if res.get("moodle_url") and drive_service:
                    drive_url = upload_file_to_drive(drive_service, res["moodle_url"], res["drive_filename"])
                    if drive_url:
                        res["source_url"] = drive_url

                create_notion_page(notion_token, res)
                existing.add(res["name"])
                created += 1
                time.sleep(0.35)
            except Exception as e:
                log.info(f"  Error on \"{res['name']}\": {e}")
                errors += 1

    log.info(f"Done. Created: {created} | Skipped: {skipped} | Errors: {errors}")


# ── MOODLE AUTH ───────────────────────────────────────────────

def moodle_auth(username, password):
    try:
        resp = requests.post(
            f"{MOODLE_BASE_URL}/login/token.php",
            data={"username": username, "password": password, "service": "moodle_mobile_app"},
            timeout=30,
        )
        data = resp.json()
        if "token" in data:
            log.info("Using Moodle mobile token API")
            return {"type": "token", "value": data["token"]}
        log.info(f"Mobile token not available ({data.get('error', 'unknown')}), falling back to session")
    except Exception as e:
        log.info(f"Token attempt failed: {e}")
    return session_login(username, password)


def session_login(username, password):
    session = requests.Session()
    r = session.get(f"{MOODLE_BASE_URL}/login/index.php", timeout=30)
    m = re.search(r'name="logintoken"\s+value="([^"]+)"', r.text)
    logintoken = m.group(1) if m else ""

    session.post(
        f"{MOODLE_BASE_URL}/login/index.php",
        data={"username": username, "password": password, "logintoken": logintoken},
        allow_redirects=True,
        timeout=30,
    )
    sesskey = extract_sesskey(session.get(f"{MOODLE_BASE_URL}/my/", timeout=30).text)
    if not sesskey:
        raise RuntimeError("Could not extract sesskey — login may have failed")

    log.info("Using session cookie + AJAX API")
    return {"type": "session", "session": session, "sesskey": sesskey}


def extract_sesskey(html):
    m = re.search(r"""["']sesskey["']\s*[=:]\s*["']([^"']{10,})["']""", html)
    return m.group(1) if m else None


# ── MOODLE API ────────────────────────────────────────────────

def call_moodle_api(auth, wsfunction, params):
    if auth["type"] == "token":
        resp = requests.post(
            f"{MOODLE_BASE_URL}/webservice/rest/server.php",
            data={"wstoken": auth["value"], "wsfunction": wsfunction, "moodlewsrestformat": "json", **params},
            timeout=60,
        )
        data = resp.json()
        if isinstance(data, dict) and "errorcode" in data:
            raise RuntimeError(f"Moodle API error: {data.get('message')}")
        return data

    resp = auth["session"].post(
        f"{MOODLE_BASE_URL}/lib/ajax/service.php?sesskey={auth['sesskey']}&info={wsfunction}",
        json=[{"index": 0, "methodname": wsfunction, "args": params}],
        timeout=60,
    )
    results = resp.json()
    if not isinstance(results, list) or not results:
        raise RuntimeError(f"Unexpected AJAX response: {resp.text[:200]}")
    if results[0].get("error"):
        msg = (results[0].get("data") or {}).get("message") or str(results[0])[:200]
        raise RuntimeError(f"AJAX API error: {msg}")
    return results[0]["data"]


def get_enrolled_courses(auth):
    info    = call_moodle_api(auth, "core_webservice_get_site_info", {})
    user_id = str(info["userid"])
    log.info(f"User ID: {user_id}")

    courses = call_moodle_api(auth, "core_enrol_get_users_courses", {"userid": user_id})
    if not isinstance(courses, list):
        raise RuntimeError("Unexpected courses response")

    if COURSE_ID_FILTER:
        filter_set = {str(c) for c in COURSE_ID_FILTER}
        return [c for c in courses if str(c["id"]) in filter_set]
    return courses


def get_course_contents(auth, course_id):
    return call_moodle_api(auth, "core_course_get_contents", {"courseid": str(course_id)})


# ── TEACHER MATCHING ──────────────────────────────────────────

_UKR_LAT = {
    "а":"a","б":"b","в":"v","г":"h","ґ":"g","д":"d","е":"e","є":"ie",
    "ж":"zh","з":"z","и":"y","і":"i","ї":"i","й":"i","к":"k","л":"l",
    "м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u",
    "ф":"f","х":"kh","ц":"ts","ч":"ch","ш":"sh","щ":"shch","ь":"",
    "ю":"iu","я":"ia",
}

def translit_ukr_to_lat(text):
    return "".join(_UKR_LAT.get(c, c) for c in text.lower())


_crisis_topics_cache = None

def load_crisis_topics(token):
    global _crisis_topics_cache
    if _crisis_topics_cache is not None:
        return _crisis_topics_cache

    topics = {}
    cursor = None

    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor

        resp = requests.post(
            f"https://api.notion.com/v1/databases/{CRISIS_TOPICS_DB_ID}/query",
            headers=notion_headers(token),
            json=body,
            timeout=30,
        )
        data = resp.json()
        if "results" not in data:
            log.info("  Crisis Topics unavailable — check integration access")
            break

        for page in data["results"]:
            t = page["properties"].get("Topic")
            if t and t.get("title"):
                ukr_name = t["title"][0]["plain_text"].lower()
                lat_name = translit_ukr_to_lat(ukr_name)
                page_id  = page["id"]
                topics[ukr_name] = page_id
                topics[lat_name] = page_id

        if data.get("has_more"):
            cursor = data["next_cursor"]
            time.sleep(0.3)
        else:
            break

    _crisis_topics_cache = topics
    log.info(f"Loaded {len(topics)} Crisis Topics entries (incl. transliterated)")
    return topics


def find_teacher_page_id(topics, fullname):
    lower = fullname.lower()
    mapped = lower
    for key, val in TEACHER_NAME_MAP.items():
        if key in lower:
            mapped = lower.replace(key, val)
            break

    if lower  in topics: return topics[lower]
    if mapped in topics: return topics[mapped]

    for name, page_id in topics.items():
        if name in lower or lower in name: return page_id
        if name in mapped or mapped in name: return page_id

    words = [w for w in lower.split() if len(w) > 3]
    for word in words:
        for name, page_id in topics.items():
            if word in name:
                return page_id

    return None


def get_course_teacher(auth, token, course_id):
    override = TEACHER_OVERRIDES.get(str(course_id))
    if override:
        m = re.search(r"([a-f0-9]{32})|([a-f0-9-]{36})", override)
        return {"page_id": m.group(0).replace("-", "") if m else None, "name": None}

    try:
        users = call_moodle_api(auth, "core_enrol_get_enrolled_users", {"courseid": str(course_id)})
        if not isinstance(users, list):
            return {"page_id": None, "name": None}

        teachers = [
            u for u in users
            if any(r.get("shortname") in ("editingteacher", "teacher", "manager")
                   for r in u.get("roles", []))
        ]

        if not teachers:
            log.info("  No teachers found")
            return {"page_id": None, "name": None}

        log.info("  Teachers: " + ", ".join(t["fullname"] for t in teachers))

        topics = load_crisis_topics(token)
        for teacher in teachers:
            page_id = find_teacher_page_id(topics, teacher["fullname"])
            if page_id:
                log.info(f"  Matched \"{teacher['fullname']}\" → Notion page")
                return {"page_id": page_id, "name": teacher["fullname"]}

        all_names = ", ".join(t["fullname"] for t in teachers)
        log.info(f"  No Notion match — will write name to page body: {all_names}")
        return {"page_id": None, "name": all_names}

    except Exception as e:
        log.info(f"  Could not get teachers: {e}")
        return {"page_id": None, "name": None}


# ── RESOURCE EXTRACTION ───────────────────────────────────────

def extract_resources(sections, course_name, auth, yt_api_key):
    items = []

    for section in sections:
        for mod in section.get("modules", []):
            modname = mod.get("modname")

            if modname in ("resource", "folder"):
                for f in mod.get("contents", []):
                    if f.get("type") != "file":
                        continue
                    ext = (f.get("filename") or "").rsplit(".", 1)[-1].lower()
                    fmt = "Slides" if ext in EXT_SLIDES else "Document"
                    url = append_moodle_token(f.get("fileurl", ""), auth)
                    items.append({
                        "name":           mod.get("name") or f.get("filename"),
                        "moodle_url":     url,
                        "drive_filename": f.get("filename") or mod.get("name"),
                        "source_url":     url,
                        "evidence_format": fmt,
                        "course_name":    course_name,
                    })

            elif modname == "url":
                contents = mod.get("contents") or []
                link = (contents[0].get("fileurl") if contents else None) or mod.get("url", "")
                if not link:
                    continue
                is_yt = bool(re.search(r"youtube\.com|youtu\.be", link))
                item = {
                    "name":           mod.get("name"),
                    "source_url":     link,
                    "evidence_format": "Video" if is_yt else "Other",
                    "course_name":    course_name,
                }
                if is_yt and yt_api_key:
                    item["teaching_hours"] = get_youtube_duration(link, yt_api_key)
                items.append(item)

    return items


def append_moodle_token(url, auth):
    if not url or auth["type"] != "token":
        return url
    if "token=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}token={auth['value']}"


# ── YOUTUBE DURATION ──────────────────────────────────────────

def get_youtube_duration(url, api_key):
    m = re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})", url)
    if not m:
        return None
    try:
        resp = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "contentDetails", "id": m.group(1), "key": api_key},
            timeout=15,
        )
        items = resp.json().get("items", [])
        if not items:
            return None
        return parse_duration_to_hours(items[0]["contentDetails"]["duration"])
    except Exception as e:
        log.info(f"  YouTube API error for {url}: {e}")
        return None


def parse_duration_to_hours(iso):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
    if not m:
        return None
    total = int(m.group(1) or 0) + int(m.group(2) or 0) / 60 + int(m.group(3) or 0) / 3600
    return 1.5 if total < 1 else int(total) + 1


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

        mime = resp.headers.get("Content-Type", "application/octet-stream").split(";")[0]
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

def resolve_course_option(moodle_name):
    lower = moodle_name.lower()
    for key, val in COURSE_MAP.items():
        if key in lower:
            return val
    for opt in NOTION_COURSE_OPTIONS:
        if opt.lower() in lower or lower in opt.lower():
            return opt
    return "Other"


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


def create_notion_page(token, res):
    properties = {
        "Name":                  {"title": [{"text": {"content": res["name"][:2000]}}]},
        "Source URL":            {"url": res["source_url"]},
        "Evidence format":       {"select": {"name": res["evidence_format"]}},
        "Artifact Type":         {"select": {"name": "Teaching evidence"}},
        "Course":                {"multi_select": [{"name": resolve_course_option(res["course_name"])}]},
        "Classification status": {"status": {"name": "Queued"}},
    }

    if res.get("teaching_hours"):
        properties["Teaching hours"] = {"number": res["teaching_hours"]}

    if res.get("teacher_page_id"):
        properties["Relation"] = {"relation": [{"id": res["teacher_page_id"]}]}

    body = {"parent": {"database_id": NOTION_DB_ID}, "properties": properties}

    if not res.get("teacher_page_id") and res.get("teacher_name"):
        body["children"] = [{
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": f"Teacher (unmatched): {res['teacher_name']}"}}],
            },
        }]

    resp = requests.post("https://api.notion.com/v1/pages", headers=notion_headers(token), json=body, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Notion create page → HTTP {resp.status_code}: {resp.text[:300]}")


def notion_headers(token):
    return {
        "Authorization":  f"Bearer {token}",
        "Content-Type":   "application/json",
        "Notion-Version": NOTION_VERSION,
    }


if __name__ == "__main__":
    main()
