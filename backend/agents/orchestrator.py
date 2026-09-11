"""
Оркестратор мультиагентной системы.

Два режима:
  1) analyze(): полный консенсус 5 агентов по одному символу (для live-сигналов и UI).
  2) scan_market(): прогон по всем символам, отбор тех, где консенсус даёт сигнал.

Консенсус: средневзвешенная уверенность по стороне (с весом риск-менеджера).
Сигнал публикуется, если уверенность >= MIN_CONFIDENCE и сторона != NEUTRAL.
Все ограничения (<=30 сделок/день, TP>=2%, удержание<=4ч) соблюдаются стратегией/движком.
"""
from __future__ import annotations

import asyncio
import json
from typing import List, Optional

from config import settings
from data.market_data import MarketData, Candle
from strategies.base import Signal, SignalSide
from strategies.registry import build_strategy
from strategies import indicators as ta
from . import roles


class Orchestrator:
    def __init__(self):
        self.market = MarketData()
        self.lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Анализ одного символа
    # ------------------------------------------------------------------ #
    async def analyze(self, symbol: str, strategy_type: str = "momentum_breakout",
                      timeframe: str = None, params: Optional[dict] = None) -> dict:
        timeframe = timeframe or settings.DEFAULT_TIMEFRAME
        candles = await self.market.get_klines(symbol, timeframe)
        strategy = build_strategy(strategy_type, params=params)

        # Индикаторный сигнал стратегии
        strat_sig: Signal = strategy.generate(candles, len(candles) - 1) if candles else None

        features = ta.compute_feature_summary(candles)
        recent = self._recent_bars(candles, 5)

        snapshot = {
            "symbol": symbol,
            "timeframe": timeframe,
            "max_trades": settings.MAX_TRADES_PER_DAY,
            "min_tp": settings.MIN_TP_PCT,
            "max_hold": settings.MAX_HOLD_HOURS,
            "sl": settings.DEFAULT_SL_PCT,
            "strategy": strategy.name,
            "strategy_signal": strat_sig.side.value + f" (conf={strat_sig.confidence})" if strat_sig else "нет данных",
            "features": features,
            "recent": recent,
        }

        # Запускаем 5 агентов параллельно
        keys = ["strategist", "momentum", "contrarian", "macro", "risk"]
        results = await asyncio.gather(*(roles.get_agent_verdict(k, snapshot) for k in keys))
        verdicts = dict(zip(keys, results))

        consensus = self._consensus(verdicts, strat_sig)
        final = self._finalize(symbol, timeframe, strat_sig, consensus)

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": features.get("price"),
            "features": features,
            "strategy": strategy.config_dict(),
            "strategy_signal": strat_sig.to_dict() if strat_sig else None,
            "agents": [
                {
                    "role": k,
                    "name": roles.ROLES[k].name,
                    "icon": roles.ROLES[k].icon,
                    "model": settings.model_role(k)["model"],
                    "demo": settings.model_role(k)["api_key"] == "",
                    **v,
                }
                for k, v in verdicts.items()
            ],
            "consensus": consensus,
            "signal": final,
        }

    def _recent_bars(self, candles: List[Candle], n: int) -> str:
        if not candles:
            return ""
        lines = []
        for c in candles[-n:]:
            lines.append(f"O={c.open} H={c.high} L={c.low} C={c.close} V={c.volume}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Консенсус
    # ------------------------------------------------------------------ #
    def _consensus(self, verdicts: dict, strat_sig: Optional[Signal]) -> dict:
        weights = {
            "strategist": 1.2, "momentum": 1.2, "contrarian": 0.8,
            "macro": 0.6, "risk": 1.5,
        }
        long_score = short_score = 0.0   # взвешенные по уверенности и весу роли
        w_dir = 0.0                      # суммарный вес «направленных» голосов (не NEUTRAL)
        total_w = 0.0
        detail = {}
        for k, v in verdicts.items():
            w = weights.get(k, 1.0)
            total_w += w
            conf = v.get("confidence", 0.0)
            if v.get("side") == "LONG":
                long_score += conf * w
                w_dir += w
            elif v.get("side") == "SHORT":
                short_score += conf * w
                w_dir += w
            detail[k] = {"side": v.get("side"), "confidence": conf}

        # Вес индикаторному сигналу (связка «стратегия + AI»)
        if strat_sig and strat_sig.side in (SignalSide.LONG, SignalSide.SHORT):
            w = 1.0
            total_w += w
            w_dir += w
            if strat_sig.side == SignalSide.LONG:
                long_score += strat_sig.confidence * w
            else:
                short_score += strat_sig.confidence * w
            detail["strategy"] = {"side": strat_sig.side.value, "confidence": strat_sig.confidence}

        # Наклон с учётом всех голосов (включая NEUTRAL) — для отображения
        bull_bear = (long_score - short_score) / total_w if total_w else 0.0

        if long_score > short_score:
            side = "LONG"
        elif short_score > long_score:
            side = "SHORT"
        else:
            side = "NEUTRAL"

        # Уверенность = чистый перевес среди голосов «с мнением» (0..1).
        # NEUTRAL-голоса не размывают уверенность, но отражаются в bull_bear.
        margin = abs(long_score - short_score)
        confidence = (margin / w_dir) if w_dir > 0 else 0.0
        # Пересчёт перевеса в той же шкале, что и confidence (для согласованности UI)
        long_conf = (long_score / w_dir) if w_dir else 0.0
        short_conf = (short_score / w_dir) if w_dir else 0.0

        return {
            "side": side,
            "confidence": round(min(confidence, 1.0), 3),
            "bull_bear": round(bull_bear, 3),
            "detail": detail,
            "agreement": round(min(confidence, 1.0), 3),
        }

    def _finalize(self, symbol: str, timeframe: str, strat_sig: Optional[Signal], consensus: dict) -> dict:
        """Формируем финальный сигнал с конкретными уровнями входа/TP/SL."""
        if consensus["side"] not in ("LONG", "SHORT") or consensus["confidence"] < settings.MIN_CONFIDENCE:
            return {
                "side": "NEUTRAL", "entry": None, "tp": None, "sl": None,
                "tp_pct": settings.MIN_TP_PCT, "sl_pct": settings.DEFAULT_SL_PCT,
                "confidence": consensus["confidence"],
                "action": "WAIT",
                "reason": f"Консенсус не достиг порога уверенности {settings.MIN_CONFIDENCE}",
            }
        # Используем уровни из стратегии, если есть, иначе по текущей цене
        if strat_sig and strat_sig.side.value == consensus["side"]:
            entry, tp, sl = strat_sig.entry, strat_sig.tp, strat_sig.sl
        else:
            # entry по цене из features (передадим через signal позже в сканере)
            entry = None
            tp = sl = None
        side = consensus["side"]
        if side == "LONG" and not settings.ALLOW_LONG:
            return self._deny("Лонг отключён настройками")
        if side == "SHORT" and not settings.ALLOW_SHORT:
            return self._deny("Шорт отключён настройками")
        return {
            "side": side, "entry": entry, "tp": tp, "sl": sl,
            "tp_pct": settings.MIN_TP_PCT, "sl_pct": settings.DEFAULT_SL_PCT,
            "confidence": consensus["confidence"],
            "action": "BUY" if side == "LONG" else "SELL",
            "reason": f"Консенсус {consensus['confidence']} по стороне {side}",
        }

    def _deny(self, reason: str) -> dict:
        return {"side": "NEUTRAL", "entry": None, "tp": None, "sl": None,
                "tp_pct": settings.MIN_TP_PCT, "sl_pct": settings.DEFAULT_SL_PCT,
                "confidence": 0.0, "action": "WAIT", "reason": reason}

    # ------------------------------------------------------------------ #
    # Скан всего рынка
    # ------------------------------------------------------------------ #
    async def scan_market(self, symbols: Optional[List[str]] = None, strategy_type: str = "momentum_breakout",
                          timeframe: str = None) -> list[dict]:
        symbols = symbols or settings.symbols
        timeframe = timeframe or settings.DEFAULT_TIMEFRAME

        # Ограничиваем параллелизм, чтобы не перегружать API
        sem = asyncio.Semaphore(4)

        async def one(sym):
            async with sem:
                try:
                    return await self.analyze(sym, strategy_type, timeframe)
                except Exception as e:
                    return {"symbol": sym, "error": str(e), "signal": {"side": "NEUTRAL", "action": "WAIT"}}

        results = await asyncio.gather(*(one(s) for s in symbols))
        # Сортируем: сначала сигналы, по убыванию уверенности
        with_signal = [r for r in results if r.get("signal", {}).get("action") in ("BUY", "SELL")]
        rest = [r for r in results if r not in with_signal]
        with_signal.sort(key=lambda r: -r["signal"]["confidence"])
        return with_signal + rest


_orchestrator: Optional[Orchestrator] = None


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator
