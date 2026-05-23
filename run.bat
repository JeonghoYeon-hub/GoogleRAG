@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================
echo   File RAG System (Node.js debug server)
echo ============================================
echo.

if not exist "node_modules" (
    echo [ERROR] node_modules not found.
    echo         Run setup.bat first.
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [WARNING] .env file not found.
    echo           API keys can be configured through the web interface.
    echo.
)

REM ---- ABI check ----------------------------------------------------
REM electron-builder rebuilds better-sqlite3 for Electron's Node ABI.
REM If we just built the Electron app, the host Node can no longer load
REM the binding (NODE_MODULE_VERSION mismatch). Detect and rebuild.
node -e "require('better-sqlite3')" >nul 2>&1
if errorlevel 1 (
    echo Native module ABI mismatch detected. Rebuilding better-sqlite3 ...
    call npm rebuild better-sqlite3
    if errorlevel 1 (
        echo [ERROR] Rebuild failed.
        pause
        exit /b 1
    )
    echo Rebuild complete.
    echo.
)

echo Server: http://localhost:3000
echo Press Ctrl+C or close this window to stop.
echo.

node server/index.js

echo.
echo Server stopped.
pause
