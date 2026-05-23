'use strict';

const express = require('express');
const router = express.Router();
const db = require('../db');
const state = require('../state');
const drive = require('../drive');

const GEMINI_MODELS = [
  'gemini-3.1-pro-preview',
  'gemini-3-flash-preview',
  'gemini-3.1-flash-lite-preview',
  'gemini-2.5-flash',
];
const DEFAULT_GEMINI_MODEL = 'gemini-2.5-flash';

function maskKey(key) {
  if (!key) return '';
  return key.length > 7 ? key.slice(0, 4) + '***' + key.slice(-3) : '****';
}

router.get('/config', (req, res) => {
  res.json({
    has_server_api_key:  Boolean(state.apiKeys.gemini),
    has_drive_oauth:     drive.hasClientSecret(),
    has_notion:          Boolean(state.apiKeys.notion),
    gemini_models:       GEMINI_MODELS,
    default_gemini_model: DEFAULT_GEMINI_MODEL,
  });
});

router.get('/settings', async (req, res) => {
  const clientId = req.query.client_id || 'default';
  const tokens = await drive.getValidTokens(clientId);
  res.json({
    gemini_key:           maskKey(state.apiKeys.gemini),
    notion_key:           maskKey(state.apiKeys.notion),
    drive_authenticated:  tokens !== null,
    drive_available:      drive.hasClientSecret(),
  });
});

router.post('/settings/gemini', (req, res) => {
  const key = (req.body.key || '').trim();
  if (!key) return res.status(400).json({ detail: '키를 입력해주세요' });
  state.apiKeys.gemini = key;
  db.saveSetting('gemini_key', key);
  res.json({ ok: true });
});

router.delete('/settings/gemini', (req, res) => {
  db.deleteSetting('gemini_key');
  state.apiKeys.gemini = process.env.GOOGLE_API_KEY || '';
  res.json({ ok: true });
});

router.post('/settings/notion', (req, res) => {
  const key = (req.body.key || '').trim();
  if (!key) return res.status(400).json({ detail: '키를 입력해주세요' });
  state.apiKeys.notion = key;
  db.saveSetting('notion_key', key);
  res.json({ ok: true });
});

router.delete('/settings/notion', (req, res) => {
  db.deleteSetting('notion_key');
  state.apiKeys.notion = process.env.NOTION_API_KEY || '';
  res.json({ ok: true });
});

module.exports = router;
module.exports.GEMINI_MODELS = GEMINI_MODELS;
module.exports.DEFAULT_GEMINI_MODEL = DEFAULT_GEMINI_MODEL;
