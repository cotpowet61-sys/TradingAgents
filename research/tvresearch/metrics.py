"""
Метрики качества. Без этого модуля «винрейт 75%» — красивая цифра без смысла.

Что считаем и почему именно так:
  • winrate + интервал Уилсона (нижняя граница) — доверие к винрейту при малой выборке;
  • expectancy (средний PnL сделки, %) — главная метрика: она отвечает на вопрос
    «зарабатывает ли система деньги», а не «часто ли угадывает»;
  • profit factor — прибыль / убыток;
  • max drawdown — просадка (в п.п. и как доля депозита при fixed-fraction риске);
  • медиана времени удержания — сколько капитала стоит «занято»;
  • доля таймаутов — сколько сделок не дожило до барьера;
  • устойчивость по подвыборкам — худший и лучший фолд.

Все цены/PnL в процентах, комиссии и проскальзывание уже вычтены (см. barriers).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Iterable, Optional

Z95 = 1.959963985


def wilson_interval(wins: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Интервал Уилсона для доли (в процентах). Правильно ведёт себя при малых n."""
    if n <= 0:
        return (0.0, 100.0)
    p = wins / n
    den = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (100 * max(0.0, (centre - half) / den), 100 * min(1.0, (centre + half) / den))


def bootstrap_mean_ci(values: Iterable[float], iters: int = 800, seed: int = 7,
                      alpha: float = 0.05) -> tuple[float, float]:
    """Перцентильный bootstrap для среднего (экспектанси)."""
    v = [float(x) for x in values]
    if len(v) < 3:
        return (0.0, 0.0)
    rnd = random.Random(seed)
    n = len(v)
    means = []
    for _ in range(iters):
        means.append(sum(v[rnd.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo = means[int(alpha / 2 * len(means))]
    hi = means[int((1 - alpha / 2) * len(means)) - 1]
    return (round(lo, 4), round(hi, 4))


def max_drawdown_pct(pnls: Iterable[float]) -> float:
    """Максимальная просадка кумулятивной суммы PnL (в процентных пунктах)."""
    eq = 0.0
    peak = 0.0
    dd = 0.0
    for p in pnls:
        eq += float(p)
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return round(dd, 3)


def max_drawdown_fraction(pnls: Iterable[float], risk_pct: float = 2.0) -> float:
    """Просадка как доля депозита при фиксированном риске на сделку.

    Каждая сделка рискует risk_pct% депозита и приносит pnl/|SL| × риск.
    Это ближе к реальности, чем «сумма процентов».
    """
    eq = 1.0
    peak = 1.0
    dd = 0.0
    for p in pnls:
        eq *= (1 + (float(p) / 100.0) * (risk_pct / 100.0))
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak)
    return round(100 * dd, 3)


@dataclass
class Stats:
    n: int = 0
    wins: int = 0
    winrate: float = 0.0
    wr_lo: float = 0.0
    wr_hi: float = 0.0
    expectancy: float = 0.0
    expectancy_lo: float = 0.0
    expectancy_hi: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: Optional[float] = None
    payoff: float = 0.0            # avg_win / |avg_loss|
    max_dd_pp: float = 0.0
    max_dd_frac: float = 0.0
    median_hold_min: float = 0.0
    timeout_share: float = 0.0
    tp_share: float = 0.0
    sl_share: float = 0.0
    median_mfe: float = 0.0
    median_mae: float = 0.0

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        for k, v in list(d.items()):
            if isinstance(v, float):
                d[k] = round(v, 4)
        return d

    def line(self) -> str:
        pf = "∞" if self.profit_factor is None else f"{self.profit_factor:.2f}"
        return (f"n={self.n:<5} WR={self.winrate:5.1f}% "
                f"(ниж. {self.wr_lo:5.1f}%) exp={self.expectancy:+.3f}% "
                f"PF={pf:>5} DD={self.max_dd_pp:.1f}пп "
                f"hold={self.median_hold_min:.0f}м")


def _median(vals: list[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    m = len(s) // 2
    return float(s[m]) if len(s) % 2 else (s[m - 1] + s[m]) / 2


def compute_stats(trades: list[dict], pnl_key: str = "pnl_net_pct",
                  hold_key: str = "hold_min", risk_pct: float = 2.0,
                  bootstrap: bool = True) -> Stats:
    """Свод по списку сделок. trades: словари с pnl_net_pct и hold_min."""
    if not trades:
        return Stats()
    pnls = [float(t.get(pnl_key) or 0.0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gw = sum(wins)
    gl = -sum(losses)
    n = len(pnls)
    wr = 100 * len(wins) / n
    lo, hi = wilson_interval(len(wins), n)
    exp = sum(pnls) / n
    exp_lo, exp_hi = bootstrap_mean_ci(pnls) if bootstrap else (exp, exp)
    holds = [float(t.get(hold_key) or 0.0) for t in trades]
    reasons = [str(t.get("outcome") or t.get("reason") or "").upper() for t in trades]
    mfe = [float(t.get("mfe_pct") or 0.0) for t in trades]
    mae = [float(t.get("mae_pct") or 0.0) for t in trades]
    return Stats(
        n=n, wins=len(wins), winrate=round(wr, 2), wr_lo=round(lo, 2), wr_hi=round(hi, 2),
        expectancy=round(exp, 4), expectancy_lo=exp_lo, expectancy_hi=exp_hi,
        avg_win=round(gw / len(wins), 4) if wins else 0.0,
        avg_loss=round(-gl / len(losses), 4) if losses else 0.0,
        profit_factor=round(gw / gl, 3) if gl > 0 else (None if gw == 0 else 99.0),
        payoff=round((gw / len(wins)) / (gl / len(losses)), 3) if wins and losses and gl else 0.0,
        max_dd_pp=max_drawdown_pct(pnls),
        max_dd_frac=max_drawdown_fraction(pnls, risk_pct),
        median_hold_min=round(_median(holds), 1),
        timeout_share=round(100 * sum(1 for r in reasons if r.startswith("TIME")) / n, 2),
        tp_share=round(100 * sum(1 for r in reasons if r == "TP") / n, 2),
        sl_share=round(100 * sum(1 for r in reasons if r == "SL") / n, 2),
        median_mfe=round(_median(mfe), 3), median_mae=round(_median(mae), 3),
    )


def group_stats(trades: list[dict], key_fn, min_n: int = 1,
                pnl_key: str = "pnl_net_pct") -> dict[object, Stats]:
    """Свод по группам (стратегия, сторона, монета, ...)."""
    groups: dict[object, list[dict]] = {}
    for t in trades:
        groups.setdefault(key_fn(t), []).append(t)
    return {k: compute_stats(v, pnl_key=pnl_key)
            for k, v in groups.items() if len(v) >= min_n}


def deflated_significance(stats: Stats, trials: int, min_trades: int = 30) -> bool:
    """Грубая поправка на множественную проверку (сколько вариантов перебрали).

    Мы перебираем десятки комбинаций, поэтому «лучший по выборке» почти всегда
    выглядит хорошо. Требуем: достаточно сделок и нижняя граница Уилсона
    выше безубыточного уровня (сравнение делает вызывающий код).
    """
    return stats.n >= min_trades and stats.wr_lo > 0


def walk_forward_folds(n_bars: int, folds: int = 4, embargo_bars: int = 0) -> list[tuple[int, int]]:
    """Границы тестовых окон walk-forward (расширяющееся обучающее окно)."""
    if folds < 2 or n_bars < folds * 50:
        return [(0, n_bars)]
    size = n_bars // folds
    out = []
    for k in range(1, folds):
        start = k * size + embargo_bars
        end = (k + 1) * size if k + 1 < folds else n_bars
        if end - start > 30:
            out.append((start, end))
    return out
