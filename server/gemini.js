'use strict';

// All Gemini operations use the REST API directly (v1beta) via built-in fetch.
// This avoids SDK version coupling and is more reliable for Electron packaging.

const fs = require('fs');

const BASE = 'https://generativelanguage.googleapis.com';

const STRICT_SYSTEM_INSTRUCTION = `당신은 사용자가 업로드한 문서만을 근거로 답변하는 검색 도우미입니다.

엄격한 규칙:
1. 반드시 file_search 도구를 사용해 업로드된 문서에서 정보를 검색한 뒤 답변하세요.
2. 검색 결과에 포함된 내용만 인용하여 답변하세요. 일반 지식, 추측, 외부 정보는 절대 사용하지 마세요.
3. 검색 결과에 관련 정보가 없으면 정확히 다음 문장으로 답하세요:
   "업로드된 문서에서 관련 정보를 찾을 수 없습니다."
4. 부분적으로만 답할 수 있으면, 어떤 부분이 문서에 있고 어떤 부분이 없는지 명시하세요. 없는 부분은 추측하지 마세요.
5. 인용한 내용에는 어떤 문서의 어느 부분에서 가져왔는지 가능한 한 표시하세요.
6. 사용자가 일반 지식 질문(예: "프랑스의 수도는?")을 해도, 그것이 업로드된 문서에 없다면 위 3번 문장으로 답하세요.`;

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

async function apiGet(apiKey, path) {
  const res = await fetch(`${BASE}/v1beta/${path}?key=${apiKey}`);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Gemini API 오류 (${res.status}): ${text}`);
  }
  return res.json();
}

async function apiPost(apiKey, path, body) {
  const res = await fetch(`${BASE}/v1beta/${path}?key=${apiKey}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Gemini API 오류 (${res.status}): ${text}`);
  }
  return res.json();
}

async function apiDelete(apiKey, path) {
  const res = await fetch(`${BASE}/v1beta/${path}?key=${apiKey}`, { method: 'DELETE' });
  if (!res.ok && res.status !== 404) {
    const text = await res.text();
    throw new Error(`Gemini API 오류 (${res.status}): ${text}`);
  }
}

async function pollOperation(apiKey, opName) {
  let op = { done: false };
  while (!op.done) {
    await sleep(3000);
    op = await apiGet(apiKey, opName);
  }
  if (op.error) throw new Error(`작업 실패: ${JSON.stringify(op.error)}`);
}

// ── File Search Store operations ──────────────────────────────────────────────

async function createStore(apiKey, displayName) {
  const data = await apiPost(apiKey, 'fileSearchStores', { displayName });
  return data.name;  // e.g. "fileSearchStores/xxx"
}

async function deleteStore(apiKey, storeName) {
  try {
    await apiDelete(apiKey, storeName);
  } catch (e) {
    console.warn('[deleteStore]', e.message);
  }
}

async function listDocuments(apiKey, storeName) {
  const docs = [];
  let pageToken = '';
  do {
    const url = `${BASE}/v1beta/${storeName}/documents?key=${apiKey}` +
      (pageToken ? `&pageToken=${pageToken}` : '');
    const res = await fetch(url);
    if (!res.ok) break;
    const data = await res.json();
    for (const doc of data.documents || []) {
      docs.push({
        id:     doc.name.split('/').pop(),
        name:   doc.displayName,
        size:   doc.sizeBytes || 0,
        status: 'ready',
        error:  null,
      });
    }
    pageToken = data.nextPageToken || '';
  } while (pageToken);
  return docs;
}

async function deleteDocument(apiKey, storeName, docId) {
  try {
    await apiDelete(apiKey, `${storeName}/documents/${docId}`);
  } catch (e) {
    console.warn('[deleteDocument]', e.message);
  }
}

async function uploadToStore(apiKey, storeName, displayName, filePath, mimeType) {
  const fileData = fs.readFileSync(filePath);

  // Step 1: initiate resumable upload session (JSON metadata only, no bytes)
  const initRes = await fetch(
    `${BASE}/upload/v1beta/${storeName}:uploadToFileSearchStore`,
    {
      method: 'POST',
      headers: {
        'x-goog-api-key':                     apiKey,
        'Content-Type':                       'application/json',
        'X-Goog-Upload-Protocol':             'resumable',
        'X-Goog-Upload-Command':              'start',
        'X-Goog-Upload-Header-Content-Length': String(fileData.length),
        'X-Goog-Upload-Header-Content-Type':  mimeType,
      },
      // mimeType intentionally omitted from JSON body — Gemini's validator rejects long
      // compound types like application/vnd.openxmlformats-... even though they are valid.
      // The actual content type is conveyed via the X-Goog-Upload-Header-Content-Type header.
      body: JSON.stringify({ displayName }),
    }
  );

  if (!initRes.ok) {
    const text = await initRes.text();
    throw new Error(`업로드 세션 시작 실패 (${initRes.status}): ${text}`);
  }

  const uploadUrl = initRes.headers.get('x-goog-upload-url') || initRes.headers.get('X-Goog-Upload-URL');
  if (!uploadUrl) throw new Error('업로드 세션 URL이 응답에 없습니다');

  // Step 2: upload bytes in a single chunk
  const uploadRes = await fetch(uploadUrl, {
    method: 'POST',
    headers: {
      'Content-Length':         String(fileData.length),
      'X-Goog-Upload-Command':  'upload, finalize',
      'X-Goog-Upload-Offset':   '0',
    },
    body: fileData,
  });

  if (!uploadRes.ok) {
    const text = await uploadRes.text();
    throw new Error(`업로드 실패 (${uploadRes.status}): ${text}`);
  }

  const op = await uploadRes.json();
  if (op.name && !op.done) {
    await pollOperation(apiKey, op.name);
  }
}

// ── Query ─────────────────────────────────────────────────────────────────────

async function query(apiKey, storeName, queryText, model) {
  const data = await apiPost(apiKey, `models/${model}:generateContent`, {
    systemInstruction: { parts: [{ text: STRICT_SYSTEM_INSTRUCTION }] },
    contents: [{ role: 'user', parts: [{ text: queryText }] }],
    tools: [{ fileSearch: { fileSearchStoreNames: [storeName] } }],
  });

  const text = (data.candidates?.[0]?.content?.parts || [])
    .map(p => p.text || '')
    .join('');

  const citations = [];
  for (const c of data.candidates || []) {
    for (const chunk of c.groundingMetadata?.groundingChunks || []) {
      const rc = chunk.retrievedContext;
      if (rc) citations.push({ title: rc.title || '', uri: rc.uri || '' });
    }
  }

  return { text, citations };
}

module.exports = {
  createStore, deleteStore, listDocuments, deleteDocument,
  uploadToStore, query,
};
