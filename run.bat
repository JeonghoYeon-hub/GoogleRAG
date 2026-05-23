@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================
echo   File RAG System
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

echo Server: http://localhost:3000
echo Press Ctrl+C or close this window to stop.
echo.

node server/index.js

echo.
echo Server stopped.
pause
