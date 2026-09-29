"""
Мини-клиент OpenAI-совместимого endpoint (по умолчанию https://api.justcode.lol/v1)
для Портала. Независим от основной платформы: модель/ключ берутся из portal-конфига.
"""
from __future__ import annotations

import json
from typing import List, Optional

import httpx

from portal_config import settings


class LLMError(Exception):
    pass


def _role_cfg(role: str) -> dict:
    return settings.role_model(role)


# ---------------------------------------------------------------------- #
# Community-AI (SaaS): юзер без своего ключа ходит через бесплатный
# OpenRouter-эндпоинт с дневной квотой. Юзер контекста ставит saas_gate.
# ---------------------------------------------------------------------- #
import contextvars
_SAAS_USER: contextvars.ContextVar = contextvars.ContextVar("llm_free_user", default=None)

FREE_AI_DAILY = 30


def _q_day() -> str:
    import time as _t
    return _t.strftime("%Y-%m-%d", _t.gmtime())


def quota_left(user_id: int) -> int:
    """Сколько бесплатных запросов осталось сегодня."""
    if not user_id:
        return FREE_AI_DAILY
    try:
        import db as _db
        r = _db.q("SELECT n FROM ai_usage WHERE user_id=? AND day=?", (user_id, _q_day()), one=True)
        return max(0, FREE_AI_DAILY - (int(r["n"]) if r else 0))
    except Exception:
        return FREE_AI_DAILY


def _quota_burn(user_id: int) -> None:
    import db as _db
    row = _db.q("SELECT n FROM ai_usage WHERE user_id=? AND day=?", (user_id, _q_day()), one=True)
    if row:
        _db.execute("UPDATE ai_usage SET n=n+1 WHERE user_id=? AND day=?", (user_id, _q_day()))
    else:
        _db.execute("INSERT INTO ai_usage(user_id, day, n) VALUES (?,?,1)", (user_id, _q_day()))


def _free_cfg() -> bool:
    return bool(getattr(settings, "FREE_AI_KEY", "") and getattr(settings, "FREE_AI_MODEL", ""))


def has_key(role: str) -> bool:
    return bool(_role_cfg(role)["api_key"])


async def list_models() -> List[str]:
    """Список моделей endpoint'а (/models) — для выбора в Настройках."""
    base = settings.AI_BASE_URL.rstrip("/")
    key = settings.AI_API_KEY
    if not key:
        raise LLMError("Сначала введите AI-ключ в Настройках")
    headers = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.get(f"{base}/models", headers=headers)
        r.raise_for_status()
        data = r.json()
    items = data.get("data", []) if isinstance(data, dict) else []
    ids = sorted([m.get("id", "") for m in items if isinstance(m, dict) and m.get("id")])
    return ids


async def chat(role: str, messages: List[dict], *,
               json_mode: bool = False,
               max_tokens: Optional[int] = None) -> str:
    """Один запрос chat/completions для роли. Бросает LLMError при проблемах."""
    cfg = _role_cfg(role)
    if not cfg["api_key"]:
        # Community-AI (SaaS): свой ключ отсутствует → бесплатный OpenRouter + квота
        if _free_cfg() and getattr(settings, "SAAS_MODE", False):
            ctx = _SAAS_USER.get()
            left = quota_left(ctx or 0)
            if left <= 0:
                raise LLMError(f"Бесплатный лимит Community-AI исчерпан ({FREE_AI_DAILY}/день). "
                               "Завтра снова доступен — или подключите свой ключ для безлимита.")
            cfg = {"api_key": settings.FREE_AI_KEY,
                   "base_url": (settings.FREE_AI_BASE_URL or "https://openrouter.ai/api/v1"),
                   "model": settings.FREE_AI_MODEL}
            cfg["_quota_burn"] = True
        else:
            raise LLMError(f"Нет API-ключа для роли {role} — введите в Настройках")

    base = cfg["base_url"].rstrip("/")
    body = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": settings.AI_TEMPERATURE,
        "max_tokens": max_tokens or settings.AI_MAX_TOKENS,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    ctx_burn_candidate = _SAAS_USER.get() if cfg.get("_quota_burn") else None
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}
    try:
        async with httpx.AsyncClient(timeout=settings.AI_TIMEOUT) as cli:
            r = await cli.post(f"{base}/chat/completions", json=body, headers=headers)
            if r.status_code != 200:
                raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
        if cfg.get("_quota_burn") and ctx_burn_candidate:
            _quota_burn(ctx_burn_candidate)
        return data["choices"][0]["message"]["content"]
    except LLMError:
        raise
    except Exception as e:
        raise LLMError(str(e)[:300])


def parse_json_verdict(text: str) -> dict:
    """Вытащить JSON-объект из ответа модели (модель могла обернуть в ```)."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    start = t.find("{")
    end = t.rfind("}")
    if start < 0 or end < 0 or end <= start:
        raise LLMError("модель вернула не-JSON")
    try:
        return json.loads(t[start:end + 1])
    except Exception as e:
        raise LLMError(f"невалидный JSON от модели: {e}")


async def test_role(role: str) -> dict:
    """Быстрая проверка связки модель+ключ."""
    cfg = _role_cfg(role)
    if not cfg["api_key"]:
        return {"ok": False, "detail": "нет ключа"}
    try:
        out = await chat(role, [{"role": "user", "content": "Ответь одним словом: ок"}], max_tokens=8)
        return {"ok": True, "detail": out.strip()[:40], "model": cfg["model"]}
    except LLMError as e:
        return {"ok": False, "detail": str(e)[:120], "model": cfg["model"]}
