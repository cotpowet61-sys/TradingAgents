"""
Курированная мета-модель: «ИИ-фильтр» поверх стратегий.

Идея проста и честна: сами стратегии генерируют сигналы (их 23, и все они шумят
по-разному). Мета-модель смотрит на контекст бара (волатильность, RSI, положение
в канале, час суток, сезонный объём, сторона, запланированный RR) и решает,
стоит ли вообще брать этот сигнал.

Что здесь принципиально:
  • модель — логистическая регрессия, посчитанная на numpy (никаких тяжёлых
    зависимостей, бандл запускается внутри портала одной функцией);
  • признаков мало и они устойчивы (см. tvresearch/features.py) — иначе 20 тысяч
    сделок превращаются в идеальную подгонку;
  • пороги подбираются на TRAIN, метрики показываются на TEST (вне выборки);
  • если цель 75% на тесте недостижима при сохранении экспектанси — так и
    пишется в отчёт и в бандл, без «подкрашивания».

Бандл (JSON) читает и research, и портал (portal/curated.py) — формат один.
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from .features import FEATURE_NAMES, FEATURE_VERSION
from .metrics import Stats, compute_stats, wilson_interval

BUNDLE_VERSION = "v1"
DEFAULT_L2 = 1.0


# --------------------------------------------------------------------- #
# Модели на numpy
# --------------------------------------------------------------------- #
def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -35, 35)))


def standardize_fit(X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mean = X.mean(axis=0)
    std = X.std(axis=0)
    std[std < 1e-9] = 1.0
    return mean, std


def fit_logreg(X: np.ndarray, y: np.ndarray, l2: float = DEFAULT_L2,
               iters: int = 600, lr: float = 0.7) -> tuple[np.ndarray, float, np.ndarray, np.ndarray]:
    """Логистическая регрессия (полный батч, стандартизованные признаки)."""
    mean, std = standardize_fit(X)
    Z = (X - mean) / std
    n, d = Z.shape
    w = np.zeros(d)
    b = 0.0
    y = y.astype(float)
    for it in range(iters):
        p = _sigmoid(Z @ w + b)
        g = (p - y) / n
        gw = Z.T @ g + (l2 / n) * w
        gb = float(g.sum())
        step = lr / (1 + 0.002 * it)
        w -= step * gw
        b -= step * gb
    return w, b, mean, std


def fit_ridge(X: np.ndarray, y: np.ndarray, l2: float = 1.0) -> tuple[np.ndarray, float, np.ndarray, np.ndarray]:
    """Ридж-регрессия (закрытая форма) для ожидаемого PnL."""
    mean, std = standardize_fit(X)
    Z = (X - mean) / std
    n, d = Z.shape
    Zc = np.hstack([Z, np.ones((n, 1))])
    A = Zc.T @ Zc + l2 * np.eye(d + 1)
    sol = np.linalg.solve(A, Zc.T @ y)
    return sol[:d], float(sol[d]), mean, std


def predict_bundle(bundle: dict, X: np.ndarray) -> dict:
    """Применяет бандл к матрице признаков → вероятности и ожидания.

    Ровно та же арифметика должна быть в портале (portal/curated.py):
    z = (x - mean) / std; p = sigmoid(w·z + b).
    """
    X = np.asarray(X, dtype=float)
    if X.ndim == 1:
        X = X.reshape(1, -1)
    order = bundle["features"]
    idx = [FEATURE_NAMES.index(f) for f in order]
    Xs = X[:, idx]
    out = {}
    for key, suffix in (("win", "win"), ("pnl", "pnl")):
        m = bundle.get(f"model_{suffix}")
        if not m:
            continue
        Z = (Xs - np.asarray(m["mean"])) / np.asarray(m["std"])
        z = Z @ np.asarray(m["coef"]) + float(m["intercept"])
        out[key] = _sigmoid(z) if suffix == "win" else z
    return out


# --------------------------------------------------------------------- #
# Пороги и оценка
# --------------------------------------------------------------------- #
def reliability(probs: np.ndarray, y: np.ndarray, bins: int = 10) -> list[dict]:
    """Диаграмма надёжности: фактический винрейт в бакетах предсказанной вероятности."""
    order = np.argsort(probs)
    probs, y = probs[order], y[order]
    n = len(probs)
    out = []
    for k in range(bins):
        lo = int(k * n / bins)
        hi = int((k + 1) * n / bins)
        if hi - lo < 5:
            continue
        p = probs[lo:hi]
        yy = y[lo:hi]
        out.append({"p_bucket": [round(float(p.min()), 3), round(float(p.max()), 3)],
                    "n": int(hi - lo),
                    "predicted": round(float(p.mean()), 3),
                    "actual_winrate": round(100 * float(yy.mean()), 1)})
    return out


def choose_threshold(probs: np.ndarray, pnls: np.ndarray, *,
                     winrate_target: float, min_coverage: float = 0.05,
                     min_trades: int = 30) -> dict:
    """Порог вероятности: выполняет цель по винрейту при максимальном охвате.

    Если цель недостижима — берём максимум винрейта и честно помечаем это.
    """
    n = len(probs)
    if n == 0:
        return {"threshold": 0.5, "coverage": 0.0, "winrate": 0.0, "expectancy": 0.0,
                "target_met": False, "reason": "нет данных"}
    cand = np.unique(np.round(np.quantile(probs, np.linspace(0, 0.99, 60)), 3))
    best_met = None
    best_wr = None
    for t in cand:
        sel = probs >= t
        m = int(sel.sum())
        if m < min_trades or m / n < min_coverage:
            continue
        wr = 100 * float((pnls[sel] > 0).mean())
        exp = float(pnls[sel].mean())
        cover = m / n
        row = {"threshold": float(t), "coverage": round(cover, 4), "trades": m,
               "winrate": round(wr, 2), "expectancy": round(exp, 4),
               "wr_lo": round(wilson_interval(int((pnls[sel] > 0).sum()), m)[0], 2)}
        if wr >= winrate_target and exp > 0:
            if best_met is None or cover > best_met["coverage"]:
                best_met = row
        if best_wr is None or row["winrate"] > best_wr["winrate"]:
            best_wr = row
    if best_met:
        return {**best_met, "target_met": True, "reason": "цель по винрейту достигнута при максимальном охвате"}
    row = best_wr or {"threshold": 0.5, "coverage": 1.0, "trades": n,
                      "winrate": round(100 * float((pnls > 0).mean()), 2),
                      "expectancy": round(float(pnls.mean()), 4), "wr_lo": 0.0}
    return {**row, "target_met": False,
            "reason": f"винрейт {winrate_target:g}% на этой выборке недостижим при осмысленном охвате"}


# --------------------------------------------------------------------- #
# Обучение бандла
# --------------------------------------------------------------------- #
def _matrix(df: pd.DataFrame) -> np.ndarray:
    return df[FEATURE_NAMES].astype(float).to_numpy()


def train_bundle(train: pd.DataFrame, test: pd.DataFrame, *,
                 winrate_target: float = 75.0, l2: float = DEFAULT_L2,
                 min_coverage: float = 0.05, min_trades: int = 30,
                 notes: str = "") -> tuple[dict, dict]:
    """Обучает мета-модель на train, оценивает на test. Возвращает (бандл, отчёт)."""
    if train.empty:
        raise ValueError("пустая обучающая выборка")
    Xtr, Xte = _matrix(train), _matrix(test if not test.empty else train)
    ytr = (train["pnl_net_pct"].to_numpy(float) > 0).astype(int)
    ptr = train["pnl_net_pct"].to_numpy(float)
    w_w, b_w, mean_w, std_w = fit_logreg(Xtr, ytr, l2=l2)
    w_p, b_p, mean_p, std_p = fit_ridge(Xtr, ptr, l2=l2)
    base = {
        "version": BUNDLE_VERSION,
        "feature_version": FEATURE_VERSION,
        "features": list(FEATURE_NAMES),
        "created_at": int(time.time()),
        "model_win": {"coef": w_w.tolist(), "intercept": b_w,
                      "mean": mean_w.tolist(), "std": std_w.tolist()},
        "model_pnl": {"coef": w_p.tolist(), "intercept": b_p,
                      "mean": mean_p.tolist(), "std": std_p.tolist()},
        "notes": notes,
    }
    # порог подбираем на train, показываем на test (честная вневыборочная оценка)
    pred_tr = predict_bundle(base, Xtr)
    thr_choice = choose_threshold(pred_tr["win"], ptr, winrate_target=winrate_target,
                                  min_coverage=min_coverage, min_trades=min_trades)
    thr = float(thr_choice["threshold"])
    thr_pnl = 0.0
    pred_te = predict_bundle(base, Xte)
    pte = test["pnl_net_pct"].to_numpy(float) if not test.empty else ptr
    sel_te = pred_te["win"] >= thr
    if pred_te.get("pnl") is not None:
        # требуем неубывающее ожидание: фильтр по предсказанному PnL ≥ 0
        sel_te = sel_te & (pred_te["pnl"] >= thr_pnl)
    bundle = {**base, "threshold_win": thr, "threshold_pnl": thr_pnl,
              "threshold_info": thr_choice,
              "trained_on": {"train_n": int(len(train)), "test_n": int(len(test)),
                             "feature_version": FEATURE_VERSION}}
    report = {
        "threshold_choice": thr_choice,
        "train": {
            "all": compute_stats(train.to_dict("records")).as_dict(),
            "filtered": compute_stats(
                train[pred_tr["win"] >= thr].to_dict("records")).as_dict(),
        },
        "test": {
            "all": compute_stats((test if not test.empty else train).to_dict("records")).as_dict(),
            "filtered": compute_stats(((test if not test.empty else train)[sel_te])
                                      .to_dict("records")).as_dict(),
        },
        "coverage_test": round(float(sel_te.mean()), 4) if len(sel_te) else 0.0,
        "reliability_test": reliability(pred_te["win"], (pte > 0).astype(int)),
        "coefficients": sorted(zip(FEATURE_NAMES, w_w.tolist()),
                               key=lambda kv: -abs(kv[1]))[:10],
    }
    return bundle, report


def apply_bundle(df: pd.DataFrame, bundle: dict) -> pd.DataFrame:
    """Оффлайн-фильтр таблицы сделок по бандлу (для отчётов и проверок)."""
    if df is None or df.empty:
        return df
    miss = [f for f in bundle["features"] if f not in df.columns]
    if miss:
        raise ValueError(f"в таблице нет признаков бандла: {miss[:5]}")
    pred = predict_bundle(bundle, _matrix(df))
    keep = pred["win"] >= float(bundle.get("threshold_win", 0.5))
    if pred.get("pnl") is not None:
        keep &= pred["pnl"] >= float(bundle.get("threshold_pnl", 0.0))
    out = df[keep].copy()
    out["meta_p_win"] = pred["win"][keep]
    out["meta_pnl_pred"] = pred["pnl"][keep]
    return out.reset_index(drop=True)


# --------------------------------------------------------------------- #
# Файлы
# --------------------------------------------------------------------- #
def save_bundle(bundle: dict, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=2)
    return path


def load_bundle(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        b = json.load(f)
    if b.get("version") != BUNDLE_VERSION:
        raise ValueError(f"версия бандла {b.get('version')} не поддерживается")
    if list(b.get("features") or []) != list(FEATURE_NAMES):
        raise ValueError("порядок/состав признаков бандла не совпадает с FEATURE_NAMES — "
                         "переобучите бандл (features.py менялся)")
    return b
