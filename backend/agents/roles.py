"""
Определения ролей агентов: системные промпты на русском и правила выдачи вердикта.

Каждый AI-агент получает компактный технический снимок рынка и обязан вернуть строгий
JSON-вердикт {side, confidence, reason, risks, key_levels}.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from llm.client import get_client

SYSTEM_TEMPLATE = """Ты — {name} в профессиональной мультиагентной системе дэйтрейдинга криптовалют.

Твоя специализация: {expertise}

КОНТЕКСТ СИСТЕМЫ:
- Торгуем {symbol} на таймфрейме {timeframe}.
- Цель стратегии: внутридневные сделки, до {max_trades} сделок в день, как лонг, так и шорт.
- Целевой Take-Profit не меньше {min_tp}% движения цены, удержание сделки не дольше {max_hold} часов.
- Stop-Loss по умолчанию {sl}% (соотношение RR к цели примерно 1:2).

ТВОЯ ЗАДАЧА:
Оцени приведённый технический снимок рынка и выдай РЕКОМЕНДАЦИЮ.

ОТВЕЧАЙ СТРОГО JSON-объектом без пояснений, в таком формате:
{{
  "side": "LONG" | "SHORT" | "NEUTRAL",
  "confidence": число от 0.0 до 1.0,
  "reason": "краткое обоснование на русском (1-3 предложения)",
  "risks": ["риск 1", "риск 2"],
  "key_levels": {{"support": число или null, "resistance": число или null}}
}}

Правила:
- NEUTRAL ставь, если нет уверенного перевеса в какую-либо сторону.
- Учитывай, что платформа уже рассчитала сигнал по индикаторной стратегии «{strategy}»: {strategy_signal}.
- Ты можешь согласиться, усилить или оспорить этот сигнал. Если оспариваешь — объясни в reason.
- confidence — твоя субъективная уверенность, не обязательно равна сигналу стратегии.
"""


@dataclass
class Role:
    key: str
    name: str
    icon: str
    expertise: str


ROLES = {
    "strategist": Role("strategist", "Ведущий стратег", "🧠",
        "формирование общего торгового плана и синтез мнений всех агентов"),
    "momentum": Role("momentum", "Аналитик моментума", "⚡",
        "поиск точек входа по импульсу, тренду и объёму"),
    "contrarian": Role("contrarian", "Контрариан / сентимент", "🦅",
        "проверка идеи от обратного, поиск перегрева рынка и ложных пробоев"),
    "macro": Role("macro", "Макро-контекст", "💎",
        "оценка общего рыночного фона, ликвидности и рисков события"),
    "risk": Role("risk", "Риск-менеджер", "🛡️",
        "финальная проверка соотношения риск/прибыль, размера позиции и стопа"),
}


def build_prompt(role_key: str, snapshot: dict) -> list[dict]:
    role = ROLES[role_key]
    system = SYSTEM_TEMPLATE.format(
        name=role.name,
        expertise=role.expertise,
        symbol=snapshot.get("symbol", "?"),
        timeframe=snapshot.get("timeframe", "?"),
        max_trades=snapshot.get("max_trades", 30),
        min_tp=snapshot.get("min_tp", 2.0),
        max_hold=snapshot.get("max_hold", 4.0),
        sl=snapshot.get("sl", 1.0),
        strategy=snapshot.get("strategy", "-"),
        strategy_signal=snapshot.get("strategy_signal", "-"),
    )
    user = (
        f"ТЕХНИЧЕСКИЙ СНИМОК РЫНКА:\n{json.dumps(snapshot.get('features', {}), ensure_ascii=False)}\n\n"
        f"Дополнительные свечные данные (последние бары):\n{snapshot.get('recent', '')}\n\n"
        f"Выдай строгий JSON-вердикт."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


async def get_agent_verdict(role_key: str, snapshot: dict) -> dict:
    """Получить вердикт агента. В демо-режиме — детерминированный результат."""
    client = get_client()
    messages = build_prompt(role_key, snapshot)
    try:
        text = await client.chat(role_key, messages, json_mode=True, seed=hash(snapshot.get("symbol", "")))
        verdict = _parse_json(text)
    except Exception as e:
        verdict = {
            "side": "NEUTRAL",
            "confidence": 0.0,
            "reason": f"Ошибка модели: {str(e)[:120]}",
            "risks": ["Ошибка API"],
            "key_levels": {"support": None, "resistance": None},
        }
    verdict.setdefault("side", "NEUTRAL")
    verdict.setdefault("confidence", 0.0)
    verdict.setdefault("reason", "")
    verdict.setdefault("risks", [])
    verdict.setdefault("key_levels", {"support": None, "resistance": None})
    verdict["side"] = str(verdict["side"]).upper()
    if verdict["side"] not in ("LONG", "SHORT", "NEUTRAL"):
        verdict["side"] = "NEUTRAL"
    try:
        verdict["confidence"] = max(0.0, min(1.0, float(verdict["confidence"])))
    except (TypeError, ValueError):
        verdict["confidence"] = 0.0
    return verdict


def _parse_json(text: str) -> dict:
    text = text.strip()
    # Убираем возможные markdown-обёртки ```json ... ```
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {}
    return json.loads(text[start:end + 1])
