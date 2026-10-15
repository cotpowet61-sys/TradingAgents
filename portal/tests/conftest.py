"""Общая обвязка тестов: изолированный DATA_DIR, свой .env, свежий импорт модулей.

Каждый тестовый модуль получает отдельный временный каталог данных и собственные
переменные окружения, поэтому тесты не трогают portal/.env и реальную БД.
Порядок важен: env выставляется ДО первого импорта portal-модулей, иначе
portal_config прочитает чужой .env.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

PORTAL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PORTAL_DIR.parent
BACKEND_DIR = REPO_ROOT / "backend"

for p in (str(PORTAL_DIR), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


def fresh_env(monkeypatch, *, saas: bool = True, auth_secret: str = "test-secret-" + "x" * 24,
              admin_token: str = "admin-token-" + "y" * 20, wallets: bool = True,
              disable_rate_limit: bool = True) -> Path:
    """Изолированное окружение + свежий импорт модулей портала.

    Возвращает путь к временному DATA_DIR. Все модули портала перезагружаются,
    чтобы настройки и пути к БД не «протекали» между тестами.
    """
    tmp = Path(tempfile.mkdtemp(prefix="tv-test-"))
    monkeypatch.setenv("PORTAL_DB_PATH", str(tmp / "saas.db"))
    monkeypatch.setenv("PORTAL_DATA_DIR", str(tmp))        # журналы/кэши тоже изолируем
    monkeypatch.setenv("PORTAL_ENV_FILE", str(tmp / ".env"))  # не читаем рабочий portal/.env
    monkeypatch.setenv("BILLING_WATCHER", "false")          # в тестах не опрашиваем блокчейн
    monkeypatch.setenv("PORTAL_AUTH_SECRET", auth_secret)
    monkeypatch.setenv("ADMIN_TOKEN", admin_token)
    monkeypatch.setenv("SAAS_MODE", "true" if saas else "false")
    monkeypatch.setenv("UNSAFE_LOCAL", "true")          # тесты не должны валиться на «боевых» проверках
    if disable_rate_limit:
        monkeypatch.setenv("RATE_LIMIT_DISABLED", "1")  # по умолчанию лимиты не мешают
    else:
        monkeypatch.delenv("RATE_LIMIT_DISABLED", raising=False)
    monkeypatch.setenv("LIVE_SCANNER", "false")         # фоновый сканер в тестах не ходит в сеть
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")   # заголовки прокси в тестах контролируем сами
    if wallets:
        monkeypatch.setenv("WALLET_TRC20", "TTestWallet0000000000000000000000001")
    # чистим кэш модулей портала
    for name in list(sys.modules):
        if name.split(".")[0] in {
            "portal_config", "db", "authn", "billing", "partners", "security", "council",
            "live_engine", "signal_learner", "signal_gates", "trades_analytics", "keyvault",
            "saas_routes", "server", "llm_client", "trade_context", "bybit_account", "backtest_local",
            "signal_backtest", "tg_bot", "data", "strategies", "agents",
        }:
            del sys.modules[name]
    return tmp


def load(name: str):
    """Свежий импорт модуля портала (после fresh_env)."""
    return importlib.import_module(name)


def make_client(monkeypatch, **env_kwargs):
    """TestClient поверх свежего приложения. Возвращает (client, модули)."""
    fresh_env(monkeypatch, **env_kwargs)
    server = importlib.import_module("server")
    from fastapi.testclient import TestClient
    client = TestClient(server.app, base_url="http://testserver")
    return client, server
