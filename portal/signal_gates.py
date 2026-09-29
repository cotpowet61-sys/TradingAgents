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
