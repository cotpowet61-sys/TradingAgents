"""
Унифицированный асинхронный клиент к LLM-провайдерам.

Поддерживает:
  * OpenAI-совместимый протокол /chat/completions (OpenRouter, OpenAI, xAI Grok,
    Google Gemini через его OpenAI-совместимый endpoint и т.д.)
  * Native Anthropic Messages API (когда модель начинается с 'anthropic/')

Если ключ не настроен — возвращает детерминированный демо-ответ, чтобы вся
цепочка агентов и UI работали без внешних API (см. config.is_demo()).
"""
from __future__ import annotations

import json
import random
from typing import Any, List, Optional

import httpx

from config import settings


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=settings.AI_TIMEOUT)
        return self._client

    async def aclose(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def chat(
        self,
        role: str,
        messages: List[dict],
        *,
        json_mode: bool = False,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        seed: Optional[int] = None,
    ) -> str:
        """Выполнить запрос модели для заданной роли. Возвращает текст ответа."""
        cfg = settings.model_role(role)
        if not cfg["api_key"]:
            return self._demo_reply(role, messages, seed=seed)

        provider = cfg["provider"]
        if provider == "anthropic":
            return await self._chat_anthropic(cfg, messages, json_mode, temperature, max_tokens)
        return await self._chat_openai(cfg, messages, json_mode, temperature, max_tokens)

    # ------------------------------------------------------------------ #
    # OpenAI-совместимый протокол
    # ------------------------------------------------------------------ #
    async def _chat_openai(self, cfg, messages, json_mode, temperature, max_tokens) -> str:
        payload: dict = {
            "model": cfg["model"],
            "messages": messages,
            "temperature": temperature if temperature is not None else settings.AI_TEMPERATURE,
            "max_tokens": max_tokens or settings.AI_MAX_TOKENS,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        }
        url = cfg["base_url"].rstrip("/") + "/chat/completions"
        http = await self._http()
        try:
            resp = await http.post(url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise LLMError(f"Сетевая ошибка при запросе {cfg['model']}: {e}")

        if resp.status_code != 200:
            raise LLMError(f"Ошибка {cfg['model']} ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as e:
            raise LLMError(f"Некорректный ответ {cfg['model']}: {e}")

    # ------------------------------------------------------------------ #
    # Native Anthropic Messages API
    # ------------------------------------------------------------------ #
    async def _chat_anthropic(self, cfg, messages, json_mode, temperature, max_tokens) -> str:
        # Anthropic ожидает отдельный system + чередование user/assistant
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        conv = [m for m in messages if m["role"] != "system"]

        payload = {
            "model": cfg["model"].split("/", 1)[-1],  # убрать префикс 'anthropic/'
            "max_tokens": max_tokens or settings.AI_MAX_TOKENS,
            "temperature": temperature if temperature is not None else settings.AI_TEMPERATURE,
            "messages": conv,
        }
        if system:
            payload["system"] = system

        headers = {
            "x-api-key": cfg["api_key"],
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        url = cfg["base_url"].rstrip("/") + "/v1/messages"
        http = await self._http()
        try:
            resp = await http.post(url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise LLMError(f"Сетевая ошибка при запросе {cfg['model']}: {e}")

        if resp.status_code != 200:
            raise LLMError(f"Ошибка {cfg['model']} ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        parts = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                parts.append(block["text"])
        return "\n".join(parts)

    async def test(self, role: str) -> dict:
        """Проверка доступности модели роли. Возвращает {ok, latency_ms, sample, error}."""
        import time
        cfg = settings.model_role(role)
        if not cfg["api_key"]:
            return {"ok": False, "latency_ms": None, "sample": None,
                    "error": "Нет ключа — введите API-ключ для этой роли (или глобальный)"}
        t0 = time.time()
        try:
            text = await self.chat(role, [
                {"role": "user", "content": "Ответь одним словом: ок"},
            ], max_tokens=16)
            return {"ok": True, "latency_ms": round((time.time() - t0) * 1000),
                    "sample": text.strip()[:120], "error": None}
        except Exception as e:
            return {"ok": False, "latency_ms": round((time.time() - t0) * 1000),
                    "sample": None, "error": str(e)[:300]}

    # ------------------------------------------------------------------ #
    # Демо-режим (без ключа). Детерминирован по входу, чтобы UI работал офлайн.
    # ------------------------------------------------------------------ #
    def _demo_reply(self, role: str, messages: List[dict], seed: Optional[int]) -> str:
        # Извлекаем краткий контекст из последнего user-сообщения
        user_text = ""
        for m in reversed(messages):
            if m["role"] == "user":
                user_text = m.get("content", "")
                break

        rng = random.Random((seed if seed is not None else 42) ^ hash(role) ^ hash(user_text))

        # Определяем, что просили: JSON-вердикт или текст
        wants_json = "json" in user_text.lower() or "вердикт" in user_text.lower()

        if wants_json:
            sides = ["LONG", "SHORT", "NEUTRAL"]
            side = rng.choices(sides, weights=[0.4, 0.35, 0.25])[0]
            confidence = round(rng.uniform(0.45, 0.85), 2)
            result = {
                "thinking": [
                    "Анализирую тренд, объём и ключевые уровни из снимка.",
                    "Сопоставляю с сигналом индикаторной стратегии.",
                    "Формирую итоговую рекомендацию по стороне.",
                ],
                "side": side,
                "confidence": confidence,
                "reason": f"[DEMO] Модель роли '{role}' отвечает без API-ключа. "
                          f"Настройте ключ в .env, чтобы получать реальный анализ.",
                "risks": ["Высокая волатильность", "Слабый объём", "Новостной риск"],
                "key_levels": {"support": None, "resistance": None},
            }
            return json.dumps(result, ensure_ascii=False)

        return (
            f"[DEMO] Ответ роли '{role}' без API-ключа. "
            f"Контекст ({len(user_text)} симв.): {user_text[:80]}..."
        )


_client: Optional[LLMClient] = None


def get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
