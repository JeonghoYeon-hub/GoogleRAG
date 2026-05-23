'use strict';

const { DatabaseSync } = require('node:sqlite');
const path = require('path');

const DB_PATH = path.join(process.cwd(), 'sessions.db');
const DEFAULT_MODEL = 'gemini-2.5-flash';

let _db = null;

function getDb() {
  if (!_db) {
    _db = new DatabaseSync(DB_PATH);
    _db.exec('PRAGMA journal_mode = WAL');
    _db.exec('PRAGMA foreign_keys = ON');
  }
  return _db;
}

function initDb() {
  const db = getDb();

  const tables = new Set(
    db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().map(r => r.name)
  );

  if (tables.has('sessions') && !tables.has('servers')) {
    console.log('[DB] 구 스키마 감지 → 마이그레이션 시작');
    db.exec('PRAGMA foreign_keys = OFF');
    db.exec(`
      CREATE TABLE servers (
        id         TEXT PRIMARY KEY,
        alias      TEXT NOT NULL DEFAULT '',
        store_name TEXT NOT NULL,
        api_key    TEXT NOT NULL,
        created_at REAL NOT NULL
      );
      INSERT INTO servers (id, alias, store_name, api_key, created_at)
        SELECT id, display_name, store_name, api_key, created_at FROM sessions;
      CREATE TABLE files_new (
        id         TEXT PRIMARY KEY,
        server_id  TEXT NOT NULL,
        name       TEXT NOT NULL,
        size       INTEGER NOT NULL,
        status     TEXT NOT NULL DEFAULT 'uploading',
        error      TEXT,
        created_at REAL NOT NULL
      );
      INSERT INTO files_new (id, server_id, name, size, status, error, created_at)
        SELECT id, session_id, name, size, status, error, created_at FROM files;
      DROP TABLE files;
      ALTER TABLE files_new RENAME TO files;
      DROP TABLE sessions;
    `);
    db.exec('PRAGMA foreign_keys = ON');
    console.log('[DB] 마이그레이션 완료');
  } else {
    db.exec(`
      CREATE TABLE IF NOT EXISTS servers (
        id         TEXT PRIMARY KEY,
        alias      TEXT NOT NULL DEFAULT '',
        store_name TEXT NOT NULL,
        api_key    TEXT NOT NULL,
        model      TEXT NOT NULL DEFAULT 'gemini-2.5-flash',
        created_at REAL NOT NULL
      );
    `);

    const cols = new Set(
      db.prepare('PRAGMA table_info(servers)').all().map(r => r.name)
    );
    if (!cols.has('model')) {
      db.exec(`ALTER TABLE servers ADD COLUMN model TEXT NOT NULL DEFAULT 'gemini-2.5-flash'`);
      console.log('[DB] servers.model 컬럼 추가 완료');
    }

    db.exec(`
      CREATE TABLE IF NOT EXISTS files (
        id         TEXT PRIMARY KEY,
        server_id  TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
        name       TEXT NOT NULL,
        size       INTEGER NOT NULL,
        status     TEXT NOT NULL DEFAULT 'uploading',
        error      TEXT,
        created_at REAL NOT NULL
      );
      CREATE TABLE IF NOT EXISTS drive_tokens (
        id         TEXT PRIMARY KEY,
        token_json TEXT NOT NULL,
        updated_at REAL NOT NULL
      );
      CREATE TABLE IF NOT EXISTS app_settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
      );
    `);
  }

  const affected = db.prepare(
    "UPDATE files SET status='error', error='서버 재시작으로 중단됨' WHERE status='uploading'"
  ).run().changes;
  if (affected) console.log(`[DB] 중단된 파일 ${affected}개 → error 처리`);
}

function loadServers() {
  const db = getDb();
  const rows = db.prepare('SELECT * FROM servers ORDER BY created_at').all();
  const result = {};
  for (const row of rows) {
    const files = db.prepare('SELECT * FROM files WHERE server_id=? ORDER BY created_at')
      .all(row.id)
      .map(f => ({ id: f.id, name: f.name, size: f.size, status: f.status, error: f.error }));
    result[row.id] = {
      alias:      row.alias,
      store_name: row.store_name,
      api_key:    row.api_key,
      model:      row.model || DEFAULT_MODEL,
      files,
    };
  }
  return result;
}

// ── Servers ──────────────────────────────────────────────────────────────────

function addServer(id, alias, apiKey, storeName, model = DEFAULT_MODEL) {
  getDb().prepare(
    'INSERT INTO servers (id, alias, store_name, api_key, model, created_at) VALUES (?,?,?,?,?,?)'
  ).run(id, alias, storeName, apiKey, model, Date.now() / 1000);
}

function updateAlias(id, alias) {
  getDb().prepare('UPDATE servers SET alias=? WHERE id=?').run(alias, id);
}

function updateModel(id, model) {
  getDb().prepare('UPDATE servers SET model=? WHERE id=?').run(model, id);
}

function deleteServer(id) {
  getDb().prepare('DELETE FROM servers WHERE id=?').run(id);
}

// ── Files ─────────────────────────────────────────────────────────────────────

function addFile(serverId, fileId, name, size) {
  getDb().prepare(
    "INSERT INTO files (id, server_id, name, size, status, created_at) VALUES (?,?,?,?,'uploading',?)"
  ).run(fileId, serverId, name, size, Date.now() / 1000);
}

function updateFile(fileId, status, error = null) {
  getDb().prepare('UPDATE files SET status=?, error=? WHERE id=?').run(status, error, fileId);
}

function deleteFile(fileId) {
  getDb().prepare('DELETE FROM files WHERE id=?').run(fileId);
}

function syncFiles(serverId, docs) {
  const db = getDb();
  const del = db.prepare('DELETE FROM files WHERE server_id=?');
  const ins = db.prepare(
    "INSERT INTO files (id, server_id, name, size, status, created_at) VALUES (?,?,?,?,'ready',?)"
  );
  db.transaction(() => {
    del.run(serverId);
    const now = Date.now() / 1000;
    for (const doc of docs) ins.run(doc.id, serverId, doc.name, doc.size, now);
  })();
}

function insertFilesIfNotExist(serverId, docs) {
  const ins = getDb().prepare(
    "INSERT OR IGNORE INTO files (id, server_id, name, size, status, created_at) VALUES (?,?,?,?,'ready',?)"
  );
  getDb().transaction(() => {
    const now = Date.now() / 1000;
    for (const doc of docs) ins.run(doc.id, serverId, doc.name, doc.size, now);
  })();
}

// ── Drive tokens ──────────────────────────────────────────────────────────────

function getDriveToken(clientId) {
  const row = getDb().prepare('SELECT token_json FROM drive_tokens WHERE id=?').get(clientId);
  return row ? row.token_json : null;
}

function saveDriveToken(clientId, tokenJson) {
  getDb().prepare(
    'INSERT OR REPLACE INTO drive_tokens (id, token_json, updated_at) VALUES (?,?,?)'
  ).run(clientId, tokenJson, Date.now() / 1000);
}

function clearDriveToken(clientId) {
  getDb().prepare('DELETE FROM drive_tokens WHERE id=?').run(clientId);
}

// ── Settings ──────────────────────────────────────────────────────────────────

function getSetting(key) {
  const row = getDb().prepare('SELECT value FROM app_settings WHERE key=?').get(key);
  return row ? row.value : '';
}

function saveSetting(key, value) {
  getDb().prepare('INSERT OR REPLACE INTO app_settings (key, value) VALUES (?,?)').run(key, value);
}

function deleteSetting(key) {
  getDb().prepare('DELETE FROM app_settings WHERE key=?').run(key);
}

module.exports = {
  initDb, loadServers,
  addServer, updateAlias, updateModel, deleteServer,
  addFile, updateFile, deleteFile, syncFiles, insertFilesIfNotExist,
  getDriveToken, saveDriveToken, clearDriveToken,
  getSetting, saveSetting, deleteSetting,
};
