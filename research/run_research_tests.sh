#!/usr/bin/env bash
# Тесты исследовательского контура (включая сверку признаков с порталом).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
PY="${PYTHON:-python3}"
if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; fi
cd "$ROOT"
PYTHONPATH="$HERE:$ROOT/portal:$ROOT/backend" "$PY" -m pytest "$HERE/tests" -q
