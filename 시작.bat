@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo ============================================
echo   파일 RAG 시스템
echo ============================================
echo.

REM ── 1. Python 확인 ─────────────────────────
python --version > nul 2>&1
if errorlevel 1 (
    echo [오류] Python이 설치되어 있지 않습니다.
    echo.
    echo https://www.python.org/downloads/  에서 설치 후 다시 실행하세요.
    echo 설치 시 "Add Python to PATH" 체크 필수.
    echo.
    pause
    exit /b 1
)

REM ── 2. 첫 실행 시 의존성 설치 ──────────────
if not exist .setup_done (
    echo [첫 실행 감지] 의존성을 설치합니다. 5~10분 소요됩니다.
    echo.
    echo [1/2] Python 패키지 설치 중...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [오류] pip 설치 실패. 인터넷 연결을 확인하세요.
        pause
        exit /b 1
    )
    echo.
    echo [2/2] Chromium 브라우저 다운로드 중 (약 170MB)...
    python -m playwright install chromium
    if errorlevel 1 (
        echo.
        echo [오류] Chromium 설치 실패.
        pause
        exit /b 1
    )
    echo. > .setup_done
    echo.
    echo ===== 설치 완료 =====
    echo.
)

REM ── 3. 설정 파일 확인 ─────────────────────
if not exist .env (
    echo [경고] .env 파일이 없습니다.
    echo .env.example 을 복사해서 .env 로 만들고 API 키를 입력하세요.
    echo.
    pause
    exit /b 1
)

REM ── 4. 서버 시작 ──────────────────────────
echo [서버 시작]  브라우저에서 http://localhost:8000  를 여세요.
echo (이 창을 닫으면 서버가 종료됩니다)
echo.
python app.py
echo.
echo 서버가 종료되었습니다.
pause
