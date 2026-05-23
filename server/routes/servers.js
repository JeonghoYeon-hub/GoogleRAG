'use strict';

const express = require('express');
const router = express.Router();
const fs = require('fs');
const path = require('path');
const os = require('os');
const { v4: uuidv4 } = require('uuid');
const multer = require('multer');

const state = require('../state');
const db = require('../db');
const gemini = require('../gemini');
const driveHelper = require('../drive');
const notionHelper = require('../notion');

const { GEMINI_MODELS, DEFAULT_GEMINI_MODEL } = require('./config');

const MIME_MAP = {
  '.pdf':  'application/pdf',
  '.txt':  'text/plain',
  '.md':   'text/plain',
  '.html': 'text/html',
  '.htm':  'text/html',
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.doc':  'application/msword',
  '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  '.xls':  'application/vnd.ms-excel',
  '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  '.ppt':  'application/vnd.ms-powerpoint',
  '.json': 'application/json',
  '.xml':  'application/xml',
  '.csv':  'text/csv',
  '.py':   'text/plain',
  '.js':   'text/plain',
  '.ts':   'text/plain',
  '.java': 'text/plain',
  '.c':    'text/plain',
  '.cpp':  'text/plain',
  '.png':  'image/png',
  '.jpg':  'image/jpeg',
  '.jpeg': 'image/jpeg',
};

function getMime(filename) {
  return MIME_MAP[path.extname(filename).toLowerCase()] || 'application/octet-stream';
}

const upload = multer({
  storage: multer.diskStorage({
    destination: os.tmpdir(),
    filename: (_req, file, cb) => {
      const ext = path.extname(file.originalname) || '';
      cb(null, `upload_${Date.now()}_${Math.random().toString(36).slice(2)}${ext}`);
    },
  }),
  limits: { fileSize: 100 * 1024 * 1024 },
});

// ── Background upload ─────────────────────────────────────────────────────────

async function bgUpload(serverId, fileId, apiKey, storeName, tmpPath, displayName, mimeType) {
  try {
    await gemini.uploadToStore(apiKey, storeName, displayName, tmpPath, mimeType);
    const srv = state.servers[serverId];
    if (srv) {
      const f = srv.files.find(f => f.id === fileId);
      if (f) f.status = 'ready';
    }
    db.updateFile(fileId, 'ready');
  } catch (err) {
    const msg = String(err.message || err);
    const srv = state.servers[serverId];
    if (srv) {
      const f = srv.files.find(f => f.id === fileId);
      if (f) { f.status = 'error'; f.error = msg; }
    }
    db.updateFile(fileId, 'error', msg);
  } finally {
    try { fs.unlinkSync(tmpPath); } catch {}
  }
}

// ── Download URL to temp file ─────────────────────────────────────────────────

async function downloadUrlToTemp(url, suffix) {
  const res = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0' } });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const buffer = Buffer.from(await res.arrayBuffer());
  const tmpPath = path.join(os.tmpdir(), `notion_att_${Date.now()}_${Math.random().toString(36).slice(2)}${suffix}`);
  fs.writeFileSync(tmpPath, buffer);
  return [tmpPath, buffer.length];
}

// ── Server CRUD ───────────────────────────────────────────────────────────────

router.get('/', (req, res) => {
  const list = Object.entries(state.servers).map(([svid, s]) => ({
    id:          svid,
    alias:       s.alias,
    store_name:  s.store_name,
    model:       s.model || DEFAULT_GEMINI_MODEL,
    file_count:  s.files.length,
    ready_count: s.files.filter(f => f.status === 'ready').length,
  }));
  res.json(list);
});

router.post('/', async (req, res) => {
  const { alias, api_key, store_name, model } = req.body;
  const apiKey = (api_key || '').trim() || state.apiKeys.gemini;
  if (!apiKey) return res.status(400).json({ detail: 'API 키가 필요합니다' });
  if (!alias || !alias.trim()) return res.status(400).json({ detail: '별칭을 입력해주세요' });

  const resolvedModel = (model || DEFAULT_GEMINI_MODEL).trim();
  if (!GEMINI_MODELS.includes(resolvedModel))
    return res.status(400).json({ detail: `지원하지 않는 모델: ${resolvedModel}` });

  const svid = uuidv4();
  let resolvedStoreName, existingDocs = [];

  if (store_name) {
    resolvedStoreName = store_name.trim();
    try { existingDocs = await gemini.listDocuments(apiKey, resolvedStoreName); } catch {}
  } else {
    try {
      resolvedStoreName = await gemini.createStore(apiKey, `rag-${svid.slice(0, 8)}`);
    } catch (e) {
      return res.status(400).json({ detail: `스토어 생성 실패: ${e.message}` });
    }
  }

  state.servers[svid] = {
    alias:      alias.trim(),
    store_name: resolvedStoreName,
    api_key:    apiKey,
    model:      resolvedModel,
    files:      existingDocs,
  };
  db.addServer(svid, alias.trim(), apiKey, resolvedStoreName, resolvedModel);
  db.insertFilesIfNotExist(svid, existingDocs);

  res.json({
    id:           svid,
    alias:        alias.trim(),
    store_name:   resolvedStoreName,
    model:        resolvedModel,
    synced_count: existingDocs.length,
  });
});

router.get('/:svid', (req, res) => {
  const { svid } = req.params;
  const s = state.servers[svid];
  if (!s) return res.status(404).json({ detail: '서버를 찾을 수 없습니다' });
  res.json({ id: svid, alias: s.alias, store_name: s.store_name, files: [...s.files] });
});

router.patch('/:svid', (req, res) => {
  const { svid } = req.params;
  const s = state.servers[svid];
  if (!s) return res.status(404).json({ detail: '서버를 찾을 수 없습니다' });

  if (req.body.alias != null) {
    const alias = req.body.alias.trim();
    if (!alias) return res.status(400).json({ detail: '별칭을 입력해주세요' });
    s.alias = alias;
    db.updateAlias(svid, alias);
  }

  if (req.body.model != null) {
    const model = req.body.model.trim();
    if (!GEMINI_MODELS.includes(model))
      return res.status(400).json({ detail: `지원하지 않는 모델: ${model}` });
    s.model = model;
    db.updateModel(svid, model);
  }

  res.json({ id: svid, alias: s.alias, model: s.model || DEFAULT_GEMINI_MODEL });
});

router.delete('/:svid', async (req, res) => {
  const { svid } = req.params;
  const s = state.servers[svid];
  if (!s) return res.status(404).json({ detail: '서버를 찾을 수 없습니다' });

  delete state.servers[svid];
  db.deleteServer(svid);
  gemini.deleteStore(s.api_key, s.store_name).catch(console.warn);

  res.json({ status: 'deleted' });
});

// ── File upload (local) ───────────────────────────────────────────────────────

router.post('/:svid/upload', upload.single('file'), async (req, res) => {
  const { svid } = req.params;
  const s = state.servers[svid];
  if (!s) return res.status(404).json({ detail: '서버를 찾을 수 없습니다' });
  if (!req.file) return res.status(400).json({ detail: '파일이 없습니다' });

  const displayName = req.file.originalname || 'file';
  const tmpPath = req.file.path;
  const fileSize = req.file.size;

  if (s.files.some(f => f.name === displayName)) {
    fs.unlink(tmpPath, () => {});
    return res.status(409).json({
      detail: `'${displayName}' 파일이 이미 존재합니다. 삭제 후 다시 업로드하거나 파일명을 변경하세요.`,
    });
  }

  const fid = uuidv4().slice(0, 8);
  s.files.push({ id: fid, name: displayName, size: fileSize, status: 'uploading', error: null });
  db.addFile(svid, fid, displayName, fileSize);

  bgUpload(svid, fid, s.api_key, s.store_name, tmpPath, displayName, getMime(displayName))
    .catch(console.error);

  res.json({ file_id: fid, name: displayName, status: 'uploading' });
});

// ── File upload (from Drive) ──────────────────────────────────────────────────

router.post('/:svid/upload-from-drive', async (req, res) => {
  const { svid } = req.params;
  const s = state.servers[svid];
  if (!s) return res.status(404).json({ detail: '서버를 찾을 수 없습니다' });

  const { file_id, file_name, mime_type, client_id = 'default' } = req.body;
  if (!file_id || !file_name) return res.status(400).json({ detail: '파일 정보가 없습니다' });

  const { GDOCS_EXPORT } = driveHelper;
  const exportInfo = GDOCS_EXPORT[mime_type];
  let displayName = exportInfo ? file_name + exportInfo.ext : file_name;

  if (s.files.some(f => f.name === displayName))
    return res.status(409).json({ detail: `'${displayName}' 파일이 이미 존재합니다.` });

  let buffer, ext;
  try {
    ({ buffer, ext } = await driveHelper.downloadFile(client_id, file_id, mime_type));
  } catch (e) {
    const status = e.status || 500;
    return res.status(status).json({ detail: e.message });
  }

  // displayName might be updated if extension was derived from export
  if (ext && !displayName.endsWith(ext)) displayName = file_name + ext;

  if (buffer.length > 100 * 1024 * 1024)
    return res.status(400).json({ detail: '파일 크기는 100MB를 초과할 수 없습니다' });

  const suffix = path.extname(displayName) || '';
  const tmpPath = path.join(os.tmpdir(), `drive_${Date.now()}_${Math.random().toString(36).slice(2)}${suffix}`);
  fs.writeFileSync(tmpPath, buffer);

  const fid = uuidv4().slice(0, 8);
  s.files.push({ id: fid, name: displayName, size: buffer.length, status: 'uploading', error: null });
  db.addFile(svid, fid, displayName, buffer.length);

  bgUpload(svid, fid, s.api_key, s.store_name, tmpPath, displayName, getMime(displayName))
    .catch(console.error);

  res.json({ file_id: fid, name: displayName, status: 'uploading' });
});

// ── File upload (from Notion) ─────────────────────────────────────────────────

router.post('/:svid/upload-from-notion', async (req, res) => {
  const { svid } = req.params;
  const s = state.servers[svid];
  if (!s) return res.status(404).json({ detail: '서버를 찾을 수 없습니다' });
  if (!state.apiKeys.notion)
    return res.status(400).json({ detail: 'NOTION_API_KEY가 설정되지 않았습니다' });

  const { page_id, title, include_subpages = true, include_attachments = true } = req.body;

  let result;
  try {
    result = await notionHelper.notionPageToMd(page_id, state.apiKeys.notion, {
      includeSubpages:    include_subpages,
      includeAttachments: include_attachments,
    });
  } catch (e) {
    return res.status(500).json({ detail: `Notion 변환 실패: ${e.message}` });
  }

  const { title: pageTitle, markdown: md, attachments } = result;
  const existingNames = new Set(s.files.map(f => f.name));

  // Deduplicate filename
  let displayName = notionHelper.safeFilename(title || pageTitle || 'notion-page') + '.md';
  if (existingNames.has(displayName)) {
    const [base, ext] = displayName.split(/\.(?=[^.]+$)/);
    let counter = 1;
    while (existingNames.has(displayName)) {
      displayName = `${base} (${++counter}).${ext}`;
    }
  }

  const content = Buffer.from(md, 'utf-8');
  if (content.length > 100 * 1024 * 1024)
    return res.status(400).json({ detail: '본문이 100MB를 초과합니다' });

  const tmpPath = path.join(os.tmpdir(), `notion_${Date.now()}_${Math.random().toString(36).slice(2)}.md`);
  fs.writeFileSync(tmpPath, content);

  const queued = [];
  const skipped = [];

  const fid = uuidv4().slice(0, 8);
  s.files.push({ id: fid, name: displayName, size: content.length, status: 'uploading', error: null });
  db.addFile(svid, fid, displayName, content.length);
  bgUpload(svid, fid, s.api_key, s.store_name, tmpPath, displayName, 'text/plain').catch(console.error);
  queued.push({ file_id: fid, name: displayName });
  existingNames.add(displayName);

  // Attachments
  for (const att of attachments) {
    let attName = att.filename;
    if (existingNames.has(attName)) {
      const dotIdx = attName.lastIndexOf('.');
      const base = dotIdx > 0 ? attName.slice(0, dotIdx) : attName;
      const ext  = dotIdx > 0 ? attName.slice(dotIdx) : '';
      let i = 1;
      while (existingNames.has(attName)) attName = `${base} (${++i})${ext}`;
    }
    existingNames.add(attName);

    const suffix = path.extname(attName) || '.bin';
    let tmpA, sizeA;
    try {
      [tmpA, sizeA] = await downloadUrlToTemp(att.url, suffix);
    } catch (e) {
      skipped.push({ name: attName, reason: `다운로드 실패: ${e.message}` });
      continue;
    }

    if (sizeA > 100 * 1024 * 1024) {
      try { fs.unlinkSync(tmpA); } catch {}
      skipped.push({ name: attName, reason: '100MB 초과' });
      continue;
    }

    const aFid = uuidv4().slice(0, 8);
    s.files.push({ id: aFid, name: attName, size: sizeA, status: 'uploading', error: null });
    db.addFile(svid, aFid, attName, sizeA);
    bgUpload(svid, aFid, s.api_key, s.store_name, tmpA, attName, getMime(attName)).catch(console.error);
    queued.push({ file_id: aFid, name: attName });
  }

  res.json({
    queued,
    skipped,
    main_file:         displayName,
    attachments_count: attachments.length,
  });
});

// ── File delete ───────────────────────────────────────────────────────────────

router.delete('/:svid/files/:fid', async (req, res) => {
  const { svid, fid } = req.params;
  const s = state.servers[svid];
  if (!s) return res.status(404).json({ detail: '서버를 찾을 수 없습니다' });

  const target = s.files.find(f => f.id === fid);
  if (!target) return res.status(404).json({ detail: '파일을 찾을 수 없습니다' });

  s.files = s.files.filter(f => f.id !== fid);
  db.deleteFile(fid);
  gemini.deleteDocument(s.api_key, s.store_name, fid).catch(console.warn);

  res.json({ status: 'deleted', file_id: fid });
});

// ── Sync ──────────────────────────────────────────────────────────────────────

router.post('/:svid/sync', async (req, res) => {
  const { svid } = req.params;
  const s = state.servers[svid];
  if (!s) return res.status(404).json({ detail: '서버를 찾을 수 없습니다' });

  let docs;
  try {
    docs = await gemini.listDocuments(s.api_key, s.store_name);
  } catch (e) {
    return res.status(500).json({ detail: `동기화 실패: ${e.message}` });
  }

  db.syncFiles(svid, docs);
  s.files = docs;
  res.json({ synced_count: docs.length });
});

// ── Query ─────────────────────────────────────────────────────────────────────

router.post('/:svid/query', async (req, res) => {
  const { svid } = req.params;
  const s = state.servers[svid];
  if (!s) return res.status(404).json({ detail: '서버를 찾을 수 없습니다' });

  const ready = s.files.filter(f => f.status === 'ready');
  if (!ready.length)
    return res.status(400).json({ detail: '준비된 파일이 없습니다. 인덱싱이 완료될 때까지 기다려주세요.' });

  const queryText = (req.body.query || '').trim();
  if (!queryText) return res.status(400).json({ detail: '질문을 입력해주세요' });

  try {
    const result = await gemini.query(
      s.api_key, s.store_name, queryText, s.model || DEFAULT_GEMINI_MODEL
    );
    res.json(result);
  } catch (e) {
    res.status(500).json({ detail: e.message });
  }
});

module.exports = router;
