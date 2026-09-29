"""
Реестр стратегий: по type строит экземпляр Strategy, отдаёт JSON-схемы для UI.
"""
from __future__ import annotations

from typing import Optional

from .base import Strategy
from .builtin import BUILTIN_STRATEGIES


def get_strategy_class(stype: str):
    return BUILTIN_STRATEGIES.get(stype)


def build_strategy(stype: str, params: Optional[dict] = None,
                   tp_pct: Optional[float] = None,
                   sl_pct: Optional[float] = None,
                   max_hold_hours: Optional[float] = None) -> Optional[Strategy]:
    cls = get_strategy_class(stype)
    if cls is None:
        return None
    return cls(params=params, tp_pct=tp_pct, sl_pct=sl_pct, max_hold_hours=max_hold_hours)


def STRATEGY_SCHEMAS() -> list[dict]:
    """Описание всех стратегий (для выпадающего списка в UI)."""
    out = []
    for stype, cls in BUILTIN_STRATEGIES.items():
        inst = cls()
        out.append(inst.config_dict())
    return out
