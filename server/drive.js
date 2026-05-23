'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { google } = require('googleapis');
const db = require('./db');

const DRIVE_SCOPES = ['https://www.googleapis.com/auth/drive.readonly'];

// In-memory store for PKCE state: state -> { codeVerifier, clientId }
const oauthStates = new Map();

const GDOCS_EXPORT = {
  'application/vnd.google-apps.document': {
    mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    ext: '.docx',
  },
  'application/vnd.google-apps.spreadsheet': {
    mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    ext: '.xlsx',
  },
  'application/vnd.google-apps.presentation': {
    mimeType: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    ext: '.pptx',
  },
};

function loadClientSecret() {
  const baseDir = process.env.APP_DATA_DIR || process.cwd();
  const envFile = process.env.GOOGLE_CLIENT_SECRET_FILE || '';
  const filePath =
    envFile && fs.existsSync(envFile) ? envFile
    : fs.existsSync(path.join(baseDir, 'client_secret.json'))
      ? path.join(baseDir, 'client_secret.json')
      : null;
  if (!filePath) return null;
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf-8'));
  } catch {
    return null;
  }
}

function hasClientSecret() {
  return loadClientSecret() !== null;
}

// ── PKCE helpers ──────────────────────────────────────────────────────────────

function generateCodeVerifier() {
  return crypto.randomBytes(32).toString('base64url');
}

function generateCodeChallenge(verifier) {
  return crypto.createHash('sha256').update(verifier).digest('base64url');
}

function generateState() {
  return crypto.randomBytes(16).toString('hex');
}

// ── Token management ──────────────────────────────────────────────────────────

async function getValidTokens(clientId) {
  const tokenJson = db.getDriveToken(clientId);
  if (!tokenJson) return null;

  let tokens;
  try { tokens = JSON.parse(tokenJson); } catch { return null; }

  // Refresh if expired (with 60s buffer)
  if (tokens.expiry_date && tokens.expiry_date < Date.now() + 60_000) {
    if (!tokens.refresh_token) return null;
    const secret = loadClientSecret();
    if (!secret) return null;

    try {
      const res = await fetch('https://oauth2.googleapis.com/token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          refresh_token: tokens.refresh_token,
          client_id:     secret.web.client_id,
          client_secret: secret.web.client_secret,
          grant_type:    'refresh_token',
        }),
      });
      if (!res.ok) return null;
      const refreshed = await res.json();
      tokens = {
        ...tokens,
        access_token: refreshed.access_token,
        expiry_date:  Date.now() + refreshed.expires_in * 1000,
      };
      if (refreshed.refresh_token) tokens.refresh_token = refreshed.refresh_token;
      db.saveDriveToken(clientId, JSON.stringify(tokens));
    } catch {
      return null;
    }
  }

  return tokens;
}

function buildDriveService(tokens) {
  const secret = loadClientSecret();
  if (!secret) throw new Error('client_secret.json이 없습니다');
  const redirectUri = process.env.OAUTH_REDIRECT_URI || 'http://localhost:3000/auth/callback';
  const auth = new google.auth.OAuth2(
    secret.web.client_id,
    secret.web.client_secret,
    redirectUri
  );
  auth.setCredentials(tokens);
  return google.drive({ version: 'v3', auth });
}

// ── Auth flow ─────────────────────────────────────────────────────────────────

function buildAuthUrl(clientId) {
  const secret = loadClientSecret();
  if (!secret) throw new Error('client_secret.json이 없습니다');

  const redirectUri = process.env.OAUTH_REDIRECT_URI || 'http://localhost:3000/auth/callback';
  const state = generateState();
  const codeVerifier = generateCodeVerifier();
  const codeChallenge = generateCodeChallenge(codeVerifier);

  oauthStates.set(state, { codeVerifier, clientId });

  const params = new URLSearchParams({
    client_id:             secret.web.client_id,
    redirect_uri:          redirectUri,
    response_type:         'code',
    scope:                 DRIVE_SCOPES.join(' '),
    state,
    access_type:           'offline',
    prompt:                'consent',
    code_challenge:        codeChallenge,
    code_challenge_method: 'S256',
  });

  return `https://accounts.google.com/o/oauth2/v2/auth?${params}`;
}

async function handleCallback(code, state) {
  const stateData = oauthStates.get(state);
  if (!stateData) throw new Error('유효하지 않은 OAuth state입니다');
  oauthStates.delete(state);

  const { codeVerifier, clientId } = stateData;
  const secret = loadClientSecret();
  if (!secret) throw new Error('client_secret.json이 없습니다');

  const redirectUri = process.env.OAUTH_REDIRECT_URI || 'http://localhost:3000/auth/callback';

  const res = await fetch('https://oauth2.googleapis.com/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      code,
      client_id:     secret.web.client_id,
      client_secret: secret.web.client_secret,
      redirect_uri:  redirectUri,
      grant_type:    'authorization_code',
      code_verifier: codeVerifier,
    }),
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`토큰 교환 실패: ${text}`);
  }

  const tokens = await res.json();
  tokens.expiry_date = Date.now() + tokens.expires_in * 1000;
  db.saveDriveToken(clientId, JSON.stringify(tokens));
  return clientId;
}

// ── Drive file operations ─────────────────────────────────────────────────────

async function listFiles(clientId, parent = 'root', q = '', pageToken = '') {
  const tokens = await getValidTokens(clientId);
  if (!tokens) throw Object.assign(new Error('Google Drive 인증 필요'), { status: 401 });

  const drive = buildDriveService(tokens);
  const parts = [`'${parent}' in parents`, 'trashed=false'];
  if (q) parts.push(`name contains '${q}'`);

  const params = {
    q:         parts.join(' and '),
    pageSize:  50,
    fields:    'nextPageToken,files(id,name,mimeType,size,modifiedTime)',
    orderBy:   'folder,name',
  };
  if (pageToken) params.pageToken = pageToken;

  const res = await drive.files.list(params);
  return {
    files:           res.data.files || [],
    next_page_token: res.data.nextPageToken || '',
  };
}

async function downloadFile(clientId, fileId, mimeType) {
  const tokens = await getValidTokens(clientId);
  if (!tokens) throw Object.assign(new Error('Google Drive 인증 필요'), { status: 401 });

  const drive = buildDriveService(tokens);
  const exportInfo = GDOCS_EXPORT[mimeType];

  let exportMime = null;
  let ext = '';
  if (exportInfo) {
    exportMime = exportInfo.mimeType;
    ext = exportInfo.ext;
  }

  const reqParams = exportMime
    ? drive.files.export({ fileId, mimeType: exportMime }, { responseType: 'arraybuffer' })
    : drive.files.get({ fileId, alt: 'media' },           { responseType: 'arraybuffer' });

  const response = await reqParams;
  return { buffer: Buffer.from(response.data), ext };
}

module.exports = {
  hasClientSecret, getValidTokens,
  buildAuthUrl, handleCallback,
  listFiles, downloadFile,
  GDOCS_EXPORT,
};
