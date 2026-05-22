import os
import io
import json
import uuid
import time
import tempfile
import threading
import asyncio
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from google import genai
from google.genai import types
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from notion_client import Client as NotionClient
from notion_client.errors import APIResponseError as NotionAPIError

# ── 설정 ──────────────────────────────────────────────────────────────────────
DB_PATH = "sessions.db"
SERVER_API_KEY = os.getenv("GOOGLE_API_KEY", "")
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "")

DRIVE_SCOPES             = ["https://www.googleapis.com/auth/drive.readonly"]
OAUTH_REDIRECT_URI        = os.getenv("OAUTH_REDIRECT_URI", "http://localhost:8000/auth/callback")

# 선택 가능한 Gemini 모델 (UI 드롭다운에 표시)
GEMINI_MODELS = [
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-flash",
]
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

# client_secret.json 경로: 환경변수 → 프로젝트 루트 자동 탐지
_env_secret_file = os.getenv("GOOGLE_CLIENT_SECRET_FILE", "")
CLIENT_SECRET_FILE = (
    _env_secret_file if (_env_secret_file and os.path.isfile(_env_secret_file))
    else ("client_secret.json" if os.path.isfile("client_secret.json") else "")
)

# Google Docs → Office 내보내기 형식
GDOCS_EXPORT = {
    "application/vnd.google-apps.document":
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet":
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation":
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
}

_oauth_states: Dict[str, dict] = {}  # state -> {"code_verifier": str, "client_id": str}

MIME_MAP = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    ".json": "application/json",
    ".xml": "application/xml",
    ".csv": "text/csv",
    ".py": "text/plain",
    ".js": "text/plain",
    ".ts": "text/plain",
    ".java": "text/plain",
    ".c": "text/plain",
    ".cpp": "text/plain",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

servers: Dict[str, dict] = {}
lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=8)


def get_mime(filename: str) -> str:
    return MIME_MAP.get(Path(filename).suffix.lower(), "application/octet-stream")


def _extract_svg_text(svg_path: str) -> str:
    """SVG XML에서 <text>/<tspan>/<title>/<desc> 요소의 텍스트 추출."""
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(svg_path)
        root = tree.getroot()
        wanted = {"text", "tspan", "textPath", "altGlyph", "title", "desc"}
        texts = []
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag in wanted:
                # element.text + tail은 부분 텍스트가 있을 수 있어 itertext()로 묶음 수집
                joined = "".join(elem.itertext()).strip()
                if joined:
                    texts.append(joined)
        # 중복/공백 정리하면서 순서 유지
        seen = set()
        out = []
        for t in texts:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return "\n".join(out)
    except Exception as e:
        print(f"[SVG 텍스트 추출 실패] {svg_path}: {e}")
        return ""


_SVG_PDF_FONT_NAME: Optional[str] = None  # 1회만 등록

# TEMP_SVG_DEBUG: 변환된 PDF 보관 폴더 (임시 디버그 기능)
SVG_DEBUG_DIR = Path("_svg_debug")
SVG_DEBUG_DIR.mkdir(exist_ok=True)


def _register_korean_font() -> str:
    """한글 지원 TTF 폰트 1회 등록. 반환: 폰트 이름."""
    global _SVG_PDF_FONT_NAME
    if _SVG_PDF_FONT_NAME:
        return _SVG_PDF_FONT_NAME
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    candidates = [
        r"C:\Windows\Fonts\malgun.ttf",
        r"C:\Windows\Fonts\malgunbd.ttf",
        r"C:\Windows\Fonts\gulim.ttc",
        "/Library/Fonts/AppleSDGothicNeo.ttc",
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    ]
    for fp in candidates:
        if os.path.isfile(fp):
            try:
                pdfmetrics.registerFont(TTFont("KFont", fp))
                _SVG_PDF_FONT_NAME = "KFont"
                return _SVG_PDF_FONT_NAME
            except Exception:
                continue
    _SVG_PDF_FONT_NAME = "Helvetica"
    return _SVG_PDF_FONT_NAME


def _patch_svg_font(svg_path: str, font_name: str) -> str:
    """SVG의 모든 text/tspan/textPath 요소 font-family를 강제로 교체.
    style 속성 내부의 font-family도 처리. 결과를 새 임시 파일에 저장하고 경로 반환."""
    import re
    with open(svg_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    # style 속성 내부의 font-family:... 제거
    def _strip_style_font(m):
        style = m.group(2)
        # font-family 선언 제거
        style = re.sub(r"font-family\s*:[^;\"']+;?", "", style, flags=re.IGNORECASE)
        # font: ... 단축 선언도 제거 (안전하게)
        style = re.sub(r"(?<![\w-])font\s*:[^;\"']+;?", "", style, flags=re.IGNORECASE)
        return f'{m.group(1)}"{style}"'
    content = re.sub(r'(style\s*=\s*)"([^"]*)"', _strip_style_font, content)

    # 속성 형태 font-family="..." 모두 우리 폰트로 치환
    content = re.sub(
        r'font-family\s*=\s*"[^"]*"',
        f'font-family="{font_name}"',
        content,
    )
    content = re.sub(
        r"font-family\s*=\s*'[^']*'",
        f'font-family="{font_name}"',
        content,
    )

    # text/tspan/textPath 중 font-family 속성이 없는 것에 주입
    def _inject(m):
        tag = m.group(0)
        if re.search(r'font-family\s*=', tag):
            return tag
        return tag[:-1] + f' font-family="{font_name}">'
    content = re.sub(r'<(?:text|tspan|textPath)\b[^>]*>', _inject, content)

    out_path = svg_path + ".kfont.svg"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    return out_path


# ── Excalidraw 렌더링 (Playwright + Excalidraw 라이브러리) ───────────────────
_EXCAL_HTML = """<!DOCTYPE html>
<html><head>
<script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
<script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
<script src="https://unpkg.com/@excalidraw/excalidraw@0.17.6/dist/excalidraw.production.min.js"></script>
</head><body>
<div id="status">loading</div>
<script>
(function(){
  function tryReady(){
    if (window.ExcalidrawLib && window.ExcalidrawLib.exportToSvg) {
      window.renderExcalidraw = async (payloadJson) => {
        const payload = JSON.parse(payloadJson);
        const svgEl = await window.ExcalidrawLib.exportToSvg({
          elements: payload.elements || [],
          appState: { exportBackground: true, viewBackgroundColor: '#ffffff', exportEmbedScene: false },
          files: payload.files || {}
        });
        return svgEl.outerHTML;
      };
      document.getElementById('status').textContent = 'ready';
    } else {
      setTimeout(tryReady, 100);
    }
  }
  tryReady();
})();
</script>
</body></html>"""

_excal_executor = ThreadPoolExecutor(max_workers=1)
_excal_local = threading.local()


def _ensure_excal_page():
    if getattr(_excal_local, "page", None) is None:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.set_content(_EXCAL_HTML)
        page.wait_for_function(
            "document.getElementById('status') && document.getElementById('status').textContent === 'ready'",
            timeout=30000,
        )
        _excal_local.pw = pw
        _excal_local.browser = browser
        _excal_local.ctx = ctx
        _excal_local.page = page
    return _excal_local.page


def _render_excalidraw_elements_to_svg(elements: list, files: dict = None) -> str:
    payload = {"elements": elements, "files": files or {}}
    def _render():
        page = _ensure_excal_page()
        return page.evaluate("(json) => window.renderExcalidraw(json)", json.dumps(payload))
    return _excal_executor.submit(_render).result(timeout=60)


def _render_excalidraw_full_pdf(
    scene: dict, title: str, notes: str, text_elements: str, out_pdf_path: str,
):
    """Excalidraw scene → 완전한 PDF (그래픽 + 텍스트). Chromium이 SVG+HTML을 그대로 PDF로."""
    def esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    svg = _render_excalidraw_elements_to_svg(scene.get("elements", []), scene.get("files") or {})

    notes_html = f'<h2>노트</h2><div class="notes">{esc(notes)}</div>' if notes else ""
    elem_html = f'<h2>다이어그램 텍스트</h2><div class="elements">{esc(text_elements)}</div>' if text_elements else ""

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body {{ font-family: "Malgun Gothic", "맑은 고딕", -apple-system, sans-serif; padding: 24px; color: #111; }}
h1 {{ font-size: 22px; margin: 0 0 16px; }}
h2 {{ font-size: 15px; margin: 24px 0 8px; color: #555; border-bottom: 1px solid #ddd; padding-bottom: 4px; }}
.diagram {{ border: 1px solid #e5e5e5; padding: 10px; margin: 12px 0; overflow: hidden; page-break-inside: avoid; }}
.diagram svg {{ max-width: 100%; height: auto; display: block; }}
.notes {{ white-space: pre-wrap; line-height: 1.6; font-size: 12px; }}
.elements {{ background: #f7f7f7; padding: 10px 12px; border-radius: 4px; line-height: 1.8; font-size: 12px; white-space: pre-wrap; }}
</style></head><body>
<h1>{esc(title)}</h1>
<div class="diagram">{svg}</div>
{notes_html}
{elem_html}
</body></html>"""

    def _render():
        _ensure_excal_page()  # ensures browser is ready (and SVG already generated above)
        browser = _excal_local.browser
        pdf_ctx = browser.new_context()
        try:
            pdf_page = pdf_ctx.new_page()
            pdf_page.set_content(html, wait_until="networkidle")
            pdf_page.pdf(
                path=out_pdf_path,
                format="A4",
                landscape=True,
                print_background=True,
                margin={"top": "12mm", "bottom": "12mm", "left": "12mm", "right": "12mm"},
            )
        finally:
            pdf_ctx.close()
    _excal_executor.submit(_render).result(timeout=120)


def _decompress_excalidraw_blob(blob_text: str) -> Optional[dict]:
    """compressed-json 블록 → 디컴프레스된 dict. 실패 시 None."""
    import lzstring
    import re as _re
    cleaned = _re.sub(r"\s+", "", blob_text)
    try:
        decoded = lzstring.LZString().decompressFromBase64(cleaned)
        if decoded:
            return json.loads(decoded)
    except Exception:
        pass
    # 폴백: 일부 변종이 raw base64 zlib을 쓸 수도 있어 시도
    try:
        import base64, zlib
        raw = base64.b64decode(cleaned + "==")
        try:
            return json.loads(zlib.decompress(raw))
        except Exception:
            return json.loads(zlib.decompress(raw, -15))
    except Exception as e:
        print(f"[Excalidraw 디컴프레스 실패] {e}")
        return None


def _read_excalidraw_scene(md_path: str) -> Optional[dict]:
    import re as _re
    with open(md_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    m = _re.search(r"```compressed-json\s*\n(.*?)\n```", content, _re.DOTALL)
    if not m:
        return None
    return _decompress_excalidraw_blob(m.group(1))


def _extract_excalidraw_text(md_path: str) -> tuple[str, str, str]:
    """Excalidraw .md 파일 → (title, 사용자 노트, Text Elements)."""
    import re
    with open(md_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    # frontmatter 제거
    fm = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
    body = content[fm.end():] if fm else content

    # "# Excalidraw Data" 이전이 사용자 노트 영역
    parts = re.split(r"^#\s+Excalidraw Data\s*$", body, maxsplit=1, flags=re.MULTILINE)
    user_notes = parts[0].strip() if parts else ""
    excal_section = parts[1] if len(parts) > 1 else ""

    # ## Text Elements 와 %% 사이 추출
    text_elements = ""
    m = re.search(
        r"##\s+Text Elements\s*\n(.*?)(?=^%%|^##\s+Drawing|\Z)",
        excal_section, flags=re.DOTALL | re.MULTILINE,
    )
    if m:
        raw = m.group(1).strip()
        # 각 줄 끝의 `^id` 앵커 제거
        cleaned_lines = []
        for ln in raw.split("\n"):
            ln = re.sub(r"\s*\^[A-Za-z0-9_-]+\s*$", "", ln).rstrip()
            if ln:
                cleaned_lines.append(ln)
        text_elements = "\n".join(cleaned_lines)

    # 안내 문구(decompress 메시지 등) 제거
    user_notes = re.sub(
        r"==⚠.*?==\s*[^\n]*\n?", "", user_notes, flags=re.DOTALL,
    ).strip()

    title = Path(md_path).stem.replace(".excalidraw", "")
    return title, user_notes, text_elements


def _write_excal_text_pages(c, font_name: str, title: str, notes: str, elements: str):
    """주어진 reportlab Canvas에 텍스트 페이지를 그린다 (현재 페이지부터 시작)."""
    PAGE_W, PAGE_H = 595, 842
    MARGIN = 50
    c.setPageSize((PAGE_W, PAGE_H))

    def _wrap_draw(lines, font_size, leading, y):
        c.setFont(font_name, font_size)
        max_chars = 60
        for line in lines:
            if not line:
                y -= leading
                if y < MARGIN:
                    c.showPage(); c.setPageSize((PAGE_W, PAGE_H))
                    c.setFont(font_name, font_size); y = PAGE_H - MARGIN
                continue
            while line:
                chunk = line[:max_chars]; line = line[max_chars:]
                if y < MARGIN:
                    c.showPage(); c.setPageSize((PAGE_W, PAGE_H))
                    c.setFont(font_name, font_size); y = PAGE_H - MARGIN
                c.drawString(MARGIN, y, chunk); y -= leading
        return y

    c.setFont(font_name, 18); y = PAGE_H - MARGIN
    c.drawString(MARGIN, y, title); y -= 28
    if notes:
        c.setFont(font_name, 12); c.drawString(MARGIN, y, "[노트]"); y -= 18
        y = _wrap_draw(notes.split("\n"), 10, 14, y); y -= 12
    if elements:
        if y < MARGIN + 40:
            c.showPage(); c.setPageSize((PAGE_W, PAGE_H))
            y = PAGE_H - MARGIN
        c.setFont(font_name, 12); c.drawString(MARGIN, y, "[다이어그램 텍스트]"); y -= 18
        y = _wrap_draw(elements.split("\n"), 10, 14, y)
    if not notes and not elements:
        c.setFont(font_name, 11); c.drawString(MARGIN, y, "(추출 가능한 텍스트가 없습니다.)")


def _maybe_convert_excalidraw(tmp_path: str, display_name: str) -> tuple[str, str, int]:
    """Excalidraw .md → 단일 PDF (Chromium이 직접 HTML+SVG → PDF 변환)."""
    lower = display_name.lower()
    if not lower.endswith(".excalidraw.md"):
        try:
            return tmp_path, display_name, os.path.getsize(tmp_path)
        except Exception:
            return tmp_path, display_name, 0

    try:
        _, notes, elements_text = _extract_excalidraw_text(tmp_path)
        base_name = display_name[:-len(".excalidraw.md")]
        title = base_name  # 업로드 파일명 기반 제목 사용
        scene = _read_excalidraw_scene(tmp_path)
        if not scene or not scene.get("elements"):
            print(f"[Excalidraw 경고] {display_name}: 다이어그램 데이터 없음")
            return tmp_path, display_name, os.path.getsize(tmp_path)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as pdf_tmp:
            pdf_path = pdf_tmp.name

        _render_excalidraw_full_pdf(scene, title, notes, elements_text, pdf_path)

        try: os.unlink(tmp_path)
        except Exception: pass

        new_name = base_name + ".pdf"
        try:
            import shutil
            shutil.copyfile(pdf_path, SVG_DEBUG_DIR / new_name)
        except Exception as _e:
            print(f"[Excalidraw 디버그 사본 저장 실패] {_e}")

        return pdf_path, new_name, os.path.getsize(pdf_path)

    except Exception as e:
        import traceback
        print(f"[Excalidraw→PDF 변환 실패] {display_name}: {type(e).__name__}: {e}")
        traceback.print_exc()
        try:
            return tmp_path, display_name, os.path.getsize(tmp_path)
        except Exception:
            return tmp_path, display_name, 0


def _maybe_convert_svg(tmp_path: str, display_name: str) -> tuple[str, str, int]:
    """SVG → 단일 페이지 PDF. 텍스트는 등록된 한글 폰트로 강제 렌더."""
    if not display_name.lower().endswith(".svg"):
        try:
            return tmp_path, display_name, os.path.getsize(tmp_path)
        except Exception:
            return tmp_path, display_name, 0

    patched_path = None
    try:
        from svglib.svglib import svg2rlg
        from reportlab.graphics import renderPDF

        font_name = _register_korean_font()
        patched_path = _patch_svg_font(tmp_path, font_name)

        drawing = svg2rlg(patched_path)
        if drawing is None:
            raise RuntimeError("SVG 파싱 결과 없음")

        # 모든 String 노드의 fontName을 등록된 한글 폰트로 강제 교체
        from reportlab.graphics.shapes import String as RLString
        def _force_font(node):
            if isinstance(node, RLString):
                node.fontName = font_name
            if hasattr(node, "contents"):
                for child in node.contents:
                    _force_font(child)
        _force_font(drawing)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as pdf_tmp:
            pdf_path = pdf_tmp.name
        renderPDF.drawToFile(drawing, pdf_path)

        try: os.unlink(tmp_path)
        except Exception: pass

        new_name = display_name[:-4] + ".pdf"

        # TEMP_SVG_DEBUG: 디버그 폴더에 사본 저장
        try:
            import shutil
            shutil.copyfile(pdf_path, SVG_DEBUG_DIR / new_name)
        except Exception as _e:
            print(f"[SVG_DEBUG 사본 저장 실패] {_e}")

        return pdf_path, new_name, os.path.getsize(pdf_path)

    except Exception as e:
        import traceback
        print(f"[SVG→PDF 변환 실패] {display_name}: {type(e).__name__}: {e}")
        traceback.print_exc()
        try:
            return tmp_path, display_name, os.path.getsize(tmp_path)
        except Exception:
            return tmp_path, display_name, 0
    finally:
        if patched_path:
            try: os.unlink(patched_path)
            except Exception: pass


# ── SQLite ────────────────────────────────────────────────────────────────────
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _existing_tables(conn) -> set:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}


def init_db():
    with get_db() as conn:
        tables = _existing_tables(conn)

        if "sessions" in tables and "servers" not in tables:
            # ── 구 스키마(sessions/files) → 새 스키마(servers/files) 마이그레이션 ──
            print("[DB] 구 스키마 감지 → 마이그레이션 시작")
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("""
                CREATE TABLE servers (
                    id         TEXT PRIMARY KEY,
                    alias      TEXT NOT NULL DEFAULT '',
                    store_name TEXT NOT NULL,
                    api_key    TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("""
                INSERT INTO servers (id, alias, store_name, api_key, created_at)
                SELECT id, display_name, store_name, api_key, created_at FROM sessions
            """)
            conn.execute("""
                CREATE TABLE files_new (
                    id        TEXT PRIMARY KEY,
                    server_id TEXT NOT NULL,
                    name      TEXT NOT NULL,
                    size      INTEGER NOT NULL,
                    status    TEXT NOT NULL DEFAULT 'uploading',
                    error     TEXT,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("""
                INSERT INTO files_new (id, server_id, name, size, status, error, created_at)
                SELECT id, session_id, name, size, status, error, created_at FROM files
            """)
            conn.execute("DROP TABLE files")
            conn.execute("ALTER TABLE files_new RENAME TO files")
            conn.execute("DROP TABLE sessions")
            conn.execute("PRAGMA foreign_keys=ON")
            print("[DB] 마이그레이션 완료")
        else:
            # ── 새 스키마 생성 (없으면) ──
            conn.execute("""
                CREATE TABLE IF NOT EXISTS servers (
                    id         TEXT PRIMARY KEY,
                    alias      TEXT NOT NULL DEFAULT '',
                    store_name TEXT NOT NULL,
                    api_key    TEXT NOT NULL,
                    model      TEXT NOT NULL DEFAULT 'gemini-2.5-flash',
                    created_at REAL NOT NULL
                )
            """)
            # 기존 servers 테이블에 model 컬럼 추가 (없는 경우)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(servers)").fetchall()}
            if "model" not in cols:
                conn.execute("ALTER TABLE servers ADD COLUMN model TEXT NOT NULL DEFAULT 'gemini-2.5-flash'")
                print("[DB] servers.model 컬럼 추가 완료")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    id        TEXT PRIMARY KEY,
                    server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
                    name      TEXT NOT NULL,
                    size      INTEGER NOT NULL,
                    status    TEXT NOT NULL DEFAULT 'uploading',
                    error     TEXT,
                    created_at REAL NOT NULL
                )
            """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS drive_tokens (
                id         TEXT PRIMARY KEY DEFAULT 'default',
                token_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # 서버 종료 시 중단됐던 uploading → error
        affected = conn.execute("""
            UPDATE files SET status='error', error='서버 재시작으로 중단됨'
            WHERE status='uploading'
        """).rowcount
        if affected:
            print(f"[DB] 중단된 파일 {affected}개 → error 처리")


def db_load_servers():
    with get_db() as conn:
        for row in conn.execute("SELECT * FROM servers ORDER BY created_at").fetchall():
            svid = row["id"]
            files = [
                {"id": f["id"], "name": f["name"], "size": f["size"],
                 "status": f["status"], "error": f["error"]}
                for f in conn.execute(
                    "SELECT * FROM files WHERE server_id=? ORDER BY created_at", (svid,)
                ).fetchall()
            ]
            servers[svid] = {
                "alias":      row["alias"],
                "store_name": row["store_name"],
                "api_key":    row["api_key"],
                "model":      row["model"] if "model" in row.keys() else DEFAULT_GEMINI_MODEL,
                "files":      files,
            }
    if servers:
        print(f"[DB] 서버 {len(servers)}개 복원 완료")


def db_add_server(svid: str, alias: str, api_key: str, store_name: str, model: str = DEFAULT_GEMINI_MODEL):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO servers (id, alias, store_name, api_key, model, created_at) VALUES (?,?,?,?,?,?)",
            (svid, alias, store_name, api_key, model, time.time()),
        )


def db_update_alias(svid: str, alias: str):
    with get_db() as conn:
        conn.execute("UPDATE servers SET alias=? WHERE id=?", (alias, svid))


def db_update_model(svid: str, model: str):
    with get_db() as conn:
        conn.execute("UPDATE servers SET model=? WHERE id=?", (model, svid))


def db_add_file(svid: str, fid: str, name: str, size: int):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO files (id, server_id, name, size, status, created_at) VALUES (?,?,?,?,'uploading',?)",
            (fid, svid, name, size, time.time()),
        )


def db_update_file(fid: str, status: str, error: Optional[str] = None):
    with get_db() as conn:
        conn.execute("UPDATE files SET status=?, error=? WHERE id=?", (status, error, fid))


def db_delete_server(svid: str):
    with get_db() as conn:
        conn.execute("DELETE FROM servers WHERE id=?", (svid,))


def db_delete_file(fid: str):
    with get_db() as conn:
        conn.execute("DELETE FROM files WHERE id=?", (fid,))


def db_get_drive_token(client_id: str) -> Optional[str]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT token_json FROM drive_tokens WHERE id=?", (client_id,)
        ).fetchone()
        return row["token_json"] if row else None


def db_save_drive_token(client_id: str, token_json: str):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO drive_tokens (id, token_json, updated_at) VALUES (?,?,?)",
            (client_id, token_json, time.time()),
        )


def db_clear_drive_token(client_id: str):
    with get_db() as conn:
        conn.execute("DELETE FROM drive_tokens WHERE id=?", (client_id,))


def _get_drive_creds(client_id: str) -> Optional[Credentials]:
    token_json = db_get_drive_token(client_id)
    if not token_json:
        return None
    creds = Credentials.from_authorized_user_info(json.loads(token_json), DRIVE_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
        db_save_drive_token(client_id, creds.to_json())
    return creds if creds.valid else None


def _drive_service(creds: Credentials):
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ── Lifespan ──────────────────────────────────────────────────────────────────
def db_get_setting(key: str) -> str:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else ""

def db_save_setting(key: str, value: str):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?,?)", (key, value))

def db_delete_setting(key: str):
    with get_db() as conn:
        conn.execute("DELETE FROM app_settings WHERE key=?", (key,))

def _mask_key(key: str) -> str:
    if not key:
        return ""
    return key[:4] + "***" + key[-3:] if len(key) > 7 else "****"


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_app: FastAPI):
    global SERVER_API_KEY, NOTION_API_KEY
    init_db()
    if not SERVER_API_KEY:
        SERVER_API_KEY = db_get_setting("gemini_key")
    if not NOTION_API_KEY:
        NOTION_API_KEY = db_get_setting("notion_key")
    db_load_servers()
    yield


app = FastAPI(title="File RAG System", lifespan=lifespan)


# ── Gemini API ────────────────────────────────────────────────────────────────
def make_client(api_key: str) -> genai.Client:
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(api_version="v1beta"),
    )


def _sync_create_store(api_key: str, display_name: str) -> str:
    client = make_client(api_key)
    return client.file_search_stores.create(
        config={"display_name": display_name}
    ).name


def _sync_delete_store(api_key: str, store_name: str) -> None:
    try:
        client = make_client(api_key)
        client.file_search_stores.delete(name=store_name)
    except Exception:
        pass


def _sync_list_documents(api_key: str, store_name: str) -> list:
    client = make_client(api_key)
    docs = []
    for doc in client.file_search_stores.documents.list(parent=store_name):
        docs.append({
            "id":     doc.name.split("/")[-1],
            "name":   doc.display_name,
            "size":   doc.size_bytes or 0,
            "status": "ready",
            "error":  None,
        })
    return docs


def _sync_delete_document(api_key: str, store_name: str, doc_id: str) -> None:
    try:
        client = make_client(api_key)
        client.file_search_stores.documents.delete(
            name=f"{store_name}/documents/{doc_id}"
        )
    except Exception:
        pass


STRICT_SYSTEM_INSTRUCTION = """당신은 사용자가 업로드한 문서만을 근거로 답변하는 검색 도우미입니다.

엄격한 규칙:
1. 반드시 file_search 도구를 사용해 업로드된 문서에서 정보를 검색한 뒤 답변하세요.
2. 검색 결과에 포함된 내용만 인용하여 답변하세요. 일반 지식, 추측, 외부 정보는 절대 사용하지 마세요.
3. 검색 결과에 관련 정보가 없으면 정확히 다음 문장으로 답하세요:
   "업로드된 문서에서 관련 정보를 찾을 수 없습니다."
4. 부분적으로만 답할 수 있으면, 어떤 부분이 문서에 있고 어떤 부분이 없는지 명시하세요. 없는 부분은 추측하지 마세요.
5. 인용한 내용에는 어떤 문서의 어느 부분에서 가져왔는지 가능한 한 표시하세요.
6. 사용자가 일반 지식 질문(예: "프랑스의 수도는?")을 해도, 그것이 업로드된 문서에 없다면 위 3번 문장으로 답하세요."""


def _sync_query(api_key: str, store_name: str, query: str, model: str = DEFAULT_GEMINI_MODEL) -> dict:
    client = make_client(api_key)
    resp = client.models.generate_content(
        model=model,
        contents=query,
        config=types.GenerateContentConfig(
            system_instruction=STRICT_SYSTEM_INSTRUCTION,
            tools=[types.Tool(file_search=types.FileSearch(
                file_search_store_names=[store_name]
            ))],
        ),
    )
    citations = []
    try:
        for c in resp.candidates or []:
            gm = getattr(c, "grounding_metadata", None)
            for chunk in getattr(gm, "grounding_chunks", []):
                rc = getattr(chunk, "retrieved_context", None)
                if rc:
                    citations.append({
                        "title": getattr(rc, "title", ""),
                        "uri":   getattr(rc, "uri", ""),
                    })
    except Exception:
        pass
    return {"text": resp.text, "citations": citations}


# ── 백그라운드 업로드 ─────────────────────────────────────────────────────────
async def _bg_upload(
    server_id: str, file_id: str,
    api_key: str, store_name: str,
    tmp_path: str, display_name: str, mime_type: str,
):
    def _do():
        client = make_client(api_key)
        op = client.file_search_stores.upload_to_file_search_store(
            file=tmp_path,
            file_search_store_name=store_name,
            config={"display_name": display_name},
        )
        while not op.done:
            time.sleep(3)
            op = client.operations.get(op)
        if op.error:
            raise RuntimeError(str(op.error))

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(executor, _do)
        with lock:
            for f in servers.get(server_id, {}).get("files", []):
                if f["id"] == file_id:
                    f["status"] = "ready"
        db_update_file(file_id, "ready")
    except Exception as e:
        err = str(e)
        with lock:
            for f in servers.get(server_id, {}).get("files", []):
                if f["id"] == file_id:
                    f["status"] = "error"
                    f["error"] = err
        db_update_file(file_id, "error", err)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ── Pydantic 모델 ─────────────────────────────────────────────────────────────
class ServerIn(BaseModel):
    alias: str
    api_key: Optional[str] = None
    store_name: Optional[str] = None  # 기존 스토어 가져오기 시 사용
    model: Optional[str] = None       # 선택: GEMINI_MODELS 중 하나


class ServerPatchIn(BaseModel):
    alias: Optional[str] = None
    model: Optional[str] = None


class QueryIn(BaseModel):
    query: str


class DriveUploadIn(BaseModel):
    file_id: str
    file_name: str
    mime_type: str
    client_id: str = "default"


# ── API 엔드포인트 ────────────────────────────────────────────────────────────
@app.get("/api/config")
async def get_config():
    return {
        "has_server_api_key":  bool(SERVER_API_KEY),
        "has_drive_oauth":     bool(CLIENT_SECRET_FILE),
        "has_notion":          bool(NOTION_API_KEY),
        "gemini_models":       GEMINI_MODELS,
        "default_gemini_model": DEFAULT_GEMINI_MODEL,
    }


class SettingKeyIn(BaseModel):
    key: str


@app.get("/api/settings")
async def get_settings(client_id: str = "default"):
    creds = _get_drive_creds(client_id)
    return {
        "gemini_key":         _mask_key(SERVER_API_KEY),
        "notion_key":         _mask_key(NOTION_API_KEY),
        "drive_authenticated": creds is not None,
        "drive_available":    bool(CLIENT_SECRET_FILE),
    }


@app.post("/api/settings/gemini")
async def save_gemini_key(data: SettingKeyIn):
    global SERVER_API_KEY
    key = data.key.strip()
    if not key:
        raise HTTPException(400, "키를 입력해주세요")
    SERVER_API_KEY = key
    db_save_setting("gemini_key", key)
    return {"ok": True}


@app.delete("/api/settings/gemini")
async def clear_gemini_key():
    global SERVER_API_KEY
    db_delete_setting("gemini_key")
    SERVER_API_KEY = os.getenv("GOOGLE_API_KEY", "")
    return {"ok": True}


@app.post("/api/settings/notion")
async def save_notion_key(data: SettingKeyIn):
    global NOTION_API_KEY
    key = data.key.strip()
    if not key:
        raise HTTPException(400, "키를 입력해주세요")
    NOTION_API_KEY = key
    db_save_setting("notion_key", key)
    return {"ok": True}


@app.delete("/api/settings/notion")
async def clear_notion_key():
    global NOTION_API_KEY
    db_delete_setting("notion_key")
    NOTION_API_KEY = os.getenv("NOTION_API_KEY", "")
    return {"ok": True}


# ── Google Drive OAuth ────────────────────────────────────────────────────────
@app.get("/api/drive/status")
async def drive_status(client_id: str = "default"):
    creds = _get_drive_creds(client_id)
    return {"authenticated": creds is not None}


@app.get("/api/drive/auth-url")
async def drive_auth_url(client_id: str = "default"):
    if not CLIENT_SECRET_FILE or not os.path.isfile(CLIENT_SECRET_FILE):
        raise HTTPException(400, "client_secret.json 파일이 없습니다")
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRET_FILE,
        scopes=DRIVE_SCOPES,
        redirect_uri=OAUTH_REDIRECT_URI,
    )
    auth_url, state = flow.authorization_url(
        access_type="offline", prompt="consent"
    )
    _oauth_states[state] = {"code_verifier": flow.code_verifier, "client_id": client_id}
    return {"auth_url": auth_url}


@app.get("/auth/callback")
async def oauth_callback(request: Request):
    code  = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or state not in _oauth_states:
        return HTMLResponse("<p>오류: 잘못된 요청</p>", status_code=400)
    state_data    = _oauth_states.pop(state)
    code_verifier = state_data["code_verifier"]
    client_id     = state_data["client_id"]

    flow = Flow.from_client_secrets_file(
        CLIENT_SECRET_FILE,
        scopes=DRIVE_SCOPES,
        redirect_uri=OAUTH_REDIRECT_URI,
    )
    flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    db_save_drive_token(client_id, flow.credentials.to_json())

    return HTMLResponse("""<!DOCTYPE html>
<html><body style="font-family:sans-serif;background:#0f172a;color:#fff;display:flex;
  align-items:center;justify-content:center;height:100vh;margin:0;flex-direction:column">
<div style="font-size:3rem">✓</div>
<h2 style="margin:.5rem 0">Google Drive 인증 완료</h2>
<p style="color:#94a3b8">이 창을 닫으세요.</p>
<script>window.opener&&window.opener.postMessage('drive_auth_ok','*');setTimeout(()=>window.close(),1500);</script>
</body></html>""")


@app.delete("/api/drive/auth")
async def drive_disconnect(client_id: str = "default"):
    db_clear_drive_token(client_id)
    return {"status": "disconnected"}


@app.get("/api/drive/files")
async def list_drive_files(client_id: str = "default", parent: str = "root", q: str = "", page_token: str = ""):
    creds = _get_drive_creds(client_id)
    if not creds:
        raise HTTPException(401, "Google Drive 인증 필요")

    def _list():
        svc = _drive_service(creds)
        parts = [f"'{parent}' in parents", "trashed=false"]
        if q:
            parts.append(f"name contains '{q}'")
        res = svc.files().list(
            q=" and ".join(parts),
            pageSize=50,
            fields="nextPageToken,files(id,name,mimeType,size,modifiedTime)",
            orderBy="folder,name",
            **( {"pageToken": page_token} if page_token else {} ),
        ).execute()
        return {
            "files":           res.get("files", []),
            "next_page_token": res.get("nextPageToken", ""),
        }

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, _list)


@app.post("/api/servers/{svid}/upload-from-drive")
async def upload_from_drive(
    svid: str,
    body: DriveUploadIn,
    background_tasks: BackgroundTasks,
):
    with lock:
        if svid not in servers:
            raise HTTPException(404, "서버를 찾을 수 없습니다")
        existing_names = [f["name"] for f in servers[svid]["files"]]
        key        = servers[svid]["api_key"]
        store_name = servers[svid]["store_name"]

    creds = _get_drive_creds(body.client_id)
    if not creds:
        raise HTTPException(401, "Google Drive 인증 필요")

    # Google Docs 계열은 Office 포맷으로 내보내기
    export_info = GDOCS_EXPORT.get(body.mime_type)
    if export_info:
        export_mime, ext = export_info
        display_name = body.file_name + ext
    else:
        export_mime  = None
        display_name = body.file_name

    if display_name in existing_names:
        raise HTTPException(409, f"'{display_name}' 파일이 이미 존재합니다.")

    suffix = Path(display_name).suffix

    def _download():
        svc = _drive_service(creds)
        if export_mime:
            req = svc.files().export_media(fileId=body.file_id, mimeType=export_mime)
        else:
            req = svc.files().get_media(fileId=body.file_id)
        buf = io.BytesIO()
        dl  = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = dl.next_chunk()
        return buf.getvalue()

    loop = asyncio.get_running_loop()
    try:
        content = await loop.run_in_executor(executor, _download)
    except Exception as e:
        raise HTTPException(500, f"Drive 다운로드 실패: {e}")

    if len(content) > 100 * 1024 * 1024:
        raise HTTPException(400, "파일 크기는 100MB를 초과할 수 없습니다")

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    loop = asyncio.get_running_loop()
    tmp_path, display_name, final_size = await loop.run_in_executor(
        executor, _maybe_convert_excalidraw, tmp_path, display_name)
    tmp_path, display_name, final_size = await loop.run_in_executor(
        executor, _maybe_convert_svg, tmp_path, display_name)

    fid = str(uuid.uuid4())[:8]
    with lock:
        servers[svid]["files"].append({
            "id": fid, "name": display_name, "size": final_size,
            "status": "uploading", "error": None,
        })
    db_add_file(svid, fid, display_name, final_size)

    background_tasks.add_task(
        _bg_upload, svid, fid, key, store_name,
        tmp_path, display_name, get_mime(display_name),
    )
    return {"file_id": fid, "name": display_name, "status": "uploading"}


@app.get("/api/servers")
async def list_servers():
    with lock:
        return [
            {
                "id":          svid,
                "alias":       s["alias"],
                "store_name":  s["store_name"],
                "model":       s.get("model", DEFAULT_GEMINI_MODEL),
                "file_count":  len(s["files"]),
                "ready_count": sum(1 for f in s["files"] if f["status"] == "ready"),
            }
            for svid, s in servers.items()
        ]


@app.post("/api/servers")
async def create_server(body: ServerIn):
    key = body.api_key or SERVER_API_KEY
    if not key:
        raise HTTPException(400, "API 키가 필요합니다")
    if not body.alias.strip():
        raise HTTPException(400, "별칭을 입력해주세요")

    model = (body.model or DEFAULT_GEMINI_MODEL).strip()
    if model not in GEMINI_MODELS:
        raise HTTPException(400, f"지원하지 않는 모델: {model}")

    svid = str(uuid.uuid4())

    loop = asyncio.get_running_loop()

    if body.store_name:
        store_name = body.store_name.strip()
        # 기존 스토어 문서 목록 동기화
        try:
            existing_docs = await loop.run_in_executor(executor, _sync_list_documents, key, store_name)
        except Exception:
            existing_docs = []
    else:
        store_display = f"rag-{svid[:8]}"
        try:
            store_name = await loop.run_in_executor(executor, _sync_create_store, key, store_display)
        except Exception as e:
            raise HTTPException(400, f"스토어 생성 실패: {e}")
        existing_docs = []

    with lock:
        servers[svid] = {
            "alias":      body.alias.strip(),
            "store_name": store_name,
            "api_key":    key,
            "model":      model,
            "files":      existing_docs,
        }
    db_add_server(svid, body.alias.strip(), key, store_name, model)
    # 동기화된 파일 DB에 저장
    now = time.time()
    with get_db() as conn:
        for doc in existing_docs:
            conn.execute(
                "INSERT OR IGNORE INTO files (id, server_id, name, size, status, created_at) VALUES (?,?,?,?,'ready',?)",
                (doc["id"], svid, doc["name"], doc["size"], now),
            )
    return {"id": svid, "alias": body.alias.strip(), "store_name": store_name, "model": model, "synced_count": len(existing_docs)}


@app.get("/api/servers/{svid}")
async def get_server(svid: str):
    with lock:
        if svid not in servers:
            raise HTTPException(404, "서버를 찾을 수 없습니다")
        s = servers[svid]
        return {
            "id":         svid,
            "alias":      s["alias"],
            "store_name": s["store_name"],
            "files":      list(s["files"]),
        }


@app.patch("/api/servers/{svid}")
async def update_server(svid: str, body: ServerPatchIn):
    with lock:
        if svid not in servers:
            raise HTTPException(404, "서버를 찾을 수 없습니다")

    if body.alias is not None:
        alias = body.alias.strip()
        if not alias:
            raise HTTPException(400, "별칭을 입력해주세요")
        with lock:
            servers[svid]["alias"] = alias
        db_update_alias(svid, alias)

    if body.model is not None:
        model = body.model.strip()
        if model not in GEMINI_MODELS:
            raise HTTPException(400, f"지원하지 않는 모델: {model}")
        with lock:
            servers[svid]["model"] = model
        db_update_model(svid, model)

    with lock:
        return {
            "id":    svid,
            "alias": servers[svid]["alias"],
            "model": servers[svid].get("model", DEFAULT_GEMINI_MODEL),
        }


@app.post("/api/servers/{svid}/upload")
async def upload_file(
    svid: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    with lock:
        if svid not in servers:
            raise HTTPException(404, "서버를 찾을 수 없습니다")
        key = servers[svid]["api_key"]
        store_name = servers[svid]["store_name"]

    content = await file.read()
    if len(content) > 100 * 1024 * 1024:
        raise HTTPException(400, "파일 크기는 100MB를 초과할 수 없습니다")

    # 중복 파일명 감지
    with lock:
        existing_names = [f["name"] for f in servers.get(svid, {}).get("files", [])]
    if file.filename in existing_names:
        raise HTTPException(409, f"'{file.filename}' 파일이 이미 존재합니다. 삭제 후 다시 업로드하거나 파일명을 변경하세요.")

    suffix = Path(file.filename or "file").suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    final_name = file.filename or "file"
    loop = asyncio.get_running_loop()
    tmp_path, final_name, final_size = await loop.run_in_executor(
        executor, _maybe_convert_excalidraw, tmp_path, final_name)
    tmp_path, final_name, final_size = await loop.run_in_executor(
        executor, _maybe_convert_svg, tmp_path, final_name)

    # 변환 후 중복 이름 재검사
    if final_name != file.filename and final_name in existing_names:
        try: os.unlink(tmp_path)
        except Exception: pass
        raise HTTPException(409, f"'{final_name}' (변환 결과) 파일이 이미 존재합니다.")

    fid = str(uuid.uuid4())[:8]
    with lock:
        servers[svid]["files"].append({
            "id": fid, "name": final_name, "size": final_size,
            "status": "uploading", "error": None,
        })
    db_add_file(svid, fid, final_name, final_size)

    background_tasks.add_task(
        _bg_upload,
        svid, fid, key, store_name, tmp_path,
        final_name, get_mime(final_name),
    )
    return {"file_id": fid, "name": final_name, "status": "uploading"}


@app.post("/api/servers/{svid}/query")
async def query_rag(svid: str, body: QueryIn):
    with lock:
        if svid not in servers:
            raise HTTPException(404, "서버를 찾을 수 없습니다")
        s = dict(servers[svid])
        ready = [f for f in s["files"] if f["status"] == "ready"]
    if not ready:
        raise HTTPException(400, "준비된 파일이 없습니다. 인덱싱이 완료될 때까지 기다려주세요.")
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            executor, _sync_query, s["api_key"], s["store_name"], body.query,
            s.get("model", DEFAULT_GEMINI_MODEL),
        )
    except Exception as e:
        raise HTTPException(500, str(e))
    return result


@app.delete("/api/servers/{svid}/files/{fid}")
async def delete_file(svid: str, fid: str):
    with lock:
        if svid not in servers:
            raise HTTPException(404, "서버를 찾을 수 없습니다")
        files = servers[svid]["files"]
        target = next((f for f in files if f["id"] == fid), None)
        if not target:
            raise HTTPException(404, "파일을 찾을 수 없습니다")
        servers[svid]["files"] = [f for f in files if f["id"] != fid]
        key        = servers[svid]["api_key"]
        store_name = servers[svid]["store_name"]

    db_delete_file(fid)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(executor, _sync_delete_document, key, store_name, fid)
    return {"status": "deleted", "file_id": fid}


@app.post("/api/servers/{svid}/sync")
async def sync_server_files(svid: str):
    with lock:
        if svid not in servers:
            raise HTTPException(404, "서버를 찾을 수 없습니다")
        key        = servers[svid]["api_key"]
        store_name = servers[svid]["store_name"]

    loop = asyncio.get_running_loop()
    try:
        docs = await loop.run_in_executor(executor, _sync_list_documents, key, store_name)
    except Exception as e:
        raise HTTPException(500, f"동기화 실패: {e}")

    now = time.time()
    with get_db() as conn:
        conn.execute("DELETE FROM files WHERE server_id=?", (svid,))
        for doc in docs:
            conn.execute(
                "INSERT INTO files (id, server_id, name, size, status, created_at) VALUES (?,?,?,?,'ready',?)",
                (doc["id"], svid, doc["name"], doc["size"], now),
            )
    with lock:
        servers[svid]["files"] = docs
    return {"synced_count": len(docs)}


@app.delete("/api/servers/{svid}")
async def delete_server(svid: str):
    with lock:
        if svid not in servers:
            raise HTTPException(404, "서버를 찾을 수 없습니다")
        s = servers.pop(svid)
    db_delete_server(svid)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(executor, _sync_delete_store, s["api_key"], s["store_name"])
    return {"status": "deleted"}


# ── Notion ────────────────────────────────────────────────────────────────────
def _rich_text_to_md(rt_list) -> str:
    out = []
    for rt in rt_list or []:
        txt = rt.get("plain_text", "")
        ann = rt.get("annotations", {})
        href = rt.get("href")
        if ann.get("code"):     txt = f"`{txt}`"
        if ann.get("bold"):     txt = f"**{txt}**"
        if ann.get("italic"):   txt = f"*{txt}*"
        if ann.get("strikethrough"): txt = f"~~{txt}~~"
        if href: txt = f"[{txt}]({href})"
        out.append(txt)
    return "".join(out)


def _safe_filename(name: str) -> str:
    bad = '<>:"/\\|?*\n\r\t'
    out = "".join("_" if c in bad else c for c in name).strip().strip(".")
    return out[:120] or "untitled"


def _attachment_filename(url: str, fallback_prefix: str = "attachment") -> str:
    try:
        from urllib.parse import urlparse, unquote
        path = unquote(urlparse(url).path)
        name = Path(path).name
        if name and "." in name:
            return _safe_filename(name)
    except Exception:
        pass
    return f"{_safe_filename(fallback_prefix)}.bin"


def _prop_value_to_text(prop: dict) -> str:
    """Notion 페이지 속성(property) 하나를 사람이 읽을 수 있는 문자열로 변환."""
    if not prop:
        return ""
    ptype = prop.get("type", "")
    val = prop.get(ptype)
    if val is None:
        return ""
    if ptype in ("title", "rich_text"):
        return _rich_text_to_md(val).replace("\n", " ").replace("|", "\\|")
    if ptype == "number":
        return "" if val is None else str(val)
    if ptype == "select":
        return val.get("name", "") if isinstance(val, dict) else ""
    if ptype == "multi_select":
        return ", ".join(v.get("name", "") for v in val) if isinstance(val, list) else ""
    if ptype == "status":
        return val.get("name", "") if isinstance(val, dict) else ""
    if ptype == "date":
        if not isinstance(val, dict):
            return ""
        s = val.get("start", "") or ""
        e = val.get("end", "") or ""
        return f"{s} ~ {e}" if e else s
    if ptype == "checkbox":
        return "✓" if val else ""
    if ptype in ("url", "email", "phone_number"):
        return str(val) if val else ""
    if ptype == "people":
        return ", ".join(p.get("name", "") for p in val) if isinstance(val, list) else ""
    if ptype == "files":
        names = []
        for f in val or []:
            names.append(f.get("name", "")
                         or (f.get("external") or {}).get("url", "")
                         or (f.get("file") or {}).get("url", ""))
        return ", ".join(filter(None, names))
    if ptype in ("created_time", "last_edited_time"):
        return str(val) if val else ""
    if ptype in ("created_by", "last_edited_by"):
        return val.get("name", "") if isinstance(val, dict) else ""
    if ptype == "formula":
        if not isinstance(val, dict):
            return ""
        ftype = val.get("type")
        return str(val.get(ftype, "") or "")
    if ptype == "relation":
        return f"({len(val)}개 관계)" if isinstance(val, list) else ""
    if ptype == "rollup":
        if not isinstance(val, dict):
            return ""
        rtype = val.get("type")
        rval = val.get(rtype)
        if isinstance(rval, list):
            return ", ".join(_prop_value_to_text({"type": (v.get("type") or "rich_text"), (v.get("type") or "rich_text"): v.get(v.get("type") or "rich_text")}) for v in rval if isinstance(v, dict))
        return str(rval) if rval is not None else ""
    return ""


def _database_to_md_table(database_id: str, notion, title_hint: str = "") -> str:
    """Notion 데이터베이스를 마크다운 테이블로 변환."""
    try:
        db_info = _retrieve_db_info(notion, database_id)
    except Exception as e:
        return f"_(데이터베이스 로딩 실패: {e})_"

    db_title = _rich_text_to_md(db_info.get("title", [])) or title_hint or "Untitled Database"
    # 컬럼 순서는 db_info["properties"]의 dict 순서를 따름 (Python 3.7+)
    col_names = list((db_info.get("properties") or {}).keys())
    if not col_names:
        return f"### 📊 {db_title}\n_(빈 데이터베이스)_"

    # 모든 행 조회 (data_sources fallback 포함). 페이지네이션은 단순화.
    try:
        rows = _query_database_rows(notion, database_id, page_size=100)
    except Exception as e:
        rows = [{"_error": str(e)}]

    header = "| " + " | ".join(col_names) + " |"
    sep = "|" + "|".join("---" for _ in col_names) + "|"
    body_lines = []
    for row in rows:
        if "_error" in row:
            body_lines.append(f"_(이후 행 로딩 실패: {row['_error']})_")
            continue
        props = row.get("properties", {}) or {}
        cells = []
        for c in col_names:
            text = _prop_value_to_text(props.get(c, {}))
            # 줄바꿈은 공백으로, 파이프는 escape
            text = text.replace("\n", " ").replace("|", "\\|").strip()
            cells.append(text)
        body_lines.append("| " + " | ".join(cells) + " |")

    return f"### 📊 {db_title}\n\n{header}\n{sep}\n" + "\n".join(body_lines)


def _block_to_md(block, notion, depth: int, attachments: list,
                 sub_pages: list, page_title_prefix: str) -> str:
    """블록 → markdown. 첨부/하위페이지를 인자로 받은 리스트에 누적."""
    btype = block.get("type", "")
    data  = block.get(btype, {}) or {}
    indent = "  " * depth
    line = ""

    if btype == "paragraph":
        line = _rich_text_to_md(data.get("rich_text", []))
    elif btype in ("heading_1", "heading_2", "heading_3"):
        n = {"heading_1": 1, "heading_2": 2, "heading_3": 3}[btype]
        line = "#" * n + " " + _rich_text_to_md(data.get("rich_text", []))
    elif btype == "bulleted_list_item":
        line = "- " + _rich_text_to_md(data.get("rich_text", []))
    elif btype == "numbered_list_item":
        line = "1. " + _rich_text_to_md(data.get("rich_text", []))
    elif btype == "to_do":
        check = "x" if data.get("checked") else " "
        line = f"- [{check}] " + _rich_text_to_md(data.get("rich_text", []))
    elif btype == "toggle":
        line = "- " + _rich_text_to_md(data.get("rich_text", []))
    elif btype == "code":
        lang = data.get("language", "")
        body = _rich_text_to_md(data.get("rich_text", []))
        line = f"```{lang}\n{body}\n```"
    elif btype == "quote":
        line = "> " + _rich_text_to_md(data.get("rich_text", []))
    elif btype == "callout":
        emoji = (data.get("icon") or {}).get("emoji", "")
        line = f"> {emoji} " + _rich_text_to_md(data.get("rich_text", []))
    elif btype == "divider":
        line = "---"
    elif btype == "child_page":
        # 재귀 처리용으로 누적
        sub_pages.append({"id": block["id"], "title": data.get("title", "Untitled")})
        line = f"**↳ 하위 페이지: {data.get('title','')}**"
    elif btype == "child_database":
        # 인라인 데이터베이스를 마크다운 테이블로 즉시 변환
        db_title = data.get("title", "Untitled Database")
        line = _database_to_md_table(block["id"], notion, title_hint=db_title)
    elif btype == "image":
        f = data.get("external") or data.get("file") or {}
        url = f.get("url", "")
        cap = _rich_text_to_md(data.get("caption", []))
        if url:
            fname = _attachment_filename(url, f"{page_title_prefix}-image")
            attachments.append({"url": url, "filename": fname})
        line = f"![{cap}]({url})" if url else ""
    elif btype in ("file", "pdf", "video", "audio"):
        f = data.get("external") or data.get("file") or {}
        url = f.get("url", "")
        if url:
            fname = data.get("name") or _attachment_filename(url, f"{page_title_prefix}-{btype}")
            fname = _safe_filename(fname)
            attachments.append({"url": url, "filename": fname})
        line = f"[📎 {btype}: {url}]({url})" if url else ""
    elif btype == "bookmark":
        line = f"[{data.get('url','')}]({data.get('url','')})"
    elif btype == "equation":
        line = f"$${data.get('expression','')}$$"
    else:
        line = _rich_text_to_md(data.get("rich_text", []))

    result = indent + line if line else ""

    if block.get("has_children") and btype != "child_page":
        try:
            children = notion.blocks.children.list(block_id=block["id"]).get("results", [])
            child_md = "\n".join(filter(None, (
                _block_to_md(c, notion, depth + 1, attachments, sub_pages, page_title_prefix)
                for c in children
            )))
            if child_md:
                result = (result + "\n" + child_md) if result else child_md
        except Exception:
            pass
    return result


def _page_title(page) -> str:
    props = page.get("properties", {}) or {}
    for v in props.values():
        if v.get("type") == "title":
            return _rich_text_to_md(v.get("title", [])) or "Untitled"
    t = props.get("title", {}).get("title", [])
    return _rich_text_to_md(t) or "Untitled"


def _sync_notion_page_to_md(
    page_id: str,
    include_subpages: bool = True,
    include_attachments: bool = True,
    depth: int = 0,
    max_depth: int = 5,
    visited: Optional[set] = None,
) -> dict:
    """페이지/데이터베이스 → {title, markdown, attachments}.
    page_id가 데이터베이스면 행을 표로 변환 + 각 행 페이지의 본문도 섹션으로 통합.
    하위 페이지는 재귀적으로 같은 markdown에 섹션으로 통합."""
    if visited is None:
        visited = set()
    if page_id in visited or depth > max_depth:
        return {"title": "", "markdown": "", "attachments": []}
    visited.add(page_id)

    notion = NotionClient(auth=NOTION_API_KEY)

    # ID가 데이터베이스(=data_source)인지 페이지인지 자동 감지
    page = None
    is_database = False
    try:
        page = notion.pages.retrieve(page_id=page_id)
    except NotionAPIError:
        # 페이지가 아니면 데이터베이스/데이터소스 시도
        try:
            db_info = _retrieve_db_info(notion, page_id)
            is_database = True
        except NotionAPIError as e:
            raise e

    if is_database:
        db_title = _rich_text_to_md(db_info.get("title", [])) or "Untitled Database"
        heading_level = min(depth + 1, 6)
        table_md = _database_to_md_table(page_id, notion, title_hint=db_title)
        md_parts = [f"{'#' * heading_level} {db_title}", "", table_md]

        # 하위(=각 행 = page) 처리도 옵션에 따라
        if include_subpages and depth < max_depth:
            try:
                child_pages = _query_database_rows(notion, page_id, page_size=100)
            except Exception:
                child_pages = []

            collected_attachments: list = []
            for child in child_pages:
                try:
                    sub = _sync_notion_page_to_md(
                        child["id"], include_subpages, include_attachments,
                        depth + 1, max_depth, visited,
                    )
                    if sub["markdown"]:
                        md_parts.append("\n---\n")
                        md_parts.append(sub["markdown"])
                    collected_attachments.extend(sub["attachments"])
                except Exception as e:
                    md_parts.append(f"\n_(행 페이지 로딩 실패: {e})_\n")

            return {
                "title":       db_title,
                "markdown":    "\n\n".join(md_parts),
                "attachments": collected_attachments if include_attachments else [],
            }

        return {
            "title":       db_title,
            "markdown":    "\n\n".join(md_parts),
            "attachments": [],
        }

    title = _page_title(page)
    safe_prefix = _safe_filename(title)
    attachments: list = []
    sub_pages: list = []

    blocks = notion.blocks.children.list(block_id=page_id).get("results", [])
    body_parts = [
        _block_to_md(b, notion, 0, attachments, sub_pages, safe_prefix)
        for b in blocks
    ]
    body = "\n\n".join(filter(None, body_parts))

    heading_level = min(depth + 1, 6)
    md_parts = [f"{'#' * heading_level} {title}", "", body]

    if include_subpages:
        for sp in sub_pages:
            try:
                sub = _sync_notion_page_to_md(
                    sp["id"], include_subpages, include_attachments,
                    depth + 1, max_depth, visited,
                )
                if sub["markdown"]:
                    md_parts.append("\n---\n")
                    md_parts.append(sub["markdown"])
                attachments.extend(sub["attachments"])
            except Exception as e:
                md_parts.append(f"\n_(하위 페이지 '{sp['title']}' 로딩 실패: {e})_\n")

    return {
        "title":       title,
        "markdown":    "\n\n".join(md_parts),
        "attachments": attachments if include_attachments else [],
    }


def _download_url_to_temp(url: str, suffix: str) -> tuple[str, int]:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        return tmp.name, len(data)


def _item_title(obj) -> str:
    """페이지/데이터베이스 통합 제목 추출"""
    if obj.get("object") in ("database", "data_source"):
        return _rich_text_to_md(obj.get("title", [])) or "Untitled Database"
    return _page_title(obj)


def _sync_notion_search(query: str = "") -> list:
    """Notion 검색: page와 database(=data_source)를 각각 조회해서 합침.
    Notion 2025 API에서 'database' 객체는 'data_source'로 마이그레이션됨.
    필터값도 'database' → 'data_source'로 변경됨."""
    notion = NotionClient(auth=NOTION_API_KEY)
    out = []
    seen_ids = set()

    def _collect(filter_value: str, ui_type: str):
        try:
            res = notion.search(
                query=query,
                filter={"value": filter_value, "property": "object"},
                page_size=100,
            )
        except Exception:
            return
        for o in res.get("results", []):
            if o["id"] in seen_ids:
                continue
            seen_ids.add(o["id"])
            out.append({
                "id":               o["id"],
                "title":            _item_title(o),
                "type":             ui_type,  # 프론트엔드용: "page" or "database"
                "has_children":     True,
                "url":              o.get("url", ""),
                "last_edited_time": o.get("last_edited_time", ""),
            })

    # 데이터베이스(=data_source)를 먼저 (상단에 표시), 그 다음 페이지
    # 구 API 호환을 위해 'database' 필터도 시도
    _collect("data_source", "database")
    _collect("database", "database")
    _collect("page", "page")
    return out


def _retrieve_db_info(notion, db_or_ds_id: str) -> dict:
    """DB 또는 data_source의 메타데이터 조회.
    Notion 2025 API에서는 검색 결과가 data_source ID를 반환하므로 그 경로를 먼저 시도."""
    try:
        return notion.request(method="GET", path=f"data_sources/{db_or_ds_id}")
    except Exception:
        pass
    return notion.databases.retrieve(database_id=db_or_ds_id)


def _query_database_rows(notion, database_id: str, page_size: int = 100) -> list:
    """DB 행 조회. Notion 2025 API의 data_sources 마이그레이션에 대응.
    검색에서 받은 ID는 보통 data_source ID이므로 그 경로를 먼저 시도한다."""
    # 1차 시도: data_sources/{id}/query (Notion 2025)
    try:
        r = notion.request(
            method="POST",
            path=f"data_sources/{database_id}/query",
            body={"page_size": page_size},
        )
        return r.get("results", [])
    except Exception:
        pass

    # 2차 시도: 기존 databases.query (구 API, 단일 소스 DB)
    try:
        res = notion.databases.query(database_id=database_id, page_size=page_size)
        return res.get("results", [])
    except Exception:
        pass

    # 3차 시도: databases.retrieve로 받은 data_sources 목록으로 각각 쿼리
    try:
        db_info = notion.databases.retrieve(database_id=database_id)
    except Exception:
        return []
    data_sources = db_info.get("data_sources") or []
    results = []
    for ds in data_sources:
        ds_id = ds.get("id")
        if not ds_id:
            continue
        try:
            r = notion.request(
                method="POST",
                path=f"data_sources/{ds_id}/query",
                body={"page_size": page_size},
            )
            results.extend(r.get("results", []))
        except Exception:
            continue
    return results


def _sync_notion_children(parent_id: str, parent_type: str) -> list:
    notion = NotionClient(auth=NOTION_API_KEY)
    out = []
    if parent_type == "database":
        rows = _query_database_rows(notion, parent_id, page_size=100)
        for p in rows:
            out.append({
                "id":               p["id"],
                "title":            _item_title(p),
                "type":             "page",
                "has_children":     True,
                "url":              p.get("url", ""),
                "last_edited_time": p.get("last_edited_time", ""),
            })
    else:  # page
        try:
            blocks = notion.blocks.children.list(block_id=parent_id, page_size=100)
        except Exception:
            return []
        for b in blocks.get("results", []):
            bt = b.get("type", "")
            if bt == "child_page":
                out.append({
                    "id":               b["id"],
                    "title":            b.get("child_page", {}).get("title", "Untitled"),
                    "type":             "page",
                    "has_children":     True,
                    "url":              "",
                    "last_edited_time": b.get("last_edited_time", ""),
                })
            elif bt == "child_database":
                out.append({
                    "id":               b["id"],
                    "title":            b.get("child_database", {}).get("title", "Untitled Database"),
                    "type":             "database",
                    "has_children":     True,
                    "url":              "",
                    "last_edited_time": b.get("last_edited_time", ""),
                })
    return out


class NotionUploadIn(BaseModel):
    page_id: str
    title: Optional[str] = None
    include_subpages: bool = True
    include_attachments: bool = True


@app.get("/api/notion/status")
async def notion_status():
    if not NOTION_API_KEY:
        return {"configured": False, "ok": False}
    loop = asyncio.get_running_loop()
    try:
        # 간단한 사용자 정보 호출로 키 유효성 확인
        def _check():
            n = NotionClient(auth=NOTION_API_KEY)
            n.users.me()
        await loop.run_in_executor(executor, _check)
        return {"configured": True, "ok": True}
    except Exception as e:
        return {"configured": True, "ok": False, "error": str(e)[:200]}


@app.get("/api/notion/pages")
async def notion_pages(q: str = ""):
    if not NOTION_API_KEY:
        raise HTTPException(400, "NOTION_API_KEY가 설정되지 않았습니다")
    loop = asyncio.get_running_loop()
    try:
        items = await loop.run_in_executor(executor, _sync_notion_search, q)
    except NotionAPIError as e:
        raise HTTPException(400, f"Notion API 오류: {e}")
    return {"items": items, "pages": items}  # pages: 하위 호환


@app.get("/api/notion/children")
async def notion_children(id: str, type: str = "page"):
    if not NOTION_API_KEY:
        raise HTTPException(400, "NOTION_API_KEY가 설정되지 않았습니다")
    loop = asyncio.get_running_loop()
    try:
        items = await loop.run_in_executor(executor, _sync_notion_children, id, type)
    except NotionAPIError as e:
        raise HTTPException(400, f"Notion API 오류: {e}")
    except Exception as e:
        # 500을 그대로 보내면 클라이언트에서 원인 파악 불가 → 메시지 노출
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"Notion children 조회 실패: {type(e).__name__}: {e}")
    return {"items": items}


@app.post("/api/servers/{svid}/upload-from-notion")
async def upload_from_notion(
    svid: str,
    body: NotionUploadIn,
    background_tasks: BackgroundTasks,
):
    if not NOTION_API_KEY:
        raise HTTPException(400, "NOTION_API_KEY가 설정되지 않았습니다")
    with lock:
        if svid not in servers:
            raise HTTPException(404, "서버를 찾을 수 없습니다")
        existing_names = [f["name"] for f in servers[svid]["files"]]
        key        = servers[svid]["api_key"]
        store_name = servers[svid]["store_name"]

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            executor, _sync_notion_page_to_md,
            body.page_id, body.include_subpages, body.include_attachments,
        )
    except NotionAPIError as e:
        raise HTTPException(400, f"Notion 페이지 가져오기 실패: {e}")
    except Exception as e:
        raise HTTPException(500, f"Notion 변환 실패: {e}")

    title       = result["title"]
    md          = result["markdown"]
    attachments = result["attachments"]

    # 1) 본문 markdown 업로드
    display_name = _safe_filename(body.title or title or "notion-page") + ".md"
    # 중복 회피
    base, ext = display_name.rsplit(".", 1)
    counter = 1
    while display_name in existing_names:
        counter += 1
        display_name = f"{base} ({counter}).{ext}"

    content = md.encode("utf-8")
    if len(content) > 100 * 1024 * 1024:
        raise HTTPException(400, "본문이 100MB를 초과합니다")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".md") as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    queued = []
    fid = str(uuid.uuid4())[:8]
    with lock:
        servers[svid]["files"].append({
            "id": fid, "name": display_name, "size": len(content),
            "status": "uploading", "error": None,
        })
    db_add_file(svid, fid, display_name, len(content))
    background_tasks.add_task(
        _bg_upload, svid, fid, key, store_name,
        tmp_path, display_name, "text/plain",
    )
    queued.append({"file_id": fid, "name": display_name})
    existing_names.append(display_name)

    # 2) 첨부파일 업로드 (각각 별도 파일로)
    skipped: list = []
    for att in attachments:
        att_name = att["filename"]
        # 중복 처리
        if att_name in existing_names:
            base_a, _, ext_a = att_name.rpartition(".")
            i = 1
            while att_name in existing_names:
                i += 1
                att_name = f"{base_a} ({i}).{ext_a}" if ext_a else f"{att_name} ({i})"
        existing_names.append(att_name)

        suffix = Path(att_name).suffix or ".bin"
        try:
            tmp_a, size_a = await loop.run_in_executor(
                executor, _download_url_to_temp, att["url"], suffix,
            )
        except Exception as e:
            skipped.append({"name": att_name, "reason": f"다운로드 실패: {e}"})
            continue

        # Excalidraw → PDF, 그 다음 SVG → PDF (필요 시)
        tmp_a, att_name, size_a = await loop.run_in_executor(
            executor, _maybe_convert_excalidraw, tmp_a, att_name)
        tmp_a, att_name, size_a = await loop.run_in_executor(
            executor, _maybe_convert_svg, tmp_a, att_name)

        if size_a > 100 * 1024 * 1024:
            try: os.unlink(tmp_a)
            except Exception: pass
            skipped.append({"name": att_name, "reason": "100MB 초과"})
            continue

        a_fid = str(uuid.uuid4())[:8]
        with lock:
            servers[svid]["files"].append({
                "id": a_fid, "name": att_name, "size": size_a,
                "status": "uploading", "error": None,
            })
        db_add_file(svid, a_fid, att_name, size_a)
        background_tasks.add_task(
            _bg_upload, svid, a_fid, key, store_name,
            tmp_a, att_name, get_mime(att_name),
        )
        queued.append({"file_id": a_fid, "name": att_name})

    return {
        "queued":     queued,
        "skipped":    skipped,
        "main_file":  display_name,
        "attachments_count": len(attachments),
    }


# TEMP_SVG_DEBUG: 변환된 PDF 목록/다운로드 ─────────────────────────────────────
@app.get("/api/debug/svg-pdfs")
async def list_svg_debug_pdfs():
    items = []
    for p in sorted(SVG_DEBUG_DIR.glob("*.pdf"), key=lambda x: -x.stat().st_mtime):
        st = p.stat()
        items.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
    return {"files": items}


@app.get("/api/debug/svg-pdfs/{name}")
async def get_svg_debug_pdf(name: str):
    # 경로 탈출 방지: 파일명만 허용
    safe = Path(name).name
    fp = SVG_DEBUG_DIR / safe
    if not fp.is_file():
        raise HTTPException(404, "파일 없음")
    return FileResponse(fp, media_type="application/pdf", filename=safe)


@app.delete("/api/debug/svg-pdfs/{name}")
async def delete_svg_debug_pdf(name: str):
    safe = Path(name).name
    fp = SVG_DEBUG_DIR / safe
    if fp.is_file():
        try: fp.unlink()
        except Exception: pass
    return {"status": "deleted"}


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
