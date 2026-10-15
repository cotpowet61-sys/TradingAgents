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

def _profile_path() -> str:
    """Куда писать профили стратегий.

    Порядок: STRATEGY_PROFILE_PATH → PORTAL_DATA_DIR (боевой volume портала) →
    backend/data (историческое поведение для старой платформы).

    Зачем: портал раньше писал профили в backend/data, то есть в дерево исходников.
    В контейнере это не volume — после редеплоя все профили (а значит и авто-выбор
    стратегий) обнулялись. Теперь по умолчанию — в каталог данных портала.
    """
    explicit = os.environ.get("STRATEGY_PROFILE_PATH")
    if explicit:
        return explicit
    portal_data = os.environ.get("PORTAL_DATA_DIR")
    if portal_data:
        return os.path.join(portal_data, "strategy_profiles.json")
    return os.path.join(PROJECT_ROOT, "data", "strategy_profiles.json")


PROFILE_PATH = _profile_path()


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

    # ------------------------------------------------------------------ #
    # Быстрый автоподбор по свежему графику (для «Анализа агентов» в режиме
    # auto): гоняет ВСЕ стратегии на уже загруженных свечах, кэширует итог
    # на RANK_CACHE_TTL сек. Возвращает (лучший_type, топ-N кандидатов, источник).
    # Приоритет: профиль оптимизатора → быстрый бэктест → fallback.
    # ------------------------------------------------------------------ #
    RANK_CACHE_TTL = 900  # 15 минут

    def quick_rank(self, symbol: str, timeframe: str, candles,
                   top_n: int = 5, min_trades: int = 3) -> list:
        """Отсортированный список стратегий по композитному score на свежих данных."""
        key = self._key(symbol, timeframe)
        ent = getattr(self, "_rank_cache", {}).get(key)
        now = time.time()
        if ent and now - ent[0] < self.RANK_CACHE_TTL and ent[2] == len(candles):
            return ent[1]

        results = []
        for schema in STRATEGY_SCHEMAS():
            stype = schema["type"]
            try:
                strategy = build_strategy(stype)
                r = run_backtest(strategy, candles, timeframe)
            except Exception:
                continue
            trades = r.total_trades
            win_rate = r.win_rate
            pnl = r.total_pnl_pct
            avg_pnl = (pnl / trades) if trades else 0.0
            # мягкие пороги: данных мало (последние ~сотни свечей)
            ok = trades >= min_trades and win_rate >= 50.0 and pnl > 0
            score = win_rate + min(avg_pnl, 5.0) * 2.0 + min(trades, 20) * 0.25
            results.append({
                "type": stype,
                "name": schema["name"],
                "trades": trades,
                "win_rate": round(win_rate, 2),
                "pnl_pct": round(pnl, 3),
                "ok": ok,
                "score": round(score, 2),
            })
        results.sort(key=lambda x: (-x["ok"], -x["score"]))

        if not hasattr(self, "_rank_cache"):
            self._rank_cache = {}
        self._rank_cache[key] = (now, results, len(candles))
        return results

    def auto_pick(self, symbol: str, timeframe: str, candles,
                  fallback: str = "momentum_breakout"):
        """(strategy_type, top_candidates, source) для режима 'auto'."""
        timeframe = timeframe or settings.DEFAULT_TIMEFRAME

        # 1) сохранённый профиль оптимизатора (полные бэктесты)
        prof = self.get_profile(symbol, timeframe)
        top = prof.get("top") or []
        if top:
            stype = top[0]
            info = prof.get("strategies", {}).get(stype) or {}
            cand = [{
                "type": stype,
                "name": info.get("name", stype),
                "trades": info.get("trades"),
                "win_rate": info.get("win_rate"),
                "pnl_pct": info.get("pnl_pct"),
            }]
            return stype, cand, "optimizer_profile"

        # 2) быстрый бэктест по свежему графику
        if candles:
            ranking = self.quick_rank(symbol, timeframe, candles)
            viable = next((r for r in ranking if r["ok"]), None)
            pick = viable or (ranking[0] if ranking else None)
            if pick and pick["trades"] > 0:
                return pick["type"], ranking[:5], "quick_backtest"

        # 3) fallback — универсальный вариант
        return fallback, [], "fallback"

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
