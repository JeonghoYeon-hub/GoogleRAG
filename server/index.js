'use strict';

require('dotenv').config();
require('express-async-errors');

const express = require('express');
const path = require('path');
const db = require('./db');
const state = require('./state');
const drive = require('./drive');

// ── Startup ───────────────────────────────────────────────────────────────────

db.initDb();

// Load persisted API keys if not in env
if (!state.apiKeys.gemini) state.apiKeys.gemini = db.getSetting('gemini_key');
if (!state.apiKeys.notion) state.apiKeys.notion = db.getSetting('notion_key');

// Restore server state from DB
const loaded = db.loadServers();
Object.assign(state.servers, loaded);
if (Object.keys(loaded).length) {
  console.log(`[DB] 서버 ${Object.keys(loaded).length}개 복원 완료`);
}

// ── Express app ───────────────────────────────────────────────────────────────

const app = express();

app.use(express.json());
app.use(express.urlencoded({ extended: false }));

// ── Routes ────────────────────────────────────────────────────────────────────

app.use('/api', require('./routes/config'));
app.use('/api/servers', require('./routes/servers'));
app.use('/api/drive', require('./routes/drive'));
app.use('/api/notion', require('./routes/notion'));

// Google OAuth callback (must match OAUTH_REDIRECT_URI)
app.get('/auth/callback', async (req, res) => {
  const { code, state: oauthState } = req.query;
  if (!code || !oauthState) {
    return res.status(400).send('<p>오류: 잘못된 요청</p>');
  }
  try {
    await drive.handleCallback(code, oauthState);
    res.send(`<!DOCTYPE html>
<html><body style="font-family:sans-serif;background:#0f172a;color:#fff;display:flex;
  align-items:center;justify-content:center;height:100vh;margin:0;flex-direction:column">
<div style="font-size:3rem">✓</div>
<h2 style="margin:.5rem 0">Google Drive 인증 완료</h2>
<p style="color:#94a3b8">이 창을 닫으세요.</p>
<script>window.opener&&window.opener.postMessage('drive_auth_ok','*');setTimeout(()=>window.close(),1500);</script>
</body></html>`);
  } catch (e) {
    res.status(400).send(`<p>인증 실패: ${e.message}</p>`);
  }
});

// Static files — STATIC_DIR is set by main.js when running inside Electron
// (asar.unpacked path), otherwise falls back to the sibling static/ directory.
const staticDir = process.env.STATIC_DIR || path.join(__dirname, '..', 'static');
app.use('/static', express.static(staticDir));

app.get('/', (_req, res) => {
  res.sendFile(path.join(staticDir, 'index.html'));
});

// ── Error handler ─────────────────────────────────────────────────────────────

app.use((err, _req, res, _next) => {
  if (err.code === 'LIMIT_FILE_SIZE') {
    return res.status(400).json({ detail: '파일 크기는 100MB를 초과할 수 없습니다' });
  }
  console.error('[Error]', err.message || err);
  const status = err.status || err.statusCode || 500;
  res.status(status).json({ detail: err.message || '서버 오류가 발생했습니다' });
});

// ── Start ─────────────────────────────────────────────────────────────────────

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`MYRAG 서버 실행 중 → http://localhost:${PORT}`);
});

module.exports = app;
