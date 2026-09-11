@echo off
chcp 65001 >nul
REM =====================================================================
REM  TradingAgents — запуск в один клик (Windows)
REM  Просто дважды кликните по этому файлу.
REM =====================================================================
cd /d "%~dp0"

echo ==============================================
echo   TradingAgents — запуск платформы
echo ==============================================
echo.

REM 1) Проверяем Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [!] Python не найден.
    echo     Скачайте и установите: https://www.python.org/downloads/
    echo     ВАЖНО: при установке поставьте галочку "Add Python to PATH".
    pause
    exit /b 1
)

REM 2) Виртуальное окружение
if not exist ".venv" (
    echo [1/3] Создаю виртуальное окружение...
    python -m venv .venv
)
call .venv\Scripts\activate.bat

REM 3) Зависимости
echo [2/3] Устанавливаю зависимости...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r backend\requirements.txt

REM 4) .env
if not exist ".env" (
    copy .env.example .env >nul
    echo       Создан файл .env. Ключи можно ввести позже в интерфейсе.
)

REM 5) Открываем браузер и запускаем
echo [3/3] Запускаю сервер...
echo.
echo   Открываю браузер: http://localhost:3001
echo   (закройте это окно, чтобы остановить)
echo.
start "" http://localhost:3001

cd backend
python main.py

pause
