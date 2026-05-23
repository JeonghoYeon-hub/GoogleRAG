'use strict';

const express = require('express');
const router = express.Router();
const state = require('../state');
const notion = require('../notion');
const { Client } = require('@notionhq/client');

router.get('/status', async (req, res) => {
  if (!state.apiKeys.notion) return res.json({ configured: false, ok: false });
  try {
    const client = new Client({ auth: state.apiKeys.notion });
    await client.users.me({});
    res.json({ configured: true, ok: true });
  } catch (e) {
    res.json({ configured: true, ok: false, error: String(e.message || e).slice(0, 200) });
  }
});

router.get('/pages', async (req, res) => {
  if (!state.apiKeys.notion) {
    return res.status(400).json({ detail: 'NOTION_API_KEY가 설정되지 않았습니다' });
  }
  try {
    const items = await notion.searchPages(state.apiKeys.notion, req.query.q || '');
    res.json({ items, pages: items });
  } catch (e) {
    res.status(400).json({ detail: `Notion API 오류: ${e.message}` });
  }
});

router.get('/children', async (req, res) => {
  if (!state.apiKeys.notion) {
    return res.status(400).json({ detail: 'NOTION_API_KEY가 설정되지 않았습니다' });
  }
  try {
    const items = await notion.getChildren(
      state.apiKeys.notion, req.query.id, req.query.type || 'page'
    );
    res.json({ items });
  } catch (e) {
    const status = e.status || 500;
    res.status(status).json({ detail: `Notion children 조회 실패: ${e.message}` });
  }
});

module.exports = router;
