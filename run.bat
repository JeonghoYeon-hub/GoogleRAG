@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================
echo   File RAG System
echo ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found.
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

echo Server: http://localhost:8000
echo Press Ctrl+C or close this window to stop.
echo.

.venv\Scripts\python.exe app.py

echo.
echo Server stopped.
pause
