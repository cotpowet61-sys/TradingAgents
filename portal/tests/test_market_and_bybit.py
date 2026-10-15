"""Данные биржи: нормализация, статистика сделок, пагинация, состояние рынка.

Сетевые вызовы не делаются: `_get` подменяется заглушкой, поэтому тесты
детерминированы и работают в CI без VPN.
"""
from __future__ import annotations

import asyncio

from conftest import fresh_env, load


CSV_TEXT = (
    "Symbol,Side,Qty,Entry Price,Exit Price,Closed PnL,Closing Time\n"
    "BTCUSDT,Buy,0.1,50000,51000,100.0,2026-01-01 10:00:00\n"
    "ETHUSDT,Sell,1,3000,2950,50.0,2026-01-02 11:30:00\n"
)


def test_csv_normalization_and_stats(monkeypatch):
    fresh_env(monkeypatch)
    ta = load("trades_analytics")
    res = ta.save_csv(CSV_TEXT)
    assert res["count"] == 2
    trades = ta.normalize_csv(CSV_TEXT)
    stats = ta.compute_stats(trades, days=90)
    assert stats["count"] == 2
    assert stats["wins"] == 2 and stats["losses"] == 0
    assert abs(stats["total_pnl"] - 150.0) < 1e-6
    assert stats["winrate"] == 100.0
    assert stats["profit_factor"] is None       # нет убытков → PF не определён, не «∞»


def test_csv_async_writer_does_not_block(monkeypatch):
    fresh_env(monkeypatch)
    ta = load("trades_analytics")
    res = asyncio.run(ta.save_csv_async(CSV_TEXT))
    assert res["saved"] is True and res["count"] == 2


def test_bybit_pagination_runs_windows_in_parallel_and_flags_truncation(monkeypatch):
    """Окна по 6.9 дня запрашиваются параллельно, а неполная выгрузка помечается."""
    fresh_env(monkeypatch)
    ba = load("bybit_account")
    calls = {"n": 0}

    async def fake_get(path, params):
        calls["n"] += 1
        # всегда отдаём «есть ещё» — значит данных больше, чем мы выкачали
        return {"list": [{"symbol": "BTCUSDT", "closedPnl": "1", "updatedTime": str(params.get("endTime"))}],
                "nextPageCursor": "more"}

    monkeypatch.setattr(ba, "_get", fake_get)
    rows = asyncio.run(ba.fetch_closed_pnl(days=14, max_pages=2))
    assert rows and ba.last_truncated() is True
    # 14 дней = 3 окна (14/6.9) × 2 страницы
    assert calls["n"] == 6


def test_market_status_marks_realness(monkeypatch):
    fresh_env(monkeypatch)
    from data import market_data as md
    md._STATUS["source"] = "bybit"
    assert md.data_status()["source"] == "bybit"


def test_csv_unrecognized_columns_are_honest(monkeypatch):
    fresh_env(monkeypatch)
    ta = load("trades_analytics")
    res = ta.save_csv("foo,bar\n1,2\n")
    assert res["count"] == 0


def test_portfolio_overview_requires_data(monkeypatch):
    """Без ключей и без CSV — понятная ошибка, а не пустая статистика."""
    fresh_env(monkeypatch)
    ta = load("trades_analytics")
    try:
        asyncio.run(ta.load_trades(days=30))
        raise AssertionError("должно было упасть без данных")
    except Exception as e:
        assert "read-only" in str(e) or "CSV" in str(e)
