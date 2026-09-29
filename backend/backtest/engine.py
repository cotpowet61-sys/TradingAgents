"""
Движок бэктеста.

Порядок:
  1) Для каждого бара стратегия генерирует сигнал (только по данным ≤ бара — без look-ahead).
  2) Если сигнал LONG/SHORT и нет открытой позиции — входим по цене закрытия бара.
  3) Устанавливаем TP/SL из сигнала и лимит удержания (<= max_hold_hours).
  4) Далее bar-by-bar проверяем: достижение TP, SL или истечение времени.
  5) Учитываем комиссию на вход и выход. Один вход на сделку, без пирамидинга/мартингейла.

Результат — метрики + список сделок (для UI: equity-кривая, таблица, анализ).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import List, Optional

from config import settings
from data.market_data import Candle
from strategies.base import Strategy, Signal, SignalSide


@dataclass
class Trade:
    id: int
    side: str
    entry_ts: int
    exit_ts: int
    entry: float
    exit: float
    pnl_pct: float       # чистый % по сделке (с учётом комиссии)
    exit_reason: str     # TP / SL / TIME
    hold_hours: float
    entry_index: int
    exit_index: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    strategy: str
    params: dict
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl_pct: float
    profit_factor: float
    max_drawdown_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    avg_hold_hours: float
    sharpe: float
    equity_curve: List[dict]
    trades: List[dict]
    started: int
    finished: int
    # --- Капитал (для расчёта «было $X → стало $Y») ---
    initial_capital: float
    final_equity: float
    return_pct: float
    start_1000: float = 1000.0
    end_1000: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _fee_pct() -> float:
    return settings.TRADING_FEE_PCT


def _interval_hours(timeframe: str) -> float:
    unit = timeframe[-1]
    n = int(timeframe[:-1])
    return n * {"m": 1 / 60, "h": 1.0, "d": 24.0}.get(unit, 1 / 60)


def run_backtest(strategy: Strategy, candles: List[Candle], timeframe: str,
                 max_hold_hours: Optional[float] = None,
                 initial_capital: Optional[float] = None) -> BacktestResult:
    capital = initial_capital or settings.INITIAL_CAPITAL
    hold_limit = max_hold_hours if max_hold_hours is not None else strategy.max_hold_hours
    step_hours = _interval_hours(timeframe)
    fee = _fee_pct() / 100.0

    trades: List[Trade] = []
    equity_curve: List[dict] = []
    equity = capital
    pnl_pcts: List[float] = []

    # Индекс текущей открытой позиции
    pos: Optional[dict] = None
    trade_id = 0
    n = len(candles)

    for i in range(n):
        # 1) Закрытие активной позиции по TP/SL/времени
        if pos is not None:
            c = candles[i]
            exit_price = None
            reason = None
            if pos["side"] == "LONG":
                hit_tp = c.high >= pos["tp"]
                hit_sl = c.low <= pos["sl"]
            else:  # SHORT
                hit_tp = c.low <= pos["tp"]
                hit_sl = c.high >= pos["sl"]

            if hit_tp and hit_sl:
                # Разрыв через оба уровня за один бар — консервативно берём SL
                exit_price, reason = pos["sl"], "SL"
            elif hit_tp:
                exit_price, reason = pos["tp"], "TP"
            elif hit_sl:
                exit_price, reason = pos["sl"], "SL"

            # лимит по времени
            held_hours = (i - pos["entry_index"]) * step_hours
            if exit_price is None and held_hours >= hold_limit:
                exit_price, reason = c.close, "TIME"
                held_hours = hold_limit

            if exit_price is not None:
                # расчёт PnL с учётом комиссии на обе стороны
                if pos["side"] == "LONG":
                    gross = (exit_price - pos["entry"]) / pos["entry"]
                else:
                    gross = (pos["entry"] - exit_price) / pos["entry"]
                net = gross - 2 * fee
                pnl_pcts.append(net)
                equity *= (1 + net * settings.POSITION_SIZE_PCT / 100.0)

                trades.append(Trade(
                    id=trade_id, side=pos["side"],
                    entry_ts=pos["entry_ts"], exit_ts=c.ts,
                    entry=pos["entry"], exit=exit_price,
                    pnl_pct=round(net * 100, 4), exit_reason=reason,
                    hold_hours=round(held_hours, 3),
                    entry_index=pos["entry_index"], exit_index=i,
                ))
                trade_id += 1
                pos = None

        # 2) Открытие позиции (только если позиции нет)
        if pos is None:
            sig: Signal = strategy.generate(candles, i)
            if sig.side in (SignalSide.LONG, SignalSide.SHORT):
                pos = {
                    "side": sig.side.value,
                    "entry": sig.entry,
                    "tp": sig.tp,
                    "sl": sig.sl,
                    "entry_ts": sig.ts,
                    "entry_index": i,
                }

        equity_curve.append({"ts": candles[i].ts, "equity": round(equity, 2)})

    # ------------------------------------------------------------------ #
    # Метрики
    # ------------------------------------------------------------------ #
    total = len(trades)
    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]
    total_pnl = sum(t.pnl_pct for t in trades)
    gross_win = sum(t.pnl_pct for t in wins)
    gross_loss = abs(sum(t.pnl_pct for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)

    # Max drawdown по equity-кривой
    peak = -float("inf")
    max_dd = 0.0
    for p in equity_curve:
        peak = max(peak, p["equity"])
        dd = (peak - p["equity"]) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)

    avg_win = (sum(t.pnl_pct for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t.pnl_pct for t in losses) / len(losses)) if losses else 0.0
    avg_hold = (sum(t.hold_hours for t in trades) / total) if total else 0.0

    # Итоговый капитал (сложный процент с учётом размера позиции)
    final_equity = equity
    return_pct = (final_equity / capital - 1) * 100 if capital else 0.0
    end_1000 = 1000.0 * (final_equity / capital) if capital else 0.0

    # Sharpe (по сделкам, приблизительно)
    if len(pnl_pcts) > 1:
        mean = sum(pnl_pcts) / len(pnl_pcts)
        var = sum((x - mean) ** 2 for x in pnl_pcts) / (len(pnl_pcts) - 1)
        std = math.sqrt(var)
        sharpe = (mean / std) if std > 0 else 0.0
    else:
        sharpe = 0.0

    return BacktestResult(
        symbol="", timeframe=timeframe, strategy=strategy.type, params=strategy.params,
        total_trades=total, wins=len(wins), losses=len(losses),
        win_rate=round((len(wins) / total * 100) if total else 0, 2),
        total_pnl_pct=round(total_pnl, 3),
        profit_factor=round(profit_factor, 2) if profit_factor != float("inf") else None,
        max_drawdown_pct=round(max_dd, 2),
        avg_win_pct=round(avg_win, 3),
        avg_loss_pct=round(avg_loss, 3),
        avg_hold_hours=round(avg_hold, 3),
        sharpe=round(sharpe, 3),
        equity_curve=equity_curve,
        trades=[t.to_dict() for t in trades],
        started=candles[0].ts if candles else 0,
        finished=candles[-1].ts if candles else 0,
        initial_capital=round(capital, 2),
        final_equity=round(final_equity, 2),
        return_pct=round(return_pct, 2),
        end_1000=round(end_1000, 2),
    )
