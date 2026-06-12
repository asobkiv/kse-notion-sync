// ============================================================
// Moodle → Notion: Course Content Sync
// Google Apps Script
//
// SETUP:
//   1. Script Properties → set NOTION_TOKEN, MOODLE_USERNAME, MOODLE_PASSWORD
//   2. Edit DRIVE_FOLDER_ID and COURSE_ID_FILTER below to match your setup
//   3. Services → Add → YouTube Data API v3  (needed for video duration)
//   4. Run syncMoodleToNotion() once manually to test
//   5. Triggers → Add trigger → syncMoodleToNotion → Time-driven → Day timer
// ============================================================

// ── CONFIGURE THESE ──────────────────────────────────────────

// Google Drive folder ID for uploaded files
// Drive → open folder → copy ID from URL (…/folders/THIS_PART)
const DRIVE_FOLDER_ID = '19igBYz95z5LJl1dxx51M7RQqzdjiIvTa';

// Notion artifacts database
const NOTION_DB_ID = 'a32758a7dea44e319d1acf59760ad6a6';

// Crisis Topics database (teachers live here as pages)
const CRISIS_TOPICS_DB_ID = 'ca860c20-0776-4862-b19d-ecd1d1370297';

// Only sync these Moodle course IDs (empty array = all enrolled courses)
const COURSE_ID_FILTER = [3261, 3508, 3942, 2688, 3563];

// Manual override: Moodle course ID → Notion page URL of teacher
// Useful when auto-matching fails for a specific course
const TEACHER_OVERRIDES = {
  // '3261': 'https://www.notion.so/...',
};

// Maps English surname fragments → Ukrainian, for cross-language matching
// Add entries when auto-transliteration misses someone (e.g. Russified surnames)
const TEACHER_NAME_MAP = {
  'sobolev':  'соболєв',
  'soboliev': 'соболєв',
};

// Moodle course name substring → Notion Course option
const COURSE_MAP = {
  'macroeconomics':                   'Макроекономіка',
  'макроекономіка':                   'Макроекономіка',
  'strategic management':             'Cтратегічний менеджмент',
  'стратегічний менеджмент':          'Cтратегічний менеджмент',
  'geopolitics':                      'Geopolitics and Global Markets',
  'business and global stakeholders': 'Business and Global Stakeholders',
  'трансформація економіки':          'Трансформація економіки України',
  'govtech':                          'Сертифікаційна програма GovTech',
  'energoatom':                       'Energoatom — навчальні семінари',
  'партнерство в gr':                 'Партнерство в GR',
  'взаємодії з владою':               'Фахівець з питань взаємодії з владою',
  'повоєнна відбудова':               'Економіка повоєнної відбудови',
  'відбудова':                        'Економіка повоєнної відбудови',
  'macroclub':                        'Macroclub',
};

const NOTION_COURSE_OPTIONS = [
  'Macroclub', 'Трансформація економіки України', 'Energoatom — навчальні семінари',
  'Other', 'Партнерство в GR', 'Сертифікаційна програма GovTech',
  'Geopolitics and Global Markets', 'Фахівець з питань взаємодії з владою',
  'Економіка повоєнної відбудови', 'Макроекономіка', 'Cтратегічний менеджмент',
  'Business and Global Stakeholders',
];

// ── INTERNALS ─────────────────────────────────────────────────

const MOODLE_BASE_URL = 'https://teaching.kse.org.ua';
const NOTION_VERSION  = '2022-06-28';
const MAX_RUN_SECONDS = 300;
const EXT_SLIDES      = new Set(['ppt', 'pptx', 'key', 'odp']);

// ============================================================
// MAIN
// ============================================================

function syncMoodleToNotion() {
  var props       = PropertiesService.getScriptProperties();
  var notionToken = props.getProperty('NOTION_TOKEN');
  var username    = props.getProperty('MOODLE_USERNAME');
  var password    = props.getProperty('MOODLE_PASSWORD');

  if (!notionToken) throw new Error('Set NOTION_TOKEN in Script Properties');
  if (!username)    throw new Error('Set MOODLE_USERNAME in Script Properties');
  if (!password)    throw new Error('Set MOODLE_PASSWORD in Script Properties');

  var startTime = Date.now();
  var auth      = moodleAuth(username, password);
  Logger.log('Auth method: ' + auth.type);

  var courses = getEnrolledCourses(auth);
  Logger.log('Courses to sync: ' + courses.length);

  var existing = fetchExistingNames(notionToken);
  Logger.log(existing.size + ' existing entries in Notion');

  var created = 0, skipped = 0, errors = 0;

  for (var ci = 0; ci < courses.length; ci++) {
    var course = courses[ci];

    if ((Date.now() - startTime) / 1000 > MAX_RUN_SECONDS - 30) {
      Logger.log('Approaching time limit — re-run to continue.');
      break;
    }

    Logger.log('Processing: ' + course.fullname + ' (id=' + course.id + ')');

    var contents;
    try {
      contents = getCourseContents(auth, course.id);
    } catch (e) {
      Logger.log('  Could not fetch contents: ' + e.message);
      continue;
    }

    var teacher   = getCourseTeacher(auth, notionToken, course.id);
    var resources = extractResources(contents, course.fullname, auth);
    Logger.log('  Found ' + resources.length + ' resource(s)');

    for (var ri = 0; ri < resources.length; ri++) {
      var res = resources[ri];
      res.teacherPageId = teacher.pageId;
      res.teacherName   = teacher.name;

      try {
        if (existing.has(res.name)) {
          skipped++;
          continue;
        }
        if (res.moodleUrl) {
          var driveUrl = uploadFileToDrive(res.moodleUrl, res.driveFilename);
          if (driveUrl) res.sourceUrl = driveUrl;
        }
        createNotionPage(notionToken, res);
        existing.add(res.name);
        created++;
        Utilities.sleep(350);
      } catch (e) {
        Logger.log('  Error on "' + res.name + '": ' + e.message);
        errors++;
      }
    }
  }

  Logger.log('Done. Created: ' + created + ' | Skipped: ' + skipped + ' | Errors: ' + errors);
}

// ============================================================
// TEACHER MATCHING
// ============================================================

// KMU-standard Ukrainian → Latin transliteration
function translitUkrToLat(text) {
  var map = {
    'а':'a','б':'b','в':'v','г':'h','ґ':'g','д':'d','е':'e','є':'ie',
    'ж':'zh','з':'z','и':'y','і':'i','ї':'i','й':'i','к':'k','л':'l',
    'м':'m','н':'n','о':'o','п':'p','р':'r','с':'s','т':'t','у':'u',
    'ф':'f','х':'kh','ц':'ts','ч':'ch','ш':'sh','щ':'shch','ь':'',
    'ю':'iu','я':'ia',
  };
  return text.toLowerCase().split('').map(function(c) {
    return map[c] !== undefined ? map[c] : c;
  }).join('');
}

var _crisisTopicsCache = null;

function loadCrisisTopics(token) {
  if (_crisisTopicsCache) return _crisisTopicsCache;

  var topics = {};
  var cursor = null;

  do {
    var body = { page_size: 100 };
    if (cursor) body.start_cursor = cursor;

    var resp = UrlFetchApp.fetch(
      'https://api.notion.com/v1/databases/' + CRISIS_TOPICS_DB_ID + '/query',
      { method: 'post', headers: notionHeaders(token), payload: JSON.stringify(body), muteHttpExceptions: true }
    );
    var json = JSON.parse(resp.getContentText());
    if (!json.results) {
      Logger.log('  Crisis Topics unavailable — check integration access');
      break;
    }
    for (var i = 0; i < json.results.length; i++) {
      var t = json.results[i].properties['Topic'];
      if (t && t.title && t.title[0]) {
        var ukrName = t.title[0].plain_text.toLowerCase();
        var latName = translitUkrToLat(ukrName);
        var pageId  = json.results[i].id;
        topics[ukrName] = pageId;
        topics[latName] = pageId;
      }
    }
    cursor = json.has_more ? json.next_cursor : null;
    if (cursor) Utilities.sleep(300);
  } while (cursor);

  _crisisTopicsCache = topics;
  Logger.log('Loaded ' + Object.keys(topics).length + ' Crisis Topics entries (incl. transliterated)');
  return topics;
}

function findTeacherPageId(topics, fullname) {
  var lower = fullname.toLowerCase();

  var mapped = lower;
  var keys = Object.keys(TEACHER_NAME_MAP);
  for (var ki = 0; ki < keys.length; ki++) {
    if (lower.indexOf(keys[ki]) !== -1) {
      mapped = lower.replace(keys[ki], TEACHER_NAME_MAP[keys[ki]]);
      break;
    }
  }

  if (topics[lower])  return topics[lower];
  if (topics[mapped]) return topics[mapped];

  for (var name in topics) {
    if (name.indexOf(lower)  !== -1 || lower.indexOf(name)  !== -1) return topics[name];
    if (name.indexOf(mapped) !== -1 || mapped.indexOf(name) !== -1) return topics[name];
  }

  // Word-by-word fallback (last name is usually unique enough)
  var words = lower.split(' ').filter(function(w) { return w.length > 3; });
  for (var wi = 0; wi < words.length; wi++) {
    for (var name in topics) {
      if (name.indexOf(words[wi]) !== -1) return topics[name];
    }
  }

  return null;
}

// Returns { pageId: string|null, name: string|null }
function getCourseTeacher(auth, token, courseId) {
  var override = TEACHER_OVERRIDES[String(courseId)];
  if (override) {
    var m = override.match(/([a-f0-9]{32})|([a-f0-9-]{36})/);
    return { pageId: m ? m[0].replace(/-/g, '') : null, name: null };
  }

  try {
    var users = callMoodleApi(auth, 'core_enrol_get_enrolled_users', { courseid: String(courseId) });
    if (!Array.isArray(users)) return { pageId: null, name: null };

    var teachers = users.filter(function(u) {
      return (u.roles || []).some(function(r) {
        return r.shortname === 'editingteacher' || r.shortname === 'teacher' || r.shortname === 'manager';
      });
    });

    if (teachers.length === 0) {
      Logger.log('  No teachers found');
      return { pageId: null, name: null };
    }

    Logger.log('  Teachers: ' + teachers.map(function(t) { return t.fullname; }).join(', '));

    var topics = loadCrisisTopics(token);
    for (var ti = 0; ti < teachers.length; ti++) {
      var pageId = findTeacherPageId(topics, teachers[ti].fullname);
      if (pageId) {
        Logger.log('  Matched "' + teachers[ti].fullname + '" → Notion page');
        return { pageId: pageId, name: teachers[ti].fullname };
      }
    }

    var allNames = teachers.map(function(t) { return t.fullname; }).join(', ');
    Logger.log('  No Notion match — will write name to page body: ' + allNames);
    return { pageId: null, name: allNames };

  } catch (e) {
    Logger.log('  Could not get teachers: ' + e.message);
    return { pageId: null, name: null };
  }
}

// ============================================================
// MOODLE AUTH
// ============================================================

function moodleAuth(username, password) {
  try {
    var resp = UrlFetchApp.fetch(MOODLE_BASE_URL + '/login/token.php', {
      method: 'post',
      payload: { username: username, password: password, service: 'moodle_mobile_app' },
      muteHttpExceptions: true,
    });
    var data = JSON.parse(resp.getContentText());
    if (data.token) {
      Logger.log('Using Moodle mobile token API');
      return { type: 'token', value: data.token };
    }
    Logger.log('Mobile token not available (' + (data.error || 'unknown') + '), falling back to session');
  } catch (e) {
    Logger.log('Token attempt failed: ' + e.message);
  }
  return sessionLogin(username, password);
}

function sessionLogin(username, password) {
  var loginPageResp = UrlFetchApp.fetch(MOODLE_BASE_URL + '/login/index.php', { muteHttpExceptions: true });
  var html          = loginPageResp.getContentText();
  var tokenMatch    = html.match(/name="logintoken"\s+value="([^"]+)"/);
  var logintoken    = tokenMatch ? tokenMatch[1] : '';

  var loginResp = UrlFetchApp.fetch(MOODLE_BASE_URL + '/login/index.php', {
    method: 'post',
    payload: { username: username, password: password, logintoken: logintoken },
    followRedirects: true,
    muteHttpExceptions: true,
  });

  var cookieRaw = loginResp.getAllHeaders()['Set-Cookie'] || '';
  var cookieStr = Array.isArray(cookieRaw) ? cookieRaw.join('; ') : cookieRaw;
  var sessMatch = cookieStr.match(/MoodleSession=([^;,\s]+)/);
  if (!sessMatch) throw new Error('Login failed — check MOODLE_USERNAME / MOODLE_PASSWORD');

  var cookie  = 'MoodleSession=' + sessMatch[1];
  var sesskey = extractSesskey(loginResp.getContentText()) || extractSesskeyFromDashboard(cookie);
  if (!sesskey) throw new Error('Could not extract sesskey — login may have failed');

  Logger.log('Using session cookie + AJAX API');
  return { type: 'session', cookie: cookie, sesskey: sesskey };
}

function extractSesskey(html) {
  var m = html.match(/["']sesskey["']\s*[=:]\s*["']([^"']{10,})["']/);
  return m ? m[1] : null;
}

function extractSesskeyFromDashboard(cookie) {
  var resp = UrlFetchApp.fetch(MOODLE_BASE_URL + '/my/', {
    headers: { Cookie: cookie },
    muteHttpExceptions: true,
  });
  return extractSesskey(resp.getContentText());
}

// ============================================================
// MOODLE API
// ============================================================

function callMoodleApi(auth, wsfunction, params) {
  if (auth.type === 'token') {
    var payload = { wstoken: auth.value, wsfunction: wsfunction, moodlewsrestformat: 'json' };
    Object.keys(params).forEach(function(k) { payload[k] = params[k]; });
    var resp = UrlFetchApp.fetch(MOODLE_BASE_URL + '/webservice/rest/server.php', {
      method: 'post', payload: payload, muteHttpExceptions: true,
    });
    var data = JSON.parse(resp.getContentText());
    if (data && data.errorcode) throw new Error('Moodle API error: ' + data.message);
    return data;
  }

  var resp = UrlFetchApp.fetch(
    MOODLE_BASE_URL + '/lib/ajax/service.php?sesskey=' + auth.sesskey + '&info=' + wsfunction,
    {
      method: 'post', contentType: 'application/json',
      payload: JSON.stringify([{ index: 0, methodname: wsfunction, args: params }]),
      headers: { Cookie: auth.cookie }, muteHttpExceptions: true,
    }
  );
  var results = JSON.parse(resp.getContentText());
  if (!Array.isArray(results) || !results[0]) {
    throw new Error('Unexpected AJAX response: ' + resp.getContentText().slice(0, 200));
  }
  if (results[0].error) {
    throw new Error('AJAX API error: ' + ((results[0].data && results[0].data.message) || JSON.stringify(results[0]).slice(0, 200)));
  }
  return results[0].data;
}

function getEnrolledCourses(auth) {
  var info   = callMoodleApi(auth, 'core_webservice_get_site_info', {});
  var userId = String(info.userid);
  Logger.log('User ID: ' + userId);

  var courses = callMoodleApi(auth, 'core_enrol_get_users_courses', { userid: userId });
  if (!Array.isArray(courses)) throw new Error('Unexpected courses response');

  if (COURSE_ID_FILTER.length > 0) {
    var filter = COURSE_ID_FILTER.map(String);
    return courses.filter(function(c) { return filter.indexOf(String(c.id)) !== -1; });
  }
  return courses;
}

function getCourseContents(auth, courseId) {
  return callMoodleApi(auth, 'core_course_get_contents', { courseid: String(courseId) });
}

// ============================================================
// RESOURCE EXTRACTION
// ============================================================

function extractResources(sections, courseName, auth) {
  var items = [];

  for (var si = 0; si < sections.length; si++) {
    var modules = sections[si].modules || [];
    for (var mi = 0; mi < modules.length; mi++) {
      var mod = modules[mi];

      if (mod.modname === 'resource' || mod.modname === 'folder') {
        var files = mod.contents || [];
        for (var fi = 0; fi < files.length; fi++) {
          var file = files[fi];
          if (file.type !== 'file') continue;

          var ext    = (file.filename || '').split('.').pop().toLowerCase();
          var format = EXT_SLIDES.has(ext) ? 'Slides' : 'Document';

          items.push({
            name:           mod.name || file.filename,
            moodleUrl:      appendMoodleToken(file.fileurl || '', auth),
            driveFilename:  file.filename || mod.name,
            sourceUrl:      appendMoodleToken(file.fileurl || '', auth),
            evidenceFormat: format,
            courseName:     courseName,
          });
        }
      }

      if (mod.modname === 'url') {
        var link = (mod.contents && mod.contents[0] && mod.contents[0].fileurl) || mod.url || '';
        if (!link) continue;
        var isYt = /youtube\.com|youtu\.be/.test(link);
        var item = {
          name:           mod.name,
          sourceUrl:      link,
          evidenceFormat: isYt ? 'Video' : 'Other',
          courseName:     courseName,
        };
        if (isYt) item.teachingHours = getYouTubeDuration(link);
        items.push(item);
      }
    }
  }

  return items;
}

function appendMoodleToken(url, auth) {
  if (!url || auth.type !== 'token') return url;
  if (url.indexOf('token=') !== -1) return url;
  return url + (url.indexOf('?') !== -1 ? '&' : '?') + 'token=' + auth.value;
}

// ============================================================
// YOUTUBE DURATION
// ============================================================

function getYouTubeDuration(url) {
  var match = url.match(/(?:youtube\.com\/watch\?v=|youtu\.be\/)([a-zA-Z0-9_-]{11})/);
  if (!match) return null;
  try {
    var resp = YouTube.Videos.list('contentDetails', { id: match[1] });
    if (!resp.items || resp.items.length === 0) return null;
    return parseDurationToHours(resp.items[0].contentDetails.duration);
  } catch (e) {
    Logger.log('  YouTube API error for ' + url + ': ' + e.message);
    return null;
  }
}

function parseDurationToHours(iso) {
  var m = iso.match(/PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?/);
  if (!m) return null;
  var total = parseInt(m[1] || 0) + parseInt(m[2] || 0) / 60 + parseInt(m[3] || 0) / 3600;
  if (total < 1) return 1.5;
  return Math.floor(total) + 1;
}

// ============================================================
// GOOGLE DRIVE UPLOAD
// ============================================================

function findFileInDrive(filename) {
  try {
    var folder = DRIVE_FOLDER_ID ? DriveApp.getFolderById(DRIVE_FOLDER_ID) : DriveApp.getRootFolder();
    var files  = folder.getFilesByName(filename);
    if (files.hasNext()) {
      return 'https://drive.google.com/file/d/' + files.next().getId() + '/view';
    }
  } catch (e) {
    Logger.log('  Drive search failed: ' + e.message);
  }
  return null;
}

function uploadFileToDrive(fileUrl, filename) {
  var existing = findFileInDrive(filename);
  if (existing) {
    Logger.log('  Drive: already exists — ' + filename);
    return existing;
  }
  try {
    var response = UrlFetchApp.fetch(fileUrl, { muteHttpExceptions: true });
    if (response.getResponseCode() !== 200) {
      Logger.log('  Could not download: ' + filename + ' (HTTP ' + response.getResponseCode() + ')');
      return null;
    }
    var blob = response.getBlob().setName(filename);
    var file = DRIVE_FOLDER_ID
      ? DriveApp.getFolderById(DRIVE_FOLDER_ID).createFile(blob)
      : DriveApp.createFile(blob);
    file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
    Logger.log('  Drive: uploaded — ' + filename);
    return 'https://drive.google.com/file/d/' + file.getId() + '/view';
  } catch (e) {
    Logger.log('  Drive upload failed for ' + filename + ': ' + e.message);
    return null;
  }
}

// ============================================================
// COURSE NAME → NOTION OPTION
// ============================================================

function resolveCourseOption(moodleName) {
  var lower = moodleName.toLowerCase();
  var keys  = Object.keys(COURSE_MAP);
  for (var i = 0; i < keys.length; i++) {
    if (lower.indexOf(keys[i]) !== -1) return COURSE_MAP[keys[i]];
  }
  for (var j = 0; j < NOTION_COURSE_OPTIONS.length; j++) {
    var opt = NOTION_COURSE_OPTIONS[j];
    if (lower.indexOf(opt.toLowerCase()) !== -1 || opt.toLowerCase().indexOf(lower) !== -1) return opt;
  }
  return 'Other';
}

// ============================================================
// NOTION API
// ============================================================

function fetchExistingNames(token) {
  var names  = new Set();
  var cursor = null;

  do {
    var body = { page_size: 100 };
    if (cursor) body.start_cursor = cursor;

    var resp = UrlFetchApp.fetch(
      'https://api.notion.com/v1/databases/' + NOTION_DB_ID + '/query',
      { method: 'post', muteHttpExceptions: true, headers: notionHeaders(token), payload: JSON.stringify(body) }
    );
    checkNotionResp(resp, 'bulk query');

    var json = JSON.parse(resp.getContentText());
    for (var i = 0; i < json.results.length; i++) {
      var titleProp = json.results[i].properties['Name'];
      if (titleProp && titleProp.title && titleProp.title[0]) {
        names.add(titleProp.title[0].plain_text);
      }
    }
    cursor = json.has_more ? json.next_cursor : null;
    if (cursor) Utilities.sleep(300);
  } while (cursor);

  return names;
}

function createNotionPage(token, res) {
  var properties = {
    'Name':                  { title: [{ text: { content: res.name.slice(0, 2000) } }] },
    'Source URL':            { url: res.sourceUrl },
    'Evidence format':       { select: { name: res.evidenceFormat } },
    'Artifact Type':         { select: { name: 'Teaching evidence' } },
    'Course':                { multi_select: [{ name: resolveCourseOption(res.courseName) }] },
    'Classification status': { status: { name: 'Queued' } },
  };

  if (res.teachingHours) {
    properties['Teaching hours'] = { number: res.teachingHours };
  }

  if (res.teacherPageId) {
    properties['Relation'] = { relation: [{ id: res.teacherPageId }] };
  }

  var body = { parent: { database_id: NOTION_DB_ID }, properties: properties };

  // Teacher found in Moodle but not matched in Crisis Topics → write name to page
  // body so the Notion agent (or human) can set the Relation manually
  if (!res.teacherPageId && res.teacherName) {
    body.children = [{
      object: 'block',
      type: 'paragraph',
      paragraph: {
        rich_text: [{ type: 'text', text: { content: 'Teacher (unmatched): ' + res.teacherName } }],
      },
    }];
  }

  var resp = UrlFetchApp.fetch('https://api.notion.com/v1/pages', {
    method: 'post', muteHttpExceptions: true, headers: notionHeaders(token),
    payload: JSON.stringify(body),
  });
  checkNotionResp(resp, 'create page "' + res.name + '"');
}

function notionHeaders(token) {
  return {
    'Authorization':  'Bearer ' + token,
    'Content-Type':   'application/json',
    'Notion-Version': NOTION_VERSION,
  };
}

function checkNotionResp(resp, context) {
  var code = resp.getResponseCode();
  if (code !== 200) {
    throw new Error('Notion ' + context + ' → HTTP ' + code + ': ' + resp.getContentText().slice(0, 300));
  }
}

// ============================================================
// TRIGGERS (run once manually to install the daily schedule)
// ============================================================

function createDailyTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'syncMoodleToNotion') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('syncMoodleToNotion')
    .timeBased()
    .everyDays(1)
    .atHour(7)
    .create();
  Logger.log('Daily trigger created → syncMoodleToNotion at 07:00');
}

// ============================================================
// DEBUG (run manually to verify auth + course contents)
// ============================================================

function debugMoodle() {
  var props    = PropertiesService.getScriptProperties();
  var username = props.getProperty('MOODLE_USERNAME');
  var password = props.getProperty('MOODLE_PASSWORD');
  var auth     = moodleAuth(username, password);

  var r1 = UrlFetchApp.fetch(MOODLE_BASE_URL + '/webservice/rest/server.php', {
    method: 'post',
    payload: { wstoken: auth.value, wsfunction: 'core_webservice_get_site_info', moodlewsrestformat: 'json' },
    muteHttpExceptions: true,
  });
  Logger.log('=== SITE INFO ===\n' + r1.getContentText().slice(0, 800));

  var r2 = UrlFetchApp.fetch(MOODLE_BASE_URL + '/webservice/rest/server.php', {
    method: 'post',
    payload: { wstoken: auth.value, wsfunction: 'core_course_get_contents', moodlewsrestformat: 'json', courseid: String(COURSE_ID_FILTER[0] || 3261) },
    muteHttpExceptions: true,
  });
  Logger.log('=== COURSE CONTENTS (raw) ===\n' + r2.getContentText().slice(0, 800));
}
