"""
Конфигурация исследовательского контура TradeVision.

Здесь нет магии: только явные параметры, влияющие на достоверность результата.
Ключевая идея — всё, что может «подсмотреть будущее» или завысить метрику,
вынесено в отдельные флаги и по умолчанию выставлено в консервативную сторону.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

# Ровно то окно, которое использует live-сканер портала (market.get_klines(limit=300)).
LIVE_WINDOW = 300

# Таймфреймы Bybit: 1m,3m,5m,15m,30m,1h,4h. Длительность в минутах нужна для таймаутов.
TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}


@dataclass(frozen=True)
class CostModel:
    """Издержки. По умолчанию — тейкер Bybit и умеренное проскальзывание.

    Винрейт и экспектанси считаются ПОСЛЕ издержек: сигнал с TP 0.3% и SL 0.3%
    после комиссии 0.11% (туда-обратно) и слиппеджа перестаёт быть прибыльным.
    """
    fee_bps: float = 5.5          # 0.055% за сторону (taker)
    slippage_bps: float = 2.0     # проскальзывание на вход/выход, за сторону
    funding_bps_per_day: float = 0.0   # можно учесть при переносе через сутки

    def total_round_trip_pct(self) -> float:
        return 2 * (self.fee_bps + self.slippage_bps) / 100.0


@dataclass
class ExitSpec:
    """Правила выхода. Зеркало portal/signal_gates.compute_targets/update_stops.

    mode:
      atr      — TP = ATR% × tp_mult, SL = ATR% × sl_mult (портал: RR_BINDING=false)
      fixed    — TP/SL в процентах (портал: RR_BINDING=true или MANUAL_*)
    Дополнительно: breakeven и трейлинг (механизмы выхода портала).
    """
    mode: str = "atr"
    tp_mult: float = 2.0
    sl_mult: float = 1.2
    fixed_tp_pct: float = 2.0
    fixed_sl_pct: float = 1.0
    # рамки «плавающих» целей (как TP_PCT_MIN/MAX, SL_PCT_MIN/MAX в портале)
    tp_min_pct: float = 0.5
    tp_max_pct: float = 3.0
    sl_min_pct: float = 0.25
    sl_max_pct: float = 2.5
    be_trigger_pct: float = 0.0   # 0 = выключено
    be_offset_pct: float = 0.15
    trail_start_pct: float = 0.0
    trail_pct: float = 0.6
    max_hold_bars: int = 0        # 0 → считать по max_hold_hours
    max_hold_hours: float = 4.0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Constraints:
    """Что именно мы называем «хорошим сигналом».

    winrate_target — не самоцель, а ОГРАНИЧЕНИЕ: оптимизируем экспектанси (средний
    PnL на сделку), но не берём решения с винрейтом ниже цели и выборкой меньше
    min_trades. TP-пол не даёт «накрутить» винрейт микроскопическими целями.
    """
    winrate_target: float = 75.0
    tp_min_pct: float = 0.6       # «не слишком маленький TP» — в процентах от цены
    sl_min_pct: float = 0.3
    min_trades: int = 40          # минимум сделок в окне теста
    min_trades_per_group: int = 15  # минимум на (стратегия, сторона) и т.п.
    min_expectancy_pct: float = 0.0
    max_dd_pct: float = 25.0
    require_wilson_lb_above_breakeven: bool = True


@dataclass
class ResearchConfig:
    """Полная конфигурация прогона. Пишется в отчёт вместе с результатами."""
    symbols: list[str] = field(default_factory=lambda: [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
        "ADAUSDT", "AVAXUSDT", "LINKUSDT", "TONUSDT", "SUIUSDT", "APTUSDT",
    ])
    timeframes: list[str] = field(default_factory=lambda: ["15m", "1h"])
    days: int = 180
    strategies: Optional[list[str]] = None      # None = все 23 из реестра
    min_confidence: float = 0.55
    # Пол извлечения: сигналы ниже этого порога не записываются вообще
    # (в бою портал тоже режет по MIN_CONFIDENCE, обычно 0.55).
    extract_min_confidence: float = 0.5
    window: int = LIVE_WINDOW
    # "stype" — каждая стратегия измеряется независимо; "pair" — одна сделка
    # на пару в моменте, как в живом сканере.
    resolve_group_by: str = "stype"
    # Пропуск сделки, если открытие следующего бара ушло от цены сигнала
    # больше чем на N × ATR% (в бою это было бы исполнение сильно хуже расчёта).
    max_gap_atr: float = 3.0
    entry_mode: str = "next_open"               # next_open (честнее) | close (как live)
    costs: CostModel = field(default_factory=CostModel)
    exit_spec: ExitSpec = field(default_factory=ExitSpec)
    constraints: Constraints = field(default_factory=Constraints)
    cooldown_bars: int = 0                      # антиспам как в live: 30 мин
    one_position_per_pair: bool = True
    data_dir: str = "research/data"
    out_dir: str = "research/artifacts"
    seed: int = 7
    jobs: int = 0                               # 0 = по числу ядер

    def cooldown_bars_for(self, tf: str) -> int:
        if self.cooldown_bars:
            return int(self.cooldown_bars)
        minutes = TF_MINUTES.get(tf, 15)
        return max(1, int(round(30 / minutes)))  # live: 30 минут на ту же сторону

    def to_dict(self) -> dict:
        d = asdict(self)
        d["strategies"] = self.strategies or "all"
        return d
