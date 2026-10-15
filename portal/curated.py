# -*- coding: utf-8 -*-
"""
ИИ-фильтр сигналов (мета-модель, обученная исследовательским контуром).

Что это: поверх 23 стратегий работает небольшая модель, которая по контексту бара
(волатильность, RSI, положение в канале, объём, время, сторона, запланированный RR)
решает, стоит ли брать этот конкретный сигнал. Так «все подряд» сигналы превращаются
в отобранные — это и есть обещанное совмещение анализа с подходящими стратегиями.

Принципы (почему этому можно верить):
  * модель обучает и оценивает research/ на тех же данных, что и портал: признаки
    считаются ТЕМ ЖЕ кодом (здесь — `features_from_candles`, в исследовании —
    `tvresearch.features.features_at`; равенство проверено тестом);
  * файл бандла — обычный JSON, внутри логистическая регрессия и ридж;
    зависимостей нет (numpy уже есть в портале), сеть не используется;
  * по умолчанию всё ВЫКЛЮЧЕНО (`USE_CURATED=false`): без бандла портал работает
    ровно как раньше;
  * включённый режим «в тени» (`CURATED_SHADOW=true`) ничего не блокирует, только
    считает и логирует отклонённые сигналы — так можно сначала посмотреть, что
    фильтр отсекает, и лишь потом доверить ему входы.

Безопасность: файл читается только по явному пути из настроек, содержимое
валидируется (версия, порядок признаков, размерности), ошибки не роняют сканер —
сигнал проходит как раньше.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time

import numpy as np

log = logging.getLogger("curated")

FEATURE_VERSION = "v1"

# ВАЖНО: порядок и состав должны совпадать с research/tvresearch/features.py
# (FEATURE_NAMES). Несовпадение = отказ загрузки бандла, а не тихая ошибка.
FEATURE_NAMES = [
    "conf", "atr_pct", "rsi", "rsi_dist", "ema_dist_pct", "ema_slope_pct",
    "vol_ratio", "bb_width_pct", "range_pos", "dist_high20_pct", "dist_low20_pct",
    "trend_up", "hour_sin", "hour_cos", "dow_sin", "dow_cos",
    "side_long", "planned_rr", "vol_ratio_seasonal",
]

_cache: dict = {"path": None, "mtime": 0.0, "bundle": None}
counters: dict = {"checked": 0, "rejected": 0, "passed": 0, "errors": 0,
                  "shadow_rejected": 0, "last_reject_reason": None}


# --------------------------------------------------------------------- #
# Признаки (арифметика идентична исследовательской)
# --------------------------------------------------------------------- #
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
    return 100 - 100 / (1 + ag / al)


def _atr_last(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
    n = len(close)
    if n < period + 1:
        return float(abs(high[-1] - low[-1])) if n else 0.0
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])))
    return float(np.mean(tr[-period:]))


def _seasonal_ratio(volume: np.ndarray, ts_s: np.ndarray) -> float:
    """Объём последнего бара / средний объём того же часа UTC внутри окна."""
    if len(volume) < 4:
        return 1.0
    hour = int((float(ts_s[-1]) // 3600) % 24)
    hours = ((ts_s // 3600) % 24).astype(int)
    mask = hours == hour
    if mask.sum() < 3:
        return 1.0
    base = float(np.mean(volume[mask]))
    return float(volume[-1] / base) if base > 0 else 1.0


def features_from_candles(candles, side: str, conf: float, planned_rr: float,
                          window: int = 300) -> dict:
    """Считает признаки по ПОСЛЕДНЕМУ бару окна (только данные ≤ текущего бара)."""
    w = list(candles)[-window:]
    if not w:
        return {k: 0.0 for k in FEATURE_NAMES}
    open_ = np.array([float(getattr(c, "open", 0.0)) for c in w], dtype=float)
    high = np.array([float(getattr(c, "high", 0.0)) for c in w], dtype=float)
    low = np.array([float(getattr(c, "low", 0.0)) for c in w], dtype=float)
    close = np.array([float(getattr(c, "close", 0.0)) for c in w], dtype=float)
    volume = np.array([float(getattr(c, "volume", 0.0)) for c in w], dtype=float)
    ts_s = np.array([float(getattr(c, "ts", 0)) / 1000.0 for c in w], dtype=float)

    px = float(close[-1]) or 1.0
    atr = _atr_last(high, low, close, 14)
    rsi = _rsi_last(close, 14)
    ema21 = _ema_last(close, 21)
    ema55 = _ema_last(close, 55)
    ema21_prev = _ema_last(close[:-5], 21) if len(close) > 25 else ema21
    bb = close[-20:] if len(close) >= 20 else close
    sd = float(np.std(bb)) if len(bb) else 0.0
    hi20 = float(np.max(high[-20:])) if len(high) else px
    lo20 = float(np.min(low[-20:])) if len(low) else px
    rng = max(hi20 - lo20, 1e-12)
    vol_ratio = float(volume[-1] / max(np.mean(volume[-20:]), 1e-12)) if len(volume) else 1.0
    t = float(ts_s[-1])
    hour = int((t // 3600) % 24)
    dow = int(((t // 86400) + 4) % 7)
    _ = open_      # оставлено для читаемости: открытие в признаках v1 не участвует
    return {
        "conf": float(conf),
        "atr_pct": float(atr / px * 100.0),
        "rsi": float(rsi),
        "rsi_dist": abs(float(rsi) - 50.0) / 50.0,
        "ema_dist_pct": float((px - ema21) / ema21 * 100.0) if ema21 else 0.0,
        "ema_slope_pct": float((ema21 - ema21_prev) / ema21_prev * 100.0) if ema21_prev else 0.0,
        "vol_ratio": float(vol_ratio),
        "bb_width_pct": float(4 * sd / px * 100.0),
        "range_pos": float((px - lo20) / rng),
        "dist_high20_pct": float((hi20 - px) / px * 100.0),
        "dist_low20_pct": float((px - lo20) / px * 100.0),
        "trend_up": 1.0 if ema21 >= ema55 else 0.0,
        "hour_sin": math.sin(2 * math.pi * hour / 24),
        "hour_cos": math.cos(2 * math.pi * hour / 24),
        "dow_sin": math.sin(2 * math.pi * dow / 7),
        "dow_cos": math.cos(2 * math.pi * dow / 7),
        "side_long": 1.0 if side == "LONG" else 0.0,
        "planned_rr": float(planned_rr),
        "vol_ratio_seasonal": _seasonal_ratio(volume, ts_s),
    }


# --------------------------------------------------------------------- #
# Бандл: загрузка и валидация
# --------------------------------------------------------------------- #
class BundleError(Exception):
    pass


def validate_bundle(b: dict) -> dict:
    if not isinstance(b, dict):
        raise BundleError("бандл не является объектом JSON")
    if b.get("version") != "v1":
        raise BundleError(f"версия бандла {b.get('version')!r} не поддерживается")
    feats = list(b.get("features") or [])
    if feats != FEATURE_NAMES:
        raise BundleError("состав/порядок признаков бандла не совпадает с кодом "
                          "(features v" + FEATURE_VERSION + ") — переобучите")
    for key in ("model_win", "model_pnl"):
        m = b.get(key)
        if not isinstance(m, dict):
            raise BundleError(f"в бандле нет {key}")
        for f in ("coef", "mean", "std", "intercept"):
            if f not in m:
                raise BundleError(f"{key}: нет поля {f}")
        if not (len(m["coef"]) == len(m["mean"]) == len(m["std"]) == len(feats)):
            raise BundleError(f"{key}: размерности не совпадают с числом признаков")
    for k in ("coef", "mean", "std"):
        for key in ("model_win", "model_pnl"):
            vals = b[key][k]
            if any(v is None or not math.isfinite(float(v)) for v in vals):
                raise BundleError(f"{key}.{k}: нечисловые значения")
    return b


def load_bundle(path: str, force: bool = False) -> dict:
    """Читает бандл с кэшем по времени изменения файла."""
    if not path:
        raise BundleError("путь к бандлу не задан")
    st = os.stat(path)      # FileNotFoundError обрабатывает вызывающий код
    if (not force and _cache.get("bundle") is not None
            and _cache.get("path") == path and _cache.get("mtime") == st.st_mtime):
        return _cache["bundle"]
    with open(path, "r", encoding="utf-8") as f:
        b = validate_bundle(json.load(f))
    _cache.update({"path": path, "mtime": st.st_mtime, "bundle": b})
    return b


def enabled(settings) -> bool:
    return bool(getattr(settings, "USE_CURATED", False)) and bool(
        getattr(settings, "CURATED_BUNDLE", "") or "")


def predict(bundle: dict, feats: dict) -> tuple[float, float]:
    """Возвращает (p_win, ожидаемый PnL%). Арифметика та же, что в research."""
    x = np.array([float(feats.get(n, 0.0) or 0.0) for n in FEATURE_NAMES], dtype=float)
    out = []
    for key in ("model_win", "model_pnl"):
        m = bundle[key]
        z = x - np.array(m["mean"], dtype=float)
        z = z / np.array(m["std"], dtype=float)
        val = float(np.dot(np.array(m["coef"], dtype=float), z) + float(m["intercept"]))
        out.append(val)
    z_win, z_pnl = out
    p_win = 1.0 / (1.0 + math.exp(-max(-35.0, min(35.0, z_win))))
    return p_win, z_pnl


def gate(settings, feats: dict, *, stype: str = "", symbol: str = "",
         side: str = "", log_prefix: str = "[curated]") -> tuple[bool, dict]:
    """Решение по сигналу: (пропустить ли, инфо).

    Возвращает True, если сигнал можно брать. Любая внутренняя ошибка трактуется
    как «не мешать» (True) и увеличивает счётчик errors — фильтр не должен
    ломать сканер.
    """
    if not enabled(settings):
        return True, {"enabled": False}
    try:
        bundle = load_bundle(str(getattr(settings, "CURATED_BUNDLE", "")))
    except Exception as e:      # noqa: BLE001 — битый бандл не должен ронять сканер
        counters["errors"] += 1
        log.warning("%s бандл недоступен (%s) — сигналы идут без фильтра", log_prefix, e)
        return True, {"enabled": False, "error": str(e)}
    counters["checked"] += 1
    p_win, exp_pnl = predict(bundle, feats)
    thr_win = float(getattr(settings, "CURATED_MIN_P_WIN", 0.0) or
                    bundle.get("threshold_win", 0.5))
    thr_pnl = float(getattr(settings, "CURATED_MIN_EV", 0.0) or
                    bundle.get("threshold_pnl", 0.0))
    ok = (p_win >= thr_win) and (exp_pnl >= thr_pnl)
    info = {"enabled": True, "p_win": round(p_win, 4), "exp_pnl": round(exp_pnl, 4),
            "threshold_win": thr_win, "threshold_pnl": thr_pnl,
            "stype": stype, "symbol": symbol, "side": side}
    shadow = bool(getattr(settings, "CURATED_SHADOW", True))
    if ok:
        counters["passed"] += 1
        return True, info
    reason = (f"p_win={p_win:.3f} < {thr_win:.3f}" if p_win < thr_win
              else f"E[pnl]={exp_pnl:.3f} < {thr_pnl:.3f}")
    counters["last_reject_reason"] = reason
    if shadow:
        counters["shadow_rejected"] += 1
        log.info("%s (тень) отклонил бы %s %s %s/%s: %s", log_prefix, symbol, stype, side,
                 "", reason)
        return True, {**info, "shadow": True, "reason": reason}
    counters["rejected"] += 1
    log.info("%s отклонил %s %s %s: %s", log_prefix, symbol, stype, side, reason)
    return False, {**info, "reason": reason}


def stats() -> dict:
    """Счётчики для /api/metrics (видно, что фильтр делает в бою)."""
    return dict(counters)


def reset_counters() -> None:
    for k in counters:
        counters[k] = 0 if k != "last_reject_reason" else None
