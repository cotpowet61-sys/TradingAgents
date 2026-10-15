"""
Барьерная разметка сделки (triple barrier) — сердце честного измерения.

Почему не «PnL на закрытии через N баров»: нас интересует, что произойдёт в бою
с конкретными TP и SL. Поэтому моделируем путь цены бар за баром и фиксируем
первое касание: TP, SL, таймаут. Дополнительно считаем MAE/MFE — максимальный
ход против и в пользу сделки: именно они показывают, «дышит» ли цель.

Консервативные допущения (по умолчанию в пользу пессимизма):
  • если в одном баре задеты и TP, и SL — считаем SL;
  • вход по цене entry (в движке это open следующего бара), стопы могут сработать
    уже на баре входа;
  • издержки (комиссия + проскальзывание) вычитаются из результата;
  • breakeven/трейлинг обновляют стоп только по закрытию бара (без «внутрибарной
    магии»), как в portal/signal_gates.update_stops.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import CostModel, ExitSpec


@dataclass
class TradeResult:
    outcome: str            # TP | SL | TIMEOUT | BREAKEVEN
    exit_price: float
    exit_index: int
    bars_held: int
    pnl_gross_pct: float
    pnl_net_pct: float
    mae_pct: float          # максимальный ход против (положительное число)
    mfe_pct: float          # максимальный ход в пользу
    tp: float
    sl: float
    hit_tp_first: bool = False

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome, "exit_price": self.exit_price,
            "bars_held": self.bars_held, "pnl_gross_pct": self.pnl_gross_pct,
            "pnl_net_pct": self.pnl_net_pct, "mae_pct": self.mae_pct,
            "mfe_pct": self.mfe_pct, "tp": self.tp, "sl": self.sl,
        }


def clamp_targets(entry: float, side: str, tp_pct: float, sl_pct: float) -> tuple[float, float]:
    """TP/SL по процентам → абсолютные цены (как в portal/signal_gates)."""
    if side == "LONG":
        return entry * (1 + tp_pct / 100.0), entry * (1 - sl_pct / 100.0)
    return entry * (1 - tp_pct / 100.0), entry * (1 + sl_pct / 100.0)


def simulate_trade(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                   entry_index: int, entry: float, side: str, tp: float, sl: float,
                   spec: ExitSpec, costs: CostModel, max_hold_bars: int) -> TradeResult:
    """Прогон одной сделки по барам начиная с entry_index (бар входа включён)."""
    n = len(close)
    be_armed = False
    trail_armed = False
    best = entry
    worst = entry
    slip = costs.slippage_bps / 10_000.0
    # вход по «невыгодной» стороне: покупка дороже, продажа дешевле
    fill_entry = entry * (1 + slip) if side == "LONG" else entry * (1 - slip)

    for k in range(entry_index, min(n, entry_index + max_hold_bars)):
        hi, lo, cl = float(high[k]), float(low[k]), float(close[k])
        if side == "LONG":
            best = max(best, hi)
            worst = min(worst, lo)
            hit_tp = hi >= tp
            hit_sl = lo <= sl
        else:
            best = min(best, lo)
            worst = max(worst, hi)
            hit_tp = lo <= tp
            hit_sl = hi >= sl

        if hit_tp and hit_sl:
            # внутрибарный конфликт: консервативно считаем стоп
            return _finish("SL", sl, k, entry_index, fill_entry, side, costs, best, worst)
        if hit_sl:
            outcome = "BREAKEVEN" if abs(sl - fill_entry) / fill_entry < 0.0005 else "SL"
            return _finish(outcome, sl, k, entry_index, fill_entry, side, costs, best, worst)
        if hit_tp:
            return _finish("TP", tp, k, entry_index, fill_entry, side, costs, best, worst)

        # выходы портала: breakeven и трейлинг по закрытию бара
        move_pct = (cl - fill_entry) / fill_entry * 100.0
        if side == "SHORT":
            move_pct = -move_pct
        if spec.be_trigger_pct and move_pct >= spec.be_trigger_pct and not be_armed:
            be_armed = True
            sl = fill_entry * (1 + spec.be_offset_pct / 100.0) if side == "LONG" \
                else fill_entry * (1 - spec.be_offset_pct / 100.0)
        if spec.trail_start_pct and move_pct >= spec.trail_start_pct:
            trail_armed = True
        if trail_armed:
            if side == "LONG":
                sl = max(sl, cl * (1 - spec.trail_pct / 100.0))
            else:
                sl = min(sl, cl * (1 + spec.trail_pct / 100.0))

    last = float(close[min(n - 1, entry_index + max_hold_bars - 1)])
    return _finish("TIMEOUT", last, min(n - 1, entry_index + max_hold_bars - 1),
                   entry_index, fill_entry, side, costs, best, worst)


def _finish(outcome: str, exit_px: float, k: int, entry_index: int, fill_entry: float,
            side: str, costs: CostModel, best: float, worst: float) -> TradeResult:
    sign = 1.0 if side == "LONG" else -1.0
    gross = sign * (exit_px - fill_entry) / fill_entry * 100.0
    net = gross - costs.total_round_trip_pct()
    mfe = sign * (best - fill_entry) / fill_entry * 100.0
    mae = -sign * (worst - fill_entry) / fill_entry * 100.0
    return TradeResult(outcome=outcome, exit_price=round(exit_px, 10),
                       exit_index=k, bars_held=max(1, k - entry_index + 1),
                       pnl_gross_pct=round(gross, 4), pnl_net_pct=round(net, 4),
                       mae_pct=round(max(0.0, mae), 4), mfe_pct=round(max(0.0, mfe), 4),
                       tp=0.0, sl=0.0, hit_tp_first=(outcome == "TP"))


def breakeven_winrate(tp_pct: float, sl_pct: float, costs_pct: float) -> float:
    """Минимальный винрейт, при котором стратегия не теряет деньги.

    Формула без «магии»: p* = (SL + c) / (TP + SL), где c — полные издержки
    сделки в процентах. Именно её надо сравнивать с целевыми 75%.
    """
    return 100.0 * (sl_pct + costs_pct) / (tp_pct + sl_pct) if (tp_pct + sl_pct) > 0 else 100.0


def cost_floor_tp_pct(costs: CostModel, multiple: float = 4.0) -> float:
    """Минимальный разумный TP: издержки должны быть малой частью цели.

    При комиссии+слиппедже 0.15% туда-обратно TP 0.6% отдаёт издержкам четверть
    результата — это ещё терпимо, а TP 0.2% — уже нет.
    """
    return round(costs.total_round_trip_pct() * multiple, 4)
