"""
Live-движок сигналов Портала: индикаторный сканер watchlist'а (0 AI-токенов).

Каждые SCAN_INTERVAL_SECONDS гоняет по монетам watchlist:
  - авто-подбор стратегии по графику (кэшируется селектором 15 мин) или
    фиксированную стратегию из настроек (SIGNAL_STRATEGY);
  - свежий сигнал; если side != NEUTRAL и conf >= MIN_CONFIDENCE — событие
    публикуется всем WS-клиентам (и складывается в ленту последних сигналов).
Плюс рассылает батч тикеров, чтобы карточки watchlist дышали.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional, Set

from fastapi import WebSocket

from portal_config import settings
from council import get_market
from strategies.registry import build_strategy
from strategies.selector import get_selector

MAX_FEED = 60


class LiveEngine:
    def __init__(self):
        self.clients: Set[WebSocket] = set()
        self.feed: list = []
        self.prices: dict = {}
        self.running = False
        self._task: Optional[asyncio.Task] = None
        self.last_scan: Optional[float] = None

    # ------------------------------------------------------------------ #
    # WS-хаб
    # ------------------------------------------------------------------ #
    async def add(self, ws: WebSocket):
        await ws.accept()
        self.clients.add(ws)
        await ws.send_json({
            "type": "hello",
            "feed": self.feed[-15:],
            "prices": self.prices,
            "watchlist": settings.watchlist,
        })

    def drop(self, ws: WebSocket):
        self.clients.discard(ws)

    async def broadcast(self, msg: dict):
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.drop(ws)

    # ------------------------------------------------------------------ #
    # Цикл сканирования
    # ------------------------------------------------------------------ #
    def start(self):
        if not self.running:
            self.running = True
            self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self):
        while self.running:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[portal-live] ошибка сканирования: {e}")
            await asyncio.sleep(max(5, settings.SCAN_INTERVAL_SECONDS))

    async def scan_once(self):
        market = get_market()
        symbols = settings.watchlist
        tf = settings.DEFAULT_TIMEFRAME
        sel = get_selector()

        # тикеры (для карточек)
        try:
            tickers = await market.get_tickers(symbols)
            if isinstance(tickers, list):
                for t in tickers:
                    if t.get("last"):
                        self.prices[t["symbol"]] = {
                            "last": t["last"],
                            "change24h": t.get("change24h", 0),
                            "market": t.get("market"),
                        }
            await self.broadcast({"type": "prices", "prices": self.prices})
        except Exception:
            pass

        sem = asyncio.Semaphore(4)

        async def one(sym):
            async with sem:
                try:
                    candles = await market.get_klines(sym, tf, limit=300)
                    if not candles:
                        return
                    stype = settings.SIGNAL_STRATEGY
                    auto_src = None
                    if stype in (None, "", "auto"):
                        stype, _cand, auto_src = sel.auto_pick(sym, tf, candles)
                    strat = build_strategy(stype)
                    if not strat:
                        return
                    sig = strat.generate(candles, len(candles) - 1)
                    if not sig or sig.side.value == "NEUTRAL":
                        return
                    if sig.confidence < settings.MIN_CONFIDENCE:
                        return
                    event = {
                        "type": "signal",
                        "symbol": sym,
                        "timeframe": tf,
                        "side": sig.side.value,
                        "price": sig.entry,
                        "tp": sig.tp,
                        "sl": sig.sl,
                        "confidence": sig.confidence,
                        "strategy": strat.name,
                        "strategy_auto": stype is not None and settings.SIGNAL_STRATEGY in (None, "", "auto"),
                        "reason": sig.reason,
                        "ts": int(time.time() * 1000),
                    }
                    key = f"{sym}:{sig.side.value}"
                    if self.feed and self.feed[-1].get("symbol") == sym and \
                            self.feed[-1].get("side") == sig.side.value and \
                            time.time() - self.feed[-1].get("ts", 0) / 1000 < 300:
                        return  # не спамим одинаковым сигналом подряд чаще 5 минут
                    self.feed.append(event)
                    self.feed = self.feed[-MAX_FEED:]
                    await self.broadcast(event)
                    print(f"[portal-live] сигнал {sym} {sig.side.value} conf={sig.confidence:.2f} ({strat.name})")
                except Exception:
                    return

        await asyncio.gather(*(one(s) for s in symbols))
        self.last_scan = time.time()

    def state(self) -> dict:
        return {
            "running": self.running,
            "watchlist": settings.watchlist,
            "interval_seconds": settings.SCAN_INTERVAL_SECONDS,
            "timeframe": settings.DEFAULT_TIMEFRAME,
            "last_scan": self.last_scan,
            "feed_count": len(self.feed),
        }


_engine = LiveEngine()


def get_engine() -> LiveEngine:
    return _engine
