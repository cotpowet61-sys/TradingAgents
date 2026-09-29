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
  "thinking": ["шаг 1 рассуждения", "шаг 2 рассуждения", "шаг 3 рассуждения"],
  "side": "LONG" | "SHORT" | "NEUTRAL",
  "confidence": число от 0.0 до 1.0,
  "reason": "краткое обоснование на русском (1-3 предложения)",
  "risks": ["риск 1", "риск 2"],
  "key_levels": {{"support": число или null, "resistance": число или null}}
}}

Правила:
- thinking — 2-4 коротких пункта твоего хода размышлений (что ты видишь в данных и почему).
- NEUTRAL ставь, если нет уверенного перевеса в какую-либо сторону.
- Учитывай, что платформа уже рассчитала сигнал по индикаторной стратегии «{strategy}»: {strategy_signal}.
{strategy_note}
- Ты можешь согласиться, усилить или оспорить этот сигнал. Если оспариваешь — объясни в reason.
- Твой главный источник — данные ниже (фичи индикаторов и последние свечи), а не чужой сигнал:
  опирайся на собственный анализ рынка. Если считаешь, что текущему графику лучше подходит
  другой тип стратегии (тренд/канал/сетка/скальп), скажи об этом в reason.
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

    # Как была выбрана индикаторная стратегия (аженту важно знать степень доверия)
    auto = snapshot.get("strategy_auto") or {}
    if auto.get("enabled"):
        src_ru = {
            "optimizer_profile": "по итогам полных сохранённых бэктестов этой монеты",
            "quick_backtest": "быстрым бэктестом ВСЕХ стратегий прямо по свежему графику",
            "fallback": "как универсальный вариант (свежих данных было мало)",
        }.get(auto.get("source"), "автоматически")
        strategy_note = (
            "- Стратегия подобрана платформой АВТОМАТИЧЕСКИ (не вручную) — "
            + src_ru + "."
        )
    else:
        strategy_note = "- Эту стратегию выбрал пользователь вручную."

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
        strategy_note=strategy_note,
    )

    user = (
        f"ТЕХНИЧЕСКИЙ СНИМОК РЫНКА:\n{json.dumps(snapshot.get('features', {}), ensure_ascii=False)}\n\n"
        f"Дополнительные свечные данные (последние бары):\n{snapshot.get('recent', '')}\n\n"
    )

    # Сравнение стратегий на свежем графике — чтобы агент видел альтернативы
    cand = auto.get("candidates") or []
    if cand:
        rows = []
        for i, c in enumerate(cand, 1):
            wr = c.get("win_rate")
            rows.append(
                f"{i}) {c.get('name', c.get('type'))}: сделок {c.get('trades', 0)}, "
                f"winrate {wr if wr is not None else '?'}%, PnL {c.get('pnl_pct', '?')}%"
            )
        user += (
            "СРАВНЕНИЕ СТРАТЕГИЙ НА СВЕЖЕМ ГРАФИКЕ (быстрый бэктест, лучшие сверху):\n"
            + "\n".join(rows) + "\n\n"
        )

    user += "Выдай строгий JSON-вердикт."
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _disabled_verdict(role_key: str) -> dict:
    """Заглушка для выключенного агента (токены не расходуются вообще)."""
    return {
        "side": "NEUTRAL",
        "confidence": 0.0,
        "reason": "Агент выключен пользователем (токены не расходуются)",
        "risks": [],
        "thinking": [],
        "key_levels": {"support": None, "resistance": None},
        "tokens_est": {"prompt": 0, "completion": 0, "total": 0},
        "disabled": True,
    }


async def get_agent_verdict(role_key: str, snapshot: dict) -> dict:
    """Получить вердикт агента. В демо-режиме — детерминированный результат.
    Выключенный агент (AGENT_<ROLE>_ENABLED=false в настройках) не вызывается
    вообще — ни токены, ни время не тратятся; возвращается заглушка."""
    from config import settings as _s
    if not _s.agent_enabled(role_key):
        return _disabled_verdict(role_key)
    client = get_client()
    messages = build_prompt(role_key, snapshot)
    try:
        text = await client.chat(role_key, messages, json_mode=True, seed=hash(snapshot.get("symbol", "")))
        verdict = _parse_json(text)
    except Exception as e:
        text = ""
        verdict = {
            "side": "NEUTRAL",
            "confidence": 0.0,
            "reason": f"Ошибка модели: {str(e)[:120]}",
            "risks": ["Ошибка API"],
            "key_levels": {"support": None, "resistance": None},
        }
    # Оценка расхода токенов (≈ 4 символа = 1 токен; точное значение отдаёт
    # провайдер, здесь приблизительная оценка для прозрачности затрат)
    prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
    verdict["tokens_est"] = {
        "prompt": prompt_chars // 4,
        "completion": len(text) // 4,
        "total": (prompt_chars + len(text)) // 4,
    }
    verdict.setdefault("side", "NEUTRAL")
    verdict.setdefault("confidence", 0.0)
    verdict.setdefault("reason", "")
    verdict.setdefault("risks", [])
    verdict.setdefault("thinking", [])
    verdict.setdefault("key_levels", {"support": None, "resistance": None})
    if not isinstance(verdict["thinking"], list):
        verdict["thinking"] = []
    verdict["thinking"] = [str(t) for t in verdict["thinking"] if str(t).strip()]
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
