"""
Базовый класс стратегии и модель сигнала.

Все стратегии возвращают Signal для текущего бара. Сторона может быть LONG/SHORT/NEUTRAL.
TP и SL рассчитываются от entry по процентам, заданным в стратегии (по умолчанию
берутся глобальные настройки из config: MIN_TP_PCT=2%, DEFAULT_SL_PCT=1%).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Optional

from config import settings
from data.market_data import Candle


class SignalSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


@dataclass
class Signal:
    side: SignalSide
    entry: float
    tp: float
    sl: float
    tp_pct: float
    sl_pct: float
    confidence: float = 0.0
    reason: str = ""
    ts: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["side"] = self.side.value
        return d


class Strategy:
    """Базовый класс. Подклассы реализуют generate()."""

    type: str = "base"
    name: str = "Base Strategy"
    description: str = ""

    # Параметры по умолчанию (переопределяются подклассами)
    default_params: dict = {}

    def __init__(self, params: Optional[dict] = None,
                 tp_pct: Optional[float] = None,
                 sl_pct: Optional[float] = None,
                 max_hold_hours: Optional[float] = None):
        self.params = {**self.default_params, **(params or {})}
        # Гарантируем требование: TP >= 2% и время сделки <= 4ч
        self.tp_pct = max(tp_pct if tp_pct is not None else settings.MIN_TP_PCT,
                          settings.MIN_TP_PCT)
        self.sl_pct = sl_pct if sl_pct is not None else settings.DEFAULT_SL_PCT
        self.max_hold_hours = min(
            max_hold_hours if max_hold_hours is not None else settings.MAX_HOLD_HOURS,
            settings.MAX_HOLD_HOURS,
        )

    def generate(self, candles: List[Candle], index: int) -> Signal:
        """Сигнал на баре index (только по данным ≤ index)."""
        raise NotImplementedError

    def _build_signal(self, side: SignalSide, candles: List[Candle], index: int,
                      confidence: float, reason: str) -> Signal:
        c = candles[index]
        entry = c.close
        if side == SignalSide.LONG:
            tp = entry * (1 + self.tp_pct / 100)
            sl = entry * (1 - self.sl_pct / 100)
        elif side == SignalSide.SHORT:
            tp = entry * (1 - self.tp_pct / 100)
            sl = entry * (1 + self.sl_pct / 100)
        else:
            tp = sl = entry
        return Signal(
            side=side, entry=entry, tp=tp, sl=sl,
            tp_pct=self.tp_pct, sl_pct=self.sl_pct,
            confidence=confidence, reason=reason, ts=c.ts,
        )

    def config_dict(self) -> dict:
        return {
            "type": self.type,
            "name": self.name,
            "description": self.description,
            "params": self.params,
            "tp_pct": self.tp_pct,
            "sl_pct": self.sl_pct,
            "max_hold_hours": self.max_hold_hours,
        }
