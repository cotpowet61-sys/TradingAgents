@echo off
setlocal
cd /d "%~dp0"

echo ==============================================
echo   TradingAgents - platform launcher
echo ==============================================
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [!] Python not found.
    echo     Download and install: https://www.python.org/downloads/
    echo     IMPORTANT: check "Add Python to PATH" during install.
    pause
    exit /b 1
)

if not exist ".venv" (
    echo [1/3] Creating virtual environment...
    python -m venv .venv
)
call .venv\Scripts\activate.bat

echo [2/3] Installing dependencies...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r backend\requirements.txt

if not exist ".env" (
    copy .env.example .env >nul
    echo       Created .env file. You can enter API keys later in the web UI.
)

echo [3/3] Starting server...
echo.
echo   Opening browser: http://localhost:3002
echo   Close this window to stop the server.
echo.
start "TradingAgents" http://localhost:3002

cd backend
python main.py

pause
