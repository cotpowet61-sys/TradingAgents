#!/usr/bin/env bash
# ==========================================================================
#  TradeVision Portal — one-click start (Linux/macOS)
#  Creates venv, installs PORTAL dependencies, starts the portal on :3005
#  Extra args are passed through, e.g.  ./start.sh --no-broker
# ==========================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

PY=python3
command -v $PY >/dev/null 2>&1 || PY=python
command -v $PY >/dev/null 2>&1 || { echo "[ERROR] Python 3.10+ not found."; exit 1; }

NO_BROKER=0
for arg in "$@"; do
    case "$arg" in
        --no-broker) NO_BROKER=1 ;;
        *) echo "[warn] неизвестный аргумент: $arg (известен: --no-broker)" ;;
    esac
done

if [ ! -d ".venv-portal" ]; then
    echo "[1/3] Creating virtual environment .venv-portal ..."
    $PY -m venv .venv-portal
fi

# shellcheck disable=SC1091
source .venv-portal/bin/activate

echo "[2/3] Installing dependencies (portal/requirements.txt) ..."
python -m pip install --quiet --upgrade pip
# ВАЖНО: именно требования портала. backend/requirements.txt не содержит
# cryptography, без которой не работает шифрование ключей клиентов.
python -m pip install --quiet -r portal/requirements.txt

echo "[3/3] Starting TradeVision Portal on http://localhost:3005 ..."
if [ "$NO_BROKER" = "1" ]; then
    export BILLING_WATCHER=false
    echo "      (--no-broker: наблюдатель за блокчейном выключен)"
fi
echo "    Press Ctrl+C to stop."
( sleep 2; (command -v xdg-open >/dev/null && xdg-open http://localhost:3005) \
    || (command -v open >/dev/null && open http://localhost:3005) || true ) &
exec python portal/server.py
