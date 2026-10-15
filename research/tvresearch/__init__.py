"""
tvresearch — исследовательский контур TradeVision.

Пакет намеренно отделён от `portal/`: он читает код портала (стратегии, движок
сигналов, рамки целей), но НИЧЕГО там не меняет. Всё, что здесь считается —
сохраняется в `research/artifacts/` и переносится в портал строками `.env`
и файлом бандла мета-модели.
"""
from .config import ResearchConfig, ExitSpec, CostModel, Constraints   # noqa: F401

__all__ = ["ResearchConfig", "ExitSpec", "CostModel", "Constraints"]
__version__ = "0.1.0"
