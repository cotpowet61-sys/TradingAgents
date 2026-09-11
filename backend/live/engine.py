"""
Live-движок: фоновое сканирование рынка и журналирование сигналов.

Что делает:
  * Периодически (SCAN_INTERVAL_SECONDS) прогоняет мультиагентный анализ по всем символам.
  * Новые сигналы (BUY/SELL) попадают в журнал и рассылаются по WebSocket.
  * Ведёт счётчик сделок за день (лимит MAX_TRADES_PER_DAY) — нотация того,
    что система не должна выдавать больше 30 сделок в день.
  * Отслеживает активные сигналы: если цена достигла TP/SL или истекло время
    (<= MAX_HOLD_HOURS) — сигнал закрывается и фиксируется результат
    (paper-трейдинг, форвард-тест на живых данных).

Это не реальное исполнение ордеров на бирже — только сигналы и их отслеживание.
Подключение торговых ключей и отправку ордеров вы можете добавить позже (см. TODO).
"""
from __future__ import annotations

import asyncio
import time
from typing import List, Optional

from config import settings
from data.market_data import MarketData
from agents.orchestrator import get_orchestrator
from strategies.selector import get_selector


class LiveEngine:
    def __init__(self):
        self.orchestrator = get_orchestrator()
        self.market = self.orchestrator.market
        self.selector = get_selector()
        self.signals: List[dict] = []        # все сигналы (история)
        self.active: List[dict] = []         # активные сигналы
        self.day_counter = {"date": None, "count": 0}
        self._task: Optional[asyncio.Task] = None
        self._listeners: List[asyncio.Queue] = []
        self._seq = 0

    # ------------------------------------------------------------------ #
    # Публикация событий в WebSocket
    # ------------------------------------------------------------------ #
    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=1000)
        self._listeners.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._listeners:
            self._listeners.remove(q)

    async def publish(self, event: dict):
        self._seq += 1
        event["seq"] = self._seq
        event["ts"] = int(time.time() * 1000)
        for q in list(self._listeners):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    # ------------------------------------------------------------------ #
    # Фоновый цикл сканирования
    # ------------------------------------------------------------------ #
    async def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run(self):
        await self.publish({"type": "system", "message": "Live-движок запущен", "level": "info"})
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                await self.publish({"type": "system", "message": f"Ошибка скана: {e}", "level": "error"})
            await asyncio.sleep(settings.SCAN_INTERVAL_SECONDS)

    async def scan_once(self):
        if not settings.AUTO_SCAN:
            return
        symbols = await self._scan_symbols()
        results = await self.orchestrator.scan_market(symbols=symbols)
        for r in results:
            sig = r.get("signal", {})
            if sig.get("action") in ("BUY", "SELL") and r.get("price"):
                await self._maybe_open_signal(r, sig)

    async def _scan_symbols(self) -> List[str]:
        """Список символов для сканирования.

        Если SCAN_MAX_SYMBOLS > 0 — берём топ-N монет по суточному объёму
        (все монеты с объёмом >= MIN_DAILY_VOLUME_USD уже отфильтрованы).
        Иначе — DEFAULT_SYMBOLS из настроек.
        """
        if settings.SCAN_MAX_SYMBOLS > 0:
            try:
                tickers = await self.market.get_all_tickers()
                top = [t["symbol"] for t in tickers[:settings.SCAN_MAX_SYMBOLS]]
                if top:
                    return top
            except Exception:
                pass
        return settings.symbols

    async def _maybe_open_signal(self, analysis: dict, sig: dict):
        symbol = analysis["symbol"]
        price = analysis["price"]
        # Дневной лимит сделок
        today = time.strftime("%Y-%m-%d")
        if self.day_counter["date"] != today:
            self.day_counter = {"date": today, "count": 0}
        if self.day_counter["count"] >= settings.MAX_TRADES_PER_DAY:
            await self.publish({"type": "system",
                                "message": f"Достигнут дневной лимит {settings.MAX_TRADES_PER_DAY} сделок",
                                "level": "warning"})
            return
        # Дубли по тому же символу/стороне за последние N минут
        now = time.time()
        for a in self.active:
            if a["symbol"] == symbol and a["side"] == sig["side"]:
                return

        entry = sig.get("entry") or price
        side = sig["side"]
        tp = entry * (1 + sig["tp_pct"] / 100) if side == "LONG" else entry * (1 - sig["tp_pct"] / 100)
        sl = entry * (1 - sig["sl_pct"] / 100) if side == "LONG" else entry * (1 + sig["sl_pct"] / 100)

        signal = {
            "id": f"{symbol}-{int(now)}",
            "symbol": symbol,
            "side": side,
            "action": sig["action"],
            "entry": round(entry, 6),
            "tp": round(tp, 6),
            "sl": round(sl, 6),
            "tp_pct": sig["tp_pct"],
            "sl_pct": sig["sl_pct"],
            "confidence": sig["confidence"],
            "reason": sig.get("reason", ""),
            "opened_at": int(now * 1000),
            "max_hold_hours": settings.MAX_HOLD_HOURS,
            "status": "OPEN",
            "pnl_pct": None,
        }
        self.active.append(signal)
        self.signals.append(signal)
        self.day_counter["count"] += 1
        await self.publish({
            "type": "signal",
            "signal": signal,
            "agents": analysis.get("agents"),
            "consensus": analysis.get("consensus"),
        })

    # ------------------------------------------------------------------ #
    # Обновление активных сигналов (TP/SL/время) — форвард-тест
    # ------------------------------------------------------------------ #
    async def update_active(self):
        changed = False
        for s in self.active:
            if s["status"] != "OPEN":
                continue
            candles = await self.market.get_klines(s["symbol"])
            if not candles:
                continue
            cur = candles[-1].close
            held_hours = (time.time() * 1000 - s["opened_at"]) / 3_600_000
            exit_price = None
            reason = None
            if s["side"] == "LONG":
                hit_tp = candles[-1].high >= s["tp"]
                hit_sl = candles[-1].low <= s["sl"]
            else:  # SHORT
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
            # чистим закрытые из active (оставляем в истории)
            self.active = [s for s in self.active if s["status"] == "OPEN"]

    def state(self) -> dict:
        today = time.strftime("%Y-%m-%d")
        if self.day_counter["date"] != today:
            self.day_counter = {"date": today, "count": 0}
        closed = [s for s in self.signals if s.get("status") == "CLOSED"]
        wins = [s for s in closed if (s.get("pnl_pct") or 0) > 0]
        return {
            "active": self.active,
            "history": self.signals[-200:],
            "day_trades": self.day_counter["count"],
            "day_limit": settings.MAX_TRADES_PER_DAY,
            "closed_count": len(closed),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
            "avg_pnl": round(sum(s.get("pnl_pct") or 0 for s in closed) / len(closed), 3) if closed else 0.0,
            "scan_interval": settings.SCAN_INTERVAL_SECONDS,
            "auto_scan": settings.AUTO_SCAN,
        }


_live: Optional[LiveEngine] = None


def get_live_engine() -> LiveEngine:
    global _live
    if _live is None:
        _live = LiveEngine()
    return _live
