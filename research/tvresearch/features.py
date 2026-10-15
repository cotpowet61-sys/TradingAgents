"""
Признаки для мета-модели (фильтра сигналов).

Жёсткие правила:
  1. Всё считается ТОЛЬКО по данным ≤ i (нет взгляда в будущее). Это проверяется
     тестом: перестановка будущих баров не меняет признаки бара i.
  2. Набор признаков ОДИН И ТОТ ЖЕ в research и в портале (portal/curated.py),
     иначе модель, обученная здесь, будет получать другие числа в бою.
  3. Никаких «грязных» прокси вроде будущей волатильности или разметки.

Поэтому набор компактный: волатильность, положение в канале, моментум,
объём, время, режим. Этого достаточно для фильтрации, но это нельзя переобучить
на десятках тысяч сделок «в ноль».
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

# Единый список признаков: порядок = порядок весов в бандле. Менять только вместе
# с переобучением и версией бандла (portal/curated.py проверяет версию).
FEATURE_NAMES = [
    "conf",              # уверенность стратегии (0..1)
    "atr_pct",           # ATR(14)/цена, % — волатильность на входе
    "rsi",               # RSI(14)
    "rsi_dist",          # |RSI-50|/50 — «насколько перекуплен/перепродан»
    "ema_dist_pct",      # (цена-EMA21)/EMA21, % — растянутость от средней
    "ema_slope_pct",     # наклон EMA21 за 5 бар, %
    "vol_ratio",         # объём последнего бара / средний за 20
    "bb_width_pct",      # ширина полос Боллинджера (20,2) / цена, %
    "range_pos",         # положение close в диапазоне 20 бар: 0..1
    "dist_high20_pct",   # (high20-close)/close, %
    "dist_low20_pct",    # (close-low20)/close, %
    "trend_up",          # 1 если EMA21>EMA55, иначе 0
    "hour_sin", "hour_cos",  # циклическое время UTC
    "dow_sin", "dow_cos",    # день недели
    "side_long",         # сторона сигнала: 1 = LONG, 0 = SHORT
    "planned_rr",        # запланированное TP/SL (может быть важно: узкие цели «шумят»)
    "vol_ratio_seasonal",  # объём vs норма этого часа (сезонная база)
]

FEATURE_VERSION = "v1"


def _ema_last(values: np.ndarray, period: int) -> float:
    if len(values) == 0:
        return float("nan")
    alpha = 2.0 / (period + 1)
    prev = float(values[0])
    for v in values[1:]:
        prev = alpha * float(v) + (1 - alpha) * prev
    return prev


def _rsi_last(close: np.ndarray, period: int = 14) -> float:
    if len(close) <= period:
        return 50.0
    delta = np.diff(close[-(period * 4 + 1):])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    ag = float(np.mean(gain[-period:])) if len(gain) >= period else float(np.mean(gain))
    al = float(np.mean(loss[-period:])) if len(loss) >= period else float(np.mean(loss))
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def _atr_last(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
    n = len(close)
    if n < period + 1:
        return float(abs(high[-1] - low[-1])) if n else 0.0
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])))
    return float(np.mean(tr[-period:]))


def seasonal_ratio_window(volume: np.ndarray, ts_s: np.ndarray) -> float:
    """Объём последнего бара / средний объём того же часа UTC внутри окна.

    Ровно эта же функция живёт в портале (portal/curated.py::_seasonal_ratio):
    признак обязан считаться одинаково в исследовании и в бою, иначе модель
    увидит в продакшене не те числа, на которых училась.
    """
    if len(volume) < 4:
        return 1.0
    hour = int((float(ts_s[-1]) // 3600) % 24)
    hours = ((ts_s // 3600) % 24).astype(int)
    mask = hours == hour
    if mask.sum() < 3:
        return 1.0
    base = float(np.mean(volume[mask]))
    return float(volume[-1] / base) if base > 0 else 1.0


def features_at(open_: np.ndarray, high: np.ndarray, low: np.ndarray,
                close: np.ndarray, volume: np.ndarray, ts_ms: np.ndarray,
                i: int, *, side: str, conf: float, planned_rr: float,
                seasonal_baseline: Optional[dict] = None, window: int = 300) -> dict:
    """Признаки бара i (вход — массивы полного ряда, i — индекс ЗАКРЫТОГО бара).

    Окно — ровно последние `window` бар (как «экран» сканера портала, limit=300).
    Параметр `seasonal_baseline` оставлен для совместимости и не используется:
    сезонный коэффициент считается внутри окна — так его может посчитать и портал.
    """
    lo = max(0, i - (window - 1))
    o_s, h_s, l_s, c_s, v_s = (open_[lo:i + 1], high[lo:i + 1], low[lo:i + 1],
                               close[lo:i + 1], volume[lo:i + 1])
    px = float(c_s[-1])
    atr = _atr_last(h_s, l_s, c_s, 14)
    rsi = _rsi_last(c_s, 14)
    ema21 = _ema_last(c_s, 21)
    ema55 = _ema_last(c_s, 55)
    ema21_prev = _ema_last(c_s[:-5], 21) if len(c_s) > 25 else ema21
    bb_window = c_s[-20:] if len(c_s) >= 20 else c_s
    sd = float(np.std(bb_window)) if len(bb_window) else 0.0
    bb_width = (4 * sd / px * 100.0) if px else 0.0
    hi20 = float(np.max(h_s[-20:])) if len(h_s) else px
    lo20 = float(np.min(l_s[-20:])) if len(l_s) else px
    rng = max(hi20 - lo20, 1e-12)
    vol_ratio = float(v_s[-1] / max(np.mean(v_s[-20:]), 1e-12)) if len(v_s) else 1.0
    t = float(ts_ms[i]) / 1000.0
    hour = int((t // 3600) % 24)
    dow = int(((t // 86400) + 4) % 7)          # 1970-01-01 = четверг
    seasonal = seasonal_ratio_window(v_s, (ts_ms[lo:i + 1].astype(float) / 1000.0))
    return {
        "conf": float(conf),
        "atr_pct": float(atr / px * 100.0) if px else 0.0,
        "rsi": float(rsi),
        "rsi_dist": abs(float(rsi) - 50.0) / 50.0,
        "ema_dist_pct": float((px - ema21) / ema21 * 100.0) if ema21 else 0.0,
        "ema_slope_pct": float((ema21 - ema21_prev) / ema21_prev * 100.0) if ema21_prev else 0.0,
        "vol_ratio": float(vol_ratio),
        "bb_width_pct": float(bb_width),
        "range_pos": float((px - lo20) / rng),
        "dist_high20_pct": float((hi20 - px) / px * 100.0) if px else 0.0,
        "dist_low20_pct": float((px - lo20) / px * 100.0) if px else 0.0,
        "trend_up": 1.0 if ema21 >= ema55 else 0.0,
        "hour_sin": math.sin(2 * math.pi * hour / 24),
        "hour_cos": math.cos(2 * math.pi * hour / 24),
        "dow_sin": math.sin(2 * math.pi * dow / 7),
        "dow_cos": math.cos(2 * math.pi * dow / 7),
        "side_long": 1.0 if side == "LONG" else 0.0,
        "planned_rr": float(planned_rr),
        "vol_ratio_seasonal": float(seasonal),
    }


def build_seasonal_baseline(volume: np.ndarray, ts_ms: np.ndarray, upto: int) -> dict:
    """Средний объём по часу UTC, посчитанный ТОЛЬКО по барам < upto.

    Портал делает то же самое (data.market_data: сезонная база), поэтому признак
    сопоставим. Считаем один раз на прогон и обновляем блоками.
    """
    hours: dict[int, list[float]] = {h: [] for h in range(24)}
    step = max(1, upto // 20000)     # не тащим лишние итерации на больших рядах
    for k in range(0, upto, step):
        h = int((float(ts_ms[k]) / 1000.0 // 3600) % 24)
        hours[h].append(float(volume[k]))
    return {"hour": {h: (float(np.mean(v)) if len(v) >= 5 else 0.0) for h, v in hours.items()}}


def matrix_from_rows(rows: list[dict]) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Собирает X (numpy) и y (1 = прибыльная сделка) из таблицы событий."""
    names = list(FEATURE_NAMES)
    X = np.array([[float(r.get(n, 0.0) or 0.0) for n in names] for r in rows], dtype=float)
    y = np.array([1 if float(r.get("pnl_net_pct", 0.0)) > 0 else 0 for r in rows], dtype=int)
    return X, y, names
