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

    headers = {"Authorization": f"Bearer {cfg['api_key']}"}
    try:
        async with httpx.AsyncClient(timeout=settings.AI_TIMEOUT) as cli:
            r = await cli.post(f"{base}/chat/completions", json=body, headers=headers)
            if r.status_code != 200:
                raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
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
