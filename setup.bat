@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================
echo   File RAG System - Setup
echo ============================================
echo.

REM ---- Check Node.js ------------------------------------------------
node --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Node.js is required but was not found.
    echo.
    echo   Download from: https://nodejs.org/
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%V in ('node --version') do set "NODE_VER=%%V"
echo [OK] Node.js: !NODE_VER!
echo.

REM ---- Install packages ---------------------------------------------
echo [1/1] Installing packages ...
call npm install
if errorlevel 1 (
    echo.
    echo [ERROR] Package installation failed.
    echo         Check your internet connection and try again.
    pause
    exit /b 1
)
echo [OK] Packages installed.
echo.

echo ============================================
echo   Setup complete. Run run.bat to start.
echo ============================================
echo.
pause
