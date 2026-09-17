#!/usr/bin/env bash
# ==========================================================================
#  TradeVision Portal - one-click start (Linux/macOS)
#  Creates venv, installs deps, starts the portal on http://localhost:3005
# ==========================================================================
set -e
cd "$(dirname "$0")/.."

PY=python3
command -v $PY >/dev/null 2>&1 || PY=python
command -v $PY >/dev/null 2>&1 || { echo "[ERROR] Python 3.10+ not found."; exit 1; }

if [ ! -d ".venv-portal" ]; then
    echo "[1/3] Creating virtual environment .venv-portal ..."
    $PY -m venv .venv-portal
fi

# shellcheck disable=SC1091
source .venv-portal/bin/activate

echo "[2/3] Installing dependencies..."
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r backend/requirements.txt

echo "[3/3] Starting TradeVision Portal on http://localhost:3005 ..."
echo "    Press Ctrl+C to stop."
( sleep 2; (command -v xdg-open >/dev/null && xdg-open http://localhost:3005) \
    || (command -v open >/dev/null && open http://localhost:3005) || true ) &
exec python portal/server.py
