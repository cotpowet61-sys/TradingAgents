#!/usr/bin/env bash
# Локальный прогон тестов портала (то же, что делает CI).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-python3}
[ -d ".venv-test" ] || $PY -m venv .venv-test
# shellcheck disable=SC1091
source .venv-test/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r portal/requirements-dev.txt
cd portal
python -m pytest tests -q
