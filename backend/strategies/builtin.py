"""
Встроенные дэйтрейдинг-стратегии (лонг и шорт).

Каждая реализует generate(candles, index) -> Signal. Все сигналы автоматически
удовлетворяют глобальным правилам: TP >= 2%, SL задан, удержание <= 4ч (см. base).
"""
from __future__ import annotations

from typing import List

import numpy as np

from .base import Strategy, Signal, SignalSide
from . import indicators as ta
from data.market_data import Candle


class MomentumBreakout(Strategy):
    """Пробой N-барового диапазона (Donchian) с фильтром RSI и объёма."""
    type = "momentum_breakout"
    name = "Momentum Breakout"
    description = "Лонг при пробое верхней границы N баров, шорт при пробое нижней. Фильтры: RSI, объём."
    default_params = {"period": 20, "rsi_min": 55, "rsi_max": 45, "volume_filter": 1.0}

    def generate(self, candles: List[Candle], index: int) -> Signal:
        p = self.params
        if index < p["period"] + 2:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        window = candles[:index + 1]
        o, h, l, c, v = (np.array([x.open for x in window]), np.array([x.high for x in window]),
                         np.array([x.low for x in window]), np.array([x.close for x in window]),
                         np.array([x.volume for x in window]))
        upper, lower = ta.donchian(h, l, p["period"])
        r = ta.rsi(c)
        vol_ma = ta.sma(v, 20)
        cur, prev = index, index - 1
        prev_high = upper[prev] if not np.isnan(upper[prev]) else None
        prev_low = lower[prev] if not np.isnan(lower[prev]) else None
        vol_ok = v[cur] >= vol_ma[cur] * p["volume_filter"] if not np.isnan(vol_ma[cur]) else True

        if prev_high is not None and c[cur] > prev_high and not np.isnan(r[cur]) and r[cur] > p["rsi_min"] and vol_ok:
            return self._build_signal(SignalSide.LONG, candles, index, 0.65,
                                      f"Пробой вверх уровня {round(prev_high,4)}, RSI={round(r[cur],1)}")
        if prev_low is not None and c[cur] < prev_low and not np.isnan(r[cur]) and r[cur] < p["rsi_max"] and vol_ok:
            return self._build_signal(SignalSide.SHORT, candles, index, 0.65,
                                      f"Пробой вниз уровня {round(prev_low,4)}, RSI={round(r[cur],1)}")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Нет пробоя")


class RsiReversal(Strategy):
    """Разворот по RSI из зон перекупленности/перепроданности."""
    type = "rsi_reversal"
    name = "RSI Reversal"
    description = "Лонг при выходе RSI из зоны перепроданности (<30), шорт при выходе из перекупленности (>70)."
    default_params = {"period": 14, "oversold": 30, "overbought": 70}

    def generate(self, candles: List[Candle], index: int) -> Signal:
        p = self.params
        if index < p["period"] + 1:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        window = candles[:index + 1]
        c = np.array([x.close for x in window])
        r = ta.rsi(c, p["period"])
        cur, prev = r[index], r[index - 1]
        if np.isnan(cur) or np.isnan(prev):
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "RSI не готов")
        if prev < p["oversold"] <= cur:
            return self._build_signal(SignalSide.LONG, candles, index, 0.55,
                                      f"RSI вышел из перепроданности: {round(prev,1)} -> {round(cur,1)}")
        if prev > p["overbought"] >= cur:
            return self._build_signal(SignalSide.SHORT, candles, index, 0.55,
                                      f"RSI вышел из перекупленности: {round(prev,1)} -> {round(cur,1)}")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "RSI в нейтральной зоне")


class EmaCrossover(Strategy):
    """Пересечение быстрой/медленной EMA с трендовым фильтром."""
    type = "ema_crossover"
    name = "EMA Crossover"
    description = "Лонг при пересечении быстрой EMA вверх медленной, шорт — вниз."
    default_params = {"fast": 9, "slow": 21}

    def generate(self, candles: List[Candle], index: int) -> Signal:
        p = self.params
        if index < p["slow"] + 2:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        window = candles[:index + 1]
        c = np.array([x.close for x in window])
        f = ta.ema(c, p["fast"])
        s = ta.ema(c, p["slow"])
        cur_diff = f[index] - s[index]
        prev_diff = f[index - 1] - s[index - 1]
        if prev_diff <= 0 < cur_diff:
            return self._build_signal(SignalSide.LONG, candles, index, 0.6,
                                      f"Быстрая EMA пересекла медленную вверх ({p['fast']}/{p['slow']})")
        if prev_diff >= 0 > cur_diff:
            return self._build_signal(SignalSide.SHORT, candles, index, 0.6,
                                      f"Быстрая EMA пересекла медленную вниз ({p['fast']}/{p['slow']})")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Нет пересечения")


class BollingerBreakout(Strategy):
    """Пробой полос Боллинджера с возвратом (mean-reversion) или по тренду (breakout)."""
    type = "bollinger_breakout"
    name = "Bollinger Bands"
    description = "Режим 'breakout' — вход по пробою полосы; 'reversal' — вход на возврат к средней."
    default_params = {"period": 20, "num_std": 2.0, "mode": "breakout"}

    def generate(self, candles: List[Candle], index: int) -> Signal:
        p = self.params
        if index < p["period"] + 1:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        window = candles[:index + 1]
        c = np.array([x.close for x in window])
        mid, upper, lower = ta.bollinger(c, p["period"], p["num_std"])
        cur, mid_v = c[index], mid[index]
        up_v, lo_v = upper[index], lower[index]
        if np.isnan(mid_v):
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Полосы не готовы")

        if p["mode"] == "breakout":
            if cur > up_v:
                return self._build_signal(SignalSide.LONG, candles, index, 0.6,
                                          f"Пробой верхней полосы Боллинджера")
            if cur < lo_v:
                return self._build_signal(SignalSide.SHORT, candles, index, 0.6,
                                          f"Пробой нижней полосы Боллинджера")
        else:  # reversal
            prev = c[index - 1]
            if prev < lo_v and cur >= lo_v:
                return self._build_signal(SignalSide.LONG, candles, index, 0.55,
                                          "Возврат от нижней полосы к средней")
            if prev > up_v and cur <= up_v:
                return self._build_signal(SignalSide.SHORT, candles, index, 0.55,
                                          "Возврат от верхней полосы к средней")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Нет сигнала")


class VwapPullback(Strategy):
    """Откат к VWAP с продолжением тренда."""
    type = "vwap_pullback"
    name = "VWAP Pullback"
    description = "В восходящем тренде лонг на откате к VWAP, в нисходящем — шорт."
    default_params = {"ema_trend": 50, "buffer_pct": 0.15}

    def generate(self, candles: List[Candle], index: int) -> Signal:
        p = self.params
        if index < p["ema_trend"] + 2:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        window = candles[:index + 1]
        o, h, l, c, v = (np.array([x.open for x in window]), np.array([x.high for x in window]),
                         np.array([x.low for x in window]), np.array([x.close for x in window]),
                         np.array([x.volume for x in window]))
        vwap_line = ta.vwap(window, period=0)
        ema_t = ta.ema(c, p["ema_trend"])
        cur, vw, trend = c[index], vwap_line[index], ema_t[index]
        buf = p["buffer_pct"] / 100.0
        if np.isnan(vw) or np.isnan(trend):
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "VWAP/EMA не готовы")
        # лонг: цена выше трендовой EMA, откат к VWAP
        if cur > trend and cur <= vw * (1 + buf):
            return self._build_signal(SignalSide.LONG, candles, index, 0.55,
                                      f"Откат к VWAP в восходящем тренде")
        if cur < trend and cur >= vw * (1 - buf):
            return self._build_signal(SignalSide.SHORT, candles, index, 0.55,
                                      f"Откат к VWAP в нисходящем тренде")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Нет отката к VWAP")


BUILTIN_STRATEGIES: dict = {
    cls.type: cls for cls in (MomentumBreakout, RsiReversal, EmaCrossover, BollingerBreakout, VwapPullback)
}

STRATEGY_TYPES = list(BUILTIN_STRATEGIES.keys())
