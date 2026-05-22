# 개발자 문서 (DEVELOPER.md)

파일 RAG 검색 시스템 내부 구조 및 유지보수 가이드입니다.

---

## 아키텍처 개요
m
```
브라우저 (index.html)
    │  Fetch / EventSource
    ▼
FastAPI (app.py)  ──────────────────────────────────────────
    │                                                        │
    ├── SQLite (sessions.db)                                 │
    │   ├── servers          파일스토어 목록                  │
    │   ├── files            파일 메타데이터                  │
    │   ├── drive_tokens     브라우저별 Drive OAuth 토큰      │
    │   └── app_settings     Gemini/Notion 키 저장            │
    │                                                        │
    ├── Google Gemini File Search API (v1beta)               │
    │   fileStores / files / generateContent                 │
    │                                                        │
    ├── Google Drive API v3 (OAuth2 PKCE)                    │
    │                                                        │
    └── Notion API v1                                        │
                                                            │
외부: Gemini 서버에 실제 파일 바이트 저장됨 ───────────────────
```

- **단일 파일 서버**: `app.py` 하나에 모든 백엔드 로직이 있습니다.
- **단일 페이지 프론트**: `static/index.html` 하나에 Vanilla JS + TailwindCSS CDN.
- **인증 모델**: localhost 접속 = 수퍼유저. 별도 사용자 구분 없음.
- **파일 저장**: 실제 파일 바이트는 Gemini 서버에 저장. 로컬 DB에는 메타데이터(gemini_file_uri 등)만 보관.

---

## 개발 환경 구성

```bash
# 1. 저장소 클론 후 가상환경 생성
python -m venv venv
venv\Scripts\activate      # Windows
# source venv/bin/activate  # macOS/Linux

# 2. 패키지 설치
pip install -r requirements.txt

# 3. 환경변수 (선택 — UI에서도 입력 가능)
copy .env.example .env
# .env 파일에 GOOGLE_API_KEY, NOTION_API_KEY 편집

# 4. 서버 실행
python app.py
# → http://localhost:8000
```

### 핫 리로드 (개발 시)

```bash
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

`--reload`를 쓰면 `app.py` 수정 시 자동 재시작. `index.html`은 항상 즉시 반영.

---

## 프로젝트 구조

```
google file search api/
├── app.py                  # FastAPI 백엔드 (전체 로직)
├── static/
│   └── index.html          # 프론트엔드 SPA
├── requirements.txt
├── .env.example
├── client_secret.json      # Google OAuth 자격증명 (gitignore)
├── sessions.db             # SQLite DB (gitignore)
├── 시작.bat                 # Windows 원클릭 실행
├── README.md               # 사용자 매뉴얼
└── DEVELOPER.md            # 이 파일
```

---

## DB 스키마

```sql
-- 파일스토어 (Gemini File Search Store에 대응)
CREATE TABLE servers (
    id          TEXT PRIMARY KEY,   -- Gemini fileStore name (e.g. "filestores/abc123")
    name        TEXT NOT NULL,      -- 사용자 지정 이름
    api_key     TEXT NOT NULL,      -- 이 스토어를 만든 Gemini API 키
    created_at  TEXT NOT NULL
);

-- 업로드된 파일 메타데이터
CREATE TABLE files (
    id              TEXT PRIMARY KEY,   -- 로컬 UUID
    server_id       TEXT NOT NULL,      -- servers.id 참조
    name            TEXT NOT NULL,      -- 파일명
    size            INTEGER,
    mime_type       TEXT,
    status          TEXT DEFAULT 'uploading',   -- uploading | ready | error
    gemini_file_uri TEXT,               -- Gemini files/* URI
    created_at      TEXT NOT NULL,
    FOREIGN KEY (server_id) REFERENCES servers(id)
);

-- 브라우저별 Google Drive OAuth 토큰
CREATE TABLE drive_tokens (
    client_id   TEXT PRIMARY KEY,   -- 브라우저 UUID (localStorage.clientId)
    token_json  TEXT NOT NULL       -- google.oauth2.credentials.Credentials JSON
);

-- 앱 전역 설정 (키-값 쌍)
CREATE TABLE app_settings (
    key     TEXT PRIMARY KEY,   -- 'gemini_key' | 'notion_key'
    value   TEXT NOT NULL
);
```

---

## API 엔드포인트 레퍼런스

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/api/config` | 서버 키 설정 여부 반환 |
| GET | `/api/settings?client_id=` | Gemini/Notion 키 마스킹 + Drive 연결 상태 반환 |
| POST | `/api/settings/gemini` | Gemini API 키 저장 |
| DELETE | `/api/settings/gemini` | Gemini API 키 삭제 |
| POST | `/api/settings/notion` | Notion API 키 저장 |
| DELETE | `/api/settings/notion` | Notion API 키 삭제 |
| GET | `/api/servers` | 서버 목록 조회 |
| POST | `/api/servers` | 새 서버(파일스토어) 생성 |
| DELETE | `/api/servers/{id}` | 서버 및 모든 파일 삭제 |
| GET | `/api/servers/{id}/sync` | Gemini 원격 상태와 동기화 |
| GET | `/api/servers/{id}/files` | 파일 목록 조회 |
| POST | `/api/servers/{id}/upload` | 로컬 파일 업로드 (multipart) |
| DELETE | `/api/files/{id}` | 파일 삭제 |
| POST | `/api/query` | RAG 질의응답 |
| GET | `/api/drive/auth-url?client_id=` | Drive OAuth URL 발급 |
| GET | `/auth/callback` | Drive OAuth 콜백 처리 |
| DELETE | `/api/drive/token?client_id=` | Drive 연결 해제 |
| GET | `/api/drive/files?client_id=&q=` | Drive 파일 검색 |
| POST | `/api/drive/upload/{server_id}` | Drive 파일 → 서버 업로드 |
| GET | `/api/notion/pages?q=` | Notion 페이지 목록/검색 |
| GET | `/api/notion/children?page_id=` | Notion 하위 페이지 조회 |
| POST | `/api/notion/upload/{server_id}` | Notion 페이지 → 서버 업로드 |
| GET | `/` | index.html 서빙 |

---

## 프론트엔드 상태 객체

`index.html` 내 전역 상태는 세 객체로 관리됩니다.

### `S` — 앱 전역 상태

```javascript
const S = {
    servers: [],            // 서버 목록 (API 응답)
    currentServer: null,    // 선택된 서버 객체
    files: [],              // 현재 서버의 파일 목록
    hasServerKey: false,    // Gemini 키 등록 여부
    hasDriveOAuth: false,   // client_secret.json 존재 여부
};
```

### `DS` — Google Drive 모달 상태

```javascript
const DS = {
    items: [],          // 검색 결과 파일 목록
    selected: new Set(),// 선택된 파일 ID Set
    searching: false,
    uploading: false,
};
```

### `NS` — Notion 모달 상태

```javascript
const NS = {
    configured: false,
    rootItems: [],              // 루트 페이지/DB 목록
    childrenMap: new Map(),     // id → children[]
    titleMap: new Map(),        // id → 제목
    typeMap: new Map(),         // id → 'page' | 'database'
    expanded: new Set(),        // 펼쳐진 항목 ID
    loading: new Set(),         // 로딩 중인 항목 ID
    selected: new Set(),        // 선택된 페이지 ID
    selectedDbs: new Set(),     // 전체선택된 DB ID (자식은 업로드 시 동적 조회)
    searchTimer: null,
};
```

---

## 주요 데이터 흐름

### 파일 업로드 흐름

```
사용자 드래그/선택
    → POST /api/servers/{id}/upload (multipart)
    → backend: Google Gemini files.create() 호출
    → DB files 테이블에 status='uploading' 저장
    → 폴링: GET /api/servers/{id}/files (2초 간격)
    → Gemini 처리 완료 시 status='ready'
```

### RAG 질의 흐름

```
사용자 질문 입력
    → POST /api/query {server_id, question}
    → backend: DB에서 server.api_key 조회
    → Gemini generateContent() with fileData 참조
    → 스트리밍 응답 or 단일 응답
    → 프론트: 마크다운 렌더링
```

> **중요**: 질의에는 항상 서버 생성 시 등록된 API 키를 사용합니다. 접속자의 키를 사용하지 않습니다.

### Google Drive OAuth 흐름 (PKCE)

```
사용자 "Google 계정 연결" 클릭
    → GET /api/drive/auth-url?client_id={브라우저UUID}
    → backend: Flow.from_client_secrets_file() + PKCE code_verifier 생성
    → _oauth_states[state] = {code_verifier, client_id} 메모리 저장
    → 팝업 창으로 Google 로그인 페이지 열기
    → 사용자 로그인 → /auth/callback?state=&code= 호출
    → backend: state로 code_verifier, client_id 조회
    → Flow.fetch_token() → drive_tokens 테이블에 저장
    → 팝업 닫힘 (window.close())
    → 부모 창: 폴링으로 연결 감지
```

---

## 설계 결정 사항

| 결정 | 이유 |
|------|------|
| 파일 바이트를 로컬에 저장하지 않음 | Gemini File Search API가 자체 스토리지 제공; 로컬 디스크 낭비 없음 |
| 단일 `app.py` | 소규모 팀 유지보수 편의; 파일 분리 시 import 복잡도 증가 대비 이득 없음 |
| `sessions.db` SQLite | 별도 DB 서버 없이 파일 하나로 영속성 확보 |
| 브라우저 UUID로 Drive 토큰 분리 | 동일 서버에 여러 명이 각자 Drive 연결 가능; 세션 쿠키 없이 구현 |
| `.env` vs UI 입력 병용 | `.env`가 있으면 우선 적용(개발자 편의), 없으면 UI 입력(일반 사용자 편의) |
| 접속자 키 미사용 | 파일스토어는 생성자 키에 종속됨; 다른 키로는 파일 접근 불가 |
| localhost = 수퍼유저 | 내부 전용 도구; 인증 레이어 추가 시 복잡도 대비 보안 이득 미미 |

---

## 알려진 제한사항

| 항목 | 내용 |
|------|------|
| Gemini 파일 만료 | Gemini Files API 업로드 파일은 **48시간 후 자동 삭제**. 장기 보관 불가. FileSearch Store 파일은 별도 정책 적용 (공식 문서 확인 필요). |
| 동시 업로드 | 대량 파일 동시 업로드 시 Gemini API rate limit 발생 가능. |
| Notion 속도 제한 | Notion API는 초당 3회 제한. 대규모 DB 업로드 시 느릴 수 있음. |
| OAuth 상태 메모리 저장 | `_oauth_states`가 프로세스 메모리에만 존재. 서버 재시작 시 진행 중인 OAuth 플로우 무효화. |
| 단일 Gemini 키 | 서버당 하나의 Gemini 키. 멀티 테넌트 확장 시 키 관리 구조 변경 필요. |
| 인터넷 의존 | 모든 AI 기능이 Gemini API 호출에 의존. 오프라인 동작 불가. |

---

## 확장 가이드

### 새로운 파일 소스 추가 (예: SharePoint)

1. `app.py`에 새 라우터 그룹 추가:
   ```python
   @app.get("/api/sharepoint/files")
   async def sharepoint_files(...): ...

   @app.post("/api/sharepoint/upload/{server_id}")
   async def sharepoint_upload(...): ...
   ```
2. `index.html`에 모달 추가 (Notion 모달 구조 참고).
3. 필요 시 `app_settings`에 새 키 타입 추가.

### 사용자 인증 추가

현재 localhost 단일 신뢰 모델에서 다중 사용자로 전환 시:

1. FastAPI `Depends`로 JWT/세션 미들웨어 추가.
2. `servers`, `files` 테이블에 `owner_id` 컬럼 추가.
3. `drive_tokens`는 이미 `client_id`로 분리되어 있어 변경 최소화.
4. 프론트에서 로그인 페이지 추가.

### Docker 배포

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["python", "app.py"]
```

```bash
docker build -t file-rag .
docker run -d \
  -p 8000:8000 \
  -v $(pwd)/sessions.db:/app/sessions.db \
  -v $(pwd)/client_secret.json:/app/client_secret.json \
  -e GOOGLE_API_KEY=AIza... \
  file-rag
```

> `sessions.db`를 볼륨 마운트해야 재시작 시 데이터가 유지됩니다.

---

## 주요 의존성

| 패키지 | 용도 |
|--------|------|
| `fastapi` | HTTP 서버 프레임워크 |
| `uvicorn` | ASGI 서버 |
| `google-generativeai` | Gemini API 클라이언트 |
| `google-api-python-client` | Drive API 클라이언트 |
| `google-auth-oauthlib` | OAuth2 PKCE 플로우 |
| `notion-client` | Notion API 클라이언트 |
| `python-docx` | Word 파일 텍스트 추출 |
| `openpyxl` | Excel 파일 텍스트 추출 |
| `python-pptx` | PowerPoint 텍스트 추출 |
| `pdfminer.six` | PDF 텍스트 추출 |
| `python-multipart` | 파일 업로드 파싱 |

버전 고정은 `requirements.txt` 참조.

---

## 개발 시 주의사항

- **`client_secret.json`은 반드시 "웹 애플리케이션" 유형**으로 발급. "데스크톱 앱" 유형은 리디렉션 URI 방식이 달라 OAuth 콜백이 동작하지 않음.
- **Google Cloud Console에서 승인된 리디렉션 URI**에 `http://localhost:8000/auth/callback` 추가 필수.
- **`_oauth_states`는 인메모리**이므로 프로덕션 다중 인스턴스 환경에서는 Redis 등 외부 저장소로 교체 필요.
- **Python 파일 인코딩**: Windows에서 `app.py` 실행 시 기본 인코딩이 `cp949`일 수 있음. 스크립트 상단에 `# -*- coding: utf-8 -*-` 또는 파일을 UTF-8로 저장.
- **`sessions.db` 마이그레이션**: 새 컬럼 추가 시 `ALTER TABLE` 또는 `init_db()` 내에서 `CREATE TABLE IF NOT EXISTS`로 처리. 기존 DB를 삭제하지 말 것.
