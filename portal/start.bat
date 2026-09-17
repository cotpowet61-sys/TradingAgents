@echo off
REM ==========================================================================
REM  TradeVision Portal - one-click start (Windows)
REM  Creates venv, installs deps, starts the portal on http://localhost:3005
REM ==========================================================================
cd /d "%~dp0\.."

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ from https://python.org
    echo         and re-run this file.
    pause
    exit /b 1
)

if not exist ".venv-portal" (
    echo [1/3] Creating virtual environment .venv-portal ...
    python -m venv .venv-portal
)

call .venv-portal\Scripts\activate.bat

echo [2/3] Installing dependencies...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r backend\requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies. Check your internet connection.
    pause
    exit /b 1
)

echo [3/3] Starting TradeVision Portal on http://localhost:3005 ...
echo     Press Ctrl+C to stop.
start "" http://localhost:3005
python portal\server.py
pause
