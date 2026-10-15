"""
Оркестрация прогонов: матрица (монета × ТФ × стратегия), кэш сигналов,
разрешение выходов и «зеркальный» прогон живого движка портала.

Три уровня достоверности:

  A. `extract` — тяжёлый проход стратегий, кэшируется на диск.
  B. `resolve` — быстрый перебор TP/SL/трейлинга по кэшу сигналов
     (математика барьеров, вход по открытию следующего бара).
  C. `mirror` — прогон РЕАЛЬНОГО кода портала (`signal_backtest._simulate`):
     авто-селектор стратегий, фильтры, кулдауны, breakeven/трейлинг.
     Им проверяется, что найденные настройки действительно работают в бою.

Поиск идёт по B (быстро, тысячи вариантов), финальная приёмка — по C (медленно,
но это в точности то, что увидит пользователь в сканере).
"""
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from . import data as D
from . import signals as S
from .config import ResearchConfig, TF_MINUTES, ExitSpec, CostModel

CACHE_DIRNAME = "signals"
MIRROR_DIRNAME = "mirror"
RAW_DIRNAME = "trades"


# --------------------------------------------------------------------- #
# Обнаружение данных
# --------------------------------------------------------------------- #
def discover(data_dir: str, symbols: Optional[Iterable[str]] = None,
             tfs: Optional[Iterable[str]] = None) -> list[tuple[str, str, str]]:
    """Находит имеющиеся пары (монета, ТФ, путь) в каталоге данных."""
    if not os.path.isdir(data_dir):
        return []
    out: list[tuple[str, str, str]] = []
    for fn in sorted(os.listdir(data_dir)):
        if not fn.endswith(".csv"):
            continue
        stem = fn[:-4]
        if "_" not in stem:
            continue
        sym, tf = stem.rsplit("_", 1)
        if tfs and tf not in set(tfs):
            continue
        if symbols and sym.upper() not in {s.upper() for s in symbols}:
            continue
        out.append((sym.upper(), tf, os.path.join(data_dir, fn)))
    return out


def quality_report(pairs: list[tuple[str, str, str]]) -> list[dict]:
    """Проверка качества данных по всем парам (до всякого анализа)."""
    out = []
    for sym, tf, path in pairs:
        try:
            df = pd.read_csv(path)
            q = D.validate_frame(df, sym, tf, TF_MINUTES.get(tf, 15))
            out.append(q.as_dict())
        except Exception as e:      # noqa: BLE001
            out.append({"symbol": sym, "tf": tf, "error": str(e)})
    return out


# --------------------------------------------------------------------- #
# Уровень A: извлечение сигналов (параллельно по монетам и ТФ)
# --------------------------------------------------------------------- #
def _extract_worker(args: dict) -> dict:
    sym, tf, path = args["pair"]
    out_dir = args["out_dir"]
    t0 = time.time()
    try:
        df = D.load_frame_cached(path)
        sig = S.extract_signals(sym, tf, df,
                                strategies=args["strategies"],
                                min_confidence=args["min_confidence"],
                                window=args["window"],
                                spacing_bars=args["spacing_bars"])
        target = os.path.join(out_dir, CACHE_DIRNAME, f"{sym}_{tf}.csv.gz")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if not sig.empty:
            sig.to_csv(target, index=False, compression="gzip")
        return {"symbol": sym, "tf": tf, "signals": int(len(sig)),
                "bars": int(len(df)), "path": target, "seconds": round(time.time() - t0, 1),
                "error": None}
    except Exception as e:          # noqa: BLE001 — одна пара не должна ронять матрицу
        return {"symbol": sym, "tf": tf, "signals": 0, "bars": 0, "path": None,
                "seconds": round(time.time() - t0, 1), "error": f"{type(e).__name__}: {e}"}


def extract_all(cfg: ResearchConfig, pairs: Optional[list] = None,
                jobs: int = 0) -> list[dict]:
    """Прогоняет стратегии по всем парам и кэширует потоки сигналов."""
    pairs = pairs or discover(cfg.data_dir, cfg.symbols, cfg.timeframes)
    if not pairs:
        raise D.DataError(f"в {cfg.data_dir} нет файлов <СИМВОЛ>_<ТФ>.csv — см. research/DOWNLOAD_SPEC.md")
    jobs = jobs or cfg.jobs or max(1, (os.cpu_count() or 4))
    tasks = [{"pair": p, "out_dir": cfg.out_dir, "strategies": cfg.strategies,
              "min_confidence": cfg.extract_min_confidence, "window": cfg.window,
              "spacing_bars": cfg.cooldown_bars_for(p[1])} for p in pairs]
    res: list[dict] = []
    if jobs == 1 or len(tasks) == 1:
        for t in tasks:
            r = _extract_worker(t)
            print(f"  {'✓' if not r['error'] else '✗'} {r['symbol']} {r['tf']}: "
                  f"{r['signals']} сигналов за {r['seconds']}с {r['error'] or ''}")
            res.append(r)
        return res
    with ProcessPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(_extract_worker, t): t for t in tasks}
        for fut in as_completed(futs):
            r = fut.result()
            print(f"  {'✓' if not r['error'] else '✗'} {r['symbol']} {r['tf']}: "
                  f"{r['signals']} сигналов за {r['seconds']}с {r['error'] or ''}", flush=True)
            res.append(r)
    return sorted(res, key=lambda r: (r["symbol"], r["tf"]))


def load_signals(out_dir: str, pairs: Optional[list] = None,
                 symbols: Optional[Iterable[str]] = None,
                 tfs: Optional[Iterable[str]] = None) -> pd.DataFrame:
    """Читает кэш сигналов. Если pairs не заданы — берёт всё из каталога."""
    d = os.path.join(out_dir, CACHE_DIRNAME)
    if not os.path.isdir(d):
        raise D.DataError(f"нет кэша сигналов {d} — сначала выполните `tvresearch extract`")
    files = [f for f in sorted(os.listdir(d)) if f.endswith(".csv.gz")]
    want_sym = {s.upper() for s in symbols} if symbols else None
    want_tf = set(tfs) if tfs else None
    frames = []
    for fn in files:
        sym, tf = fn[:-len(".csv.gz")].rsplit("_", 1)
        if want_sym and sym not in want_sym:
            continue
        if want_tf and tf not in want_tf:
            continue
        frames.append(pd.read_csv(os.path.join(d, fn)))
    if not frames:
        raise D.DataError("кэш сигналов пуст для заданных монет/ТФ")
    return pd.concat(frames, ignore_index=True)


# --------------------------------------------------------------------- #
# Уровень B: разрешение выходов и перебор TP/SL
# --------------------------------------------------------------------- #
def _frames_for(cfg: ResearchConfig, pairs: list) -> dict:
    out: dict[str, pd.DataFrame] = {}
    for sym, tf, path in pairs:
        key = f"{sym}_{tf}"
        if key not in out:
            out[key] = D.load_frame_cached(path)
    return out


def resolve_all(cfg: ResearchConfig, specs: list[tuple[str, ExitSpec]],
                signals: Optional[pd.DataFrame] = None,
                pairs: Optional[list] = None,
                workers: int = 0) -> pd.DataFrame:
    """Перебирает варианты выхода по одному кэшу сигналов.

    Возвращает единый DataFrame сделок со столбцом `variant` (метка варианта).
    """
    pairs = pairs or discover(cfg.data_dir, cfg.symbols, cfg.timeframes)
    if signals is None:
        signals = load_signals(cfg.out_dir, pairs)
    frames = _frames_for(cfg, pairs)          # {"SYM_TF": DataFrame}
    tasks = []
    for key, df in frames.items():
        sym, tf = key.rsplit("_", 1)
        sub = signals[(signals["symbol"] == sym) & (signals["tf"] == tf)]
        if sub.empty:
            continue
        tasks.append({"key": key, "signals": sub, "df": df, "tf": tf,
                      "specs": [(lab, asdict(sp)) for lab, sp in specs],
                      "costs": asdict(cfg.costs), "entry_mode": cfg.entry_mode,
                      "cooldown": cfg.cooldown_bars_for(tf),
                      "group_by": cfg.resolve_group_by,
                      "max_gap_atr": cfg.max_gap_atr,
                      "one_position": cfg.one_position_per_pair})
    workers = workers or cfg.jobs or max(1, min(os.cpu_count() or 4, len(tasks) or 1))
    if workers == 1 or len(tasks) <= 1:
        parts = [_resolve_worker(t) for t in tasks]
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            parts = list(ex.map(_resolve_worker, tasks))
    parts = [p for p in parts if p is not None and not p.empty]
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    raw_dir = os.path.join(cfg.out_dir, RAW_DIRNAME)
    os.makedirs(raw_dir, exist_ok=True)
    out.to_csv(os.path.join(raw_dir, "trades_all.csv.gz"), index=False, compression="gzip")
    return out


def _resolve_worker(task: dict) -> Optional[pd.DataFrame]:
    from .config import ExitSpec as _ES, CostModel as _CM
    sig = task["signals"]
    df = task["df"]
    tf = task["tf"]
    costs = _CM(**task["costs"])
    frames = []
    for label, spec_dict in task["specs"]:
        spec = _ES(**spec_dict)
        trades = S.resolve_trades(sig, df, spec, costs, tf=tf,
                                  entry_mode=task["entry_mode"],
                                  cooldown_bars=task["cooldown"],
                                  one_position=task["one_position"],
                                  group_by=task.get("group_by", "stype"),
                                  max_gap_atr=task.get("max_gap_atr", 3.0),
                                  label=label)
        if not trades.empty:
            trades["variant"] = label
            frames.append(trades)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# --------------------------------------------------------------------- #
# Уровень C: зеркальный прогон живого движка портала
# --------------------------------------------------------------------- #
def _mirror_worker(args: dict) -> dict:
    """Прогон `signal_backtest._simulate` на локальных свечах."""
    sym, tf, path = args["pair"]
    t0 = time.time()
    try:
        import signal_backtest as sb          # модуль портала (см. sys.path в signals.py)
        df = D.load_frame_cached(path)
        candles = S.candles_from_df(df)
        res = sb._simulate(sym, tf, candles)
        trades = res.get("trades") or []
        if trades:
            td = pd.DataFrame(trades)
            # признаки на баре сигнала: нужны фильтрам и мета-модели
            high = df["high"].to_numpy(float)
            low = df["low"].to_numpy(float)
            close = df["close"].to_numpy(float)
            open_ = df["open"].to_numpy(float)
            vol = df["volume"].to_numpy(float)
            ts = df["ts"].to_numpy("int64")
            from .features import features_at, FEATURE_NAMES, build_seasonal_baseline
            seasonal = build_seasonal_baseline(vol, ts, min(len(df), 250))
            rows = []
            for t in trades:
                i = int(t["i"])
                f = features_at(open_, high, low, close, vol, ts, i, side=t["side"],
                                conf=float(t.get("conf") or 0.0),
                                planned_rr=0.0, seasonal_baseline=seasonal)
                rows.append({k: f[k] for k in FEATURE_NAMES if k != "planned_rr"})
            for k in rows[0].keys():
                td[k] = [r[k] for r in rows]
            td["variant"] = args.get("variant", "live")
            os.makedirs(args["out_dir"], exist_ok=True)
            td.to_csv(os.path.join(args["out_dir"], f"{sym}_{tf}.csv.gz"),
                      index=False, compression="gzip")
        return {"symbol": sym, "tf": tf, "trades": len(trades),
                "seconds": round(time.time() - t0, 1), "error": None}
    except Exception as e:        # noqa: BLE001
        return {"symbol": sym, "tf": tf, "trades": 0,
                "seconds": round(time.time() - t0, 1),
                "error": f"{type(e).__name__}: {e}"}


def mirror_run(cfg: ResearchConfig, pairs: Optional[list] = None,
               jobs: int = 0, variant: str = "live") -> pd.DataFrame:
    """Зеркальный прогон по всем парам (авто-селектор + фильтры + BE/трейлинг)."""
    pairs = pairs or discover(cfg.data_dir, cfg.symbols, cfg.timeframes)
    out_dir = os.path.join(cfg.out_dir, MIRROR_DIRNAME)
    os.makedirs(out_dir, exist_ok=True)
    jobs = jobs or cfg.jobs or max(1, min(os.cpu_count() or 4, len(pairs) or 1))
    tasks = [{"pair": p, "out_dir": out_dir, "variant": variant} for p in pairs]
    res: list[dict] = []
    if jobs == 1 or len(tasks) <= 1:
        for t in tasks:
            r = _mirror_worker(t)
            print(f"  {'✓' if not r['error'] else '✗'} {r['symbol']} {r['tf']}: "
                  f"{r['trades']} сделок за {r['seconds']}с {r['error'] or ''}", flush=True)
            res.append(r)
    else:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            for r in ex.map(_mirror_worker, tasks):
                print(f"  {'✓' if not r['error'] else '✗'} {r['symbol']} {r['tf']}: "
                      f"{r['trades']} сделок за {r['seconds']}с {r['error'] or ''}", flush=True)
                res.append(r)
    frames = []
    if os.path.isdir(out_dir):
        for fn in sorted(os.listdir(out_dir)):
            if fn.endswith(".csv.gz"):
                frames.append(pd.read_csv(os.path.join(out_dir, fn)))
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    if "pnl_pct" in out.columns:
        out["pnl_net_pct"] = out["pnl_pct"]      # в зеркале PnL уже без издержек портала
        out["hold_min"] = out.get("duration_min", 0)
        out["outcome"] = out.get("reason", "").astype(str).str.upper()
    return out


# --------------------------------------------------------------------- #
# Фильтры (гейты) — общие для сигналов и сделок
# --------------------------------------------------------------------- #
def parse_ranges(spec: str) -> list[tuple[float, float]]:
    out = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if "-" not in chunk:
            continue
        try:
            lo, hi = chunk.split("-", 1)
            out.append((float(lo), float(hi)))
        except ValueError:
            continue
    return out


def in_ranges(x, spec: str) -> bool:
    if x is None or x != x:      # None или NaN
        return False
    for lo, hi in parse_ranges(spec):
        if lo <= float(x) < hi:
            return True
    return False


def apply_gates(df: pd.DataFrame, gates: dict) -> pd.DataFrame:
    """Применяет фильтры входа (те же поля, что у портала в signal_gates).

    Фильтры только ИСКЛЮЧАЮТ входы — путь цены не меняется, поэтому их можно
    честно применять оффлайн к уже записанным сделкам.
    """
    if df is None or df.empty:
        return df
    m = pd.Series(True, index=df.index)
    conf_col = "conf"
    if gates.get("min_conf") is not None and conf_col in df.columns:
        m &= df[conf_col].astype(float) >= float(gates["min_conf"])
    sides = gates.get("sides")
    if sides:
        m &= df["side"].isin(set(sides))
    stypes = [s for s in (gates.get("stypes") or []) if s]
    if stypes:
        m &= df["stype"].isin(set(stypes))
    block = {s.strip() for s in (gates.get("stype_blocklist") or "").split(",") if s.strip()}
    if block:
        keep = ~df["stype"].isin(block)
        keep &= ~df.apply(lambda r: f"{r['stype']}/{r['side'][0]}" in block, axis=1)
        m &= keep
    hours = {int(h) for h in (gates.get("hours_block") or [])}
    if hours and "hour" in df.columns:
        m &= ~df["hour"].astype(int).isin(hours)
    if gates.get("atr_block") and "atr_pct" in df.columns:
        m &= ~df["atr_pct"].apply(lambda v: in_ranges(v, gates["atr_block"]))
    if gates.get("vol_season_block") and "vol_ratio_seasonal" in df.columns:
        m &= ~df["vol_ratio_seasonal"].apply(lambda v: in_ranges(v, gates["vol_season_block"]))
    if gates.get("tp_min_pct") is not None and "tp_pct" in df.columns:
        m &= df["tp_pct"].astype(float) >= float(gates["tp_min_pct"])
    if gates.get("rsi_block"):
        m &= ~df["rsi"].apply(lambda v: in_ranges(v, gates["rsi_block"]))
    return df[m].reset_index(drop=True)
