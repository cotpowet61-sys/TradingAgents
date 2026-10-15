"""
Извлечение сигналов и разрешение выходов.

Почему разделено на два шага:

  1. `extract_signals` — тяжёлая часть (гонять 23 стратегии по каждому бару).
     Делается ОДИН раз на (монета × ТФ × стратегия) и сохраняется на диск.
  2. `resolve_trades` — лёгкая часть: по готовому потоку сигналов считаются
     сделки для ЛЮБОГО набора TP/SL/трейлинга. Именно так работает перебор
     целей: тяжёлые стратегии не пересчитываются, а перебираются десятки
     вариантов выхода за секунды.

Честность входа: сигнал фиксируется на закрытии бара i (только данные ≤ i),
вход — по открытию бара i+1 (`entry_mode="next_open"`). Это исключает главную
ошибку бэктестов — «вход по цене, которую мы увидели задним числом».
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from .config import CostModel, ExitSpec, TF_MINUTES
from . import barriers
from .features import FEATURE_NAMES, features_at, build_seasonal_baseline

# Портал живёт в portal/, стратегии — в backend/. Пути добавляем так же, как
# portal/backtest_local.py, чтобы импорты были идентичны рабочему приложению.
_HERE = os.path.dirname(os.path.abspath(__file__))
_RESEARCH = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_RESEARCH)
for _p in (os.path.join(_ROOT, "portal"), os.path.join(_ROOT, "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_cache: dict = {}


def _imports():
    """Ленивый импорт движка портала (тяжёлый из-за pydantic/настроек)."""
    if "mods" not in _cache:
        from data.market_data import Candle                      # noqa: N813
        from strategies.registry import build_strategy, BUILTIN_STRATEGIES
        _cache["mods"] = (Candle, build_strategy, BUILTIN_STRATEGIES)
    return _cache["mods"]


def all_strategy_types() -> list[str]:
    _Candle, _bs, builtin = _imports()
    return sorted(builtin.keys())


@dataclass
class SignalRow:
    symbol: str
    tf: str
    stype: str
    i: int
    ts: int
    side: str
    conf: float
    entry_close: float
    reason: str


def candles_from_df(df: pd.DataFrame) -> list:
    """DataFrame → список Candle (то же, что делает портал перед сканом)."""
    Candle, _bs, _builtin = _imports()
    opens = df["open"].to_numpy(float)
    highs = df["high"].to_numpy(float)
    lows = df["low"].to_numpy(float)
    closes = df["close"].to_numpy(float)
    vols = df["volume"].to_numpy(float)
    tss = df["ts"].to_numpy("int64")
    return [Candle(ts=int(t), open=float(o), high=float(h),
                   low=float(lo), close=float(c), volume=float(v))
            for t, o, h, lo, c, v in zip(tss, opens, highs, lows, closes, vols)]


def extract_signals(symbol: str, tf: str, df: pd.DataFrame, *,
                    strategies: Optional[Iterable[str]] = None,
                    min_confidence: float = 0.55, window: int = 300,
                    warmup: int = 250, spacing_bars: int = 2,
                    max_signals_per_strategy: int = 200_000,
                    progress: bool = False) -> pd.DataFrame:
    """Поток сигналов по всем стратегиям для одной монеты и ТФ."""
    Candle, build_strategy, builtin = _imports()
    stypes = list(strategies) if strategies else sorted(builtin.keys())
    candles = candles_from_df(df)
    n = len(candles)
    if n < warmup + 60:
        return pd.DataFrame()
    close = df["close"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    open_ = df["open"].to_numpy(float)
    vol = df["volume"].to_numpy(float)
    ts = df["ts"].to_numpy("int64")
    seasonal = build_seasonal_baseline(vol, ts, min(n, warmup))
    rows: list[dict] = []

    for stype in stypes:
        strat = build_strategy(stype)
        if strat is None:
            continue
        last_i = -10 ** 9
        last_side = None
        emitted = 0
        for i in range(warmup, n - 1):
            w = candles[i - window + 1:i + 1]
            try:
                sig = strat.generate(w, len(w) - 1)
            except Exception:      # noqa: BLE001 — одна стратегия не должна ломать прогон
                continue
            if sig is None or getattr(sig.side, "value", sig.side) == "NEUTRAL":
                continue
            side = getattr(sig.side, "value", str(sig.side))
            conf = float(sig.confidence or 0.0)
            if conf < min_confidence:
                continue
            # антиспам как в live: сигналы той же стороны слишком часто не берём
            if last_side == side and (i - last_i) < spacing_bars:
                continue
            px = float(close[i])
            if px <= 0:
                continue
            feat = features_at(open_, high, low, close, vol, ts, i, side=side,
                               conf=conf,
                               planned_rr=float(strat.tp_pct / max(strat.sl_pct, 1e-9)),
                               seasonal_baseline=seasonal)
            rows.append({
                "symbol": symbol, "tf": tf, "stype": stype, "strategy_name": getattr(strat, "name", stype),
                "i": i, "ts": int(ts[i]), "side": side, "conf": round(conf, 4),
                "entry_close": px, "reason": str(getattr(sig, "reason", ""))[:120],
                "atr_pct_sig": feat["atr_pct"],
                **{k: feat[k] for k in FEATURE_NAMES if k not in ("conf", "side_long", "planned_rr")},
            })
            last_i, last_side = i, side
            emitted += 1
            if emitted >= max_signals_per_strategy:
                break
        if progress:
            print(f"    {symbol} {tf} {stype}: {emitted} сигналов")
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["i", "stype"]).reset_index(drop=True)
    return out


def resolve_trades(signals: pd.DataFrame, df: pd.DataFrame, spec: ExitSpec,
                   costs: CostModel, *, tf: str, entry_mode: str = "next_open",
                   cooldown_bars: int = 2, one_position: bool = True,
                   group_by: str = "stype", max_gap_atr: float = 3.0,
                   label: str = "") -> pd.DataFrame:
    """Поток сигналов → сделки по правилам выхода spec.

    group_by:
      • "stype" — каждая стратегия считается независимо (видно чистый вклад каждой);
      • "pair"  — одна сделка на пару в моменте (как в живом сканере портала).
    max_gap_atr — защита от нереалистичных входов: если открытие следующего бара
    ушло от цены сигнала больше чем на max_gap_atr × ATR%, сделку пропускаем
    (в бою такой гэп означал бы исполнение сильно хуже расчётного).
    """
    if signals is None or signals.empty:
        return pd.DataFrame()
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    open_ = df["open"].to_numpy(float)
    n = len(close)
    tf_min = TF_MINUTES.get(tf, 15)
    hold_bars = spec.max_hold_bars or max(1, int(round(spec.max_hold_hours * 60 / tf_min)))
    out: list[dict] = []
    busy: dict[str, int] = {}
    last: dict[str, tuple] = {}

    for rec in signals.sort_values("i").itertuples(index=False):
        i = int(rec.i)
        if i + 1 >= n:
            break
        side = rec.side
        key = str(rec.stype) if group_by == "stype" else "pair"
        if one_position and i <= busy.get(key, -1):
            continue
        prev_side, prev_i = last.get(key, (None, -10 ** 9))
        if prev_side == side and (i - prev_i) < cooldown_bars:
            continue
        atr_pct = float(getattr(rec, "atr_pct_sig", 0.0) or 0.0)
        tp_pct, sl_pct = spec_targets(spec, atr_pct)
        if tp_pct <= 0 or sl_pct <= 0:
            continue
        entry_index = i + 1
        entry = float(open_[entry_index]) if entry_mode == "next_open" else float(rec.entry_close)
        if entry <= 0:
            continue
        if max_gap_atr and atr_pct > 0:
            gap_pct = abs(entry - float(rec.entry_close)) / float(rec.entry_close) * 100.0
            if gap_pct > max_gap_atr * atr_pct:
                continue
        tp, sl = barriers.clamp_targets(entry, side, tp_pct, sl_pct)
        res = barriers.simulate_trade(high, low, close, entry_index, entry, side, tp, sl,
                                      spec, costs, hold_bars)
        row = {
            "symbol": rec.symbol, "tf": tf, "stype": rec.stype,
            "strategy_name": rec.strategy_name, "side": side,
            "ts_signal": int(rec.ts), "ts_entry": int(df["ts"].iloc[entry_index]),
            "i_signal": i, "entry": entry, "tp": round(tp, 8), "sl": round(sl, 8),
            "tp_pct": round(tp_pct, 4), "sl_pct": round(sl_pct, 4),
            "conf": float(rec.conf), "reason": rec.reason,
            "outcome": res.outcome, "exit_price": res.exit_price,
            "pnl_gross_pct": res.pnl_gross_pct, "pnl_net_pct": res.pnl_net_pct,
            "hold_min": round(res.bars_held * tf_min, 1), "bars_held": res.bars_held,
            "mae_pct": res.mae_pct, "mfe_pct": res.mfe_pct,
            "exit_spec": label or spec.mode,
        }
        for name in FEATURE_NAMES:
            row[name] = float(getattr(rec, name, 0.0) or 0.0)
        row["side_long"] = 1.0 if side == "LONG" else 0.0
        out.append(row)
        busy[key] = res.exit_index
        last[key] = (side, i)
    return pd.DataFrame(out)


def spec_targets(spec: ExitSpec, atr_pct: float) -> tuple[float, float]:
    """Цели TP/SL в процентах по режиму spec (fixed | atr)."""
    if spec.mode == "fixed" or atr_pct <= 0:
        tp = spec.fixed_tp_pct
        sl = spec.fixed_sl_pct
    else:
        tp = atr_pct * spec.tp_mult
        sl = atr_pct * spec.sl_mult
    tp = min(max(tp, spec.tp_min_pct), spec.tp_max_pct)
    sl = min(max(sl, spec.sl_min_pct), spec.sl_max_pct)
    return float(tp), float(sl)
