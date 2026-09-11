"""Бэктест-движок: векторный и bar-by-bar расчёт статистики стратегии."""
from .engine import run_backtest, Trade, BacktestResult

__all__ = ["run_backtest", "Trade", "BacktestResult"]
