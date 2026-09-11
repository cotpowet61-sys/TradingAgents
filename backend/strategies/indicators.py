"""
Технические индикаторы на numpy. Все функции работают «в потоке» (значение в точке i
зависит только от данных ≤ i), что исключает look-ahead в бэктесте.
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np

from data.market_data import Candle


def _np(candles: List[Candle]):
    o = np.array([c.open for c in candles], dtype=float)
    h = np.array([c.high for c in candles], dtype=float)
    l = np.array([c.low for c in candles], dtype=float)
    c = np.array([c.close for c in candles], dtype=float)
    v = np.array([c.volume for c in candles], dtype=float)
    return o, h, l, c, v


def sma(values: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if period <= 0 or len(values) < period:
        return out
    cs = np.cumsum(np.insert(values, 0, 0.0))
    out[period - 1:] = (cs[period:] - cs[:-period]) / period
    return out


def ema(values: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if period <= 0 or len(values) == 0:
        return out
    alpha = 2.0 / (period + 1)
    prev = values[0]
    out[0] = prev
    for i in range(1, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    out = np.full(len(close), np.nan)
    if len(close) <= period:
        return out
    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = np.mean(gain[:period])
    avg_loss = np.mean(loss[:period])
    out[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, len(close)):
        avg_gain = (avg_gain * (period - 1) + gain[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i - 1]) / period
        out[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    out = np.full(len(close), np.nan)
    if len(close) <= period:
        return out
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])))
    tr = np.insert(tr, 0, high[0] - low[0])
    out[period] = np.mean(tr[:period + 1])
    for i in range(period + 1, len(close)):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def bollinger(close: np.ndarray, period: int = 20, num_std: float = 2.0):
    mid = sma(close, period)
    std = np.full(len(close), np.nan)
    for i in range(period - 1, len(close)):
        std[i] = np.std(close[i - period + 1: i + 1])
    upper = mid + num_std * std
    lower = mid - num_std * std
    return mid, upper, lower


def macd(close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    line = ema_fast - ema_slow
    sig = ema(line, signal)
    hist = line - sig
    return line, sig, hist


def vwap(candles: List[Candle], period: int = 0) -> np.ndarray:
    """VWAP за сессию (period=0) или скользящий (period>0)."""
    o, h, l, c, v = _np(candles)
    typical = (h + l + c) / 3.0
    out = np.full(len(c), np.nan)
    if period <= 0:
        cum_pv = np.cumsum(typical * v)
        cum_v = np.cumsum(v)
        out = np.where(cum_v > 0, cum_pv / np.where(cum_v == 0, 1, cum_v), np.nan)
    else:
        for i in range(len(c)):
            j = max(0, i - period + 1)
            seg = slice(j, i + 1)
            pv = np.sum(typical[seg] * v[seg])
            vv = np.sum(v[seg])
            out[i] = pv / vv if vv > 0 else np.nan
    return out


def donchian(high: np.ndarray, low: np.ndarray, period: int = 20):
    upper = np.full(len(high), np.nan)
    lower = np.full(len(low), np.nan)
    for i in range(len(high)):
        j = max(0, i - period + 1)
        upper[i] = np.max(high[j:i + 1])
        lower[i] = np.min(low[j:i + 1])
    return upper, lower


def compute_feature_summary(candles: List[Candle]) -> dict:
    """Краткая техническая сводка для передачи агентам (текст/JSON)."""
    if not candles:
        return {}
    o, h, l, c, v = _np(candles)
    last = c[-1]
    change = (c[-1] / c[0] - 1) * 100 if c[0] else 0
    r = rsi(c)
    a = atr(h, l, c)
    vol_ma = sma(v, 20)
    vol_ratio = (v[-1] / vol_ma[-1]) if not np.isnan(vol_ma[-1]) and vol_ma[-1] else 1.0
    return {
        "price": round(float(last), 6),
        "change_pct": round(float(change), 2),
        "rsi": round(float(r[-1]), 2) if not np.isnan(r[-1]) else None,
        "atr": round(float(a[-1]), 6) if not np.isnan(a[-1]) else None,
        "atr_pct": round(float(a[-1] / last * 100), 3) if last and not np.isnan(a[-1]) else None,
        "volume_ratio": round(float(vol_ratio), 2),
        "high": round(float(h[-1]), 6),
        "low": round(float(l[-1]), 6),
        "candles": len(candles),
    }
