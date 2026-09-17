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
from strategies import indicators as ta

MAX_FEED = 60


class LiveEngine:
    def __init__(self):
        self.clients: Set[WebSocket] = set()
        self.feed: list = []
        self.prices: dict = {}
        self.running = False
        self._task: Optional[asyncio.Task] = None
        self.last_scan: Optional[float] = None
        self._dna = None
        self._dna_mtime = 0.0

    def _fresh_dna(self):
        """ДНК трейдера с диска (перечитываем при обновлении файла)."""
        try:
            from trade_context import load_dna, dna_mtime
            mt = dna_mtime()
            if mt and mt != self._dna_mtime:
                self._dna = load_dna()
                self._dna_mtime = mt
        except Exception:
            pass
        return self._dna

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

    def _fresh_my_strategy(self):
        """Персональная стратегия из my_strategy.json, с перечитыванием по mtime."""
        try:
            from trade_context import load_my_strategy, my_strategy_mtime
            mt = my_strategy_mtime()
            if mt and mt != getattr(self, "_mystrat_mtime", None):
                self._mystrat = load_my_strategy()
                self._mystrat_mtime = mt
            elif not mt:
                self._mystrat = None
                self._mystrat_mtime = None
        except Exception:
            self._mystrat = getattr(self, "_mystrat", None)
        return getattr(self, "_mystrat", None)

    async def scan_once(self):
        market = get_market()
        symbols = settings.watchlist
        tfs = settings.signal_timeframes
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

        async def one(sym, tf):
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
                    # персонализация под вашу историю (ДНК; 0 токенов)
                    fit = None
                    tp2, sl2, rr_free, risk_manual = sig.tp, sig.sl, False, False
                    hold_min = None
                    mystrat_hit = None
                    try:
                        from trade_context import fit_score, _regime, match_my_strategy
                        dna = self._fresh_dna()
                        summ = ta.compute_feature_summary(candles) or {}
                        regime = _regime([c.close for c in candles])
                        fit = fit_score(dna, sym, sig.side.value,
                                        rsi=(summ or {}).get("rsi"), regime=regime)
                        mystrat_hit = match_my_strategy(self._fresh_my_strategy(),
                                                        sym, sig.side.value, tf)
                        e = sig.entry
                        # Приоритет целей: 1) ручной риск-фактор → 2) ATR (2:1 выкл) → 3) дефолт стратегии
                        if settings.manual_risk and e:
                            tp_m, sl_m = settings.MANUAL_TP_PCT, settings.MANUAL_SL_PCT
                            if sig.side.value == "LONG":
                                tp2 = e * (1 + tp_m / 100)
                                sl2 = e * (1 - sl_m / 100)
                            else:
                                tp2 = e * (1 - tp_m / 100)
                                sl2 = e * (1 + sl_m / 100)
                            risk_manual = True
                            hold_min = settings.MANUAL_HOLD_MIN or None
                        elif not settings.RR_BINDING:
                            # Правило 2/1 снято → цели по волатильности графика (ATR)
                            atr_pct = (summ or {}).get("atr_pct") or 0.0
                            if atr_pct > 0 and e:
                                tp_move = min(6.0, max(0.4, 2.0 * atr_pct))
                                sl_move = min(4.0, max(0.25, 1.2 * atr_pct))
                                if sig.side.value == "LONG":
                                    tp2 = e * (1 + tp_move / 100)
                                    sl2 = e * (1 - sl_move / 100)
                                else:
                                    tp2 = e * (1 - tp_move / 100)
                                    sl2 = e * (1 + sl_move / 100)
                                rr_free = True
                    except Exception:
                        fit = None
                    event = {
                        "fit": fit,
                        "rr_free": rr_free,
                        "risk_manual": risk_manual,
                        "hold_min": hold_min,
                        "my_strategy": mystrat_hit,
                        "type": "signal",
                        "symbol": sym,
                        "timeframe": tf,
                        "side": sig.side.value,
                        "price": sig.entry,
                        "tp": tp2,
                        "sl": sl2,
                        "confidence": sig.confidence,
                        "strategy": strat.name,
                        "strategy_auto": stype is not None and settings.SIGNAL_STRATEGY in (None, "", "auto"),
                        "reason": sig.reason,
                        "ts": int(time.time() * 1000),
                    }
                    # антиспам: по паре (монета, сторона, TF) чаще 5 минут не повторяем
                    if self.feed and self.feed[-1].get("symbol") == sym and \
                            self.feed[-1].get("side") == sig.side.value and \
                            self.feed[-1].get("timeframe") == tf and \
                            time.time() - self.feed[-1].get("ts", 0) / 1000 < 300:
                        return
                    self.feed.append(event)
                    self.feed = self.feed[-MAX_FEED:]
                    await self.broadcast(event)
                    print(f"[portal-live] сигнал {sym} {tf} {sig.side.value} conf={sig.confidence:.2f} ({strat.name})")
                except Exception:
                    return

        await asyncio.gather(*(one(s, tf) for s in symbols for tf in tfs))
        self.last_scan = time.time()

    def state(self) -> dict:
        return {
            "running": self.running,
            "watchlist": settings.watchlist,
            "interval_seconds": settings.SCAN_INTERVAL_SECONDS,
            "timeframe": settings.DEFAULT_TIMEFRAME,
            "timeframes": settings.signal_timeframes,
            "last_scan": self.last_scan,
            "feed_count": len(self.feed),
        }


_engine = LiveEngine()


def get_engine() -> LiveEngine:
    return _engine
