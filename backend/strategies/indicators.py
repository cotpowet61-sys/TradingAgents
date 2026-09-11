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


def stochastic(high: np.ndarray, low: np.ndarray, close: np.ndarray,
               k_period: int = 14, d_period: int = 3):
    """Stochastic %K и %D."""
    k = np.full(len(close), np.nan)
    for i in range(len(close)):
        j = max(0, i - k_period + 1)
        hh = np.max(high[j:i + 1])
        ll = np.min(low[j:i + 1])
        k[i] = 100.0 if hh == ll else (close[i] - ll) / (hh - ll) * 100.0
    d = sma(k, d_period)
    return k, d


def adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14):
    """ADX + направленные индикаторы +DI/-DI (Wilder)."""
    n = len(close)
    up = np.zeros(n)
    down = np.zeros(n)
    for i in range(1, n):
        up[i] = high[i] - high[i - 1]
        down[i] = low[i - 1] - low[i]
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    # сглаживание Wilder
    def _wilder(x):
        out = np.full(n, np.nan)
        if n <= period:
            return out
        out[period] = np.sum(x[1:period + 1])
        for i in range(period + 1, n):
            out[i] = out[i - 1] - out[i - 1] / period + x[i]
        return out
    atr_w = _wilder(tr)
    plus_di = np.full(n, np.nan)
    minus_di = np.full(n, np.nan)
    dx = np.full(n, np.nan)
    for i in range(period, n):
        if atr_w[i] == 0:
            continue
        pdi = 100 * plus_dm[i] / atr_w[i]
        mdi = 100 * minus_dm[i] / atr_w[i]
        plus_di[i] = pdi
        minus_di[i] = mdi
        denom = pdi + mdi
        dx[i] = 100 * abs(pdi - mdi) / denom if denom else 0.0
    adx_line = sma(dx, period)
    return adx_line, plus_di, minus_di


def supertrend(high: np.ndarray, low: np.ndarray, close: np.ndarray,
               period: int = 10, multiplier: float = 3.0):
    """Supertrend: возвращает (направление, линия). Направление +1 = бычий."""
    n = len(close)
    atr_line = atr(high, low, close, period)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    direction = np.zeros(n)
    trend = np.full(n, np.nan)
    hl2 = (high + low) / 2
    for i in range(1, n):
        if np.isnan(atr_line[i]):
            continue
        ub = hl2[i] + multiplier * atr_line[i]
        lb = hl2[i] - multiplier * atr_line[i]
        upper[i] = ub if (np.isnan(upper[i - 1]) or ub < upper[i - 1] or close[i - 1] > upper[i - 1]) else upper[i - 1]
        lower[i] = lb if (np.isnan(lower[i - 1]) or lb > lower[i - 1] or close[i - 1] < lower[i - 1]) else lower[i - 1]
        if not np.isnan(trend[i - 1]):
            prev_dir = direction[i - 1]
            if prev_dir >= 0:
                direction[i] = -1 if close[i] < lower[i] else 1
            else:
                direction[i] = 1 if close[i] > upper[i] else -1
        else:
            direction[i] = 1
        trend[i] = lower[i] if direction[i] >= 0 else upper[i]
    return direction, trend


def parabolic_sar(high: np.ndarray, low: np.ndarray, step: float = 0.02, max_step: float = 0.2):
    """Parabolic SAR: возвращает массив SAR и направление (+1 восходящий)."""
    n = len(high)
    sar = np.full(n, np.nan)
    direction = np.ones(n)
    if n == 0:
        return sar, direction
    # начальная инициализация
    sar[0] = low[0]
    ep = high[0]
    af = step
    trend_up = True
    for i in range(1, n):
        if trend_up:
            sar[i] = sar[i - 1] + af * (ep - sar[i - 1])
            if low[i] < sar[i]:
                trend_up = False
                sar[i] = ep
                ep = low[i]
                af = step
                direction[i] = -1
            else:
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + step, max_step)
                if low[i - 1] < sar[i]:
                    sar[i] = low[i - 1]
                if low[i - 2] < sar[i] and i >= 2:
                    sar[i] = low[i - 2]
                direction[i] = 1
        else:
            sar[i] = sar[i - 1] - af * (sar[i - 1] - ep)
            if high[i] > sar[i]:
                trend_up = True
                sar[i] = ep
                ep = high[i]
                af = step
                direction[i] = 1
            else:
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + step, max_step)
                if high[i - 1] > sar[i]:
                    sar[i] = high[i - 1]
                if high[i - 2] > sar[i] and i >= 2:
                    sar[i] = high[i - 2]
                direction[i] = -1
    return sar, direction


def cci(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 20):
    """Commodity Channel Index."""
    n = len(close)
    out = np.full(n, np.nan)
    tp = (high + low + close) / 3.0
    for i in range(period - 1, n):
        seg = tp[i - period + 1: i + 1]
        mean = np.mean(seg)
        mad = np.mean(np.abs(seg - mean))
        out[i] = (tp[i] - mean) / (0.015 * mad) if mad else 0.0
    return out


def williams_r(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14):
    """Williams %R (от -100 до 0)."""
    n = len(close)
    out = np.full(n, np.nan)
    for i in range(period - 1, n):
        hh = np.max(high[i - period + 1: i + 1])
        ll = np.min(low[i - period + 1: i + 1])
        out[i] = -100.0 if hh == ll else (hh - close[i]) / (hh - ll) * -100.0
    return out


def mfi(high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray, period: int = 14):
    """Money Flow Index."""
    n = len(close)
    out = np.full(n, np.nan)
    tp = (high + low + close) / 3.0
    mf = tp * volume
    for i in range(period, n):
        pos = neg = 0.0
        for j in range(i - period + 1, i + 1):
            if tp[j] > tp[j - 1]:
                pos += mf[j]
            elif tp[j] < tp[j - 1]:
                neg += mf[j]
        out[i] = 100.0 if neg == 0 else 100 - 100 / (1 + pos / neg) if pos + neg else 50.0
    return out


def ichimoku(high: np.ndarray, low: np.ndarray, close: np.ndarray,
             tenkan: int = 9, kijun: int = 26, senkou: int = 52):
    """Упрощённый Ichimoku: возвращает (tenkan, kijun, senkou_span_a, senkou_span_b)."""
    n = len(close)
    def _mid(period):
        out = np.full(n, np.nan)
        for i in range(period - 1, n):
            out[i] = (np.max(high[i - period + 1: i + 1]) + np.min(low[i - period + 1: i + 1])) / 2
        return out
    ten = _mid(tenkan)
    kij = _mid(kijun)
    span_a = np.full(n, np.nan)
    span_b = np.full(n, np.nan)
    for i in range(senkou - 1, n):
        span_a[i] = (ten[i] + kij[i]) / 2
        span_b[i] = (np.max(high[i - senkou + 1: i + 1]) + np.min(low[i - senkou + 1: i + 1])) / 2
    return ten, kij, span_a, span_b


def zscore(close: np.ndarray, period: int = 20) -> np.ndarray:
    """Z-score цены относительно скользящей средней (для mean-reversion)."""
    n = len(close)
    out = np.full(n, np.nan)
    mean = sma(close, period)
    for i in range(period - 1, n):
        seg = close[i - period + 1: i + 1]
        std = np.std(seg)
        out[i] = (close[i] - mean[i]) / std if std else 0.0
    return out


def pivot_levels(high: np.ndarray, low: np.ndarray, close: np.ndarray, lookback: int = 5):
    """Свинг-пивоты: возвращает (support, resistance) — ближайшие уровни из прошлых баров."""
    n = len(close)
    support = np.full(n, np.nan)
    resistance = np.full(n, np.nan)
    for i in range(lookback, n):
        j = i - lookback
        hh = np.max(high[j:i])
        ll = np.min(low[j:i])
        support[i] = ll
        resistance[i] = hh
    return support, resistance


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
