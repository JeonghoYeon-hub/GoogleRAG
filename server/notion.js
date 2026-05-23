'use strict';

const path = require('path');
const { Client, APIResponseError } = require('@notionhq/client');

// ── Text helpers ──────────────────────────────────────────────────────────────

function richTextToMd(rtList) {
  return (rtList || []).map(rt => {
    let txt = rt.plain_text || '';
    const ann = rt.annotations || {};
    const href = rt.href;
    if (ann.code)          txt = `\`${txt}\``;
    if (ann.bold)          txt = `**${txt}**`;
    if (ann.italic)        txt = `*${txt}*`;
    if (ann.strikethrough) txt = `~~${txt}~~`;
    if (href)              txt = `[${txt}](${href})`;
    return txt;
  }).join('');
}

function safeFilename(name) {
  const bad = '<>:"/\\|?*\n\r\t';
  const out = [...(name || '')].map(c => bad.includes(c) ? '_' : c).join('')
    .trim().replace(/^\.+|\.+$/g, '');
  return (out.slice(0, 120)) || 'untitled';
}

function attachmentFilename(url, fallbackPrefix = 'attachment') {
  try {
    const pathname = decodeURIComponent(new URL(url).pathname);
    const name = path.posix.basename(pathname);
    if (name && name.includes('.')) return safeFilename(name);
  } catch {}
  return `${safeFilename(fallbackPrefix)}.bin`;
}

function propValueToText(prop) {
  if (!prop) return '';
  const ptype = prop.type || '';
  const val = prop[ptype];
  if (val == null) return '';

  if (ptype === 'title' || ptype === 'rich_text')
    return richTextToMd(val).replace(/\n/g, ' ').replace(/\|/g, '\\|');
  if (ptype === 'number')
    return val == null ? '' : String(val);
  if (ptype === 'select')
    return (val && val.name) || '';
  if (ptype === 'multi_select')
    return Array.isArray(val) ? val.map(v => v.name || '').join(', ') : '';
  if (ptype === 'status')
    return (val && val.name) || '';
  if (ptype === 'date') {
    if (!val || typeof val !== 'object') return '';
    const s = val.start || '';
    const e = val.end || '';
    return e ? `${s} ~ ${e}` : s;
  }
  if (ptype === 'checkbox')
    return val ? '✓' : '';
  if (ptype === 'url' || ptype === 'email' || ptype === 'phone_number')
    return val ? String(val) : '';
  if (ptype === 'people')
    return Array.isArray(val) ? val.map(p => p.name || '').join(', ') : '';
  if (ptype === 'files') {
    const names = (val || []).map(f =>
      f.name || (f.external || {}).url || (f.file || {}).url || ''
    ).filter(Boolean);
    return names.join(', ');
  }
  if (ptype === 'created_time' || ptype === 'last_edited_time')
    return val ? String(val) : '';
  if (ptype === 'created_by' || ptype === 'last_edited_by')
    return (val && val.name) || '';
  if (ptype === 'formula') {
    if (!val || typeof val !== 'object') return '';
    const ftype = val.type;
    return String(val[ftype] ?? '');
  }
  if (ptype === 'relation')
    return Array.isArray(val) ? `(${val.length}개 관계)` : '';
  if (ptype === 'rollup') {
    if (!val || typeof val !== 'object') return '';
    const rtype = val.type;
    const rval = val[rtype];
    if (Array.isArray(rval))
      return rval.map(v => propValueToText({ type: v.type || 'rich_text', [v.type || 'rich_text']: v[v.type || 'rich_text'] })).join(', ');
    return rval != null ? String(rval) : '';
  }
  return '';
}

// ── Notion API helpers (2025 API / data_sources) ─────────────────────────────

async function retrieveDbInfo(notion, dbOrDsId) {
  try {
    return await notion.request({ method: 'GET', path: `data_sources/${dbOrDsId}` });
  } catch {}
  return notion.databases.retrieve({ database_id: dbOrDsId });
}

async function queryDatabaseRows(notion, databaseId, pageSize = 100) {
  // 1st: data_sources/{id}/query (Notion 2025)
  try {
    const r = await notion.request({
      method: 'POST',
      path:   `data_sources/${databaseId}/query`,
      body:   { page_size: pageSize },
    });
    return r.results || [];
  } catch {}

  // 2nd: classic databases.query
  try {
    const res = await notion.databases.query({ database_id: databaseId, page_size: pageSize });
    return res.results || [];
  } catch {}

  // 3rd: fetch data_sources listed inside databases.retrieve
  let dbInfo;
  try { dbInfo = await notion.databases.retrieve({ database_id: databaseId }); }
  catch { return []; }

  const results = [];
  for (const ds of dbInfo.data_sources || []) {
    if (!ds.id) continue;
    try {
      const r = await notion.request({
        method: 'POST',
        path:   `data_sources/${ds.id}/query`,
        body:   { page_size: pageSize },
      });
      results.push(...(r.results || []));
    } catch {}
  }
  return results;
}

function pageTitle(page) {
  const props = page.properties || {};
  for (const v of Object.values(props)) {
    if (v.type === 'title') return richTextToMd(v.title) || 'Untitled';
  }
  return richTextToMd((props.title || {}).title) || 'Untitled';
}

function itemTitle(obj) {
  if (obj.object === 'database' || obj.object === 'data_source')
    return richTextToMd(obj.title) || 'Untitled Database';
  return pageTitle(obj);
}

// ── Database → Markdown table ─────────────────────────────────────────────────

async function databaseToMdTable(notion, databaseId, titleHint = '') {
  let dbInfo;
  try { dbInfo = await retrieveDbInfo(notion, databaseId); }
  catch (e) { return `_(데이터베이스 로딩 실패: ${e.message})_`; }

  const dbTitle = richTextToMd(dbInfo.title || []) || titleHint || 'Untitled Database';
  const colNames = Object.keys(dbInfo.properties || {});
  if (!colNames.length) return `### 📊 ${dbTitle}\n_(빈 데이터베이스)_`;

  let rows;
  try { rows = await queryDatabaseRows(notion, databaseId, 100); }
  catch (e) { rows = [{ _error: e.message }]; }

  const header = '| ' + colNames.join(' | ') + ' |';
  const sep    = '|' + colNames.map(() => '---').join('|') + '|';
  const lines  = rows.map(row => {
    if (row._error) return `_(이후 행 로딩 실패: ${row._error})_`;
    const props = row.properties || {};
    const cells = colNames.map(c => {
      const text = propValueToText(props[c] || {});
      return text.replace(/\n/g, ' ').replace(/\|/g, '\\|').trim();
    });
    return '| ' + cells.join(' | ') + ' |';
  });

  return `### 📊 ${dbTitle}\n\n${header}\n${sep}\n${lines.join('\n')}`;
}

// ── Block → Markdown ──────────────────────────────────────────────────────────

async function blockToMd(block, notion, depth, attachments, subPages, pageTitlePrefix) {
  const btype = block.type || '';
  const data  = block[btype] || {};
  const indent = '  '.repeat(depth);
  let line = '';

  if (btype === 'paragraph') {
    line = richTextToMd(data.rich_text);
  } else if (btype === 'heading_1') {
    line = '# ' + richTextToMd(data.rich_text);
  } else if (btype === 'heading_2') {
    line = '## ' + richTextToMd(data.rich_text);
  } else if (btype === 'heading_3') {
    line = '### ' + richTextToMd(data.rich_text);
  } else if (btype === 'bulleted_list_item') {
    line = '- ' + richTextToMd(data.rich_text);
  } else if (btype === 'numbered_list_item') {
    line = '1. ' + richTextToMd(data.rich_text);
  } else if (btype === 'to_do') {
    const check = data.checked ? 'x' : ' ';
    line = `- [${check}] ` + richTextToMd(data.rich_text);
  } else if (btype === 'toggle') {
    line = '- ' + richTextToMd(data.rich_text);
  } else if (btype === 'code') {
    const lang = data.language || '';
    const body = richTextToMd(data.rich_text);
    line = `\`\`\`${lang}\n${body}\n\`\`\``;
  } else if (btype === 'quote') {
    line = '> ' + richTextToMd(data.rich_text);
  } else if (btype === 'callout') {
    const emoji = (data.icon || {}).emoji || '';
    line = `> ${emoji} ` + richTextToMd(data.rich_text);
  } else if (btype === 'divider') {
    line = '---';
  } else if (btype === 'child_page') {
    subPages.push({ id: block.id, title: data.title || 'Untitled' });
    line = `**↳ 하위 페이지: ${data.title || ''}**`;
  } else if (btype === 'child_database') {
    const dbTitle = data.title || 'Untitled Database';
    line = await databaseToMdTable(notion, block.id, dbTitle);
  } else if (btype === 'image') {
    const f = data.external || data.file || {};
    const url = f.url || '';
    const cap = richTextToMd(data.caption || []);
    if (url) {
      const fname = attachmentFilename(url, `${pageTitlePrefix}-image`);
      attachments.push({ url, filename: fname });
    }
    line = url ? `![${cap}](${url})` : '';
  } else if (['file', 'pdf', 'video', 'audio'].includes(btype)) {
    const f = data.external || data.file || {};
    const url = f.url || '';
    if (url) {
      const fname = safeFilename(data.name || attachmentFilename(url, `${pageTitlePrefix}-${btype}`));
      attachments.push({ url, filename: fname });
    }
    line = url ? `[📎 ${btype}: ${url}](${url})` : '';
  } else if (btype === 'bookmark') {
    line = `[${data.url || ''}](${data.url || ''})`;
  } else if (btype === 'equation') {
    line = `$$${data.expression || ''}$$`;
  } else {
    line = richTextToMd(data.rich_text || []);
  }

  let result = line ? indent + line : '';

  if (block.has_children && btype !== 'child_page') {
    try {
      const res = await notion.blocks.children.list({ block_id: block.id });
      const childParts = [];
      for (const c of res.results || []) {
        const cm = await blockToMd(c, notion, depth + 1, attachments, subPages, pageTitlePrefix);
        if (cm) childParts.push(cm);
      }
      if (childParts.length) {
        result = result ? result + '\n' + childParts.join('\n') : childParts.join('\n');
      }
    } catch {}
  }

  return result;
}

// ── Main conversion ───────────────────────────────────────────────────────────

async function notionPageToMd(pageId, notionApiKey, {
  includeSubpages   = true,
  includeAttachments = true,
  depth             = 0,
  maxDepth          = 5,
  visited           = null,
} = {}) {
  if (visited === null) visited = new Set();
  if (visited.has(pageId) || depth > maxDepth)
    return { title: '', markdown: '', attachments: [] };
  visited.add(pageId);

  const notion = new Client({ auth: notionApiKey });

  // Detect page vs database
  let page = null;
  let isDatabase = false;
  let dbInfo = null;

  try {
    page = await notion.pages.retrieve({ page_id: pageId });
  } catch {
    try {
      dbInfo = await retrieveDbInfo(notion, pageId);
      isDatabase = true;
    } catch (e) {
      throw e;
    }
  }

  if (isDatabase) {
    const dbTitle = richTextToMd(dbInfo.title || []) || 'Untitled Database';
    const headingLevel = Math.min(depth + 1, 6);
    const tableMd = await databaseToMdTable(notion, pageId, dbTitle);
    const mdParts = [`${'#'.repeat(headingLevel)} ${dbTitle}`, '', tableMd];
    const collectedAttachments = [];

    if (includeSubpages && depth < maxDepth) {
      let childPages = [];
      try { childPages = await queryDatabaseRows(notion, pageId, 100); } catch {}
      for (const child of childPages) {
        try {
          const sub = await notionPageToMd(child.id, notionApiKey, {
            includeSubpages, includeAttachments, depth: depth + 1, maxDepth, visited,
          });
          if (sub.markdown) { mdParts.push('\n---\n'); mdParts.push(sub.markdown); }
          collectedAttachments.push(...sub.attachments);
        } catch (e) {
          mdParts.push(`\n_(행 페이지 로딩 실패: ${e.message})_\n`);
        }
      }
    }

    return {
      title:       dbTitle,
      markdown:    mdParts.join('\n\n'),
      attachments: includeAttachments ? collectedAttachments : [],
    };
  }

  const title = pageTitle(page);
  const safePrefix = safeFilename(title);
  const attachments = [];
  const subPages = [];

  const blocksRes = await notion.blocks.children.list({ block_id: pageId });
  const bodyParts = [];
  for (const b of blocksRes.results || []) {
    const md = await blockToMd(b, notion, 0, attachments, subPages, safePrefix);
    if (md) bodyParts.push(md);
  }
  const body = bodyParts.join('\n\n');

  const headingLevel = Math.min(depth + 1, 6);
  const mdParts = [`${'#'.repeat(headingLevel)} ${title}`, '', body];

  if (includeSubpages) {
    for (const sp of subPages) {
      try {
        const sub = await notionPageToMd(sp.id, notionApiKey, {
          includeSubpages, includeAttachments, depth: depth + 1, maxDepth, visited,
        });
        if (sub.markdown) { mdParts.push('\n---\n'); mdParts.push(sub.markdown); }
        attachments.push(...sub.attachments);
      } catch (e) {
        mdParts.push(`\n_(하위 페이지 '${sp.title}' 로딩 실패: ${e.message})_\n`);
      }
    }
  }

  return {
    title,
    markdown:    mdParts.join('\n\n'),
    attachments: includeAttachments ? attachments : [],
  };
}

// ── Search & children ─────────────────────────────────────────────────────────

async function searchPages(notionApiKey, query = '') {
  const notion = new Client({ auth: notionApiKey });
  const out = [];
  const seenIds = new Set();

  async function collect(filterValue, uiType) {
    try {
      const res = await notion.search({
        query,
        filter: { value: filterValue, property: 'object' },
        page_size: 100,
      });
      for (const o of res.results || []) {
        if (seenIds.has(o.id)) continue;
        seenIds.add(o.id);
        out.push({
          id:               o.id,
          title:            itemTitle(o),
          type:             uiType,
          has_children:     true,
          url:              o.url || '',
          last_edited_time: o.last_edited_time || '',
        });
      }
    } catch {}
  }

  await collect('data_source', 'database');
  await collect('database',    'database');
  await collect('page',        'page');
  return out;
}

async function getChildren(notionApiKey, parentId, parentType = 'page') {
  const notion = new Client({ auth: notionApiKey });
  const out = [];

  if (parentType === 'database') {
    const rows = await queryDatabaseRows(notion, parentId, 100);
    for (const p of rows) {
      out.push({
        id:               p.id,
        title:            itemTitle(p),
        type:             'page',
        has_children:     true,
        url:              p.url || '',
        last_edited_time: p.last_edited_time || '',
      });
    }
  } else {
    let blocks;
    try {
      blocks = await notion.blocks.children.list({ block_id: parentId, page_size: 100 });
    } catch { return []; }
    for (const b of blocks.results || []) {
      const bt = b.type || '';
      if (bt === 'child_page') {
        out.push({
          id:               b.id,
          title:            (b.child_page || {}).title || 'Untitled',
          type:             'page',
          has_children:     true,
          url:              '',
          last_edited_time: b.last_edited_time || '',
        });
      } else if (bt === 'child_database') {
        out.push({
          id:               b.id,
          title:            (b.child_database || {}).title || 'Untitled Database',
          type:             'database',
          has_children:     true,
          url:              '',
          last_edited_time: b.last_edited_time || '',
        });
      }
    }
  }
  return out;
}

module.exports = {
  notionPageToMd, searchPages, getChildren, safeFilename,
};
