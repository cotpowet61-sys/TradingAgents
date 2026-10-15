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
import logging
import time
from typing import Optional, Set

from fastapi import WebSocket

from portal_config import settings
from council import get_market
from strategies.registry import build_strategy
from strategies.selector import get_selector
from strategies import indicators as ta

log = logging.getLogger("portal.live")

MAX_FEED = 60
SIGNAL_COOLDOWN_SEC = 1800   # повтор сигнала по (монета, TF, та же сторона) — не чаще 30 мин
FLIP_CONF_MARGIN = 0.10      # разворот SHORT↔LONG — только если новая уверенность выше на 10 п.п.
SYNTHETIC_WARN_EVERY = 10    # как часто напоминать в лог, что биржа недоступна


def real_data_ok() -> tuple[bool, str]:
    """Можно ли доверять текущему источнику свечей.

    Портал с 4 монетами × 7 ТФ опрашивает публичный API Bybit; при гео-блоке
    market_data молча подставляет синтетику («учебный график»). Выдавать по ней
    сигналы, обучаться на её исходах и публиковать их в трек-рекорде — это
    самообман, поэтому здесь жёсткий стоп (симметрично signal_backtest.py).
    """
    try:
        from data import market_data as md
        src = (md._STATUS or {}).get("source") or "unknown"
    except Exception:
        return True, "unknown"
    if src == "bybit":
        return True, src
    if not settings.REQUIRE_REAL_DATA:
        return True, src
    return False, src


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
        self._tf_cursor = 0
        # Реестр активных сигналов: (symbol, tf) -> event. Сигнал «живёт»,
        # пока цена не дойдёт до TP/SL или не выйдет таймаут удержания.
        self.active: dict = {}
        # метрики эксплуатации (видны в /api/live/state и /api/health)
        self.metrics: dict = {
            "scans": 0, "emitted": 0, "closed": 0, "skipped_no_data": 0,
            "skipped_low_conf": 0, "errors": 0, "last_error": "", "last_scan_ms": 0,
            "scan_duration_ms": 0, "data_source": "unknown", "_syn_warned": 0,
        }

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

    def _cadence_options(self) -> list[int]:
        """Лестница интервалов сканирования (сек). Пусто → фиксированный интервал.

        Портал не должен «долбить» публичный API Bybit: на 4 монетах × 7 ТФ
        круг выходит ~28 запросов. По умолчанию берём SCAN_INTERVAL_SECONDS.
        """
        raw = (settings.SCAN_CADENCE_SECONDS or "").replace(";", ",")
        vals = []
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit() and int(part) >= 5:
                vals.append(int(part))
        return sorted(set(vals)) or [max(5, int(settings.SCAN_INTERVAL_SECONDS))]

    async def _loop(self):
        options = self._cadence_options()
        i = 0
        while self.running:
            interval = options[i % len(options)]
            i += 1
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — сканер не должен умирать
                self.metrics["errors"] += 1
                self.metrics["last_error"] = f"{type(e).__name__}: {str(e)[:200]}"
                log.warning("[portal-live] ошибка сканирования: %s", e)
            await asyncio.sleep(interval)

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

    def _close_event(self, sig: dict, reason: str, exit_px: Optional[float]) -> dict:
        entry = sig.get("price")
        pnl = None
        if exit_px and entry:
            pnl = (exit_px - entry) / entry * 100
            if sig.get("side") == "SHORT":
                pnl = -pnl
        return {
            "type": "signal_close",
            "symbol": sig.get("symbol"),
            "timeframe": sig.get("timeframe"),
            "side": sig.get("side"),
            "strategy": sig.get("strategy"),
            "stype": sig.get("stype"),
            "conf": sig.get("confidence"),
            "reason": reason,                    # TP / SL / TIMEOUT / REVERSED / REFRESH
            "entry": entry,
            "exit": exit_px,
            "pnl_pct": round(pnl, 3) if pnl is not None else None,
            "open_ts": sig.get("ts"),
            "duration_min": round((time.time() - (sig.get("ts") or 0) / 1000) / 60),
            "ts": int(time.time() * 1000),
        }

    def _close_matured_signals(self) -> list:
        """Проверяет активные сигналы по последним ценам: TP/SL/таймаут →
        события signal_close. Вызывается каждое сканирование."""
        out = []
        now = time.time()
        hold_sec = (settings.MANUAL_HOLD_MIN or 240) * 60
        for key, sig in list(self.active.items()):
            if sig.get("status") != "ACTIVE":
                if now - sig.get("closed_at", now) > 86_400:
                    del self.active[key]
                continue
            px = (self.prices.get(sig.get("symbol") or "") or {}).get("last")
            reason, exit_px = None, None
            tp, sl, side = sig.get("tp"), sig.get("sl"), sig.get("side")
            # breakeven + трейлинг: подтягиваем стоп вслед за ценой (signal_gates)
            if px:
                try:
                    import signal_gates as G
                    if G.update_stops(settings, sig, px):
                        sl = sig.get("sl")
                except Exception:
                    pass
            if px and tp and sl:
                if side == "LONG":
                    if px >= tp:
                        reason, exit_px = "TP", tp
                    elif px <= sl:
                        reason, exit_px = "SL", sl
                elif side == "SHORT":
                    if px <= tp:
                        reason, exit_px = "TP", tp
                    elif px >= sl:
                        reason, exit_px = "SL", sl
            if not reason and now - (sig.get("ts") or 0) / 1000 > hold_sec:
                reason, exit_px = "TIMEOUT", px
            if reason:
                sig["status"] = "CLOSED_" + reason
                sig["closed_at"] = now
                out.append(self._close_event(sig, reason, exit_px))
        return out

    def _gate(self, sym: str, tf: str, side: str, conf: float) -> tuple:
        """(allow, pending_close_event|None). Решает, пускать ли новый сигнал:
        - та же сторона: только по кулдауну 30 мин (старый гасим как REFRESH);
        - противоположная: только если conf >= старая + 0.10 (старый = REVERSED)."""
        key = (sym, tf)
        act = self.active.get(key)
        if not act or act.get("status") != "ACTIVE":
            return True, None
        now_s = time.time()
        if act.get("side") == side:
            if now_s - (act.get("ts") or 0) / 1000 < SIGNAL_COOLDOWN_SEC:
                return False, None
            act["status"] = "CLOSED_REFRESH"
            act["closed_at"] = now_s
            return True, self._close_event(act, "REFRESH", act.get("price"))
        # флип: новый сигнал обязан быть заметно увереннее живого
        if conf < (act.get("confidence") or 0) + FLIP_CONF_MARGIN:
            return False, None
        act["status"] = "CLOSED_REVERSED"
        act["closed_at"] = now_s
        px = (self.prices.get(sym) or {}).get("last")
        return True, self._close_event(act, "REVERSED", px)

    @staticmethod
    def _sizing(entry: float, side: str, tp: float, sl: float) -> Optional[dict]:
        """Размер позиции под сигнал: риск $ = депозит × риск%, объём = риск / дистанция SL.
        Кап: маржа ≤ 95% депозита. Комиссия тейкера Bybit ~0.055% × 2 стороны."""
        if not entry or not tp or not sl:
            return None
        d_sl = abs(entry - sl) / entry
        d_tp = abs(tp - entry) / entry
        if d_sl <= 0:
            return None
        dep = settings.DEPOSIT_USD
        risk_usd = dep * settings.RISK_PER_TRADE_PCT / 100
        if dep <= 0 or risk_usd <= 0:
            return None          # сайзинг выключен (риск/депозит = 0)
        lev = max(1.0, settings.LEVERAGE)
        notional = risk_usd / d_sl
        max_notional = dep * 0.95 * lev
        capped = notional > max_notional
        notional = min(notional, max_notional)
        return {
            "risk_usd": round(notional * d_sl, 2),
            "planned_risk": round(risk_usd, 2),
            "margin": round(notional / lev, 2),
            "notional": round(notional, 2),
            "qty": round(notional / entry, 6),
            "tp_usd": round(notional * d_tp, 2),
            "fee": round(notional * 0.00055 * 2, 2),
            "leverage": lev,
            "capped": capped,
        }

    def _tick_tfs(self) -> list[str]:
        """Таймфреймы на один проход: ротация по кругу (не все сразу).

        Все ТФ за раз = n_монет × n_ТФ запросов каждые 30 с — это лишняя
        нагрузка на публичный API и риск получить бан. Крутим по одному-двум,
        полный обход watchlist'а остаётся быстрым.
        """
        all_tfs = settings.signal_timeframes
        if not all_tfs:
            return []
        per_tick = max(1, int(settings.TFS_PER_TICK or 1))
        if per_tick >= len(all_tfs):
            return list(all_tfs)
        out = []
        for k in range(per_tick):
            out.append(all_tfs[(self._tf_cursor + k) % len(all_tfs)])
        self._tf_cursor = (self._tf_cursor + per_tick) % len(all_tfs)
        return out

    async def scan_once(self):
        t0 = time.time()
        self.metrics["scans"] += 1
        self.metrics["last_scan_ms"] = int(t0 * 1000)

        ok, src = real_data_ok()
        self.metrics["data_source"] = src
        if not ok:
            # Биржа недоступна → синтетика. Никаких сигналов, никаких исходов
            # (иначе «учебные» числа уйдут в обучение и в публичный трек-рекорд).
            self.metrics["skipped_no_data"] += 1
            if self.metrics["skipped_no_data"] % SYNTHETIC_WARN_EVERY == 1:
                log.warning("[portal-live] источник свечей = %s: сканирование пропущено "
                            "(нужен доступ к Bybit или REQUIRE_REAL_DATA=false для локальной отладки)", src)
            try:
                await self.broadcast({"type": "data_status", "source": src, "trading": False})
            except Exception:
                pass
            self.last_scan = t0
            self.metrics["scan_duration_ms"] = int((time.time() - t0) * 1000)
            return

        market = get_market()
        symbols = settings.watchlist
        tfs = self._tick_tfs()
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
            await self.broadcast({"type": "prices", "prices": self.prices, "source": src})
        except Exception:
            pass

        sem = asyncio.Semaphore(4)

        # 1) обновляем судьбу активных сигналов по свежим тикерам
        #    (каждый исход — еда для обучения сканера)
        import signal_learner as L
        for ev in self._close_matured_signals():
            try:
                await asyncio.to_thread(L.learn_from_event, ev)   # БД + файлы — не в loop
            except Exception as e:  # noqa: BLE001
                log.warning("[portal-live] не записался исход сигнала: %s", e)
            self.feed.append(ev)
            self.feed = self.feed[-MAX_FEED:]
            self.metrics["closed"] += 1
            await self.broadcast(ev)

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
                    # самообучение по монете: глушим проигрышные комбинации,
                    # подстраиваем порог под статистику исходов (0 токенов)
                    learned = None
                    min_conf = settings.MIN_CONFIDENCE
                    try:
                        import signal_learner as L
                        adj = await asyncio.to_thread(L.get_adjustment, sym, stype, sig.side.value)
                        if adj and adj.get("blocked"):
                            return
                        if adj and adj.get("conf_delta"):
                            min_conf = max(0.30, min(0.95, min_conf + adj["conf_delta"]))
                            learned = adj
                    except Exception as e:  # noqa: BLE001
                        log.warning("[portal-live] правила обучения недоступны для %s: %s", sym, e)
                    if sig.confidence < min_conf:
                        self.metrics["skipped_low_conf"] += 1
                        return
                    summ = ta.compute_feature_summary(candles) or {}
                    # единые квант-фильтры входа (зеркало бэктестов) — signal_gates
                    try:
                        import signal_gates as G
                        if G.entry_block_reason(
                                settings, stype=stype, side=sig.side.value, symbol=sym,
                                hour_utc=time.gmtime((candles[-1].ts or 0) / 1000).tm_hour,
                                vol_season=(summ or {}).get("vol_ratio_seasonal"),
                                atr_pct=(summ or {}).get("atr_pct")):
                            return
                    except Exception:
                        pass
                    # персонализация под вашу историю (ДНК; 0 токенов)
                    fit = None
                    tp2, sl2, rr_free, risk_manual = sig.tp, sig.sl, False, False
                    hold_min = None
                    mystrat_hit = None
                    try:
                        from trade_context import fit_score, _regime, match_my_strategy
                        dna = self._fresh_dna()
                        summ = summ or {}
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
                        else:
                            # Плавающие цели через общий хелпер (ATR → рамки TP/SL_PCT_*)
                            import signal_gates as G
                            tg = G.compute_targets(settings, e, sig.side.value,
                                                   (summ or {}).get("atr_pct"), tp2, sl2)
                            tp2, sl2 = tg["tp"], tg["sl"]
                            rr_free = tg["mode"] != "manual"
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
                        "sizing": self._sizing(sig.entry or 0, sig.side.value, tp2, sl2),
                        "strategy": strat.name,
                        "strategy_auto": stype is not None and settings.SIGNAL_STRATEGY in (None, "", "auto"),
                        "reason": sig.reason,
                        "ts": int(time.time() * 1000),
                    }
                    # реестр: активный сигнал по (монета, TF) — один.
                    # Повтор той же стороны — по кулдауну; разворот — только
                    # при заметно большей уверенности (иначе старый сигнал живёт).
                    allow, pending_close = self._gate(sym, tf, sig.side.value, sig.confidence)
                    if not allow:
                        return
                    if pending_close:
                        try:
                            await asyncio.to_thread(L.learn_from_event, pending_close)
                        except Exception as e:  # noqa: BLE001
                            log.warning("[portal-live] исход разворота не записан: %s", e)
                        self.feed.append(pending_close)
                        self.feed = self.feed[-MAX_FEED:]
                        await self.broadcast(pending_close)
                    event["status"] = "ACTIVE"
                    event["source"] = src
                    event["real_data"] = True
                    event["stype"] = stype
                    event["entry"] = sig.entry
                    event["hi"] = sig.entry
                    event["lo"] = sig.entry
                    if learned:
                        event["learned"] = learned
                    self.active[(sym, tf)] = dict(event)
                    self.feed.append(event)
                    self.feed = self.feed[-MAX_FEED:]
                    self.metrics["emitted"] += 1
                    await self.broadcast(event)
                    log.info("[portal-live] сигнал %s %s %s conf=%.2f (%s)",
                             sym, tf, sig.side.value, sig.confidence, strat.name)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — одна монета не должна ронять проход
                    self.metrics["errors"] += 1
                    self.metrics["last_error"] = f"{sym}:{type(e).__name__}: {str(e)[:160]}"
                    log.warning("[portal-live] ошибка по %s %s: %s", sym, tf, e)

        await asyncio.gather(*(one(s, tf) for s in symbols for tf in tfs))
        self.last_scan = time.time()
        self.metrics["scan_duration_ms"] = int((self.last_scan - t0) * 1000)

    def state(self) -> dict:
        m = {k: v for k, v in self.metrics.items() if not k.startswith("_")}
        return {
            "running": self.running,
            "watchlist": settings.watchlist,
            "interval_seconds": settings.SCAN_INTERVAL_SECONDS,
            "cadence_seconds": self._cadence_options(),
            "tfs_per_tick": max(1, int(settings.TFS_PER_TICK or 1)),
            "timeframe": settings.DEFAULT_TIMEFRAME,
            "timeframes": settings.signal_timeframes,
            "last_scan": self.last_scan,
            "feed_count": len(self.feed),
            "active_signals": len([1 for v in self.active.values() if v.get("status") == "ACTIVE"]),
            "metrics": m,
        }


_engine = LiveEngine()


def get_engine() -> LiveEngine:
    return _engine
