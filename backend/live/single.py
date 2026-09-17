"""
Live-движок «1-на-1»: сигналы только по ОДНОЙ выбранной монете.

Работает параллельно с общим live-движком. Особенности:
  * Собственные настройки: символ, таймфрейм, стратегия (или "auto" —
    лучшая из профиля оптимизатора для этой монеты), параметры стратегии,
    TP/SL, макс. удержание, лимит сделок/день, размер позиции %.
  * Настройки меняются ОНЛАЙН (POST /api/live/single/config) без
    перезапуска цикла — конфиг перечитывается на каждой итерации.
  * Сигналы: стратегия оценивает последнюю свечу; LONG/SHORT с достаточной
    уверенностью -> открывается сигнал с расчётными TP/SL.
  * Активные сигналы отслеживаются: TP / SL / таймаут (paper trading).
  * События публикуются в общий WebSocket с меткой source="single".

Ограничения платформы соблюдаются: <= MAX_TRADES_PER_DAY в день,
TP >= MIN_TP_PCT, удержание <= MAX_HOLD_HOURS.
"""
from __future__ import annotations

import asyncio
import time
from typing import List, Optional

from config import settings
from data.market_data import MarketData
from strategies.base import SignalSide
from strategies.registry import build_strategy
from strategies.selector import get_selector
from live.engine import get_live_engine


class SingleCoinEngine:
    def __init__(self):
        self.market = MarketData()
        self.selector = get_selector()

        # --- ONLINE-настройки (меняются без рестарта) ---
        self.symbol: str = "BTCUSDT"
        self.timeframe: str = "5m"
        self.strategy_type: str = "auto"   # "auto" = лучшая стратегия монеты из профиля
        self.params: dict = {}
        self.tp_pct: float = settings.MIN_TP_PCT
        self.sl_pct: float = settings.DEFAULT_SL_PCT
        self.max_hold_hours: float = settings.MAX_HOLD_HOURS
        self.max_trades_per_day: int = settings.MAX_TRADES_PER_DAY
        self.position_size_pct: float = settings.POSITION_SIZE_PCT
        self.min_confidence: float = settings.MIN_CONFIDENCE

        # --- состояние ---
        self.running: bool = False
        self.signals: List[dict] = []
        self.active: List[dict] = []
        self.day_counter = {"date": None, "count": 0}
        self.last_scan: Optional[dict] = None   # последний осмотр (для UI)
        self._task: Optional[asyncio.Task] = None
        self._eval_cache = {}                   # (strategy_type, кэш) -> strategy instance

    # ------------------------------------------------------------------ #
    # Публикация через общий live-движок (единый WebSocket)
    # ------------------------------------------------------------------ #
    async def publish(self, event: dict):
        event["source"] = "single"
        await get_live_engine().publish(event)

    # ------------------------------------------------------------------ #
    # Онлайн-обновление настроек (без перезапуска цикла)
    # ------------------------------------------------------------------ #
    def apply_config(self, cfg: dict) -> List[str]:
        applied = []
        for key, val in cfg.items():
            if val is None:
                continue
            if key == "symbol":
                self.symbol = str(val).upper().strip()
            elif key == "timeframe":
                self.timeframe = str(val)
            elif key == "strategy_type":
                self.strategy_type = str(val)
            elif key == "params" and isinstance(val, dict):
                self.params = val
            elif key == "tp_pct":
                # жёсткое правило: TP не ниже платформенного минимума
                self.tp_pct = max(float(val), settings.MIN_TP_PCT)
            elif key == "sl_pct":
                self.sl_pct = float(val)
            elif key == "max_hold_hours":
                # жёсткое правило: не дольше платформенного максимума
                self.max_hold_hours = min(float(val), settings.MAX_HOLD_HOURS)
            elif key == "max_trades_per_day":
                self.max_trades_per_day = min(int(val), settings.MAX_TRADES_PER_DAY)
            elif key == "position_size_pct":
                self.position_size_pct = max(1.0, min(float(val), 100.0))
            elif key == "min_confidence":
                self.min_confidence = max(0.0, min(float(val), 1.0))
            else:
                continue
            applied.append(key)
        return applied

    def config_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "strategy_type": self.strategy_type,
            "params": self.params,
            "tp_pct": self.tp_pct,
            "sl_pct": self.sl_pct,
            "max_hold_hours": self.max_hold_hours,
            "max_trades_per_day": self.max_trades_per_day,
            "position_size_pct": self.position_size_pct,
            "min_confidence": self.min_confidence,
        }

    # ------------------------------------------------------------------ #
    # Управление циклом
    # ------------------------------------------------------------------ #
    async def start(self):
        self.running = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run(self):
        await self.publish({"type": "system",
                            "message": f"Live 1-на-1 запущен: {self.symbol} ({self.timeframe})",
                            "level": "info"})
        while self.running:
            try:
                await self.scan_once()
                await self.update_active()
            except asyncio.CancelledError:
                break
            except Exception as e:
                await self.publish({"type": "system",
                                    "message": f"1-на-1 ошибка скана: {e}", "level": "error"})
            await asyncio.sleep(settings.SCAN_INTERVAL_SECONDS)

    # ------------------------------------------------------------------ #
    # Осмотр монеты одной стратегией
    # ------------------------------------------------------------------ #
    def _strategy(self):
        stype = self.strategy_type
        if stype in (None, "", "auto"):
            stype = self.selector.best_strategy(self.symbol, self.timeframe)
        strat = build_strategy(stype, params=self.params,
                               tp_pct=self.tp_pct, sl_pct=self.sl_pct,
                               max_hold_hours=self.max_hold_hours)
        return stype, strat

    async def scan_once(self):
        stype, strat = self._strategy()
        if strat is None:
            self.last_scan = {"ok": False, "error": f"неизвестная стратегия {stype}"}
            return
        # данных за ~последние сутки с разгоном для индикаторов
        limit = 200
        candles = await self.market.get_klines(self.symbol, self.timeframe, limit)
        if len(candles) < 30:
            self.last_scan = {"ok": False, "error": "мало данных"}
            return

        idx = len(candles) - 1
        sig = strat.generate(candles, idx)
        price = candles[idx].close
        self.last_scan = {
            "ok": True,
            "ts": int(time.time() * 1000),
            "price": price,
            "strategy": stype,
            "strategy_name": strat.name,
            "side": sig.side.value if sig else "NEUTRAL",
            "confidence": round(sig.confidence, 3) if sig else 0,
            "reason": sig.reason if sig else "",
            "candles": len(candles),
        }

        if sig and sig.side in (SignalSide.LONG, SignalSide.SHORT) \
                and sig.confidence >= self.min_confidence:
            await self._maybe_open(sig)

    async def _maybe_open(self, sig):
        today = time.strftime("%Y-%m-%d")
        if self.day_counter["date"] != today:
            self.day_counter = {"date": today, "count": 0}
        if self.day_counter["count"] >= self.max_trades_per_day:
            await self.publish({"type": "system",
                                "message": f"1-на-1 {self.symbol}: дневной лимит {self.max_trades_per_day} сделок",
                                "level": "warning"})
            return
        # Дедупликация: та же сторона по тому же символу уже открыта
        for a in self.active:
            if a["symbol"] == self.symbol and a["side"] == sig.side.value:
                return

        now = time.time()
        signal = {
            "id": f"1v1-{self.symbol}-{int(now)}",
            "symbol": self.symbol,
            "side": sig.side.value,
            "action": "BUY" if sig.side == SignalSide.LONG else "SELL",
            "entry": round(sig.entry, 6),
            "tp": round(sig.tp, 6),
            "sl": round(sig.sl, 6),
            "tp_pct": sig.tp_pct,
            "sl_pct": sig.sl_pct,
            "confidence": sig.confidence,
            "reason": sig.reason,
            "strategy": self.strategy_type,
            "timeframe": self.timeframe,
            "position_size_pct": self.position_size_pct,
            "opened_at": int(now * 1000),
            "max_hold_hours": self.max_hold_hours,
            "status": "OPEN",
            "pnl_pct": None,
            "source": "single",
        }
        self.active.append(signal)
        self.signals.append(signal)
        self.day_counter["count"] += 1
        await self.publish({"type": "signal", "signal": signal})

    # ------------------------------------------------------------------ #
    # Отслеживание активных сигналов (TP/SL/таймаут)
    # ------------------------------------------------------------------ #
    async def update_active(self):
        changed = False
        for s in self.active:
            if s["status"] != "OPEN":
                continue
            candles = await self.market.get_klines(s["symbol"], s.get("timeframe") or self.timeframe, 3)
            if not candles:
                continue
            cur = candles[-1].close
            held_hours = (time.time() * 1000 - s["opened_at"]) / 3_600_000
            exit_price = None
            reason = None
            if s["side"] == "LONG":
                hit_tp = candles[-1].high >= s["tp"]
                hit_sl = candles[-1].low <= s["sl"]
            else:
                hit_tp = candles[-1].low <= s["tp"]
                hit_sl = candles[-1].high >= s["sl"]
            if hit_tp and hit_sl:
                exit_price, reason = s["sl"], "SL"
            elif hit_tp:
                exit_price, reason = s["tp"], "TP"
            elif hit_sl:
                exit_price, reason = s["sl"], "SL"
            if exit_price is None and held_hours >= s["max_hold_hours"]:
                exit_price, reason = cur, "TIME"

            if exit_price is not None:
                if s["side"] == "LONG":
                    pnl = (exit_price - s["entry"]) / s["entry"] * 100
                else:
                    pnl = (s["entry"] - exit_price) / s["entry"] * 100
                s["status"] = "CLOSED"
                s["exit_price"] = round(exit_price, 6)
                s["pnl_pct"] = round(pnl, 4)
                s["exit_reason"] = reason
                s["hold_hours"] = round(held_hours, 3)
                changed = True
                await self.publish({"type": "signal_close", "signal": s})
        if changed:
            self.active = [s for s in self.active if s["status"] == "OPEN"]

    # ------------------------------------------------------------------ #
    # Состояние для UI
    # ------------------------------------------------------------------ #
    def state(self) -> dict:
        today = time.strftime("%Y-%m-%d")
        if self.day_counter["date"] != today:
            self.day_counter = {"date": today, "count": 0}
        closed = [s for s in self.signals if s.get("status") == "CLOSED"]
        wins = [s for s in closed if (s.get("pnl_pct") or 0) > 0]
        return {
            "running": self.running,
            "config": self.config_dict(),
            "last_scan": self.last_scan,
            "active": self.active,
            "history": self.signals[-100:],
            "day_trades": self.day_counter["count"],
            "day_limit": self.max_trades_per_day,
            "closed_count": len(closed),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
            "avg_pnl": round(sum(s.get("pnl_pct") or 0 for s in closed) / len(closed), 3) if closed else 0.0,
        }


_single: Optional[SingleCoinEngine] = None


def get_single_engine() -> SingleCoinEngine:
    global _single
    if _single is None:
        _single = SingleCoinEngine()
    return _single
