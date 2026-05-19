@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================
echo   File RAG System - Setup
echo ============================================
echo.

REM ---- Find Python --------------------------------------------------
set "PYTHON_EXE="

py --version >nul 2>&1
if not errorlevel 1 set "PYTHON_EXE=py"

if "!PYTHON_EXE!"=="" (
    python --version >nul 2>&1
    if not errorlevel 1 set "PYTHON_EXE=python"
)

if "!PYTHON_EXE!"=="" (
    python3 --version >nul 2>&1
    if not errorlevel 1 set "PYTHON_EXE=python3"
)

if "!PYTHON_EXE!"=="" call :find_python

if "!PYTHON_EXE!"=="" (
    echo [ERROR] Python 3.8 or later is required but was not found.
    echo.
    echo   Download from: https://www.python.org/downloads/
    echo   During installation, check "Add Python to PATH".
    echo.
    pause
    exit /b 1
)
echo [OK] Python: !PYTHON_EXE!
echo.

REM ---- Step 1: Create virtual environment ---------------------------
set "VENV_PY=.venv\Scripts\python.exe"

if exist "!VENV_PY!" (
    echo [1/4] Virtual environment already exists.
) else (
    echo [1/4] Creating virtual environment ...
    "!PYTHON_EXE!" -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [OK] Virtual environment created.
)
echo.

REM ---- Step 2: Bootstrap pip ----------------------------------------
echo [2/4] Checking pip ...
"!VENV_PY!" -m ensurepip --upgrade >nul 2>&1
"!VENV_PY!" -m pip install --upgrade pip --quiet
echo [OK] pip ready.
echo.

REM ---- Step 3: Install packages -------------------------------------
echo [3/4] Installing packages (this may take 5-10 minutes) ...
"!VENV_PY!" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Package installation failed.
    echo         Check your internet connection and try again.
    pause
    exit /b 1
)
echo [OK] Packages installed.
echo.

REM ---- Step 4: Download Playwright Chromium -------------------------
echo [4/4] Downloading Chromium browser (about 170 MB) ...
"!VENV_PY!" -m playwright install chromium
if errorlevel 1 (
    echo.
    echo [ERROR] Chromium download failed.
    pause
    exit /b 1
)
echo [OK] Chromium ready.
echo.

echo ============================================
echo   Setup complete. Run run.bat to start.
echo ============================================
echo.
pause
goto :eof

:find_python
set "_la=%LOCALAPPDATA%"
set "_pf=%ProgramFiles%"
for %%V in (314 313 312 311 310 39 38) do (
    if "!PYTHON_EXE!"=="" if exist "!_la!\Programs\Python\Python%%V\python.exe" (
        set "PYTHON_EXE=!_la!\Programs\Python\Python%%V\python.exe"
    )
    if "!PYTHON_EXE!"=="" if exist "C:\Python%%V\python.exe" (
        set "PYTHON_EXE=C:\Python%%V\python.exe"
    )
    if "!PYTHON_EXE!"=="" if exist "!_pf!\Python%%V\python.exe" (
        set "PYTHON_EXE=!_pf!\Python%%V\python.exe"
    )
)
if "!PYTHON_EXE!"=="" if exist "%USERPROFILE%\Anaconda3\python.exe" (
    set "PYTHON_EXE=%USERPROFILE%\Anaconda3\python.exe"
)
if "!PYTHON_EXE!"=="" if exist "%USERPROFILE%\Miniconda3\python.exe" (
    set "PYTHON_EXE=%USERPROFILE%\Miniconda3\python.exe"
)
goto :eof
