// ============================================================
// Google Sheets → Notion: Ukrainian Media Mentions Sync
// Google Apps Script (bound to the Sheets spreadsheet)
//
// SETUP:
//   1. Script Properties → set NOTION_TOKEN
//   2. Run syncToNotion() once manually to test
//   3. Triggers → Add trigger → syncToNotion → Time-driven → Day timer
// ============================================================

// ── CONFIGURE THESE ──────────────────────────────────────────

// Notion database for Ukrainian media mentions
const NOTION_DATABASE_ID = '342d1fbd7688805f80a7e70bf32f4762';

// ── INTERNALS ─────────────────────────────────────────────────

const NOTION_VERSION  = '2022-06-28';
const MAX_RUN_SECONDS = 300;

// ============================================================
// MAIN SYNC
// ============================================================

function syncToNotion() {
  const props = PropertiesService.getScriptProperties();
  const token = props.getProperty('NOTION_TOKEN');
  if (!token) throw new Error('Set NOTION_TOKEN in Script Properties');

  const sheet   = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  const data    = sheet.getDataRange().getValues();
  const headers = data[0].map(h => String(h).trim());

  const idx = {
    date:   headers.indexOf('Дата'),
    status: headers.indexOf('Статус'),
    screen: headers.indexOf('Скрін'),
    link:   headers.indexOf('Лінк'),
    synced: headers.indexOf('Notion Synced'),
  };

  if (idx.synced === -1) {
    idx.synced = headers.length;
    sheet.getRange(1, idx.synced + 1).setValue('Notion Synced');
  }

  const startRow  = Number(props.getProperty('SYNC_LAST_ROW') || 1);
  const startTime = Date.now();

  // Carry the last known date forward from already-processed rows
  let lastKnownDate = null;
  for (let i = 1; i < startRow; i++) {
    const d = parseDate(data[i][idx.date]);
    if (d) lastKnownDate = d;
  }

  Logger.log('Loading existing Notion links...');
  const existingLinks = fetchAllNotionLinks(token);
  Logger.log(`Loaded ${existingLinks.size} existing links from Notion.`);

  let synced = 0, skipped = 0, errors = 0, lastRow = startRow;

  for (let i = startRow; i < data.length; i++) {
    if ((Date.now() - startTime) / 1000 > MAX_RUN_SECONDS) {
      props.setProperty('SYNC_LAST_ROW', String(i));
      Logger.log(`Paused at row ${i + 1}. Synced ${synced}, skipped ${skipped}, errors ${errors}. Will resume next run.`);
      return;
    }

    const row = data[i];
    const d = parseDate(row[idx.date]);
    if (d) lastKnownDate = d;

    if (row[idx.synced]) { lastRow = i + 1; continue; }

    const link = String(row[idx.link] || '').trim();
    if (!link)  { lastRow = i + 1; continue; }

    const isoDate = lastKnownDate;
    const status  = String(row[idx.status] || '').trim();
    const screen  = String(row[idx.screen] || '').trim();
    const title   = extractTitle(link);

    try {
      if (existingLinks.has(link)) {
        sheet.getRange(i + 1, idx.synced + 1).setValue('exists');
        skipped++;
      } else {
        createNotionPage(token, title, link, screen, isoDate, status);
        sheet.getRange(i + 1, idx.synced + 1).setValue(new Date().toISOString());
        existingLinks.add(link);
        synced++;
        Utilities.sleep(350);
      }
    } catch (e) {
      Logger.log(`Row ${i + 1} error: ${e.message}`);
      errors++;
    }

    lastRow = i + 1;
  }

  props.setProperty('SYNC_LAST_ROW', '1');
  Logger.log(`Finished. Synced ${synced}, skipped ${skipped}, errors ${errors}.`);
}

// ============================================================
// ADMIN UTILITIES
// ============================================================

// Undo the sync: archives all pages created from this sheet and resets sync markers.
// Run multiple times until complete (handles the 5-minute timeout).
function deleteNewRecords() {
  const props = PropertiesService.getScriptProperties();
  const token = props.getProperty('NOTION_TOKEN');
  if (!token) throw new Error('Set NOTION_TOKEN in Script Properties');

  const sheet   = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  const data    = sheet.getDataRange().getValues();
  const headers = data[0].map(h => String(h).trim());
  const idx = {
    link:   headers.indexOf('Лінк'),
    synced: headers.indexOf('Notion Synced'),
  };

  const startRow  = Number(props.getProperty('DELETE_LAST_ROW') || 1);
  const startTime = Date.now();
  let deleted = 0, notFound = 0, errors = 0;

  for (let i = startRow; i < data.length; i++) {
    if ((Date.now() - startTime) / 1000 > MAX_RUN_SECONDS) {
      props.setProperty('DELETE_LAST_ROW', String(i));
      Logger.log(`Paused at row ${i + 1}. Deleted ${deleted}. Run again to continue.`);
      return;
    }

    const row = data[i];
    if (!row[idx.synced]) continue;

    const link = String(row[idx.link] || '').trim();
    if (!link) {
      sheet.getRange(i + 1, idx.synced + 1).clearContent();
      continue;
    }

    try {
      const pageId = findNotionPageByLink(token, link);
      if (pageId) {
        archiveNotionPage(token, pageId);
        deleted++;
      } else {
        notFound++;
      }
      sheet.getRange(i + 1, idx.synced + 1).clearContent();
    } catch (e) {
      Logger.log(`Row ${i + 1} error: ${e.message}`);
      errors++;
    }

    Utilities.sleep(350);
  }

  props.deleteProperty('DELETE_LAST_ROW');
  props.deleteProperty('SYNC_LAST_ROW');
  Logger.log(`Delete done. Archived ${deleted}, not found ${notFound}, errors ${errors}. Sheet reset.`);
}

// Backfill dates for already-synced records that got a null date.
// Run once manually after deploying the date-inference fix.
function backfillDates() {
  const props = PropertiesService.getScriptProperties();
  const token = props.getProperty('NOTION_TOKEN');
  if (!token) throw new Error('Set NOTION_TOKEN in Script Properties');

  const sheet   = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  const data    = sheet.getDataRange().getValues();
  const headers = data[0].map(h => String(h).trim());
  const idx = {
    date:   headers.indexOf('Дата'),
    link:   headers.indexOf('Лінк'),
    synced: headers.indexOf('Notion Synced'),
  };

  const startTime = Date.now();
  let lastKnownDate = null;
  let updated = 0, skipped = 0, errors = 0;

  for (let i = 1; i < data.length; i++) {
    if ((Date.now() - startTime) / 1000 > MAX_RUN_SECONDS) {
      Logger.log(`Backfill paused at row ${i + 1}. Updated ${updated}.`);
      return;
    }

    const row = data[i];
    const d = parseDate(row[idx.date]);
    if (d) lastKnownDate = d;

    if (!row[idx.synced]) continue;

    const link = String(row[idx.link] || '').trim();
    if (!link || !lastKnownDate) { skipped++; continue; }

    try {
      const pageId = findNotionPageByLink(token, link);
      if (!pageId) { skipped++; continue; }
      updateNotionDate(token, pageId, lastKnownDate);
      updated++;
      Logger.log(`Row ${i + 1}: updated date to ${lastKnownDate}`);
    } catch (e) {
      Logger.log(`Row ${i + 1} error: ${e.message}`);
      errors++;
    }

    Utilities.sleep(350);
  }

  Logger.log(`Backfill done. Updated ${updated}, skipped ${skipped}, errors ${errors}.`);
}

// ============================================================
// TRIGGERS (run once manually to install the daily schedule)
// ============================================================

function createDailyTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'syncToNotion') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('syncToNotion')
    .timeBased()
    .everyDays(1)
    .atHour(6)
    .create();
  Logger.log('Daily trigger created → syncToNotion at 06:00');
}

// ============================================================
// NOTION API
// ============================================================

function createNotionPage(token, title, link, screen, isoDate, status) {
  const props = {
    'What':                  { title: [{ text: { content: title } }] },
    'Classification status': { status: { name: 'Queued' } },
  };

  if (link)    props['Лінк']  = { url: link };
  if (screen)  props['Скрін'] = { url: screen };
  if (isoDate) props['Дата_YYYY_MM_DD_inferred'] = { date: { start: isoDate } };
  if (status === 'Подія' || status === 'Чисто') {
    props['Статус'] = { select: { name: status } };
  }

  const resp = UrlFetchApp.fetch('https://api.notion.com/v1/pages', {
    method: 'post',
    muteHttpExceptions: true,
    headers: notionHeaders(token),
    payload: JSON.stringify({ parent: { database_id: NOTION_DATABASE_ID }, properties: props }),
  });

  if (resp.getResponseCode() !== 200) {
    throw new Error(`HTTP ${resp.getResponseCode()}: ${resp.getContentText().slice(0, 300)}`);
  }
}

function findNotionPageByLink(token, link) {
  const resp = UrlFetchApp.fetch(
    `https://api.notion.com/v1/databases/${NOTION_DATABASE_ID}/query`,
    {
      method: 'post',
      muteHttpExceptions: true,
      headers: notionHeaders(token),
      payload: JSON.stringify({ filter: { property: 'Лінк', url: { equals: link } }, page_size: 1 }),
    }
  );

  if (resp.getResponseCode() !== 200) {
    throw new Error(`Query HTTP ${resp.getResponseCode()}: ${resp.getContentText().slice(0, 200)}`);
  }

  const results = JSON.parse(resp.getContentText()).results;
  return results.length > 0 ? results[0].id : null;
}

function fetchAllNotionLinks(token) {
  const links = new Set();
  let cursor  = null;

  do {
    const body = { page_size: 100 };
    if (cursor) body.start_cursor = cursor;

    const resp = UrlFetchApp.fetch(
      `https://api.notion.com/v1/databases/${NOTION_DATABASE_ID}/query`,
      { method: 'post', muteHttpExceptions: true, headers: notionHeaders(token), payload: JSON.stringify(body) }
    );

    if (resp.getResponseCode() !== 200) {
      throw new Error(`Bulk fetch HTTP ${resp.getResponseCode()}: ${resp.getContentText().slice(0, 200)}`);
    }

    const json = JSON.parse(resp.getContentText());
    for (const page of json.results) {
      const prop = page.properties['Лінк'];
      if (prop && prop.url) links.add(prop.url);
    }

    cursor = json.has_more ? json.next_cursor : null;
    if (cursor) Utilities.sleep(300);
  } while (cursor);

  return links;
}

function archiveNotionPage(token, pageId) {
  const resp = UrlFetchApp.fetch(
    `https://api.notion.com/v1/pages/${pageId}`,
    {
      method: 'patch',
      muteHttpExceptions: true,
      headers: notionHeaders(token),
      payload: JSON.stringify({ archived: true }),
    }
  );

  if (resp.getResponseCode() !== 200) {
    throw new Error(`Archive HTTP ${resp.getResponseCode()}: ${resp.getContentText().slice(0, 200)}`);
  }
}

function updateNotionDate(token, pageId, isoDate) {
  const resp = UrlFetchApp.fetch(
    `https://api.notion.com/v1/pages/${pageId}`,
    {
      method: 'patch',
      muteHttpExceptions: true,
      headers: notionHeaders(token),
      payload: JSON.stringify({ properties: { 'Дата_YYYY_MM_DD_inferred': { date: { start: isoDate } } } }),
    }
  );

  if (resp.getResponseCode() !== 200) {
    throw new Error(`Update HTTP ${resp.getResponseCode()}: ${resp.getContentText().slice(0, 200)}`);
  }
}

function notionHeaders(token) {
  return {
    'Authorization':  `Bearer ${token}`,
    'Content-Type':   'application/json',
    'Notion-Version': NOTION_VERSION,
  };
}

// ============================================================
// HELPERS
// ============================================================

function parseDate(raw) {
  if (raw === null || raw === undefined || raw === '') return null;

  if (raw instanceof Date) {
    if (isNaN(raw.getTime())) return null;
    return Utilities.formatDate(raw, Session.getScriptTimeZone(), 'yyyy-MM-dd');
  }

  const cleaned = String(raw).replace(/\s+/g, ' ').trim();
  if (!cleaned) return null;

  if (/^\d{4}-\d{2}-\d{2}$/.test(cleaned)) return cleaned;

  const m = cleaned.match(/^(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?/);
  if (m) {
    const day   = m[1].padStart(2, '0');
    const month = m[2].padStart(2, '0');
    const year  = m[3] || String(inferYear(Number(m[2])));
    return `${year}-${month}-${day}`;
  }

  return null;
}

function inferYear(month) {
  const now = new Date();
  const cur = now.getMonth() + 1;
  const yr  = now.getFullYear();
  return (month > cur + 1) ? yr - 1 : yr;
}

function extractTitle(link) {
  if (!link) return 'Untitled';
  const m = link.match(/^https?:\/\/([^\/]+)(\/.*)?$/);
  if (!m) return link.slice(0, 100);

  const host  = m[1].replace(/^www\./, '');
  const path  = (m[2] || '').replace(/\/$/, '');
  const parts = path.split('/').filter(Boolean);

  if (host === 't.me' && parts.length >= 2) return `@${parts[0]} · ${parts[1]}`;
  if (host === 't.me' && parts.length === 1) return `@${parts[0]}`;

  const last = parts[parts.length - 1] || '';
  return last ? `${host} / ${last}` : host;
}
