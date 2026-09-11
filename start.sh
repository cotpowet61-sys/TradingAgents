#!/usr/bin/env bash
# =====================================================================
#  TradingAgents — запуск в один клик (Mac / Linux)
#  Просто запустите:  ./start.sh
# =====================================================================
set -e

cd "$(dirname "$0")"

PY=python3
# если python3 нет, пробуем python
command -v python3 >/dev/null 2>&1 || PY=python

echo "=============================================="
echo "  TradingAgents — запуск платформы"
echo "=============================================="
echo ""

# 1) Виртуальное окружение
if [ ! -d ".venv" ]; then
    echo "[1/3] Создаю виртуальное окружение..."
    $PY -m venv .venv
fi
source .venv/bin/activate

# 2) Зависимости
echo "[2/3] Проверяю зависимости..."
pip install -q --upgrade pip
pip install -q -r backend/requirements.txt

# 3) .env (если нет — копируем пример)
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "      Создан файл .env (из примера). Ключи можно ввести позже в интерфейсе."
fi

# 4) Запуск
echo "[3/3] Запускаю сервер..."
echo ""
echo "  Открываю браузер: http://localhost:3002"
echo "  (нажмите Ctrl+C, чтобы остановить)"
echo ""

# пробуем открыть браузер
( sleep 2; (open http://localhost:3002 2>/dev/null || xdg-open http://localhost:3002 2>/dev/null) ) &

cd backend
python main.py
