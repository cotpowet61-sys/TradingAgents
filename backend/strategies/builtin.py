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


class MacdCrossover(Strategy):
    """Пересечение MACD с сигнальной линией."""
    type = "macd_crossover"
    name = "MACD Crossover"
    description = "Лонг при пересечении MACD вверх сигнальной линии, шорт — вниз."
    default_params = {"fast": 12, "slow": 26, "signal": 9}

    def generate(self, candles, index):
        p = self.params
        if index < p["slow"] + p["signal"] + 2:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        c = np.array([x.close for x in candles[:index + 1]])
        line, sig, hist = ta.macd(c, p["fast"], p["slow"], p["signal"])
        cur, prev = line[index] - sig[index], line[index - 1] - sig[index - 1]
        if prev <= 0 < cur:
            return self._build_signal(SignalSide.LONG, candles, index, 0.6, "MACD пересек сигнальную вверх")
        if prev >= 0 > cur:
            return self._build_signal(SignalSide.SHORT, candles, index, 0.6, "MACD пересек сигнальную вниз")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Нет пересечения MACD")


class MacdHistogram(Strategy):
    """Разворот гистограммы MACD (ослабление импульса)."""
    type = "macd_histogram"
    name = "MACD Histogram Reversal"
    description = "Разворот импульса: гистограмма MACD меняет знак прироста."
    default_params = {"fast": 12, "slow": 26, "signal": 9}

    def generate(self, candles, index):
        p = self.params
        if index < p["slow"] + p["signal"] + 3:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        c = np.array([x.close for x in candles[:index + 1]])
        line, sig, hist = ta.macd(c, p["fast"], p["slow"], p["signal"])
        if np.isnan(hist[index]) or np.isnan(hist[index - 1]) or np.isnan(hist[index - 2]):
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "MACD не готов")
        # рост гистограммы снизу вверх -> лонг, сверху вниз -> шорт
        if hist[index - 2] < hist[index - 1] < hist[index] and hist[index - 2] < 0:
            return self._build_signal(SignalSide.LONG, candles, index, 0.55, "Гистограмма MACD растёт после спада")
        if hist[index - 2] > hist[index - 1] > hist[index] and hist[index - 2] > 0:
            return self._build_signal(SignalSide.SHORT, candles, index, 0.55, "Гистограмма MACD падает после роста")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Нет разворота гистограммы")


class StochasticCrossover(Strategy):
    """Пересечение %K/%D стохастика из зон перекупленности/перепроданности."""
    type = "stochastic_crossover"
    name = "Stochastic Crossover"
    description = "Лонг при %K>%D в зоне перепроданности, шорт при %K<%D в перекупленности."
    default_params = {"k": 14, "d": 3, "oversold": 20, "overbought": 80}

    def generate(self, candles, index):
        p = self.params
        if index < p["k"] + p["d"] + 2:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        w = candles[:index + 1]
        h = np.array([x.high for x in w]); l = np.array([x.low for x in w]); c = np.array([x.close for x in w])
        k, d = ta.stochastic(h, l, c, p["k"], p["d"])
        if np.isnan(k[index]) or np.isnan(d[index]) or np.isnan(k[index - 1]) or np.isnan(d[index - 1]):
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Стохастик не готов")
        if k[index - 1] <= d[index - 1] and k[index] > d[index] and k[index] < p["oversold"]:
            return self._build_signal(SignalSide.LONG, candles, index, 0.6, "Стохастик развернулся вверх из перепроданности")
        if k[index - 1] >= d[index - 1] and k[index] < d[index] and k[index] > p["overbought"]:
            return self._build_signal(SignalSide.SHORT, candles, index, 0.6, "Стохастик развернулся вниз из перекупленности")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Нет сигнала стохастика")


class AdxTrend(Strategy):
    """Тренд по ADX и направлению +DI/-DI."""
    type = "adx_trend"
    name = "ADX Trend"
    description = "Вход по направлению тренда, когда ADX подтверждает силу (>threshold)."
    default_params = {"period": 14, "threshold": 20}

    def generate(self, candles, index):
        p = self.params
        if index < p["period"] * 2 + 2:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        w = candles[:index + 1]
        h = np.array([x.high for x in w]); l = np.array([x.low for x in w]); c = np.array([x.close for x in w])
        adx_line, pdi, mdi = ta.adx(h, l, c, p["period"])
        if np.isnan(adx_line[index]):
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "ADX не готов")
        if adx_line[index] < p["threshold"]:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "ADX ниже порога — тренда нет")
        if pdi[index] > mdi[index] and pdi[index - 1] <= mdi[index - 1]:
            return self._build_signal(SignalSide.LONG, candles, index, 0.62, f"Сильный бычий тренд (ADX={round(adx_line[index],1)})")
        if mdi[index] > pdi[index] and mdi[index - 1] <= pdi[index - 1]:
            return self._build_signal(SignalSide.SHORT, candles, index, 0.62, f"Сильный медвежий тренд (ADX={round(adx_line[index],1)})")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Нет нового тренда")


class SupertrendFollow(Strategy):
    """Supertrend: вход при смене направления."""
    type = "supertrend"
    name = "Supertrend"
    description = "Следование за Supertrend: лонг при смене на бычий, шорт при смене на медвежий."
    default_params = {"period": 10, "multiplier": 3.0}

    def generate(self, candles, index):
        p = self.params
        if index < p["period"] + 2:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        w = candles[:index + 1]
        h = np.array([x.high for x in w]); l = np.array([x.low for x in w]); c = np.array([x.close for x in w])
        direction, trend = ta.supertrend(h, l, c, p["period"], p["multiplier"])
        prev, cur = direction[index - 1], direction[index]
        if prev < 0 <= cur:
            return self._build_signal(SignalSide.LONG, candles, index, 0.63, "Supertrend развернулся вверх")
        if prev > 0 >= cur:
            return self._build_signal(SignalSide.SHORT, candles, index, 0.63, "Supertrend развернулся вниз")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Supertrend без смены направления")


class ParabolicSar(Strategy):
    """Parabolic SAR: смена направления."""
    type = "parabolic_sar"
    name = "Parabolic SAR"
    description = "Вход по развороту Parabolic SAR."
    default_params = {"step": 0.02, "max_step": 0.2}

    def generate(self, candles, index):
        p = self.params
        if index < 4:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        w = candles[:index + 1]
        h = np.array([x.high for x in w]); l = np.array([x.low for x in w])
        sar, direction = ta.parabolic_sar(h, l, p["step"], p["max_step"])
        prev, cur = direction[index - 1], direction[index]
        if prev < 0 <= cur:
            return self._build_signal(SignalSide.LONG, candles, index, 0.58, "SAR развернулся вверх")
        if prev > 0 >= cur:
            return self._build_signal(SignalSide.SHORT, candles, index, 0.58, "SAR развернулся вниз")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "SAR без разворота")


class CciReversal(Strategy):
    """CCI: выход из экстремальных зон."""
    type = "cci_reversal"
    name = "CCI Reversal"
    description = "Лонг при выходе CCI из зоны <-100, шорт при выходе из зоны >+100."
    default_params = {"period": 20, "low": -100, "high": 100}

    def generate(self, candles, index):
        p = self.params
        if index < p["period"] + 1:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        w = candles[:index + 1]
        h = np.array([x.high for x in w]); l = np.array([x.low for x in w]); c = np.array([x.close for x in w])
        ci = ta.cci(h, l, c, p["period"])
        cur, prev = ci[index], ci[index - 1]
        if np.isnan(cur) or np.isnan(prev):
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "CCI не готов")
        if prev < p["low"] <= cur:
            return self._build_signal(SignalSide.LONG, candles, index, 0.56, "CCI вышел из зоны перепроданности")
        if prev > p["high"] >= cur:
            return self._build_signal(SignalSide.SHORT, candles, index, 0.56, "CCI вышел из зоны перекупленности")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "CCI в нейтральной зоне")


class WilliamsR(Strategy):
    """Williams %R: отскок из экстремумов."""
    type = "williams_r"
    name = "Williams %R"
    description = "Лонг при выходе %R из зоны <-80, шорт при выходе из зоны >-20."
    default_params = {"period": 14, "oversold": -80, "overbought": -20}

    def generate(self, candles, index):
        p = self.params
        if index < p["period"] + 1:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        w = candles[:index + 1]
        h = np.array([x.high for x in w]); l = np.array([x.low for x in w]); c = np.array([x.close for x in w])
        wr = ta.williams_r(h, l, c, p["period"])
        cur, prev = wr[index], wr[index - 1]
        if np.isnan(cur) or np.isnan(prev):
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "%R не готов")
        if prev < p["oversold"] <= cur:
            return self._build_signal(SignalSide.LONG, candles, index, 0.56, "%R вышел из перепроданности")
        if prev > p["overbought"] >= cur:
            return self._build_signal(SignalSide.SHORT, candles, index, 0.56, "%R вышел из перекупленности")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "%R в нейтральной зоне")


class MfiReversal(Strategy):
    """Money Flow Index: приток/отток денег."""
    type = "mfi_reversal"
    name = "MFI Reversal"
    description = "Лонг при выходе MFI из зоны <20, шорт при выходе из зоны >80."
    default_params = {"period": 14, "oversold": 20, "overbought": 80}

    def generate(self, candles, index):
        p = self.params
        if index < p["period"] + 1:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        w = candles[:index + 1]
        h = np.array([x.high for x in w]); l = np.array([x.low for x in w]); c = np.array([x.close for x in w]); v = np.array([x.volume for x in w])
        mf = ta.mfi(h, l, c, v, p["period"])
        cur, prev = mf[index], mf[index - 1]
        if np.isnan(cur) or np.isnan(prev):
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "MFI не готов")
        if prev < p["oversold"] <= cur:
            return self._build_signal(SignalSide.LONG, candles, index, 0.57, "MFI: приток денег после оттока")
        if prev > p["overbought"] >= cur:
            return self._build_signal(SignalSide.SHORT, candles, index, 0.57, "MFI: отток денег после притока")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "MFI в нейтральной зоне")


class IchimokuTrend(Strategy):
    """Ichimoku: пересечение Tenkan/Kijun + облако."""
    type = "ichimoku"
    name = "Ichimoku"
    description = "Лонг при Tenkan>Kijun и цене над облаком, шорт — наоборот."
    default_params = {"tenkan": 9, "kijun": 26, "senkou": 52}

    def generate(self, candles, index):
        p = self.params
        if index < p["senkou"] + 2:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        w = candles[:index + 1]
        h = np.array([x.high for x in w]); l = np.array([x.low for x in w]); c = np.array([x.close for x in w])
        ten, kij, span_a, span_b = ta.ichimoku(h, l, c, p["tenkan"], p["kijun"], p["senkou"])
        if np.isnan(ten[index]) or np.isnan(kij[index]):
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Ишимоку не готов")
        cloud_top = max(span_a[index], span_b[index]) if not np.isnan(span_a[index]) else None
        cloud_bot = min(span_a[index], span_b[index]) if not np.isnan(span_a[index]) else None
        prev_cross = ten[index - 1] - kij[index - 1]
        cur_cross = ten[index] - kij[index]
        if prev_cross <= 0 < cur_cross and (cloud_top is None or c[index] > cloud_top):
            return self._build_signal(SignalSide.LONG, candles, index, 0.6, "Tenkan пересек Kijun вверх над облаком")
        if prev_cross >= 0 > cur_cross and (cloud_bot is None or c[index] < cloud_bot):
            return self._build_signal(SignalSide.SHORT, candles, index, 0.6, "Tenkan пересек Kijun вниз под облаком")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Нет сигнала Ишимоку")


class MeanReversion(Strategy):
    """Mean-reversion по Z-score (возврат к средней)."""
    type = "mean_reversion"
    name = "Mean Reversion (Z-score)"
    description = "Лонг при Z-score <-2 (перепродано), шорт при Z-score >2 (перекуплено)."
    default_params = {"period": 20, "z_long": -2.0, "z_short": 2.0}

    def generate(self, candles, index):
        p = self.params
        if index < p["period"] + 1:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        w = candles[:index + 1]
        c = np.array([x.close for x in w])
        z = ta.zscore(c, p["period"])
        cur = z[index]
        if np.isnan(cur):
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Z-score не готов")
        if cur <= p["z_long"]:
            return self._build_signal(SignalSide.LONG, candles, index, 0.6, f"Цена сильно ниже средней (z={round(cur,2)})")
        if cur >= p["z_short"]:
            return self._build_signal(SignalSide.SHORT, candles, index, 0.6, f"Цена сильно выше средней (z={round(cur,2)})")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Цена около средней")


class TripleSma(Strategy):
    """Выравнивание трёх SMA (тренд-фильтр)."""
    type = "triple_sma"
    name = "Triple SMA"
    description = "Лонг при SMA20>SMA50>SMA100 (сильный восходящий тренд), шорт наоборот."
    default_params = {"fast": 20, "mid": 50, "slow": 100}

    def generate(self, candles, index):
        p = self.params
        if index < p["slow"] + 1:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        w = candles[:index + 1]
        c = np.array([x.close for x in w])
        f = ta.sma(c, p["fast"]); m = ta.sma(c, p["mid"]); s = ta.sma(c, p["slow"])
        if np.isnan(f[index]) or np.isnan(m[index]) or np.isnan(s[index]):
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "SMA не готовы")
        cur_aligned_up = f[index] > m[index] > s[index]
        prev_aligned_up = f[index - 1] > m[index - 1] > s[index - 1]
        cur_aligned_dn = f[index] < m[index] < s[index]
        prev_aligned_dn = f[index - 1] < m[index - 1] < s[index - 1]
        if cur_aligned_up and not prev_aligned_up:
            return self._build_signal(SignalSide.LONG, candles, index, 0.64, "SMA выстроились в восходящий тренд")
        if cur_aligned_dn and not prev_aligned_dn:
            return self._build_signal(SignalSide.SHORT, candles, index, 0.64, "SMA выстроились в нисходящий тренд")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "SMA не выстроены")


class VolumeBreakout(Strategy):
    """Пробой с всплеском объёма."""
    type = "volume_breakout"
    name = "Volume Breakout"
    description = "Пробой предыдущего максимума/минимума при объёме в N раз выше среднего."
    default_params = {"lookback": 10, "vol_mult": 1.8}

    def generate(self, candles, index):
        p = self.params
        if index < p["lookback"] + 21:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        w = candles[:index + 1]
        h = np.array([x.high for x in w]); l = np.array([x.low for x in w]); c = np.array([x.close for x in w]); v = np.array([x.volume for x in w])
        vol_ma = ta.sma(v, 20)
        if np.isnan(vol_ma[index]):
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Объём не готов")
        vol_ok = v[index] >= vol_ma[index] * p["vol_mult"]
        prev_high = np.max(h[index - p["lookback"]: index])
        prev_low = np.min(l[index - p["lookback"]: index])
        if vol_ok and c[index] > prev_high:
            return self._build_signal(SignalSide.LONG, candles, index, 0.66, "Пробой вверх на повышенном объёме")
        if vol_ok and c[index] < prev_low:
            return self._build_signal(SignalSide.SHORT, candles, index, 0.66, "Пробой вниз на повышенном объёме")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Нет объёмного пробоя")


class SrBounce(Strategy):
    """Отскок от уровней поддержки/сопротивления (свинг-пивоты)."""
    type = "sr_bounce"
    name = "Support/Resistance Bounce"
    description = "Отскок вверх от локальной поддержки, вниз от сопротивления."
    default_params = {"lookback": 10, "buffer_pct": 0.3}

    def generate(self, candles, index):
        p = self.params
        if index < p["lookback"] + 1:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        w = candles[:index + 1]
        h = np.array([x.high for x in w]); l = np.array([x.low for x in w]); c = np.array([x.close for x in w])
        support, resistance = ta.pivot_levels(h, l, c, p["lookback"])
        cur = c[index]
        buf = p["buffer_pct"] / 100.0
        if not np.isnan(support[index]) and abs(cur - support[index]) / support[index] <= buf and cur >= support[index]:
            # подтверждение: предыдущий бар тоже касался поддержки
            if c[index - 1] >= support[index] * (1 - buf):
                return self._build_signal(SignalSide.LONG, candles, index, 0.58, "Отскок от уровня поддержки")
        if not np.isnan(resistance[index]) and abs(resistance[index] - cur) / resistance[index] <= buf and cur <= resistance[index]:
            if c[index - 1] <= resistance[index] * (1 + buf):
                return self._build_signal(SignalSide.SHORT, candles, index, 0.58, "Отскок от уровня сопротивления")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Нет отскока от уровней")


class RsiDivergence(Strategy):
    """Упрощённая дивергенция RSI и цены."""
    type = "rsi_divergence"
    name = "RSI Divergence"
    description = "Дивергенция: цена делает новый минимум, RSI — нет (бычий), и наоборот."
    default_params = {"period": 14, "lookback": 8}

    def generate(self, candles, index):
        p = self.params
        if index < p["period"] + p["lookback"] + 1:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        w = candles[:index + 1]
        c = np.array([x.close for x in w])
        r = ta.rsi(c, p["period"])
        if np.isnan(r[index]):
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "RSI не готов")
        lb = p["lookback"]
        price_low = np.min(c[index - lb: index + 1])
        price_high = np.max(c[index - lb: index + 1])
        rsi_low = np.nanmin(r[index - lb: index + 1])
        rsi_high = np.nanmax(r[index - lb: index + 1])
        # бычья дивергенция: новый минимум цены, но RSI выше предыдущего минимума
        if c[index] <= price_low and r[index] > rsi_low and r[index] < 45:
            return self._build_signal(SignalSide.LONG, candles, index, 0.55, "Бычья дивергенция RSI")
        if c[index] >= price_high and r[index] < rsi_high and r[index] > 55:
            return self._build_signal(SignalSide.SHORT, candles, index, 0.55, "Медвежья дивергенция RSI")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Нет дивергенции")


class VwapTrend(Strategy):
    """Тренд относительно VWAP (удержание выше/ниже)."""
    type = "vwap_trend"
    name = "VWAP Trend"
    description = "Пробой VWAP с закреплением: лонг при переходе цены выше VWAP, шорт ниже."
    default_params = {"confirm_bars": 2}

    def generate(self, candles, index):
        p = self.params
        if index < p["confirm_bars"] + 2:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        w = candles[:index + 1]
        vwap_line = ta.vwap(w, period=0)
        c = np.array([x.close for x in w])
        if np.isnan(vwap_line[index]):
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "VWAP не готов")
        prev = c[index - 1] - vwap_line[index - 1]
        cur = c[index] - vwap_line[index]
        if prev <= 0 < cur:
            return self._build_signal(SignalSide.LONG, candles, index, 0.6, "Цена пробила VWAP вверх")
        if prev >= 0 > cur:
            return self._build_signal(SignalSide.SHORT, candles, index, 0.6, "Цена пробила VWAP вниз")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Нет пробоя VWAP")


class OpeningRangeBreakout(Strategy):
    """Пробой диапазона первого часа (intraday)."""
    type = "opening_range"
    name = "Opening Range Breakout"
    description = "Пробой максимума/минимума первых N баров сессии (классика дэйтрейдинга)."
    default_params = {"range_bars": 12}

    def generate(self, candles, index):
        p = self.params
        rb = p["range_bars"]
        if index < rb + 1:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        # диапазон = последние rb баров, где бар index-rb был «открытием»
        w = candles[:index + 1]
        c = np.array([x.close for x in w]); h = np.array([x.high for x in w]); l = np.array([x.low for x in w])
        or_high = np.max(h[index - rb: index])
        or_low = np.min(l[index - rb: index])
        if c[index] > or_high and c[index - 1] <= or_high:
            return self._build_signal(SignalSide.LONG, candles, index, 0.63, "Пробой диапазона вверх")
        if c[index] < or_low and c[index - 1] >= or_low:
            return self._build_signal(SignalSide.SHORT, candles, index, 0.63, "Пробой диапазона вниз")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Внутри диапазона")


class RsiMacdCombo(Strategy):
    """Комбо: RSI + MACD (двойное подтверждение)."""
    type = "rsi_macd_combo"
    name = "RSI + MACD Combo"
    description = "Сигнал только при одновременном подтверждении RSI и MACD."
    default_params = {"rsi_period": 14, "rsi_low": 35, "rsi_high": 65}

    def generate(self, candles, index):
        p = self.params
        if index < 30:
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Недостаточно данных")
        w = candles[:index + 1]
        c = np.array([x.close for x in w])
        r = ta.rsi(c, p["rsi_period"])
        line, sig, hist = ta.macd(c)
        if np.isnan(r[index]):
            return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Индикаторы не готовы")
        macd_up = line[index] > sig[index] and line[index - 1] <= sig[index - 1]
        macd_dn = line[index] < sig[index] and line[index - 1] >= sig[index - 1]
        if macd_up and r[index] < p["rsi_low"] + 15 and r[index] > 30:
            return self._build_signal(SignalSide.LONG, candles, index, 0.66, "RSI+MACD подтверждают лонг")
        if macd_dn and r[index] > p["rsi_high"] - 15 and r[index] < 70:
            return self._build_signal(SignalSide.SHORT, candles, index, 0.66, "RSI+MACD подтверждают шорт")
        return self._build_signal(SignalSide.NEUTRAL, candles, index, 0, "Нет двойного подтверждения")


BUILTIN_STRATEGIES: dict = {
    cls.type: cls for cls in (
        MomentumBreakout, RsiReversal, EmaCrossover, BollingerBreakout, VwapPullback,
        MacdCrossover, MacdHistogram, StochasticCrossover, AdxTrend, SupertrendFollow,
        ParabolicSar, CciReversal, WilliamsR, MfiReversal, IchimokuTrend,
        MeanReversion, TripleSma, VolumeBreakout, SrBounce, RsiDivergence,
        VwapTrend, OpeningRangeBreakout, RsiMacdCombo,
    )
}

STRATEGY_TYPES = list(BUILTIN_STRATEGIES.keys())
