"""Контракт конфигурации: всё, что обещано в .env.example, обязано существовать.

Класс дефектов, который этот файл закрывает: ключ есть в шаблоне .env, но поля
в настройках нет (или дефолт кода расходится с примером). Такой рассинхрон
выглядит как «настройка не работает» и ищется часами.
"""
from __future__ import annotations

import re
from pathlib import Path

from conftest import PORTAL_DIR, fresh_env, load

# Ключи, которые читаются не pydantic-моделью, а окружением напрямую
ENV_ONLY = {
    "PORTAL_DATA_DIR",         # portal_config.DATA_DIR
    "PORTAL_ENV_FILE",         # portal_config.ENV_PATH
    "PORTAL_DB_PATH",          # db.DB_PATH
    "PORTAL_DATABASE_URL",     # db.py: Postgres вместо SQLite
    "RATE_LIMIT_DISABLED",     # security.rate_ok (тестовый хук)
    "PORTAL_HOST", "PORTAL_PORT",  # читаются uvicorn-ом и portal_config
}


def _example_keys() -> set[str]:
    text = (Path(PORTAL_DIR) / ".env.example").read_text(encoding="utf-8")
    keys = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip())
    return keys


def _known_names(model) -> set[str]:
    """Имена полей + их env-алиасы (PORTAL_AUTH_SECRET → AUTH_SECRET и т.п.)."""
    names = set(model.model_fields.keys())
    for f in model.model_fields.values():
        alias = f.validation_alias
        if isinstance(alias, str):
            names.add(alias)
        elif alias is not None and hasattr(alias, "choices"):
            names.update(str(c) for c in alias.choices)
    return names


def test_portal_auth_secret_alias_is_wired(monkeypatch):
    """Регрессия: PORTAL_AUTH_SECRET из .env/деплоя обязан попадать в AUTH_SECRET.

    Без алиаса значение игнорировалось, секрет генерировался заново при каждом
    старте, и сохранённые ключи клиентов становились нерасшифровываемыми.
    """
    fresh_env(monkeypatch, auth_secret="env-secret-" + "k" * 24, saas=True)
    portal_config = load("portal_config")
    assert portal_config.settings.AUTH_SECRET.startswith("env-secret-")


def test_every_example_key_exists_in_settings(monkeypatch):
    fresh_env(monkeypatch)
    portal_config = load("portal_config")
    known = _known_names(portal_config.PortalSettings)
    missing = sorted(k for k in _example_keys()
                     if k not in known and k not in ENV_ONLY)
    assert not missing, (
        "В .env.example есть ключи, которых нет в настройках (значения будут молча "
        f"игнорироваться): {missing}")


def test_example_defaults_match_code_defaults(monkeypatch):
    """Значения в примере не должны противоречить дефолтам кода.

    Проверяем только те ключи, где противоречие уже один раз привело к путанице
    (режим продаж, привязка RR, минимальный TP, реальные данные).
    """
    fresh_env(monkeypatch, saas=False)
    portal_config = load("portal_config")
    s = portal_config.settings
    assert s.SAAS_MODE is False           # пример: SAAS_MODE=false (боевой — в deploy.sh)
    assert s.RR_BINDING is False
    assert s.MIN_TP_PCT == 0.5
    assert s.REQUIRE_REAL_DATA is True
    assert s.SCAN_CADENCE_SECONDS == ""
    assert s.TFS_PER_TICK == 1


def test_startup_validation_blocks_broken_saas(monkeypatch):
    """Валидатор старта: SaaS без кошельков/секрета/админ-токена — это стоп."""
    fresh_env(monkeypatch, saas=True, wallets=False)
    portal_config = load("portal_config")
    s = portal_config.settings
    object.__setattr__(s, "AUTH_SECRET", "")
    object.__setattr__(s, "ADMIN_TOKEN", "")
    errors, warns = portal_config.validate_startup(s)
    joined = " | ".join(errors)
    assert "PORTAL_AUTH_SECRET" in joined
    assert "ADMIN_TOKEN" in joined
    assert "кошелёк" in joined or "WALLET" in joined
    # и без UNSAFE_LOCAL старт обязан упасть
    object.__setattr__(s, "UNSAFE_LOCAL", False)
    try:
        portal_config.enforce_startup(s)
        raise AssertionError("должно было отказать в старте")
    except RuntimeError as e:
        assert "не готов" in str(e)


def test_startup_validation_passes_for_ready_config(monkeypatch):
    monkeypatch.setenv("TELEGRAM_URL", "https://t.me/x")
    monkeypatch.setenv("SITE_URL", "https://tv.example.com")
    monkeypatch.setenv("TRC20_WATCH_ONLY_ACK", "true")
    monkeypatch.setenv("AI_API_KEY", "test-key-123")
    fresh_env(monkeypatch, saas=True, wallets=True)
    portal_config = load("portal_config")
    errors, warns = portal_config.validate_startup(portal_config.settings)
    assert errors == []
    assert not [w for w in warns if "кошел" in w.lower() or "AI-ключ" in w]
