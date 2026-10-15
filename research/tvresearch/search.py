"""
Поиск правил: TP/SL, механизм выхода, входные фильтры, отбор стратегий.

Главные принципы (иначе 75% винрейта — самообман):

  1. Цель — экспектанси (средний PnL сделки после издержек), а винрейт — ОГРАНИЧЕНИЕ.
     Мы не максимизируем «частоту угадывания» ценой слива на редких убытках.
  2. Отбор только там, где статистика значима: нижняя граница Уилсона выше
     безубыточного винрейта, минимум сделок, устойчивость по фолдам.
  3. Никакого выбора параметров на тесте: `walk_forward` учится на train-окне
     и считается на следующем, непросмотренном.
  4. TP не микроскопический: снизу — `constraints.tp_min_pct` (иначе издержки
     съедают всё, а винрейт становится косметикой).
"""
from __future__ import annotations

import math
import random
from dataclasses import asdict
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from .config import Constraints, ExitSpec, ResearchConfig
from .metrics import Stats, compute_stats, group_stats, wilson_interval
from .barriers import breakeven_winrate
from .engine import apply_gates


# --------------------------------------------------------------------- #
# Сетка вариантов выхода
# --------------------------------------------------------------------- #
def spec_grid(tp_values: Iterable[float], sl_values: Iterable[float],
              be_triggers: Iterable[float] = (0.0,),
              trail_starts: Iterable[float] = (0.0,),
              hold_hours: float = 4.0) -> list[tuple[str, ExitSpec]]:
    """Декартова сетка «TP × SL × (breakeven/трейлинг)».

    Метки вида `tp0.8_sl1.2` / `tp0.8_sl1.2_be0.6` попадают в отчёты и в .env.
    """
    out: list[tuple[str, ExitSpec]] = []
    for tp in tp_values:
        for sl in sl_values:
            for be in be_triggers:
                for tr in trail_starts:
                    if be == 0 and tr == 0:
                        label = f"tp{tp:g}_sl{sl:g}"
                    elif be and not tr:
                        label = f"tp{tp:g}_sl{sl:g}_be{be:g}"
                    elif tr and not be:
                        label = f"tp{tp:g}_sl{sl:g}_tr{tr:g}"
                    else:
                        label = f"tp{tp:g}_sl{sl:g}_be{be:g}_tr{tr:g}"
                    spec = ExitSpec(mode="fixed", fixed_tp_pct=float(tp), fixed_sl_pct=float(sl),
                                    tp_min_pct=0.0, tp_max_pct=99.0,
                                    sl_min_pct=0.0, sl_max_pct=99.0,
                                    be_trigger_pct=float(be), be_offset_pct=0.15 if be else 0.0,
                                    trail_start_pct=float(tr), trail_pct=0.6 if tr else 0.0,
                                    max_hold_hours=hold_hours)
                    out.append((label, spec))
    return out


def default_specs(cfg: ResearchConfig) -> list[tuple[str, ExitSpec]]:
    """Разумный стартовый набор: «пол» по TP, RR 1:1…1:2, breakeven-варианты."""
    c = cfg.constraints
    tp_values = sorted({c.tp_min_pct, round(c.tp_min_pct * 1.5, 3), 1.0, 1.5, 2.0, 3.0})
    sl_values = sorted({max(c.sl_min_pct, 0.5), 0.8, 1.0, 1.5, 2.0})
    return spec_grid(tp_values, sl_values, be_triggers=(0.0, 0.8), trail_starts=(0.0,))


# --------------------------------------------------------------------- #
# Оценка и цель
# --------------------------------------------------------------------- #
def score(stats: Stats, costs_pct: float, c: Constraints,
          trials: int = 1) -> tuple[bool, float, str]:
    """Пригодность варианта: (прошёл ли ограничения, целевая функция, причина).

    Целевая функция = экспектанси × доля удержаний (штраф за «мёртвые» сделки),
    но решение принимается ТОЛЬКО если:
      • сделок ≥ min_trades;
      • винрейт ≥ целевого (75% по умолчанию) — как ограничение, не как цель;
      • нижняя граница Уилсона винрейта выше безубыточного уровня для этого TP/SL
        (защита от «75% на 30 сделках»);
      • экспектанси > min_expectancy_pct;
      • просадка ≤ max_dd_pct.
    """
    if stats.n < c.min_trades:
        return False, -1e9, f"мало сделок ({stats.n} < {c.min_trades})"
    if stats.expectancy <= c.min_expectancy_pct:
        return False, stats.expectancy, "экспектанси ≤ 0 после издержек"
    if stats.winrate < c.winrate_target:
        return False, stats.expectancy, f"винрейт {stats.winrate:.1f}% < цели {c.winrate_target:g}%"
    be = breakeven_winrate(stats.avg_win or 1.0, abs(stats.avg_loss) or 1.0, costs_pct)
    if c.require_wilson_lb_above_breakeven and stats.wr_lo < be:
        return False, stats.expectancy, f"нижняя граница винрейта {stats.wr_lo:.1f}% < безубытка {be:.1f}%"
    if stats.max_dd_frac > c.max_dd_pct:
        return False, stats.expectancy, f"просадка {stats.max_dd_frac:.1f}% > {c.max_dd_pct:g}%"
    val = stats.expectancy * (1.0 - 0.3 * stats.timeout_share / 100.0)
    return True, val, "ok"


def best_variant(trades: pd.DataFrame, cfg: ResearchConfig,
                 subset: Optional[pd.DataFrame] = None) -> dict:
    """Лучший вариант выхода по целевой функции (по всем вариантам в таблице)."""
    df = trades if subset is None else subset
    costs_pct = cfg.costs.total_round_trip_pct()
    rows = []
    for variant, sub in df.groupby("variant"):
        st = compute_stats(sub.to_dict("records"))
        ok, val, why = score(st, costs_pct, cfg.constraints, trials=df["variant"].nunique())
        rows.append({"variant": variant, "ok": ok, "value": val, "why": why, **st.as_dict()})
    out = pd.DataFrame(rows).sort_values(["ok", "value"], ascending=False)
    return out


# --------------------------------------------------------------------- #
# Входные фильтры (гейты): случайно-жадный поиск
# --------------------------------------------------------------------- #
def gate_candidates(trades: pd.DataFrame) -> dict:
    """Кандидаты в фильтры — по слабым срезам данных (часы, ATR, сезонный объём, стратегии)."""
    cand: dict = {"hours_block": [], "atr_block": [], "vol_season_block": [],
                  "stype_blocklist": [], "sides": [], "min_conf": []}
    if trades.empty:
        return cand
    if "hour" in trades.columns:
        by = group_stats(trades.to_dict("records"), lambda r: int(r.get("hour", 0)))
        weak = [h for h, s in by.items() if s.n >= 20 and s.expectancy < 0]
        cand["hours_block"] = sorted(weak, key=lambda h: by[h].expectancy)[:6]
    if "atr_pct" in trades.columns:
        edges = [0, 0.3, 0.5, 0.7, 1.0, 1.5, 2.0, 3.0, 99]
        b = trades.copy()
        b["_bucket"] = pd.cut(b["atr_pct"], edges)
        by = group_stats(b.to_dict("records"), lambda r: str(r["_bucket"]))
        cand["atr_block"] = [k for k, s in by.items() if s.n >= 20 and s.expectancy < 0][:4]
        cand["vol_edges"] = edges
    if "vol_ratio_seasonal" in trades.columns:
        edges = [0, 0.5, 0.8, 1.0, 1.3, 1.6, 2.0, 3.0, 99]
        b = trades.copy()
        b["_bucket"] = pd.cut(b["vol_ratio_seasonal"], edges)
        by = group_stats(b.to_dict("records"), lambda r: str(r["_bucket"]))
        cand["vol_season_block"] = [k for k, s in by.items() if s.n >= 20 and s.expectancy < 0][:4]
        cand["vse_edges"] = edges
    if "stype" in trades.columns:
        by = group_stats(trades.to_dict("records"), lambda r: r["stype"])
        weak = [k for k, s in by.items() if s.n >= 25 and s.expectancy <= 0]
        cand["stype_blocklist"] = sorted(weak)
        by_side = group_stats(trades.to_dict("records"),
                              lambda r: f"{r['stype']}/{str(r['side'])[0]}")
        cand["stype_blocklist"] += [k for k, s in by_side.items() if s.n >= 15 and s.expectancy <= 0]
    cand["min_conf"] = [0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85]
    cand["sides"] = [["LONG"], ["SHORT"], ["LONG", "SHORT"]]
    return cand


def bucket_to_ranges(keys: list[str], edges: list[float], prefix: str = "(") -> str:
    """'(0.0, 0.3]' → '0.0-0.3' (портал читает именно такой формат)."""
    out = []
    for k in keys or []:
        try:
            lo, hi = k.strip("()[] ").split(",")
            lo, hi = float(lo), float(hi.strip("]"))
        except Exception:      # noqa: BLE001
            continue
        out.append(f"{lo:g}-{hi:g}")
    return ",".join(out)


def random_gates(cands: dict, rng: random.Random) -> dict:
    """Случайный набор фильтров из кандидатов (для поиска комбинации)."""
    g: dict = {}
    if cands.get("hours_block") and rng.random() < 0.6:
        k = rng.randint(1, len(cands["hours_block"]))
        g["hours_block"] = sorted(rng.sample(cands["hours_block"], k))
    if cands.get("atr_block") and rng.random() < 0.6:
        k = rng.randint(1, len(cands["atr_block"]))
        g["atr_block"] = bucket_to_ranges(rng.sample(cands["atr_block"], k), cands.get("vol_edges", []))
    if cands.get("vol_season_block") and rng.random() < 0.6:
        k = rng.randint(1, len(cands["vol_season_block"]))
        g["vol_season_block"] = bucket_to_ranges(
            rng.sample(cands["vol_season_block"], k), cands.get("vse_edges", []))
    if cands.get("stype_blocklist") and rng.random() < 0.7:
        k = rng.randint(1, len(cands["stype_blocklist"]))
        g["stype_blocklist"] = ",".join(sorted(rng.sample(cands["stype_blocklist"], k)))
    if cands.get("min_conf") and rng.random() < 0.5:
        g["min_conf"] = rng.choice(cands["min_conf"])
    if cands.get("sides") and rng.random() < 0.3:
        g["sides"] = rng.choice(cands["sides"])
    return g


def search_gates(trades: pd.DataFrame, cfg: ResearchConfig, trials: int = 200,
                 seed: int = 7, verbose: bool = False) -> tuple[dict, Stats]:
    """Случайный поиск комбинации фильтров. Возвращает (гейты, статистика)."""
    if trades.empty:
        return {}, Stats()
    c = cfg.constraints
    costs_pct = cfg.costs.total_round_trip_pct()
    cands = gate_candidates(trades)
    rng = random.Random(seed)
    base_stats = compute_stats(trades.to_dict("records"))
    best = ({}, base_stats, score(base_stats, costs_pct, c)[1], score(base_stats, costs_pct, c)[0])
    for t in range(trials):
        g = random_gates(cands, rng)
        sub = apply_gates(trades, g)
        if len(sub) < c.min_trades:
            continue
        st = compute_stats(sub.to_dict("records"))
        ok, val, why = score(st, costs_pct, c, trials=trials)
        better = (ok and not best[3]) or (ok == best[3] and val > best[2])
        if better:
            best = (g, st, val, ok)
            if verbose:
                print(f"    gate#{t}: {g} → {st.line()}")
    return best[0], best[1]


# --------------------------------------------------------------------- #
# Отбор стратегий (то самое «совмещение ИИ с подходящими стратегиями»)
# --------------------------------------------------------------------- #
def select_portfolio(trades: pd.DataFrame, cfg: ResearchConfig,
                     top_k: int = 0, min_n: int = 15) -> dict:
    """Оставляет стратегии/стороны, которые на ОБУЧАЮЩЕЙ части дают плюс.

    Это «белый ящик» вместо чёрного: правило = список разрешённых (стратегия, сторона),
    отобранных по ожиданию, а не по максимуму винрейта.
    """
    if trades.empty:
        return {"allowed": [], "notes": "нет сделок"}
    by = group_stats(trades.to_dict("records"),
                     lambda r: f"{r['stype']}/{str(r['side'])[0]}", min_n=min_n)
    ranked = sorted(((k, s) for k, s in by.items() if s.n >= min_n),
                    key=lambda kv: (-(kv[1].expectancy), -kv[1].n))
    keep_positive = [k for k, s in ranked if s.expectancy > 0]
    if top_k:
        keep_positive = keep_positive[:top_k]
    return {"allowed": keep_positive,
            "ranked": [(k, round(s.expectancy, 4), s.n) for k, s in ranked]}


# --------------------------------------------------------------------- #
# Walk-forward: отбор на train, проверка на следующем окне
# --------------------------------------------------------------------- #
def walk_forward(trades: pd.DataFrame, cfg: ResearchConfig, folds: int = 4,
                 trials: int = 120, seed: int = 7) -> dict:
    """Скользящая проверка: правила подбираются на train, считаются на test.

    Если на тестовых окнах результат держится — это уже не «подгонка под историю».
    """
    if trades.empty or "ts_entry" not in trades.columns:
        return {"folds": []}
    df = trades.sort_values("ts_entry").reset_index(drop=True)
    n = len(df)
    size = max(1, n // folds)
    out = []
    for k in range(1, folds):
        train = df.iloc[: k * size]
        test = df.iloc[k * size:(k + 1) * size if k + 1 < folds else n]
        if len(test) < cfg.constraints.min_trades_per_group:
            continue
        be_row = best_variant(df, cfg, subset=train)
        if be_row.empty:
            continue
        best = be_row.iloc[0]
        train_v = train[train["variant"] == best["variant"]]
        gates, _st = search_gates(train_v, cfg, trials=trials, seed=seed + k)
        test_v = test[test["variant"] == best["variant"]]
        test_f = apply_gates(test_v, gates)
        tr_st = compute_stats(train_v.to_dict("records"))
        te_st = compute_stats(test_f.to_dict("records"))
        out.append({
            "fold": k, "variant": best["variant"], "gates": gates,
            "train_n": tr_st.n, "train_wr": tr_st.winrate, "train_exp": tr_st.expectancy,
            "test_n": te_st.n, "test_wr": te_st.winrate, "test_wlo": te_st.wr_lo,
            "test_exp": te_st.expectancy, "test_pf": te_st.profit_factor,
        })
    return {"folds": out}


# --------------------------------------------------------------------- #
# Диагностика «почему цель не берётся» + формулы по волатильности
# --------------------------------------------------------------------- #
def target_diagnostics(trades: pd.DataFrame, atr_col: str = "atr_pct") -> dict:
    """Проверка двух главных причин недостижения цели (без обмана метриками).

    M1. Цель не по масштабу волатильности: доля сделок, где ход в пользу сделки
        так и не дошёл до TP (miss_share при фиксированном TP), по ATR-бакетам.
    M2. Убыток меньше цели: средний MAE в момент достижения TP; если MAE ≈ TP,
        значит стоп можно ставить шире и винрейт вырастет честно.
    """
    if trades.empty:
        return {}
    df = trades.copy()
    df["reached_tp_move"] = (df["mfe_pct"] >= df["tp_pct"]).astype(int)
    res: dict = {"overall": {
        "miss_share_pct": round(100 * (1 - df["reached_tp_move"].mean()), 2),
        "median_mfe_pct": round(float(df["mfe_pct"].median()), 3),
        "median_mae_pct": round(float(df["mae_pct"].median()), 3),
        "median_tp_pct": round(float(df["tp_pct"].median()), 3),
        "tp_reached_before_sl_share": round(float(((df["outcome"] == "TP").mean()) * 100), 2),
    }}
    if atr_col in df.columns:
        edges = [0, 0.3, 0.5, 0.7, 1.0, 1.5, 2.0, 3.0, 99]
        df["_b"] = pd.cut(df[atr_col], edges)
        rows = []
        for b, sub in df.groupby("_b", observed=True):
            if len(sub) < 10:
                continue
            rows.append({"atr_bucket": str(b), "n": len(sub),
                         "tp_pct": round(float(sub["tp_pct"].median()), 3),
                         "wr": round(float(100 * (sub["pnl_net_pct"] > 0).mean()), 1),
                         "miss_share_pct": round(100 * (1 - sub["reached_tp_move"].mean()), 1),
                         "median_mfe": round(float(sub["mfe_pct"].median()), 3),
                         "median_mae": round(float(sub["mae_pct"].median()), 3),
                         "exp": round(float(sub["pnl_net_pct"].mean()), 4)})
        res["by_atr"] = rows
    return res


def volatility_formula(trades: pd.DataFrame, atr_col: str = "atr_pct") -> dict:
    """Формулы «TP/SL от волатильности» — лучше фиксированных процентов.

    Считаем по ATR-бакетам медианный достижимый ход (MFE) и медианный ход против
    (MAE): TP ≈ k_tp × MFE, SL ≈ k_sl × MAE. Это переносимо между монетами,
    потому что нормируется на текущую волатильность.
    """
    if trades.empty or atr_col not in trades.columns:
        return {}
    sub = trades[trades[atr_col] > 0]
    if sub.empty:
        return {}
    ratio_tp = (sub["mfe_pct"] / sub[atr_col]).replace([np.inf, -np.inf], np.nan).dropna()
    ratio_sl = (sub["mae_pct"] / sub[atr_col]).replace([np.inf, -np.inf], np.nan).dropna()
    return {
        "n": int(len(sub)),
        "tp_atr_mult_median": round(float(ratio_tp.median()), 3),
        "sl_atr_mult_median": round(float(ratio_sl.median()), 3),
        "tp_atr_mult_q25": round(float(ratio_tp.quantile(0.25)), 3),
        "sl_atr_mult_q75": round(float(ratio_sl.quantile(0.75)), 3),
        "atr_median_pct": round(float(sub[atr_col].median()), 3),
    }


# --------------------------------------------------------------------- #
# Полная рекомендация
# --------------------------------------------------------------------- #
def recommend(cfg: ResearchConfig, trades: pd.DataFrame, *,
              fold_factor: float = 0.7, trials: int = 200,
              per_timeframe: bool = True, seed: int = 7) -> dict:
    """Готовит практические настройки: TP/SL/выход/фильтры/стратегии по таймфреймам.

    Первые `fold_factor` сделок по времени — train (на них подбор), остальное — test
    (на них честная итоговая цифра). То, что показывается пользователю, берётся
    с TEST-части; если на train красиво, а на test нет — это будет видно.
    """
    if trades.empty:
        return {"error": "нет сделок"}
    df = trades.sort_values("ts_entry").reset_index(drop=True)
    cut = int(len(df) * fold_factor)
    train, test = df.iloc[:cut], df.iloc[cut:]
    costs_pct = cfg.costs.total_round_trip_pct()
    out: dict = {"train_n": len(train), "test_n": len(test),
                 "costs_round_trip_pct": round(costs_pct, 4),
                 "verdicts": [], "variants": [], "analysis": {}}

    groups = [("all", df, train, test)]
    if per_timeframe and "tf" in df.columns:
        groups = [(tf, df[df["tf"] == tf], train[train["tf"] == tf], test[test["tf"] == tf])
                  for tf in sorted(df["tf"].unique())]

    for name, g_all, g_train, g_test in groups:
        if g_train.empty:
            continue
        variants = best_variant(g_all, cfg, subset=g_train)
        if variants.empty:
            continue
        top = variants.head(5).to_dict("records")
        for r in top:
            r.pop("why", None)
        out["variants"].append({"group": name, "top": top})
        best = variants.iloc[0]
        best_label = best["variant"]
        train_v = g_train[g_train["variant"] == best_label]
        gates, train_st = search_gates(train_v, cfg, trials=trials, seed=seed)
        portfolio = select_portfolio(apply_gates(train_v, gates), cfg)
        allowed = set(portfolio["allowed"])
        test_f = g_test[g_test["variant"] == best_label]
        test_f = apply_gates(test_f, gates)
        if allowed:
            test_f = test_f[test_f.apply(
                lambda r: f"{r['stype']}/{str(r['side'])[0]}", axis=1).isin(allowed)]
        test_st = compute_stats(test_f.to_dict("records"))
        ok, val, why = score(test_st, costs_pct, cfg.constraints)
        out["verdicts"].append({
            "group": name,
            "variant": best_label,
            "tp_pct": train_v["tp_pct"].median() if not train_v.empty else None,
            "sl_pct": train_v["sl_pct"].median() if not train_v.empty else None,
            "gates": gates,
            "allowed_strategies": sorted(portfolio["allowed"]),
            "train": train_st.as_dict(),
            "test": test_st.as_dict(),
            "passes_target": bool(ok),
            "why": why,
            "diagnostics": target_diagnostics(g_train[g_train["variant"] == best_label]),
            "formula": volatility_formula(g_train[g_train["variant"] == best_label]),
        })
    return out
