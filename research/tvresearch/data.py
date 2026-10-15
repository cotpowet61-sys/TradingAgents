"""
Данные: контракт, скачивание с Bybit, валидация и синтетика для самопроверки.

Контракт файла: research/data/<SYMBOL>_<TF>.csv
  ts,open,high,low,close,volume
  ts — миллисекунды UTC открытия бара, строго возрастают, без дублей.

Качество данных проверяется ДО анализа (validate_frame): дырки, дубли, битые OHLC.
Если данных не хватает — контур честно падает, а не считает «на глазок».
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import os
import random
import time
from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np
import pandas as pd

# Публичные хосты Bybit (совпадают с portal/bybit_account.BASES)
BYBIT_HOSTS = ["https://api.bybit.com", "https://api2.bybit.com",
               "https://api.bytick.com", "https://api.bybit.eu"]
_TF_TO_BYBIT = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
                "1h": "60", "4h": "240"}
REQUIRED_COLS = ["ts", "open", "high", "low", "close", "volume"]


# --------------------------------------------------------------------- #
# Пути
# --------------------------------------------------------------------- #
def symbol_path(data_dir: str, symbol: str, tf: str) -> str:
    return os.path.join(data_dir, f"{symbol.upper()}_{tf}.csv")


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


# --------------------------------------------------------------------- #
# Контракт и валидация
# --------------------------------------------------------------------- #
class DataError(Exception):
    pass


@dataclass
class DataQuality:
    symbol: str
    tf: str
    bars: int
    first_ts: int
    last_ts: int
    gaps: int
    dupes: int
    bad_ohlc: int
    expected_bars: int

    @property
    def coverage(self) -> float:
        return round(100.0 * self.bars / self.expected_bars, 2) if self.expected_bars else 0.0

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "tf": self.tf, "bars": self.bars,
            "first": _iso(self.first_ts), "last": _iso(self.last_ts),
            "gaps": self.gaps, "dupes": self.dupes, "bad_ohlc": self.bad_ohlc,
            "coverage_pct": self.coverage,
        }


def _iso(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime((ms or 0) / 1000))


def validate_frame(df: pd.DataFrame, symbol: str, tf: str,
                   tf_minutes: int, min_coverage: float = 0.9) -> DataQuality:
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise DataError(f"{symbol} {tf}: нет колонок {missing}")
    if df.empty:
        raise DataError(f"{symbol} {tf}: пустой файл")
    d = df.copy()
    dupes = int(d["ts"].duplicated().sum())
    d = d.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    bar_ms = tf_minutes * 60_000
    bad = int(((d["high"] < d[["open", "close"]].max(axis=1)) |
               (d["low"] > d[["open", "close"]].min(axis=1)) |
               (d[["open", "high", "low", "close", "volume"]] <= 0).any(axis=1)).sum())
    diffs = np.diff(d["ts"].to_numpy())
    gaps = int((diffs > bar_ms * 1.5).sum())
    span = int(d["ts"].iloc[-1] - d["ts"].iloc[0])
    expected = int(span // bar_ms) + 1
    q = DataQuality(symbol=symbol, tf=tf, bars=len(d), first_ts=int(d["ts"].iloc[0]),
                    last_ts=int(d["ts"].iloc[-1]), gaps=gaps, dupes=dupes,
                    bad_ohlc=bad, expected_bars=expected)
    if q.coverage < min_coverage:
        raise DataError(f"{symbol} {tf}: покрытие {q.coverage}% < {min_coverage}% — "
                        f"данные дырявые, анализ не имеет смысла")
    return q


def load_frame(data_dir: str, symbol: str, tf: str) -> pd.DataFrame:
    path = symbol_path(data_dir, symbol, tf)
    if not os.path.exists(path):
        raise DataError(f"нет файла {path} — сначала скачайте данные (tvresearch download)")
    df = pd.read_csv(path)
    df = df[REQUIRED_COLS].drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    for c in REQUIRED_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if df[REQUIRED_COLS].isna().any().any():
        raise DataError(f"{path}: есть NaN после приведения типов")
    df["ts"] = df["ts"].astype("int64")
    return df


_FRAME_CACHE: dict = {}


def load_frame_cached(path: str) -> pd.DataFrame:
    """Читает файл данных с кэшем в памяти (в одном процессе — один раз)."""
    key = os.path.abspath(path)
    df = _FRAME_CACHE.get(key)
    if df is None:
        import pandas as _pd
        df = _pd.read_csv(path, usecols=REQUIRED_COLS)
        df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        for c in REQUIRED_COLS:
            df[c] = _pd.to_numeric(df[c], errors="coerce")
        df["ts"] = df["ts"].astype("int64")
        _FRAME_CACHE[key] = df
    return df


def save_frame(df: pd.DataFrame, data_dir: str, symbol: str, tf: str) -> str:
    ensure_dir(data_dir)
    path = symbol_path(data_dir, symbol, tf)
    df[REQUIRED_COLS].to_csv(path, index=False)
    return path


def frame_fingerprint(df: pd.DataFrame) -> str:
    """Отпечаток данных: попадает в отчёт, чтобы результаты нельзя было перепутать."""
    arr = df[REQUIRED_COLS].to_numpy(dtype=float)
    h = hashlib.sha256(arr.tobytes()).hexdigest()[:16]
    return f"{h}:{len(df)}:{int(df['ts'].iloc[0])}-{int(df['ts'].iloc[-1])}"


# --------------------------------------------------------------------- #
# Скачивание (публичный REST Bybit, ключи не нужны)
# --------------------------------------------------------------------- #
def download_symbol(symbol: str, tf: str, days: int, data_dir: str,
                    category: str = "linear", pause: float = 0.25) -> DataQuality:
    import httpx
    if tf not in _TF_TO_BYBIT:
        raise DataError(f"неизвестный таймфрейм {tf}")
    ensure_dir(data_dir)
    interval = _TF_TO_BYBIT[tf]
    bar_ms = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
              "1h": 3600, "4h": 14400}[tf] * 1000
    now = int(time.time() * 1000) // bar_ms * bar_ms
    start = now - days * 86_400_000
    rows: list[list] = []
    cursor_end = now
    hosts = list(BYBIT_HOSTS)
    with httpx.Client(timeout=30) as cli:
        while cursor_end > start:
            params = {"category": category, "symbol": symbol.upper(), "interval": interval,
                      "start": int(max(start, cursor_end - 1000 * bar_ms)), "end": int(cursor_end),
                      "limit": 1000}
            data = None
            last_err = None
            for host in hosts:
                try:
                    r = cli.get(f"{host}/v5/market/kline", params=params)
                    r.raise_for_status()
                    data = r.json()
                    break
                except Exception as e:  # noqa: BLE001 — пробуем следующий хост
                    last_err = e
            if data is None:
                raise DataError(f"{symbol} {tf}: все хосты Bybit недоступны ({last_err})")
            if data.get("retCode") != 0:
                raise DataError(f"{symbol} {tf}: Bybit вернул {data.get('retCode')} {data.get('retMsg')}")
            batch = data.get("result", {}).get("list") or []
            if not batch:
                break
            rows.extend(batch)
            oldest = min(int(b[0]) for b in batch)
            if oldest >= cursor_end:
                break
            cursor_end = oldest - 1
            time.sleep(pause)          # вежливость к публичному API
    if not rows:
        raise DataError(f"{symbol} {tf}: Bybit не вернул ни одного бара")
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "turnover"])
    df = df[["ts", "open", "high", "low", "close", "volume"]]
    for c in REQUIRED_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna().drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    q = validate_frame(df, symbol.upper(), tf, bar_ms // 60000, min_coverage=0.85)
    save_frame(df, data_dir, symbol, tf)
    return q


def download_many(symbols: Iterable[str], tfs: Iterable[str], days: int, data_dir: str,
                  out_manifest: Optional[str] = None) -> list[DataQuality]:
    out: list[DataQuality] = []
    for sym in symbols:
        for tf in tfs:
            try:
                q = download_symbol(sym, tf, days, data_dir)
                print(f"  ✓ {sym} {tf}: {q.bars} баров ({q.coverage}% покрытие)")
                out.append(q)
            except Exception as e:  # noqa: BLE001 — одна монета не должна ломать выгрузку
                print(f"  ✗ {sym} {tf}: {e}")
    if out_manifest:
        ensure_dir(os.path.dirname(out_manifest) or ".")
        with open(out_manifest, "w", encoding="utf-8") as f:
            json.dump([q.as_dict() for q in out], f, ensure_ascii=False, indent=2)
    return out


# --------------------------------------------------------------------- #
# Синтетика — ТОЛЬКО для самопроверки контура (не для выводов о рынке)
# --------------------------------------------------------------------- #
def synthetic_frame(bars: int = 6000, tf_minutes: int = 15, seed: int = 7,
                    start_px: float = 100.0, regimes: bool = True) -> pd.DataFrame:
    """Режимно-переключающийся ряд с толстыми хвостами.

    Зачем: проверить, что пайплайн (сигналы → барьеры → метрики → мета-модель)
    работает end-to-end там, где нет доступа к бирже. Выводы о стратегиях по
    этим данным делать НЕЛЬЗЯ — в отчёте такие прогоны помечаются `synthetic`.
    """
    rng = random.Random(seed)
    n = bars
    # режимы: тренд вверх / тренд вниз / флет, со сменой по марковской цепи
    vol = 0.004
    drift = 0.0
    px = start_px
    rows = []
    bar_ms = tf_minutes * 60_000
    t0 = int(time.time() * 1000) // bar_ms * bar_ms - n * bar_ms
    for i in range(n):
        if regimes and i % max(50, n // 12) == 0:
            kind = rng.random()
            if kind < 0.35:
                drift, vol = 0.00040, 0.0035
            elif kind < 0.7:
                drift, vol = -0.00040, 0.0035
            else:
                drift, vol = 0.0, 0.0022
        shock = rng.gauss(0, 1)
        if rng.random() < 0.01:          # толстый хвост
            shock *= 4
        ret = drift + vol * shock
        o = px
        c = max(0.01, px * (1 + ret))
        wick = abs(rng.gauss(0, vol * 0.6))
        h = max(o, c) * (1 + wick)
        l = min(o, c) * (1 - wick)
        v = abs(rng.gauss(1000, 250)) * (1 + 2 * abs(ret) / max(vol, 1e-9))
        rows.append((t0 + i * bar_ms, round(o, 6), round(h, 6), round(l, 6), round(c, 6), round(v, 4)))
        px = c
    return pd.DataFrame(rows, columns=REQUIRED_COLS)


def save_synthetic(data_dir: str, symbols: Iterable[str], tfs: Iterable[str],
                   bars: int = 6000, seed: int = 7) -> list[str]:
    paths = []
    for k, sym in enumerate(symbols):
        for tf in tfs:
            minutes = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}[tf]
            df = synthetic_frame(bars=bars, tf_minutes=minutes, seed=seed + k * 13 + minutes)
            paths.append(save_frame(df, data_dir, sym, tf))
    return paths


def gzip_csv(path: str) -> str:
    """Сжатие артефакта для переноса между машинами."""
    out = path + ".gz"
    with open(path, "rb") as fi, gzip.open(out, "wb") as fo:
        fo.write(fi.read())
    return out
