# -*- coding: utf-8 -*-
"""
Тесты исследовательского контура.

Что здесь проверяется (по важности):

  1. Признаки считаются ОДИНАКОВО в research и в портале — иначе модель,
     обученная на истории, в бою увидит другие числа и будет мусором.
  2. Нет взгляда в будущее: признаки бара i не меняются от правок будущих баров.
  3. Барьеры разрешаются консервативно: задеты TP и SL в одном баре → считаем SL.
  4. Вход по открытию следующего бара, а не по цене сигнала.
  5. Метрики (Уилсон, безубыточный винрейт) считаются правильно.
  6. Бандл мета-модели: обучение → сохранение → загрузка порталом → те же числа.
  7. Одна позиция на стратегию/пару и кулдаун соблюдаются.
"""
from __future__ import annotations

import math
import os
import sys
import time

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tvresearch import barriers, data as D, engine as E, features as F, signals as S  # noqa: E402
from tvresearch.config import CostModel, ExitSpec, ResearchConfig, Constraints       # noqa: E402
from tvresearch.metrics import compute_stats, wilson_interval, max_drawdown_pct      # noqa: E402
from tvresearch.curated import (train_bundle, save_bundle, load_bundle,             # noqa: E402
                                predict_bundle, apply_bundle, choose_threshold, reliability)


# --------------------------------------------------------------------- #
# 1. Признаки: research == портал
# --------------------------------------------------------------------- #
def _rand_candles(n=320, seed=1, tf_ms=900_000):
    rng = np.random.default_rng(seed)
    px = 100.0
    rows = []
    t0 = 1_700_000_000_000
    for i in range(n):
        ret = rng.normal(0.0002, 0.005)
        o = px
        c = max(0.01, px * (1 + ret))
        w = abs(rng.normal(0, 0.002))
        h = max(o, c) * (1 + w)
        lo = min(o, c) * (1 - w)
        v = abs(rng.normal(1000, 200))
        rows.append((t0 + i * tf_ms, o, h, lo, c, v))
        px = c
    df = pd.DataFrame(rows, columns=D.REQUIRED_COLS)
    return df


def test_features_match_portal_exactly():
    """Признаки должны совпадать до численной точности (иначе модель «слепнет»)."""
    curated = pytest.importorskip("curated")
    df = _rand_candles(seed=3)
    candles = S.candles_from_df(df)
    open_ = df["open"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    vol = df["volume"].to_numpy(float)
    ts = df["ts"].to_numpy("int64")

    i = len(df) - 1
    res = F.features_at(open_, high, low, close, vol, ts, i, side="LONG",
                        conf=0.77, planned_rr=2.0)
    por = curated.features_from_candles(candles, "LONG", 0.77, 2.0)
    assert list(por.keys()) == F.FEATURE_NAMES
    for k in F.FEATURE_NAMES:
        assert por[k] == pytest.approx(res[k], rel=1e-12, abs=1e-12), f"признак {k} разошёлся"


def test_features_match_portal_random_windows():
    """То же на десятке случайных окон и обеих сторонах."""
    curated = pytest.importorskip("curated")
    for seed in range(10):
        df = _rand_candles(n=310 + seed, seed=seed + 100)
        candles = S.candles_from_df(df)
        i = len(df) - 1
        arr = {k: df[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume")}
        ts = df["ts"].to_numpy("int64")
        side = "LONG" if seed % 2 else "SHORT"
        res = F.features_at(arr["open"], arr["high"], arr["low"], arr["close"], arr["volume"],
                            ts, i, side=side, conf=0.6 + seed / 100.0, planned_rr=1.4 + seed / 10)
        por = curated.features_from_candles(candles, side, 0.6 + seed / 100.0, 1.4 + seed / 10)
        for k in F.FEATURE_NAMES:
            assert por[k] == pytest.approx(res[k], rel=1e-12, abs=1e-12), (seed, k)


# --------------------------------------------------------------------- #
# 2. Отсутствие взгляда в будущее
# --------------------------------------------------------------------- #
def test_no_lookahead_in_features():
    """Правка будущих баров не должна менять признаки текущего бара."""
    df = _rand_candles(seed=5)
    i = 250
    arr = {k: df[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume")}
    ts = df["ts"].to_numpy("int64")
    base = F.features_at(arr["open"], arr["high"], arr["low"], arr["close"], arr["volume"],
                         ts, i, side="LONG", conf=0.7, planned_rr=2.0)
    df2 = df.copy()
    df2.loc[i + 1:, ["open", "high", "low", "close", "volume"]] *= 1.5
    arr2 = {k: df2[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume")}
    same = F.features_at(arr2["open"], arr2["high"], arr2["low"], arr2["close"],
                         arr2["volume"], ts, i, side="LONG", conf=0.7, planned_rr=2.0)
    assert base == same


def test_no_lookahead_in_signals_and_entry():
    """Сигнал на баре i и вход по open бара i+1 — сверяем по факту сделки."""
    rng = np.random.default_rng(11)
    n = 400
    px = 100.0
    rows = []
    t0 = 1_700_000_000_000
    for k in range(n):
        ret = 0.0008 + rng.normal(0, 0.004)       # лёгкий тренд вверх, чтобы LONG-сигналы были
        o = px
        c = max(0.01, px * (1 + ret))
        h = max(o, c) * 1.001
        lo = min(o, c) * 0.999
        rows.append((t0 + k * 900_000, o, h, lo, c, 1000 + k))
        px = c
    df = pd.DataFrame(rows, columns=D.REQUIRED_COLS)
    sig = pd.DataFrame([{
        "symbol": "TESTUSDT", "tf": "15m", "stype": "momentum_breakout",
        "strategy_name": "momentum", "i": 300, "ts": int(df["ts"].iloc[300]),
        "side": "LONG", "conf": 0.8, "entry_close": float(df["close"].iloc[300]),
        "reason": "тест", "atr_pct_sig": 0.6,
        **{k: 0.0 for k in F.FEATURE_NAMES},
    }])
    spec = ExitSpec(mode="fixed", fixed_tp_pct=1.0, fixed_sl_pct=1.0,
                    tp_min_pct=0, tp_max_pct=99, sl_min_pct=0, sl_max_pct=99)
    trades = S.resolve_trades(sig, df, spec, CostModel(fee_bps=0, slippage_bps=0),
                              tf="15m", group_by="stype")
    assert len(trades) == 1
    row = trades.iloc[0]
    assert row["entry"] == pytest.approx(float(df["open"].iloc[301]))
    assert row["i_signal"] == 300 and row["ts_signal"] == int(df["ts"].iloc[300])


# --------------------------------------------------------------------- #
# 3-4. Барьеры
# --------------------------------------------------------------------- #
def test_conservative_bar_resolution():
    """Один бар, в котором задеты и TP, и SL → консервативно считаем SL."""
    close = np.array([100.0, 100.5])
    high = np.array([100.0, 103.0])
    low = np.array([100.0, 97.0])
    spec = ExitSpec(mode="fixed")
    res = barriers.simulate_trade(high, low, close, 0, 100.0, "LONG",
                                  102.0, 98.0, spec, CostModel(), max_hold_bars=2)
    assert res.outcome == "SL"
    assert res.pnl_net_pct < 0


def test_tp_and_sl_prices_long_short():
    tp, sl = barriers.clamp_targets(100.0, "LONG", 2.0, 1.0)
    assert (tp, sl) == pytest.approx((102.0, 99.0))
    tp, sl = barriers.clamp_targets(100.0, "SHORT", 2.0, 1.0)
    assert (tp, sl) == pytest.approx((98.0, 101.0))


def test_timeout_closes_at_close_with_costs():
    close = np.array([100.0, 100.2, 100.4])
    high = np.array([100.1, 100.3, 100.5])
    low = np.array([99.9, 100.1, 100.3])
    spec = ExitSpec(mode="fixed")
    costs = CostModel(fee_bps=10, slippage_bps=0)
    res = barriers.simulate_trade(high, low, close, 0, 100.0, "LONG", 105.0, 95.0,
                                  spec, costs, max_hold_bars=2)
    assert res.outcome == "TIMEOUT"
    assert res.pnl_net_pct == pytest.approx(res.pnl_gross_pct - costs.total_round_trip_pct(), abs=1e-6)


def test_breakeven_winrate_formula():
    # TP 0.6 / SL 1.8, издержки 0.15% → безубыток ≈ (1.8+0.15)/(0.6+1.8) = 81.25%
    be = barriers.breakeven_winrate(0.6, 1.8, 0.15)
    assert be == pytest.approx(81.25, abs=0.01)
    assert barriers.breakeven_winrate(2.0, 1.0, 0.0) == pytest.approx(33.333, abs=0.01)


def test_breakeven_stop_moves_trade_to_scrap():
    """При включённом breakeven убыток превращается в ~нулевой выход."""
    close = np.array([100.0, 101.0, 100.05, 100.05])
    high = np.array([100.1, 101.2, 100.2, 100.2])
    low = np.array([99.9, 100.9, 99.0, 99.0])
    spec_off = ExitSpec(mode="fixed", max_hold_bars=4)
    spec_on = ExitSpec(mode="fixed", max_hold_bars=4, be_trigger_pct=0.5, be_offset_pct=0.05)
    off = barriers.simulate_trade(high, low, close, 0, 100.0, "LONG", 105.0, 99.0,
                                  spec_off, CostModel(fee_bps=0, slippage_bps=0), 4)
    on = barriers.simulate_trade(high, low, close, 0, 100.0, "LONG", 105.0, 99.0,
                                 spec_on, CostModel(fee_bps=0, slippage_bps=0), 4)
    assert off.outcome in ("SL", "TIMEOUT")
    assert on.outcome in ("BREAKEVEN", "TIMEOUT")
    assert on.pnl_net_pct >= off.pnl_net_pct


# --------------------------------------------------------------------- #
# 5. Метрики
# --------------------------------------------------------------------- #
def test_wilson_interval_known_values():
    lo, hi = wilson_interval(75, 100)
    assert lo == pytest.approx(65.8, abs=0.5)
    assert hi == pytest.approx(82.4, abs=0.6)
    assert wilson_interval(0, 0) == (0.0, 100.0)


def test_stats_after_costs_and_drawdown():
    rows = [{"pnl_net_pct": 1.0, "hold_min": 10, "outcome": "TP"},
            {"pnl_net_pct": -1.0, "hold_min": 20, "outcome": "SL"},
            {"pnl_net_pct": 1.0, "hold_min": 30, "outcome": "TP"},
            {"pnl_net_pct": -1.0, "hold_min": 40, "outcome": "SL"}]
    st = compute_stats(rows)
    assert st.n == 4 and st.winrate == 50.0
    assert st.expectancy == 0.0
    assert st.profit_factor == pytest.approx(1.0)
    assert st.median_hold_min == 25.0
    assert max_drawdown_pct([1, -2, 1]) == 2.0


def test_choose_threshold_prefers_coverage_and_is_honest():
    """Порог должен брать максимальный охват, но не врать при недостижимой цели."""
    probs = np.array([0.9] * 40 + [0.2] * 60)
    pnls = np.array([1.0] * 38 + [-1.0] * 2 + [1.0] * 10 + [-1.0] * 50)
    ch = choose_threshold(probs, pnls, winrate_target=75.0, min_coverage=0.05, min_trades=20)
    assert ch["threshold"] > 0.5 and ch["coverage"] <= 0.45
    hard = choose_threshold(probs, np.zeros(len(probs)) - 1.0, winrate_target=75.0,
                            min_coverage=0.05, min_trades=20)
    assert hard["target_met"] is False and "недостиж" in hard["reason"]


def test_reliability_monotone_shape():
    probs = np.linspace(0.05, 0.95, 500)
    y = (np.random.default_rng(0).random(500) < probs).astype(int)
    rel = reliability(probs, y, bins=10)
    assert len(rel) >= 5
    assert rel[-1]["actual_winrate"] > rel[0]["actual_winrate"]


# --------------------------------------------------------------------- #
# 6. Бандл мета-модели: обучение → портал
# --------------------------------------------------------------------- #
def _toy_trades(n=600, seed=4) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    ts0 = 1_700_000_000_000
    for k in range(n):
        feat = {name: float(rng.normal(0, 1)) for name in F.FEATURE_NAMES}
        feat["conf"] = float(np.clip(rng.normal(0.6, 0.1), 0, 1))
        feat["atr_pct"] = float(abs(rng.normal(0.6, 0.3)) + 0.1)
        feat["side_long"] = float(rng.integers(0, 2))
        # «истинная» зависимость: чем выше conf и ниже ATR, тем чаще плюс
        score = 2.0 * feat["conf"] - 0.8 * feat["atr_pct"] + rng.normal(0, 1)
        win = score > 0.4
        pnl = float(rng.normal(1.0, 0.4)) if win else -float(abs(rng.normal(1.0, 0.3))) - 0.15
        rows.append({"symbol": "X", "tf": "15m", "stype": "s1", "side": "LONG",
                     "ts_entry": ts0 + k * 900_000, "pnl_net_pct": pnl,
                     "outcome": "TP" if win else "SL", "hold_min": 30,
                     "mfe_pct": 0.8, "mae_pct": 0.5, "tp_pct": 1.0, "sl_pct": 1.0,
                     "atr_pct": feat["atr_pct"], **feat})
    return pd.DataFrame(rows)


def test_bundle_roundtrip_and_portal_agreement(tmp_path):
    curated = pytest.importorskip("curated")
    df = _toy_trades()
    train, test = df.iloc[:420], df.iloc[420:]
    bundle, rep = train_bundle(train, test, winrate_target=60.0, min_trades=20)
    path = str(tmp_path / "bundle.json")
    save_bundle(bundle, path)

    por = curated.load_bundle(path)
    assert por["features"] == curated.FEATURE_NAMES == F.FEATURE_NAMES
    assert por["threshold_win"] == pytest.approx(bundle["threshold_win"])

    # одна и та же сделка → одно и то же предсказание в research и в портале
    rec = df.iloc[0].to_dict()
    feats = {k: float(rec.get(k, 0.0) or 0.0) for k in F.FEATURE_NAMES}
    p_res, _ = predict_bundle(bundle, np.array([[feats[k] for k in F.FEATURE_NAMES]]))["win"][0], None
    p_por, ev_por = curated.predict(por, feats)
    assert p_por == pytest.approx(float(p_res), rel=1e-12, abs=1e-12)
    assert isinstance(ev_por, float)
    assert rep["train"]["filtered"]["n"] <= rep["train"]["all"]["n"]


def test_bundle_rejects_broken_files(tmp_path):
    curated = pytest.importorskip("curated")
    df = _toy_trades(200)
    bundle, _ = train_bundle(df.iloc[:150], df.iloc[150:], winrate_target=50.0, min_trades=10)
    path = str(tmp_path / "b.json")
    save_bundle(bundle, path)
    ok = curated.load_bundle(path)
    assert len(ok["features"]) == len(F.FEATURE_NAMES)

    broken = dict(bundle)
    broken["features"] = list(reversed(bundle["features"]))
    with pytest.raises(curated.BundleError):
        curated.validate_bundle(broken)
    bad = dict(bundle)
    bad["version"] = "v0"
    with pytest.raises(curated.BundleError):
        curated.validate_bundle(bad)


def test_apply_bundle_filters_offline():
    df = _toy_trades(300)
    bundle, _ = train_bundle(df.iloc[:200], df.iloc[200:], winrate_target=55.0, min_trades=10)
    filtered = apply_bundle(df, bundle)
    assert len(filtered) <= len(df)
    assert "meta_p_win" in filtered.columns
    if not filtered.empty:
        assert filtered["meta_p_win"].min() >= bundle["threshold_win"] - 1e-9


# --------------------------------------------------------------------- #
# 7. Движок: гейты, кулдаун, один вход на группу
# --------------------------------------------------------------------- #
def test_gates_and_cooldown_and_single_position():
    df = _rand_candles(n=200, seed=9)
    base = {"symbol": "T", "tf": "15m", "stype": "s1", "strategy_name": "s1",
            "side": "LONG", "conf": 0.8, "reason": "t", "atr_pct_sig": 0.6,
            **{k: 0.0 for k in F.FEATURE_NAMES}}
    rows = []
    for i in (120, 121, 122, 180):
        rows.append({**base, "i": i, "ts": int(df["ts"].iloc[i]),
                     "entry_close": float(df["close"].iloc[i])})
    sig = pd.DataFrame(rows)
    spec = ExitSpec(mode="fixed", fixed_tp_pct=5.0, fixed_sl_pct=5.0,
                    tp_min_pct=0, tp_max_pct=99, sl_min_pct=0, sl_max_pct=99)
    trades = S.resolve_trades(sig, df, spec, CostModel(), tf="15m",
                              cooldown_bars=2, group_by="stype", one_position=True)
    # 120 и 121 — кулдаун, 122 — внутри удерживаемой позиции, 180 — новая сделка
    assert len(trades) == 2
    assert list(trades["i_signal"]) == [120, 180]

    # фильтры исключают входы (оффлайн, путь цены не меняется)
    trades_df = trades.assign(hour=12, atr_pct=0.6, vol_ratio_seasonal=1.2)
    assert len(E.apply_gates(trades_df, {"hours_block": [12]})) == 0
    assert len(E.apply_gates(trades_df, {"atr_block": "0.5-0.7"})) == 0
    assert len(E.apply_gates(trades_df, {"stype_blocklist": "s1"})) == 0
    assert len(E.apply_gates(trades_df, {"min_conf": 0.9})) == 0
    assert len(E.apply_gates(trades_df, {"vol_season_block": "1.0-2.0"})) == 0


# --------------------------------------------------------------------- #
# 8. Сквозная проверка на синтетике (механика, не рынок)
# --------------------------------------------------------------------- #
def test_pipeline_smoke_on_synthetic(tmp_path):
    data_dir = str(tmp_path / "d")
    out_dir = str(tmp_path / "o")
    D.save_synthetic(data_dir, ["AAAUSDT"], ["15m"], bars=600, seed=2)
    cfg = ResearchConfig(symbols=["AAAUSDT"], timeframes=["15m"], data_dir=data_dir,
                         out_dir=out_dir, strategies=["rsi_reversal", "momentum_breakout"],
                         jobs=1)
    cfg.constraints = Constraints(min_trades=3, winrate_target=0.0, min_trades_per_group=2)
    pairs = E.discover(data_dir, cfg.symbols, cfg.timeframes)
    assert len(pairs) == 1
    res = E.extract_all(cfg, pairs, jobs=1)
    assert res[0]["signals"] >= 0
    sig = E.load_signals(out_dir)
    assert not sig.empty
    specs = [(f"tp{tp}_sl{sl}", ExitSpec(mode="fixed", fixed_tp_pct=tp, fixed_sl_pct=sl,
                                         tp_min_pct=0, tp_max_pct=99,
                                         sl_min_pct=0, sl_max_pct=99))
             for tp, sl in ((0.6, 1.0), (1.0, 1.0))]
    trades = E.resolve_all(cfg, specs, signals=sig, pairs=pairs)
    assert not trades.empty and trades["variant"].nunique() == 2
    st = compute_stats(trades.to_dict("records"))
    assert st.n == len(trades)
    # отчёт и рекомендация должны строиться без ошибок
    from tvresearch import search as SE, report as R
    variants = SE.best_variant(trades, cfg)
    assert not variants.empty
    rec = SE.recommend(cfg, trades, trials=10)
    assert rec["verdicts"]
    md = R.render_markdown(cfg=cfg, quality=E.quality_report(pairs), trades=trades,
                           variants=variants, verdicts=rec["verdicts"],
                           walk_forward={"folds": []}, data_note="СИНТЕТИКА")
    assert "Отчёт исследовательского контура" in md
    paths = R.write_outputs(out_dir, md, {"x": 1})
    assert os.path.exists(paths["markdown"])


def test_volatility_formula_and_diagnostics():
    df = _toy_trades(400)
    from tvresearch import search as SE
    f = SE.volatility_formula(df)
    assert f["tp_atr_mult_median"] > 0 and f["sl_atr_mult_median"] > 0
    d = SE.target_diagnostics(df)
    assert 0 <= d["overall"]["miss_share_pct"] <= 100
    assert d["by_atr"]


def test_download_spec_exists_and_documented():
    """Инструкция для выгрузки данных должна быть на месте (её читает пользователь)."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("DOWNLOAD_SPEC.md", "ARCHITECTURE.md", "README.md"):
        assert os.path.exists(os.path.join(here, name)), f"нет research/{name}"
