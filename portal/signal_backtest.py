# -*- coding: utf-8 -*-
"""
Бэктест «онлайн»-сигналов Портала — ПОЛНАЯ аналогия live-сканера, только
на истории. 0 AI-токенов (та же математика индикаторов, что и в live_engine).

Аналогия с live-сканером 1-в-1:
  - те же монеты (избранные из watchlist) и те же таймфреймы (включая 1m);
  - тот же авто-подбор стратегии (SIGNAL_STRATEGY=auto → selector, с
    периодическим переподбором каждые REPICK_EVERY баров — в лайве селектор
    кэширует выбор на 15 минут);
  - тот же порог уверенности MIN_CONFIDENCE;
  - тот же приоритет целей: ручной риск (MANUAL_*) → ATR (RR_BINDING выкл) →
    дефолт стратегии (RR 2:1);
  - допуск на одну открытую «сделку» на (монета, TF) — как антиспам в лайве.

Результат: по каждой монете/TF/стратегии — сделки, winrate, PnL% без плеча,
доли выходов по TP/SL/таймауту. Данные — только реальные свечи Bybit
(синтетика честно помечается ошибкой, как и везде в проекте).
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

from portal_config import settings
from council import get_market
from strategies.registry import build_strategy
from strategies.selector import get_selector
from strategies import indicators as ta

_TF_MIN = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}
# столько дней истории реально прогнать (пагинация Bybit «съедает» до ~12000 баров)
_MAX_DAYS = {"1m": 5, "3m": 10, "5m": 20, "15m": 45, "30m": 90, "1h": 180, "4h": 180}
WINDOW = 300            # ширина «экрана» сканера (как limit=300 в лайве)
WARMUP = 250            # прогрев индикаторов
REPICK_EVERY = 6        # переподбор авто-стратегии каждые N баров
DEFAULT_HOLD_MIN = 240  # таймаут сделки, если ручное время не задано (4 ч)
MAX_BARS_TOTAL = 240_000  # защита от долгих прогонов


class BacktestError(Exception):
    pass


def _tf_ms(tf: str) -> int:
    return _TF_MIN.get(tf, 5) * 60_000


def _targets(entry: float, side: str, summ: dict, sig_tp, sig_sl) -> dict:
    """Приоритет целей — как в live_engine.scan_once."""
    tp2, sl2, mode = sig_tp, sig_sl, "strategy"
    if settings.manual_risk and entry:
        tp_m, sl_m = settings.MANUAL_TP_PCT, settings.MANUAL_SL_PCT
        tp2 = entry * (1 + tp_m / 100) if side == "LONG" else entry * (1 - tp_m / 100)
        sl2 = entry * (1 - sl_m / 100) if side == "LONG" else entry * (1 + sl_m / 100)
        mode = "manual"
    elif not settings.RR_BINDING:
        atr_pct = (summ or {}).get("atr_pct") or 0.0
        if atr_pct > 0 and entry:
            tp_move = min(6.0, max(0.4, settings.ATR_TP_MULT * atr_pct))
            sl_move = min(4.0, max(0.25, settings.ATR_SL_MULT * atr_pct))
            tp2 = entry * (1 + tp_move / 100) if side == "LONG" else entry * (1 - tp_move / 100)
            sl2 = entry * (1 - sl_move / 100) if side == "LONG" else entry * (1 + sl_move / 100)
            mode = "atr"
    if (not tp2 or not sl2) and entry:  # у стратегии нет целей → дефолт 2%/1%
        tp2 = entry * 1.02 if side == "LONG" else entry * 0.98
        sl2 = entry * 0.99 if side == "LONG" else entry * 1.01
        if mode == "strategy":
            mode = "default_2_1"
    return {"tp": tp2, "sl": sl2, "mode": mode}


async def _load_history(symbol: str, tf: str, days: int) -> List:
    """Диапазон дат → свечи (get_klines сам пагинирует до ~12000 баров)."""
    now = int(time.time() * 1000)
    start = now - days * 86_400_000 - WARMUP * _tf_ms(tf)
    market = get_market()
    candles = await market.get_klines(symbol, tf, limit=1000, start_ts=start, end_ts=now)
    if candles is None or (hasattr(candles, "__len__") and len(candles) == 0):
        raise BacktestError(f"{symbol} {tf}: Bybit не вернул свечи (пара отсутствует?)")
    # защита от тихой синтетики: свечи обязаны быть реальными
    try:
        from data import market_data as md
        src_now = md._STATUS.get("source")
        if src_now == "synthetic":
            raise BacktestError(f"{symbol} {tf}: Bybit недоступен, отдаётся учебный график — "
                                f"бэктест по нему не считаем. Включи сеть/VPN и повтори.")
    except ImportError:
        pass
    if len(candles) < WARMUP + 10:
        raise BacktestError(f"{symbol} {tf}: мало свечей ({len(candles)}) для прогрева индикаторов")
    return candles


def _simulate(symbol: str, tf: str, candles: List) -> dict:
    sel = get_selector()
    fixed = settings.SIGNAL_STRATEGY not in (None, "", "auto")
    min_conf = settings.MIN_CONFIDENCE
    hold_bars = max(1, int((settings.MANUAL_HOLD_MIN or DEFAULT_HOLD_MIN) / _TF_MIN.get(tf, 5)))
    tf_min = _TF_MIN.get(tf, 5)

    trades: List[dict] = []
    n = len(candles)
    last_type, last_pick_bar = None, -10**9
    open_t: Optional[dict] = None

    for i in range(max(WARMUP, 60), n - 1):
        c = candles[i]
        if open_t is not None:
            # выход: сначала проверяем SL (консервативно), затем TP, затем таймаут
            e, side = open_t["entry"], open_t["side"]
            hit, exit_px, reason = False, None, None
            if side == "LONG":
                if c.low <= open_t["sl"]:
                    hit, exit_px, reason = True, open_t["sl"], "SL"
                elif c.high >= open_t["tp"]:
                    hit, exit_px, reason = True, open_t["tp"], "TP"
            else:
                if c.high >= open_t["sl"]:
                    hit, exit_px, reason = True, open_t["sl"], "SL"
                elif c.low <= open_t["tp"]:
                    hit, exit_px, reason = True, open_t["tp"], "TP"
            if not hit:
                # breakeven/трейлинг по экстремуму бара — зеркало live-тикера (signal_gates)
                try:
                    import signal_gates as G
                    G.update_stops(settings, open_t, c.high if side == "LONG" else c.low)
                except Exception:
                    pass
            if not hit and i - open_t["i"] >= hold_bars:
                hit, exit_px, reason = True, c.close, "timeout"
            if hit:
                pnl = (exit_px - e) / e * 100
                if side == "SHORT":
                    pnl = -pnl
                open_t.update({"exit": exit_px, "exit_i": i, "pnl_pct": round(pnl, 3),
                               "reason": reason,
                               "duration_min": round((i - open_t["i"]) * tf_min)})
                trades.append(open_t)
                open_t = None
            continue

        # вход: авто-подбор (с переподбором раз в REPICK_EVERY баров)
        window = candles[max(0, i - WINDOW + 1):i + 1]
        if i - last_pick_bar >= REPICK_EVERY or last_type is None:
            if fixed:
                last_type = settings.SIGNAL_STRATEGY
            else:
                try:
                    last_type, _cand, _src = sel.auto_pick(symbol, tf, window)
                except Exception:
                    last_type = "momentum_breakout"
            last_pick_bar = i
        strat = build_strategy(last_type)
        if not strat:
            continue
        try:
            sig = strat.generate(window, len(window) - 1)
        except Exception:
            continue
        if not sig or sig.side.value == "NEUTRAL":
            continue
        if sig.confidence < min_conf:
            continue
        entry = sig.entry or c.close
        try:
            summ = ta.compute_feature_summary(window) or {}
        except Exception:
            summ = {}
        # зеркало квант-фильтров live_engine — единый модуль signal_gates
        try:
            import signal_gates as G
            if G.entry_block_reason(
                    settings, stype=last_type, side=sig.side.value, symbol=symbol,
                    hour_utc=(c.ts // 3_600_000) % 24,
                    vol_season=summ.get("vol_ratio_seasonal"),
                    atr_pct=summ.get("atr_pct")):
                continue
        except Exception:
            pass
        tg = _targets(entry, sig.side.value, summ, sig.tp, sig.sl)
        open_t = {"symbol": symbol, "tf": tf, "side": sig.side.value,
                  "strategy": getattr(strat, "name", last_type), "stype": last_type,
                  "i": i, "ts": c.ts, "entry": entry, "tp": tg["tp"], "sl": tg["sl"],
                  "mode": tg["mode"], "conf": round(sig.confidence, 3),
                  "hi": entry, "lo": entry,
                  "rsi": summ.get("rsi"), "atr_pct": summ.get("atr_pct"),
                  "vol_ratio": summ.get("volume_ratio"),
                  "vol_ratio_seasonal": summ.get("vol_ratio_seasonal"),
                  "hour": (c.ts // 3_600_000) % 24}

    return {"trades": trades, "bars": n}


def _agg(rows: List[dict]) -> dict:
    if not rows:
        return {"trades": 0}
    wins = [r for r in rows if r["pnl_pct"] > 0]
    gross_w = sum(r["pnl_pct"] for r in wins)
    gross_l = -sum(r["pnl_pct"] for r in rows if r["pnl_pct"] < 0)
    by_reason: Dict[str, int] = {}
    for r in rows:
        by_reason[r["reason"]] = by_reason.get(r["reason"], 0) + 1
    return {
        "trades": len(rows),
        "winrate": round(100 * len(wins) / len(rows), 1),
        "total_pnl_pct": round(sum(r["pnl_pct"] for r in rows), 2),
        "avg_pnl_pct": round(sum(r["pnl_pct"] for r in rows) / len(rows), 3),
        "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else (None if gross_w == 0 else 99.0),
        "avg_duration_min": round(sum(r["duration_min"] for r in rows) / len(rows)),
        "exits": by_reason,
    }


async def run_signal_backtest(symbols: List[str], tfs: List[str], days: int,
                              max_symbols: int = 6) -> dict:
    symbols = [s.strip().upper() for s in symbols if s.strip()][:max_symbols]
    tfs = [t for t in tfs if t in _TF_MIN] or [settings.DEFAULT_TIMEFRAME]
    if not symbols:
        raise BacktestError("Не выбраны монеты для бэктеста")
    settings.SIGNAL_TIMEFRAMES  # noqa — гарантируем загрузку конфига

    per_pair, all_trades, errors, notes = [], [], [], []
    bars_used = 0
    for sym in symbols:
        for tf in tfs:
            if bars_used > MAX_BARS_TOTAL:
                notes.append(f"стоп по лимиту баров ({MAX_BARS_TOTAL}) — часть комбинаций пропущена")
                break
            days_eff = min(days, _MAX_DAYS.get(tf, 20))
            if days_eff < days:
                notes.append(f"{sym} {tf}: период урезан до {days_eff} дн. (лимит истории для этого TF)")
            try:
                candles = await _load_history(sym, tf, days_eff)
                sim = _simulate(sym, tf, candles)
                bars_used += sim["bars"]
                rows = sim["trades"]
                all_trades.extend(rows)
                agg = _agg(rows)
                agg.update({"symbol": sym, "tf": tf, "days": days_eff, "bars": sim["bars"]})
                per_pair.append(agg)
            except BacktestError as e:
                errors.append(str(e))
            except Exception as e:
                errors.append(f"{sym} {tf}: {type(e).__name__}: {e}")

    # разрез по стратегиям
    by_strat: Dict[str, List[dict]] = {}
    for r in all_trades:
        by_strat.setdefault(r["strategy"], []).append(r)
    strategies = [dict(_agg(v), strategy=k) for k, v in
                  sorted(by_strat.items(), key=lambda kv: -len(kv[1]))]

    recent = sorted(all_trades, key=lambda r: -r["ts"])[:25]
    return {
        "params": {"symbols": symbols, "tfs": tfs, "days_requested": days,
                   "min_confidence": settings.MIN_CONFIDENCE,
                   "risk": ("manual TP {}% / SL {}% / {} мин".format(
                                settings.MANUAL_TP_PCT, settings.MANUAL_SL_PCT,
                                settings.MANUAL_HOLD_MIN or DEFAULT_HOLD_MIN)
                            if settings.manual_risk else
                            ("ATR графика" if not settings.RR_BINDING else "стратегия/2:1")),
                   "strategy_mode": ("фикс: " + settings.SIGNAL_STRATEGY
                                     if settings.SIGNAL_STRATEGY not in (None, "", "auto")
                                     else "auto-подбор")},
        "overall": _agg(all_trades),
        "per_pair": per_pair,
        "by_strategy": strategies,
        "recent_trades": [{k: r[k] for k in
                           ("symbol", "tf", "side", "strategy", "reason", "pnl_pct",
                            "duration_min", "conf", "entry", "exit", "mode")} for r in recent],
        "errors": errors,
        "notes": notes[:6],
        "synthetic": False,
        "built_at": int(time.time() * 1000),
    }
