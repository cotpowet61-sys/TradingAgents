@echo off
REM TradeVision Portal — one-click start (Windows): python 3.10+ required.
setlocal
cd /d "%~dp0\.."

if not exist ".venv-portal" (
    echo [1/3] Creating virtual environment .venv-portal ...
    python -m venv .venv-portal || goto :err
)
call .venv-portal\Scripts\activate.bat

echo [2/3] Installing dependencies ...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r portal\requirements.txt || goto :err

echo [3/3] Starting TradeVision Portal on http://localhost:3005 ...
start "" http://localhost:3005
python portal\server.py
goto :eof

:err
echo [ERROR] Setup failed. Check that Python 3.10+ is installed and on PATH.
pause
