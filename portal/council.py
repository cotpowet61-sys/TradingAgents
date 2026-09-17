"""
Совет AI-агентов Портала: снимок рынка с Bybit + авто-подбор стратегии по
графику (переиспользует модули основной платформы) + вызовы моделей через
justcode endpoint из НАСТРОЕК ПОРТАЛА (свои ключи/модели, свои тумблеры).

Всё считается на лету, ничего не кэшируется между запусками анализа,
кроме внутреннего кэша подбора стратегии (15 мин, модуль селектора).
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Optional

# Переиспользуем модули основной платформы (данные Bybit, индикаторы,
# стратегии, промпты ролей) — читаем их как библиотеку.
_BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from data.market_data import MarketData                      # noqa: E402
from strategies import indicators as ta                      # noqa: E402
from strategies.registry import build_strategy               # noqa: E402
from strategies.selector import get_selector                 # noqa: E402
from agents import roles as backend_roles                    # noqa: E402

from portal_config import settings                           # noqa: E402
from llm_client import chat, has_key, LLMError, parse_json_verdict  # noqa: E402

COUNCIL_ROLES = ["strategist", "momentum", "contrarian", "macro", "risk"]

_market = MarketData()


def get_market() -> MarketData:
    return _market


def role_meta(role: str) -> dict:
    if role in backend_roles.ROLES:
        r = backend_roles.ROLES[role]
        return {"name": r.name, "icon": r.icon}
    return {"name": "Аналитик сделок", "icon": "💼"}


# ---------------------------------------------------------------------- #
# Снимок рынка
# ---------------------------------------------------------------------- #
async def build_snapshot(symbol: str, timeframe: str) -> dict:
    candles = await _market.get_klines(symbol, timeframe)

    strategy_type, candidates, src = get_selector().auto_pick(symbol, timeframe, candles)
    strategy = build_strategy(strategy_type)
    strat_sig = strategy.generate(candles, len(candles) - 1) if (strategy and candles) else None

    features = ta.compute_feature_summary(candles)
    recent = ""
    for c in (candles[-5:] if candles else []):
        recent += f"O={c.open} H={c.high} L={c.low} C={c.close} V={c.volume}\n"

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "max_trades": settings.MAX_TRADES_PER_DAY,
        "min_tp": settings.MIN_TP_PCT,
        "max_hold": settings.MAX_HOLD_HOURS,
        "sl": settings.DEFAULT_SL_PCT,
        "strategy": strategy.name if strategy else strategy_type,
        "strategy_auto": {"enabled": True, "source": src, "candidates": candidates},
        "strategy_signal": strat_sig.side.value + f" (conf={strat_sig.confidence})" if strat_sig else "нет данных",
        "strategy_signal_obj": strat_sig.to_dict() if strat_sig else None,
        "features": features,
        "recent": recent,
        "candles": candles,
        "price": features.get("price"),
    }


# ---------------------------------------------------------------------- #
# Вердикты
# ---------------------------------------------------------------------- #
def _tokens_est(messages, text) -> dict:
    p = sum(len(str(m.get("content", ""))) for m in messages) // 4
    c = len(text) // 4
    return {"prompt": p, "completion": c, "total": p + c}


def _demo_verdict(role: str, snapshot: dict) -> dict:
    """Без ключа — детерминированный «макет» вердикта из индикаторов."""
    f = snapshot.get("features", {})
    rsi = f.get("rsi", 50) or 50
    chg = f.get("change_pct", 0) or 0
    side = "LONG" if (rsi < 35 or chg > 3) else "SHORT" if (rsi > 65 or chg < -3) else "NEUTRAL"
    conf = min(0.75, abs(rsi - 50) / 50 + abs(chg) / 20)
    return {
        "side": side,
        "confidence": round(conf, 2),
        "reason": "Демо-оценка по индикаторам (RSI {0}, движение {1}%). Введите AI-ключ в Настройках — агенты начнут рассуждать по-настоящему.".format(rsi, chg),
        "risks": ["Демо-режим: вердикт не от модели"],
        "thinking": [f"RSI = {rsi}", f"Движение 24ч = {chg}%"],
        "key_levels": {"support": f.get("low"), "resistance": f.get("high")},
        "demo": True,
        "tokens_est": {"prompt": 0, "completion": 0, "total": 0},
    }


def _disabled_verdict(role: str) -> dict:
    return {
        "side": "NEUTRAL", "confidence": 0.0,
        "reason": "Агент выключен тумблером (токены не тратятся)",
        "risks": [], "thinking": [],
        "key_levels": {"support": None, "resistance": None},
        "disabled": True, "tokens_est": {"prompt": 0, "completion": 0, "total": 0},
    }


async def get_verdict(role: str, snapshot: dict) -> dict:
    """Вердикт одного агента: уважает тумблеры и наличие ключа Портала."""
    if not settings.role_enabled(role):
        return _disabled_verdict(role)
    if not has_key(role):
        return _demo_verdict(role, snapshot)

    messages = backend_roles.build_prompt(role, snapshot)
    try:
        text = await chat(role, messages, json_mode=True)
        verdict = parse_json_verdict(text)
    except Exception as e:
        return {
            "side": "NEUTRAL", "confidence": 0.0,
            "reason": f"Ошибка модели: {str(e)[:140]}",
            "risks": ["Ошибка API"], "thinking": [],
            "key_levels": {"support": None, "resistance": None},
            "error": True, "tokens_est": _tokens_est(messages, ""),
        }
    verdict.setdefault("side", "NEUTRAL")
    verdict.setdefault("confidence", 0.0)
    verdict.setdefault("reason", "")
    verdict.setdefault("thinking", [])
    verdict.setdefault("risks", [])
    verdict["tokens_est"] = _tokens_est(messages, text)
    return verdict


def view(role: str, verdict: dict) -> dict:
    meta = role_meta(role)
    cfg = settings.role_model(role)
    return {
        "role": role,
        "name": meta["name"],
        "icon": meta["icon"],
        "model": cfg["model"],
        "demo": not has_key(role),
        "enabled": settings.role_enabled(role),
        **verdict,
    }


def consensus(verdicts: dict, strategy_signal: Optional[dict]) -> dict:
    weights = {"strategist": 1.2, "momentum": 1.2, "contrarian": 0.8, "macro": 0.6, "risk": 1.5}
    long_s = short_s = 0.0
    w_dir = total_w = 0.0
    detail = {}
    for k, v in verdicts.items():
        if v.get("disabled"):
            detail[k] = {"side": "NEUTRAL", "confidence": 0.0, "disabled": True}
            continue
        w = weights.get(k, 1.0)
        total_w += w
        conf = float(v.get("confidence") or 0)
        side = v.get("side")
        if side == "LONG":
            long_s += conf * w
            w_dir += w
        elif side == "SHORT":
            short_s += conf * w
            w_dir += w
        detail[k] = {"side": side, "confidence": conf}

    if strategy_signal and strategy_signal.get("side") in ("LONG", "SHORT"):
        w = 1.0
        total_w += w
        w_dir += w
        conf = float(strategy_signal.get("confidence") or 0)
        if strategy_signal["side"] == "LONG":
            long_s += conf * w
        else:
            short_s += conf * w
        detail["strategy"] = {"side": strategy_signal["side"], "confidence": conf}

    side = "LONG" if long_s > short_s else "SHORT" if short_s > long_s else "NEUTRAL"
    margin = abs(long_s - short_s)
    confidence = (margin / w_dir) if w_dir > 0 else 0.0
    return {
        "side": side,
        "confidence": round(confidence, 3),
        "bull_bear": round((long_s - short_s) / total_w if total_w else 0.0, 3),
        "detail": detail,
        "action": "RIDE" if confidence >= settings.MIN_CONFIDENCE and side != "NEUTRAL" else "WAIT",
    }
