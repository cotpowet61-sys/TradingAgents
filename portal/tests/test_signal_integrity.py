"""Честность сканера: синтетика не попадает в ленту, обучение и трек-рекорд."""
from __future__ import annotations

from conftest import fresh_env, load


def _event(**kw):
    base = {"type": "signal_close", "symbol": "BTCUSDT", "timeframe": "5m", "side": "LONG",
            "stype": "momentum_breakout", "conf": 0.7, "reason": "TP", "pnl_pct": 1.5,
            "duration_min": 30, "real_data": True, "source": "bybit", "ts": 1}
    base.update(kw)
    return base


def test_synthetic_outcomes_are_not_recorded(monkeypatch):
    """Исходы по «учебному графику» не пишутся в журнал: иначе они отравят
    и обучение, и публичную статистику."""
    fresh_env(monkeypatch)
    learner = load("signal_learner")
    learner.record_outcome(_event(source="synthetic", real_data=False))
    assert learner.read_outcomes() == []
    learner.record_outcome(_event(source="bybit", real_data=True))
    assert len(learner.read_outcomes()) == 1


def test_track_summary_excludes_synthetic_and_flags_small_sample(monkeypatch):
    fresh_env(monkeypatch)
    learner = load("signal_learner")
    learner.record_outcome(_event(source="synthetic", real_data=False, reason="TP", pnl_pct=9.9))
    for i in range(3):
        learner.record_outcome(_event(ts=i, pnl_pct=1.0 if i % 2 else -1.0))
    t = learner.track_summary()
    assert t["all"]["n"] == 3                 # синтетика не учтена
    assert t["collecting"] is True            # маленькая выборка честно помечена
    assert t["winrate_ci95"][0] <= t["all"]["winrate"] <= t["winrate_ci95"][1]
    assert "учебн" in t["note"].lower() or "real" in t["note"].lower()


def test_track_summary_shows_stats_when_sample_is_enough(monkeypatch):
    fresh_env(monkeypatch)
    learner = load("signal_learner")
    for i in range(learner.MIN_PUBLIC_N + 5):
        learner.record_outcome(_event(ts=i, pnl_pct=1.0 if i % 3 else -1.0))
    t = learner.track_summary()
    assert t["collecting"] is False
    assert t["all"]["n"] == learner.MIN_PUBLIC_N + 5


def test_rules_use_confidence_intervals_not_raw_winrate(monkeypatch):
    """Правила принимаются только по верхней/нижней границе Уилсона, не по сырому winrate.

    Ключевой кейс: 3 победы из 12 (25%) по старой логике (wr < 30% и n ≥ 12)
    блокировались бы, хотя интервал 95% тянется выше 50% — это ещё шум.
    """
    fresh_env(monkeypatch)
    learner = load("signal_learner")
    # Шум: 3/12, суммарный минус — старая логика блокировала, новая не должна
    noise = [_event(reason=("TP" if i < 3 else "SL"), pnl_pct=(1.0 if i < 3 else -0.6), ts=i)
             for i in range(12)]
    rule = learner.compute_rules(noise)["BTCUSDT"][0]
    assert rule["action"] != "block"
    assert rule["wr_ci95"][0] < rule["winrate"] < rule["wr_ci95"][1]
    assert rule["wr_ci95"][1] > 30.0            # верхняя граница ещё не говорит «плохо»

    # Убедительно убыточно: 2/30 и минус по сумме → блок
    bad = [_event(reason=("TP" if i < 2 else "SL"), pnl_pct=(1.0 if i < 2 else -0.8), ts=i)
           for i in range(30)]
    rb = learner.compute_rules(bad)["BTCUSDT"][0]
    assert rb["action"] == "block"
    assert rb["wr_ci95"][1] < learner.BLOCK_WR


def test_wilson_bounds_are_sane(monkeypatch):
    fresh_env(monkeypatch)
    learner = load("signal_learner")
    lo, hi = learner.wilson(5, 10)
    assert 0.2 < lo < 0.5 < hi < 0.8
    lo2, hi2 = learner.wilson(100, 200)
    assert hi2 - lo2 < hi - lo          # больше выборка → уже интервал
    assert learner.wilson(0, 0) == (0.0, 1.0)


def test_live_engine_refuses_synthetic_feed(monkeypatch):
    """Сканер не работает на синтетике, пока REQUIRE_REAL_DATA=true."""
    fresh_env(monkeypatch)
    import sys
    live = load("live_engine")
    md = sys.modules.get("data.market_data")
    if md is None:
        from data import market_data as md
    md._STATUS["source"] = "synthetic"
    ok, src = live.real_data_ok()
    assert ok is False and src == "synthetic"
    md._STATUS["source"] = "bybit"
    assert live.real_data_ok() == (True, "bybit")


def test_sizing_math_respects_risk_and_cap(monkeypatch):
    fresh_env(monkeypatch)
    portal_config = load("portal_config")
    live = load("live_engine")
    s = portal_config.settings
    object.__setattr__(s, "DEPOSIT_USD", 1000.0)
    object.__setattr__(s, "RISK_PER_TRADE_PCT", 2.0)
    object.__setattr__(s, "LEVERAGE", 20.0)
    # вход 100, стоп 99 (1%) → риск $20 ⇒ нотионал $2000, маржа $100
    out = live.LiveEngine._sizing(100.0, "LONG", 102.0, 99.0)
    assert abs(out["planned_risk"] - 20.0) < 1e-6
    assert abs(out["notional"] - 2000.0) < 1.0
    assert abs(out["margin"] - 100.0) < 1.0
    assert out["capped"] is False
    # стоп 0.05% → нотионал огромный, но маржа ограничена 95% депозита
    capped = live.LiveEngine._sizing(100.0, "LONG", 110.0, 99.95)
    assert capped["capped"] is True
    assert capped["margin"] <= 1000 * 0.95 * 20


def test_tf_rotation_covers_all_timeframes(monkeypatch):
    """Ротация ТФ: каждый таймфрейм получает свой тик, полный круг — без пропусков."""
    fresh_env(monkeypatch)
    portal_config = load("portal_config")
    live = load("live_engine")
    s = portal_config.settings
    object.__setattr__(s, "SIGNAL_TIMEFRAMES", "1m,5m,15m")
    object.__setattr__(s, "TFS_PER_TICK", 1)
    eng = live.LiveEngine()
    seen = [eng._tick_tfs()[0] for _ in range(3)]
    assert sorted(seen) == ["15m", "1m", "5m"]


def test_cadence_and_tf_rotation_read_settings(monkeypatch):
    """SCAN_CADENCE_SECONDS/TFS_PER_TICK обязаны читаться из настроек.

    Раньше значения брались через getattr с дефолтом — то есть .env молча
    игнорировался. Тест фиксирует контракт.
    """
    fresh_env(monkeypatch)
    portal_config = load("portal_config")
    live = load("live_engine")
    s = portal_config.settings
    object.__setattr__(s, "SCAN_CADENCE_SECONDS", "30, 60,90,abc,2")
    assert live.LiveEngine()._cadence_options() == [30, 60, 90]   # мусор и <5 отброшены
    object.__setattr__(s, "SCAN_CADENCE_SECONDS", "")
    object.__setattr__(s, "SCAN_INTERVAL_SECONDS", 45)
    assert live.LiveEngine()._cadence_options() == [45]
