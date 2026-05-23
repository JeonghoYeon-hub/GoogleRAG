'use strict';

const express = require('express');
const router = express.Router();
const db = require('../db');
const drive = require('../drive');

// ── Drive auth routes ─────────────────────────────────────────────────────────

router.get('/status', async (req, res) => {
  const clientId = req.query.client_id || 'default';
  const tokens = await drive.getValidTokens(clientId);
  res.json({ authenticated: tokens !== null });
});

router.get('/auth-url', (req, res) => {
  const clientId = req.query.client_id || 'default';
  if (!drive.hasClientSecret()) {
    return res.status(400).json({ detail: 'client_secret.json 파일이 없습니다' });
  }
  try {
    const authUrl = drive.buildAuthUrl(clientId);
    res.json({ auth_url: authUrl });
  } catch (e) {
    res.status(400).json({ detail: e.message });
  }
});

router.delete('/auth', (req, res) => {
  const clientId = req.query.client_id || 'default';
  db.clearDriveToken(clientId);
  res.json({ status: 'disconnected' });
});

router.get('/files', async (req, res) => {
  const clientId  = req.query.client_id  || 'default';
  const parent    = req.query.parent     || 'root';
  const q         = req.query.q          || '';
  const pageToken = req.query.page_token || '';
  try {
    const result = await drive.listFiles(clientId, parent, q, pageToken);
    res.json(result);
  } catch (e) {
    const status = e.status || 500;
    res.status(status).json({ detail: e.message });
  }
});

module.exports = router;
