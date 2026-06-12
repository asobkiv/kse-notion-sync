// ============================================================
// Rada TSK → Notion: Document Sync
// Google Apps Script
//
// SETUP:
//   1. Script Properties → set NOTION_TOKEN
//   2. Edit DRIVE_FOLDER_ID and HONCHARENKO_PAGE_ID below
//   3. Run syncRadaTskToNotion() once manually to test
//   4. Triggers → Add trigger → syncRadaTskToNotion → Time-driven → Week timer
// ============================================================

// ── CONFIGURE THESE ──────────────────────────────────────────

// Google Drive folder ID for downloaded PDFs
// Drive → open folder → copy ID from URL (…/folders/THIS_PART)
const DRIVE_FOLDER_ID = ''; // ← paste your folder ID here

// Notion artifacts database
const NOTION_DB_ID = 'a32758a7dea44e319d1acf59760ad6a6';

// Notion page ID for the Honcharenko Crisis Topic (used for Relation)
// Open the Crisis Topic page in Notion → copy the 32-char ID from the URL
const HONCHARENKO_PAGE_ID = '358d1fbd-7688-813e-a700-e4119c3d2709';

// ── INTERNALS ─────────────────────────────────────────────────

const RADA_BASE_URL  = 'https://www.rada.gov.ua';
const RADA_INDEX_URL = RADA_BASE_URL + '/documents/tskVRU/tskzakon/dijal_tskzakon';
const NOTION_VERSION = '2022-06-28';

// ============================================================
// MAIN
// ============================================================

function syncRadaTskToNotion() {
  var props       = PropertiesService.getScriptProperties();
  var notionToken = props.getProperty('NOTION_TOKEN');
  if (!notionToken)    throw new Error('Set NOTION_TOKEN in Script Properties');
  if (!DRIVE_FOLDER_ID) throw new Error('Set DRIVE_FOLDER_ID in the script config');

  var subPages = fetchSubPageLinks();
  Logger.log('Found ' + subPages.length + ' sub-page(s)');

  var existingNotion = fetchExistingNames(notionToken);
  Logger.log(existingNotion.size + ' existing entries in Notion');

  var created = 0, skipped = 0, errors = 0;

  for (var pi = 0; pi < subPages.length; pi++) {
    Logger.log('Processing: ' + subPages[pi]);

    var files;
    try {
      files = fetchFileLinks(subPages[pi]);
    } catch (e) {
      Logger.log('  Could not fetch sub-page: ' + e.message);
      continue;
    }
    Logger.log('  Found ' + files.length + ' file(s)');

    for (var fi = 0; fi < files.length; fi++) {
      var file = files[fi];
      try {
        if (existingNotion.has(file.name)) {
          skipped++;
          continue;
        }
        var driveUrl = uploadFileToDrive(file.radaUrl, file.filename);
        file.sourceUrl = driveUrl || file.radaUrl;

        createNotionDocument(notionToken, file);
        existingNotion.add(file.name);
        created++;
        Utilities.sleep(350);
      } catch (e) {
        Logger.log('  Error on "' + file.name + '": ' + e.message);
        errors++;
      }
    }
  }

  Logger.log('Done. Created: ' + created + ' | Skipped: ' + skipped + ' | Errors: ' + errors);
}

// ============================================================
// RADA PARSING
// ============================================================

function fetchSubPageLinks() {
  var resp = UrlFetchApp.fetch(RADA_INDEX_URL, { muteHttpExceptions: true });
  var html = resp.getContentText('UTF-8');

  var links = [];
  var seen  = {};
  var re    = /href="(\/documents\/tskVRU\/tskzakon\/dijal_tskzakon\/\d+\.html)"/g;
  var match;

  while ((match = re.exec(html)) !== null) {
    var url = RADA_BASE_URL + match[1];
    if (!seen[url]) { seen[url] = true; links.push(url); }
  }
  return links;
}

function fetchFileLinks(subPageUrl) {
  var resp = UrlFetchApp.fetch(subPageUrl, { muteHttpExceptions: true });
  var html = resp.getContentText('UTF-8');

  var files = [];
  var seen  = {};

  // Matches: <a class="attachment-list__name" href="/uploads/documents/…">
  //            <i class="fa fa-paperclip"></i>
  //            Стенограма 15.05.2026
  //          </a>
  var re = /<a[^>]*class="attachment-list__name"[^>]*href="(\/uploads\/documents\/[^"]+)"[^>]*>([\s\S]*?)<\/a>/gi;
  var match;

  while ((match = re.exec(html)) !== null) {
    var href = match[1];
    var name = match[2].replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();
    name = decodeHtmlEntities(name);

    var radaUrl = RADA_BASE_URL + href;
    if (!name || seen[radaUrl]) continue;
    seen[radaUrl] = true;

    var ext      = href.split('.').pop().toLowerCase();
    var filename = name + '.' + ext;

    files.push({
      name:     name,
      filename: filename,
      radaUrl:  radaUrl,
      date:     extractDate(name),
    });
  }

  return files;
}

function extractDate(text) {
  var m = text.match(/(\d{2})\.(\d{2})\.(\d{4})/);
  if (m) return m[3] + '-' + m[2] + '-' + m[1];
  return null;
}

function decodeHtmlEntities(str) {
  return str
    .replace(/&amp;/g,  '&')
    .replace(/&lt;/g,   '<')
    .replace(/&gt;/g,   '>')
    .replace(/&quot;/g, '"')
    .replace(/&#039;/g, "'")
    .replace(/&nbsp;/g, ' ')
    .trim();
}

// ============================================================
// GOOGLE DRIVE — upload with dedup
// ============================================================

function uploadFileToDrive(radaUrl, filename) {
  var existing = findFileInDrive(filename);
  if (existing) {
    Logger.log('  Drive: already exists — ' + filename);
    return existing;
  }

  try {
    var response = UrlFetchApp.fetch(radaUrl, { muteHttpExceptions: true });
    if (response.getResponseCode() !== 200) {
      Logger.log('  Could not download: ' + filename + ' (HTTP ' + response.getResponseCode() + ')');
      return null;
    }
    var blob = response.getBlob().setName(filename);
    var file = DriveApp.getFolderById(DRIVE_FOLDER_ID).createFile(blob);
    file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
    Logger.log('  Drive: uploaded — ' + filename);
    return 'https://drive.google.com/file/d/' + file.getId() + '/view';
  } catch (e) {
    Logger.log('  Drive upload failed for ' + filename + ': ' + e.message);
    return null;
  }
}

function findFileInDrive(filename) {
  try {
    var folder = DriveApp.getFolderById(DRIVE_FOLDER_ID);
    var files  = folder.getFilesByName(filename);
    if (files.hasNext()) {
      return 'https://drive.google.com/file/d/' + files.next().getId() + '/view';
    }
  } catch (e) {
    Logger.log('  Drive search failed: ' + e.message);
  }
  return null;
}

// ============================================================
// NOTION
// ============================================================

function createNotionDocument(token, file) {
  var properties = {
    'Name':                  { title: [{ text: { content: file.name.slice(0, 2000) } }] },
    'Source URL':            { url: file.sourceUrl },
    'Artifact Type':         { select: { name: 'Document' } },
    'Evidence format':       { select: { name: 'Document' } },
    'Classification status': { status: { name: 'Queued' } },
    'Tags':                  { multi_select: [{ name: 'Evidence' }, { name: 'Timeline' }] },
    'Relation':              { relation: [{ id: HONCHARENKO_PAGE_ID }] },
  };

  if (file.date) {
    properties['Date'] = { date: { start: file.date } };
  }

  var resp = UrlFetchApp.fetch('https://api.notion.com/v1/pages', {
    method:             'post',
    muteHttpExceptions: true,
    headers:            notionHeaders(token),
    payload:            JSON.stringify({ parent: { database_id: NOTION_DB_ID }, properties: properties }),
  });

  if (resp.getResponseCode() !== 200) {
    throw new Error('HTTP ' + resp.getResponseCode() + ': ' + resp.getContentText().slice(0, 300));
  }
}

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

    var json = JSON.parse(resp.getContentText());
    if (!json.results) throw new Error('Notion query failed: ' + resp.getContentText().slice(0, 200));

    for (var i = 0; i < json.results.length; i++) {
      var t = json.results[i].properties['Name'];
      if (t && t.title && t.title[0]) names.add(t.title[0].plain_text);
    }
    cursor = json.has_more ? json.next_cursor : null;
    if (cursor) Utilities.sleep(300);
  } while (cursor);

  return names;
}

function notionHeaders(token) {
  return {
    'Authorization':  'Bearer ' + token,
    'Content-Type':   'application/json',
    'Notion-Version': NOTION_VERSION,
  };
}

// ============================================================
// TRIGGERS (run once manually to install the weekly schedule)
// ============================================================

function createWeeklyTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'syncRadaTskToNotion') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('syncRadaTskToNotion')
    .timeBased()
    .onWeekDay(ScriptApp.WeekDay.MONDAY)
    .atHour(8)
    .create();
  Logger.log('Weekly trigger created → syncRadaTskToNotion every Monday at 08:00');
}
