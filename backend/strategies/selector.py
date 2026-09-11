"""
Оптимизатор и селектор стратегий.

Задача:
  1) Прогнать ВСЕ стратегии на символе (бэктест) и ранжировать их по качеству.
  2) Выбрать лучшие стратегии под конкретную монету (адаптивная торговля).
  3) Сохранить «профиль» монеты (лучшие стратегии + метрики) в JSON.
  4) Live-движок использует профиль: для каждой монеты берётся её лучшая стратегия.

Критерий отбора (чтобы не ловить переобучение на 2 сделках):
  - сделок >= min_trades (по умолчанию 10)
  - win_rate >= min_win_rate (по умолчанию 55%)
  - ранжирование по композитному score = win_rate + ожидание (pnl/сделку)

⚠️ Честно: метрики зависят от данных. На синтетических (демо) данных результат
будет хорошим почти всегда; на реальном рынке win rate выше ~60-65% при TP=2%/SL=1%
достижим редко. Оптимизатор помогает выбрать лучшие из имеющихся, но не гарантирует 70%+.
"""
from __future__ import annotations

import json
import math
import os
import time
from typing import List, Optional

from config import settings, PROJECT_ROOT
from data.market_data import MarketData
from strategies.registry import build_strategy, STRATEGY_SCHEMAS
from backtest.engine import run_backtest

PROFILE_PATH = os.path.join(PROJECT_ROOT, "data", "strategy_profiles.json")


class StrategySelector:
    def __init__(self):
        self.market = MarketData()
        self.profiles: dict = {}          # {key: {strategy: {..}}}
        self._load()

    # ------------------------------------------------------------------ #
    # Профили (персистентность)
    # ------------------------------------------------------------------ #
    def _key(self, symbol: str, timeframe: str) -> str:
        return f"{symbol}:{timeframe}"

    def _load(self):
        try:
            if os.path.exists(PROFILE_PATH):
                with open(PROFILE_PATH, "r", encoding="utf-8") as f:
                    self.profiles = json.load(f)
        except Exception:
            self.profiles = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(PROFILE_PATH), exist_ok=True)
            with open(PROFILE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.profiles, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[selector] не удалось сохранить профили: {e}")

    def get_profile(self, symbol: str, timeframe: str) -> dict:
        return self.profiles.get(self._key(symbol, timeframe), {})

    # ------------------------------------------------------------------ #
    # Оптимизация: прогон всех стратегий
    # ------------------------------------------------------------------ #
    async def optimize(self, symbol: str, timeframe: str = None,
                       limit: int = None,
                       min_trades: int = 10,
                       min_win_rate: float = 55.0,
                       top_n: int = 5,
                       start_ts: Optional[int] = None,
                       end_ts: Optional[int] = None) -> dict:
        timeframe = timeframe or settings.DEFAULT_TIMEFRAME
        limit = limit or settings.KLINE_LIMIT
        candles = await self.market.get_klines(symbol, timeframe, limit, start_ts, end_ts)
        if not candles:
            return {"symbol": symbol, "timeframe": timeframe, "strategies": [], "error": "нет данных"}

        results = []
        for schema in STRATEGY_SCHEMAS():
            stype = schema["type"]
            strategy = build_strategy(stype)
            try:
                r = run_backtest(strategy, candles, timeframe)
            except Exception as e:
                continue
            trades = r.total_trades
            win_rate = r.win_rate
            pnl = r.total_pnl_pct
            pf = r.profit_factor if r.profit_factor is not None else float("inf")
            # композитный score: win_rate (главное) + ожидание на сделку + награда за кол-во сделок
            avg_pnl = (pnl / trades) if trades else 0.0
            score = win_rate + min(avg_pnl, 5.0) * 2.0
            results.append({
                "type": stype,
                "name": schema["name"],
                "trades": trades,
                "win_rate": round(win_rate, 2),
                "pnl_pct": round(pnl, 3),
                "profit_factor": _safe(pf),
                "max_drawdown": _safe(r.max_drawdown_pct),
                "avg_pnl_per_trade": round(avg_pnl, 3),
                "score": round(score, 2),
                "selected": trades >= min_trades and win_rate >= min_win_rate,
                "params": schema["params"],
            })

        results.sort(key=lambda x: (-x["selected"], -x["score"]))

        # выбор лучших
        selected = [r for r in results if r["selected"]][:top_n]
        profile = {r["type"]: r for r in results}
        self.profiles[self._key(symbol, timeframe)] = {
            "updated_at": time.time(),
            "symbol": symbol,
            "timeframe": timeframe,
            "top": [r["type"] for r in selected],
            "strategies": profile,
        }
        self._save()

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "candles": len(candles),
            "selected": selected,
            "top_strategies": [r["type"] for r in selected],
            "strategies": results,
            "filters": {"min_trades": min_trades, "min_win_rate": min_win_rate},
        }

    # ------------------------------------------------------------------ #
    # Выбор лучшей стратегии для live-режима
    # ------------------------------------------------------------------ #
    def best_strategy(self, symbol: str, timeframe: str = None,
                      fallback: str = "momentum_breakout") -> str:
        """Возвращает type лучшей стратегии для символа (из профиля)."""
        timeframe = timeframe or settings.DEFAULT_TIMEFRAME
        prof = self.get_profile(symbol, timeframe)
        top = prof.get("top") or []
        if top:
            return top[0]
        return fallback

    def best_strategies(self, symbol: str, timeframe: str = None,
                        fallback: str = "momentum_breakout") -> List[str]:
        timeframe = timeframe or settings.DEFAULT_TIMEFRAME
        prof = self.get_profile(symbol, timeframe)
        top = prof.get("top") or []
        return top if top else [fallback]

    def all_profiles(self) -> dict:
        return self.profiles


_selector: Optional[StrategySelector] = None


def _safe(v):
    """Заменяет inf/nan на None (для JSON-сериализации)."""
    if v is None:
        return None
    try:
        if math.isinf(v) or math.isnan(v):
            return None
    except TypeError:
        pass
    return v


def get_selector() -> StrategySelector:
    global _selector
    if _selector is None:
        _selector = StrategySelector()
    return _selector
