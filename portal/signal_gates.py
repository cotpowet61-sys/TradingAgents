# -*- coding: utf-8 -*-
"""
Единые фильтры входа и механика выхода для сканера.

Используется ТРЕМЯ местами (зеркально, чтобы лайв и бэктест не расходились):
  • portal/live_engine.py        — онлайн-выдача;
  • portal/signal_backtest.py    — серверный бэктест;
  • portal/backtest_local.py     — локальный харнесс.

Входы фильтруем только по замеренным на реальных данных зонам
(report → analyze → .env). Выход: breakeven-подтяжка стопа и трейлинг —
механизм, который легитимно поднимает winrate (проигрыш → нулевой выход),
а не «подгонка» истории.
"""
from __future__ import annotations

from typing import Optional


def _ranges(spec: str):
    """"1.0-2.0,3.0-999" → [(1.0,2.0),(3.0,999.0)]"""
    out = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk or "-" not in chunk:
            continue
        try:
            lo, hi = chunk.split("-", 1)
            out.append((float(lo), float(hi)))
        except ValueError:
            continue
    return out


def _in_ranges(x: Optional[float], spec: str) -> bool:
    if x is None:
        return False
    for lo, hi in _ranges(spec):
        if lo <= x < hi:
            return True
    return False


def entry_block_reason(settings, *, stype: str, side: str, symbol: str,
                       hour_utc: Optional[int], vol_season: Optional[float],
                       atr_pct: Optional[float]) -> Optional[str]:
    """None = пропускаем сигнал; строка = причина блока (для логов/дебага)."""
    s = settings
    # 1) стоп-лист стратегий: stype | stype:SYMBOL | stype/SIDE
    bl = {x.strip() for x in (getattr(s, "STYPE_BLOCKLIST", "") or "").split(",") if x.strip()}
    if stype in bl or f"{stype}:{symbol}" in bl or f"{stype}/{side}" in bl:
        return "stype_blocklist"
    # 2) запретные зоны сезонного объёма (Kalman-фича)
    if vol_season is not None and _in_ranges(float(vol_season), getattr(s, "VOL_SEASON_BLOCK", "") or ""):
        return "vol_season_block"
    # 3) запретные зоны ATR%
    if atr_pct is not None and _in_ranges(float(atr_pct), getattr(s, "ATR_PCT_BLOCK", "") or ""):
        return "atr_pct_block"
    # 4) запретные часы UTC
    hb = getattr(s, "HOURS_BLOCK", "") or ""
    if hb and hour_utc is not None and str(int(hour_utc)) in {h.strip() for h in hb.split(",") if h.strip()}:
        return "hours_block"
    # 5) старый сезонный порог (min) — если включён явно
    if getattr(s, "VOL_SEASON_GATE", False) and vol_season is not None:
        vst = {x.strip() for x in (getattr(s, "VOL_SEASON_STYPES", "") or "").split(",") if x.strip()}
        if float(vol_season) < float(getattr(s, "VOL_SEASON_MIN", 1.0)) and (not vst or stype in vst):
            return "vol_season_min"
    return None


def update_stops(settings, sig: dict, px: float) -> bool:
    """Breakeven + трейлинг по свежей цене. sig: {side, entry, sl, hi/lo}.
    Возвращает True, если стоп реально подвинулся (для логов)."""
    side = sig.get("side")
    entry = sig.get("entry") or sig.get("price") or 0
    sl = sig.get("sl") or 0
    if not side or not entry or not sl or not px:
        return False
    be_t = float(getattr(settings, "BE_TRIGGER_PCT", 0) or 0)
    be_o = float(getattr(settings, "BE_OFFSET_PCT", 0.15) or 0.15)
    tr_s = float(getattr(settings, "TRAIL_START_PCT", 0) or 0)
    tr_d = float(getattr(settings, "TRAIL_PCT", 0) or 0)
    if side == "LONG":
        hi = max(float(sig.get("hi") or entry), px)
        sig["hi"] = hi
        pnl_best = (hi - entry) / entry * 100
        new_sl = sl
        if be_t > 0 and pnl_best >= be_t:
            new_sl = max(new_sl, entry * (1 + be_o / 100))
        if tr_s > 0 and tr_d > 0 and pnl_best >= tr_s:
            new_sl = max(new_sl, hi * (1 - tr_d / 100))
    else:  # SHORT
        lo = min(float(sig.get("lo") or entry), px)
        sig["lo"] = lo
        pnl_best = (entry - lo) / entry * 100
        new_sl = sl
        if be_t > 0 and pnl_best >= be_t:
            new_sl = min(new_sl, entry * (1 - be_o / 100))
        if tr_s > 0 and tr_d > 0 and pnl_best >= tr_s:
            new_sl = min(new_sl, lo * (1 + tr_d / 100))
    # стоп движем только в прибыльную сторону и не за текущую цену
    if side == "LONG":
        moved = new_sl > sl and new_sl < px
    else:
        moved = new_sl < sl and new_sl > px
    if moved:
        sig["sl"] = new_sl
    return moved


def compute_targets(settings, entry: float, side: str,
                    atr_pct: float | None, sig_tp, sig_sl) -> dict:
    """Плавающие цели TP/SL в адекватных рамках (сторожки против 0.001%/10%).

    Приоритет:
      1) ручной риск-фактор (MANUAL_TP/SL) — как задано, без клампа;
      2) ATR-режим (по умолчанию): move = множитель × ATR%, но в рамках
         TP_PCT_MIN..TP_PCT_MAX / SL_PCT_MIN..SL_PCT_MAX;
      3) цели стратегии — поджимаются в те же рамки;
      4) запасной плавающий дефолт (середина рамок);
      5) RR_BINDING=True (наследие) — старый строгий 2%/1%, если ATR нет.
    """
    lo_tp = float(getattr(settings, "TP_PCT_MIN", 0.5) or 0.5)
    hi_tp = float(getattr(settings, "TP_PCT_MAX", 3.0) or 3.0)
    lo_sl = float(getattr(settings, "SL_PCT_MIN", 0.25) or 0.25)
    hi_sl = float(getattr(settings, "SL_PCT_MAX", 2.5) or 2.5)

    def mk(e: float, sd: str, tp_move: float, sl_move: float):
        if sd == "LONG":
            return e * (1 + tp_move / 100), e * (1 - sl_move / 100)
        return e * (1 - tp_move / 100), e * (1 + sl_move / 100)

    tp2, sl2, mode = sig_tp, sig_sl, "strategy"
    if settings.manual_risk and entry:
        tp2, sl2 = mk(entry, side, settings.MANUAL_TP_PCT, settings.MANUAL_SL_PCT)
        mode = "manual"
    elif entry and (atr_pct or 0) > 0:
        tp_move = min(hi_tp, max(lo_tp, float(getattr(settings, "ATR_TP_MULT", 1.6)) * atr_pct))
        sl_move = min(hi_sl, max(lo_sl, float(getattr(settings, "ATR_SL_MULT", 1.2)) * atr_pct))
        tp2, sl2 = mk(entry, side, tp_move, sl_move)
        mode = "atr"
    elif entry and getattr(settings, "RR_BINDING", False):
        tp2, sl2 = mk(entry, side, 2.0, 1.0)
        mode = "rr21"

    if mode == "strategy" and entry and tp2 and sl2:
        # цели стратегии — тоже в адекватных рамках
        tp_move = abs(tp2 - entry) / entry * 100
        sl_move = abs(entry - sl2) / entry * 100
        tp_move = min(hi_tp, max(lo_tp, tp_move))
        sl_move = min(hi_sl, max(lo_sl, sl_move))
        tp2, sl2 = mk(entry, side, tp_move, sl_move)
        mode = "strategy_clamped"

    if (not tp2 or not sl2) and entry:
        tp2, sl2 = mk(entry, side, (lo_tp + hi_tp) / 2 if mode != "rr21" else 2.0,
                      (lo_sl + hi_sl) / 2 if mode != "rr21" else 1.0)
        if mode == "strategy":
            mode = "default_float"
    return {"tp": tp2, "sl": sl2, "mode": mode}
