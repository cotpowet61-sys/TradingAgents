#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Исследовательский контур TradeVision — командная строка.

Быстрый старт (после того как данные скачаны в research/data):

    python research/run.py quality                 # проверить качество выгрузки
    python research/run.py extract                 # прогнать 23 стратегии по всем парам
    python research/run.py sweep                   # перебрать TP/SL/выход и отфильтровать
    python research/run.py all --mirror            # всё сразу + проверка движком портала
    python research/run.py verify                  # финальная приёмка найденных настроек

Самопроверка без биржи (синтетика — только механика, не выводы о рынке):

    python research/run.py selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tvresearch import data as D           # noqa: E402
from tvresearch import engine as E         # noqa: E402
from tvresearch import report as R         # noqa: E402
from tvresearch import search as SE        # noqa: E402
from tvresearch import signals as SG       # noqa: E402
from tvresearch.config import ResearchConfig, Constraints, CostModel   # noqa: E402
from tvresearch.curated import train_bundle, save_bundle, apply_bundle  # noqa: E402
from tvresearch.metrics import compute_stats               # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------- #
# Общие ключи
# --------------------------------------------------------------------- #
def add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--data", default=os.path.join(HERE, "data"), help="каталог с CSV")
    p.add_argument("--out", default=os.path.join(HERE, "artifacts"), help="каталог артефактов")
    p.add_argument("--symbols", default="", help="монеты через запятую (пусто = все в каталоге)")
    p.add_argument("--timeframes", default="15m,1h", help="таймфреймы через запятую")
    p.add_argument("--strategies", default="", help="стратегии через запятую (пусто = все 23)")
    p.add_argument("--jobs", type=int, default=0, help="процессов (0 = по числу ядер)")
    p.add_argument("--days", type=int, default=180, help="сколько дней истории (для download)")
    p.add_argument("--fee-bps", type=float, default=5.5, help="комиссия за сторону, б.п.")
    p.add_argument("--slip-bps", type=float, default=2.0, help="проскальзывание за сторону, б.п.")
    p.add_argument("--winrate-target", type=float, default=75.0, help="целевой винрейт, %%")
    p.add_argument("--tp-min", type=float, default=0.6, help="минимальный TP, %% (пол)")
    p.add_argument("--min-trades", type=int, default=40, help="минимум сделок в выборке")
    p.add_argument("--hold-hours", type=float, default=4.0, help="макс. удержание, часы")
    p.add_argument("--seed", type=int, default=7)


def build_cfg(a) -> ResearchConfig:
    cfg = ResearchConfig(
        symbols=[s.strip().upper() for s in a.symbols.split(",") if s.strip()],
        timeframes=[t.strip() for t in a.timeframes.split(",") if t.strip()],
        days=a.days,
        strategies=[s.strip() for s in a.strategies.split(",") if s.strip()] or None,
        data_dir=a.data, out_dir=a.out, seed=a.seed, jobs=a.jobs,
    )
    cfg.costs = CostModel(fee_bps=a.fee_bps, slippage_bps=a.slip_bps)
    cfg.constraints = Constraints(winrate_target=a.winrate_target, tp_min_pct=a.tp_min,
                                  min_trades=a.min_trades)
    cfg.exit_spec.max_hold_hours = a.hold_hours
    return cfg


def _pairs(cfg: ResearchConfig):
    pairs = E.discover(cfg.data_dir, cfg.symbols, cfg.timeframes)
    if not pairs:
        print(f"✗ в {cfg.data_dir} нет файлов вида <СИМВОЛ>_<ТФ>.csv.\n"
              f"  Что скачивать — research/DOWNLOAD_SPEC.md, "
              f"можно и командой: python research/run.py download")
        sys.exit(2)
    print(f"• пар (монета × ТФ): {len(pairs)}")
    return pairs


# --------------------------------------------------------------------- #
# Команды
# --------------------------------------------------------------------- #
def cmd_quality(a) -> int:
    cfg = build_cfg(a)
    pairs = _pairs(cfg)
    rows = E.quality_report(pairs)
    bad = [r for r in rows if r.get("error") or (r.get("coverage_pct") or 0) < 90]
    for r in rows:
        if r.get("error"):
            print(f"  ✗ {r['symbol']} {r['tf']}: {r['error']}")
        else:
            flag = "✓" if (r.get("coverage_pct") or 0) >= 95 else "!"
            print(f"  {flag} {r['symbol']:10s} {r['tf']:4s} {r['bars']:6d} баров, "
                  f"покрытие {r['coverage_pct']:5.1f}%, дырки {r['gaps']}, дубли {r['dupes']}")
    print(f"\nИтого: {len(rows)} пар, проблемных {len(bad)}")
    return 0


def cmd_download(a) -> int:
    cfg = build_cfg(a)
    if not cfg.symbols:
        print("✗ укажите --symbols (например --symbols BTCUSDT,ETHUSDT,...)")
        return 2
    print(f"• качаю Bybit: {len(cfg.symbols)} монет × {cfg.timeframes} × {cfg.days} дней")
    qs = D.download_many(cfg.symbols, cfg.timeframes, cfg.days, cfg.data_dir,
                         out_manifest=os.path.join(cfg.out_dir, "data_manifest.json"))
    ok = [q for q in qs if q.bars]
    print(f"\nСкачано пар: {len(ok)} из {len(cfg.symbols) * len(cfg.timeframes)}")
    print("Дальше: python research/run.py all")
    return 0 if ok else 1


def cmd_extract(a) -> int:
    cfg = build_cfg(a)
    pairs = _pairs(cfg)
    t0 = time.time()
    res = E.extract_all(cfg, pairs, jobs=a.jobs)
    bad = [r for r in res if r["error"]]
    sigs = sum(r["signals"] for r in res)
    print(f"\nГотово за {time.time() - t0:.0f}с: {sigs} сигналов, ошибок {len(bad)}")
    for r in bad[:10]:
        print(f"  ✗ {r['symbol']} {r['tf']}: {r['error']}")
    return 0 if not bad else 1


def cmd_sweep(a) -> int:
    cfg = build_cfg(a)
    pairs = _pairs(cfg)
    signals = E.load_signals(cfg.out_dir, symbols=cfg.symbols, tfs=cfg.timeframes)
    print(f"• сигналов в кэше: {len(signals)}")
    specs = SE.default_specs(cfg)
    if a.grid:
        tp_values = [cfg.constraints.tp_min_pct, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0]
        sl_values = [0.4, 0.6, 0.8, 1.0, 1.2, 1.5, 2.0]
        specs += SE.spec_grid(tp_values, sl_values, be_triggers=(0.0,), trail_starts=(0.0,))
        specs = list({lab: (lab, sp) for lab, sp in specs}.values())
    print(f"• вариантов выхода: {len(specs)}")
    t0 = time.time()
    trades = E.resolve_all(cfg, specs, signals=signals, pairs=pairs)
    print(f"• сделок: {len(trades)} за {time.time() - t0:.0f}с")
    variants = SE.best_variant(trades, cfg)
    print("\nТоп вариантов (train+test целиком, проверка — в отчёте):")
    for _, r in variants.head(8).iterrows():
        flag = "✓" if r["ok"] else " "
        print(f"  {flag} {r['variant']:22s} n={int(r['n']):6d} WR={r['winrate']:5.1f}% "
              f"(ниж. {r['wr_lo']:5.1f}%) exp={r['expectancy']:+.3f}% "
              f"PF={r['profit_factor']} → {r['why']}")
    return 0


def _train_meta_and_report(cfg: ResearchConfig, trades, variants, wf, bundle_path_out: str,
                           trials: int, note: str = ""):
    """Общая часть для `sweep --report` и `all`."""
    rec = SE.recommend(cfg, trades, trials=trials, seed=cfg.seed)
    verdicts = rec.get("verdicts") or []
    best_label = variants.iloc[0]["variant"] if not variants.empty else None
    bundle = breport = None
    bundle_path = None
    if best_label:
        sub = trades[trades["variant"] == best_label].sort_values("ts_entry").reset_index(drop=True)
        cut = int(len(sub) * 0.7)
        train, test = sub.iloc[:cut], sub.iloc[cut:]
        if len(train) >= cfg.constraints.min_trades:
            bundle, breport = train_bundle(
                train, test, winrate_target=cfg.constraints.winrate_target,
                min_coverage=0.05, min_trades=cfg.constraints.min_trades_per_group,
                notes=f"вариант {best_label}; данные: {cfg.data_dir}")
            bundle_path = bundle_path_out
    md = R.render_markdown(cfg=cfg, quality=E.quality_report(_pairs(cfg)), trades=trades,
                           variants=variants, verdicts=verdicts, walk_forward=wf,
                           bundle_report=breport, bundle_path=bundle_path, data_note=note)
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": cfg.to_dict(),
        "variants_top": variants.head(20).to_dict("records") if not variants.empty else [],
        "recommendation": rec,
        "walk_forward": wf,
        "meta_model": {k: v for k, v in (breport or {}).items() if k != "coefficients"},
        "env_snippet": R.env_snippet(verdicts[0], bundle_path=bundle_path) if verdicts else "",
    }
    paths = R.write_outputs(cfg.out_dir, md, payload, bundle=bundle)
    return paths, rec, verdicts, breport


def cmd_all(a) -> int:
    cfg = build_cfg(a)
    pairs = _pairs(cfg)
    print("\n[1/6] Качество данных")
    quality = E.quality_report(pairs)
    broken = [q for q in quality if q.get("error")]
    if broken:
        print(f"  ! проблемных пар: {len(broken)} (в анализ пойдут только валидные)")
    print("\n[2/6] Извлечение сигналов (23 стратегии × пары)")
    E.extract_all(cfg, pairs, jobs=a.jobs)
    print("\n[3/6] Перебор вариантов выхода")
    signals = E.load_signals(cfg.out_dir, symbols=cfg.symbols, tfs=cfg.timeframes)
    print(f"  сигналов: {len(signals)}")
    specs = SE.default_specs(cfg)
    if a.grid:
        specs += SE.spec_grid([cfg.constraints.tp_min_pct, 0.8, 1.0, 1.2, 1.5, 2.0],
                              [0.4, 0.6, 0.8, 1.0, 1.2, 1.5, 2.0],
                              be_triggers=(0.0,), trail_starts=(0.0,))
        specs = list({lab: (lab, sp) for lab, sp in specs}.values())
    trades = E.resolve_all(cfg, specs, signals=signals, pairs=pairs)
    print(f"  сделок: {len(trades)}")
    if trades.empty:
        print("✗ сделок нет — проверьте порог conf и данные")
        return 1
    print("\n[4/6] Walk-forward (отбор на train, проверка вне выборки)")
    wf = SE.walk_forward(trades, cfg, folds=a.folds, trials=a.trials, seed=cfg.seed)
    for f in wf.get("folds") or []:
        print(f"  фолд {f['fold']}: train n={f['train_n']} WR={f['train_wr']}% → "
              f"test n={f['test_n']} WR={f['test_wr']}% (ниж. {f['test_wlo']}%) "
              f"exp={f['test_exp']:+}%")
    print("\n[5/6] Отбор стратегий + фильтры + мета-модель")
    variants = SE.best_variant(trades, cfg)
    paths, rec, verdicts, breport = _train_meta_and_report(
        cfg, trades, variants, wf, os.path.join(cfg.out_dir, "bundle.json"), a.trials)
    for v in verdicts:
        t = v.get("test") or {}
        print(f"  {v['group']:>4s}: {v['variant']:20s} test n={t.get('n')} "
              f"WR={t.get('winrate')}% exp={t.get('expectancy')}% "
              f"{'✓ цель' if v.get('passes_target') else '✗ ' + str(v.get('why'))}")
    mirror_stats = None
    if a.mirror:
        print("\n[6/6] Зеркальный прогон движка портала (медленно)")
        mirror = E.mirror_run(cfg, pairs, jobs=a.jobs)
        if not mirror.empty:
            mirror_stats = compute_stats(mirror.to_dict("records")).as_dict()
            print(f"  портал как есть: n={mirror_stats['n']} WR={mirror_stats['winrate']}% "
                  f"exp={mirror_stats['expectancy']:+.3f}%")
            env = _env_from_verdict(verdicts[0], paths.get("bundle")) if verdicts else {}
            if env:
                for k, v in env.items():
                    os.environ[k] = str(v)
                print("  повтор с найденными настройками (переменные окружения)...")
                mirror2 = E.mirror_run(cfg, pairs, jobs=a.jobs, variant="tuned")
                if not mirror2.empty:
                    st2 = compute_stats(mirror2.to_dict("records")).as_dict()
                    print(f"  портал с настройками: n={st2['n']} WR={st2['winrate']}% "
                          f"exp={st2['expectancy']:+.3f}% PF={st2['profit_factor']}")
                    payload = json.load(open(paths["json"], encoding="utf-8"))
                    payload["mirror"] = {"baseline": mirror_stats, "tuned": st2,
                                         "env_applied": env}
                    json.dump(payload, open(paths["json"], "w", encoding="utf-8"),
                              ensure_ascii=False, indent=2, default=str)
    else:
        print("\n[6/6] зеркальный прогон пропущен (--mirror чтобы включить)")
    print("\n=== Готово ===")
    print(f"Отчёт: {paths['markdown']}")
    print(f"Машиночитаемые настройки: {paths['json']}")
    if paths.get("bundle"):
        print(f"Бандл мета-модели: {paths['bundle']}")
    if verdicts:
        print("\nСтроки для portal/.env:")
        print(R.env_snippet(verdicts[0], bundle_path=paths.get("bundle")))
    return 0


def _env_from_verdict(verdict: dict, bundle_path=None) -> dict:
    """Кнобы портала из вердикта (имена совпадают с portal_config.py)."""
    gates = verdict.get("gates") or {}
    env: dict = {}
    if verdict.get("tp_pct"):
        env["MANUAL_TP_PCT"] = round(float(verdict["tp_pct"]), 3)
        env["MANUAL_SL_PCT"] = round(float(verdict["sl_pct"]), 3)
        env["RR_BINDING"] = "false"
    if gates.get("min_conf"):
        env["MIN_CONFIDENCE"] = gates["min_conf"]
    if gates.get("stype_blocklist"):
        env["STYPE_BLOCKLIST"] = gates["stype_blocklist"]
    if gates.get("hours_block"):
        env["HOURS_BLOCK"] = ",".join(str(h) for h in gates["hours_block"])
    if gates.get("atr_block"):
        env["ATR_PCT_BLOCK"] = gates["atr_block"]
    if gates.get("vol_season_block"):
        env["VOL_SEASON_BLOCK"] = gates["vol_season_block"]
    if bundle_path:
        env["CURATED_BUNDLE"] = bundle_path
        env["USE_CURATED"] = "true"
    return env


def cmd_verify(a) -> int:
    """Финальная приёмка: прогон движка портала с найденными настройками."""
    cfg = build_cfg(a)
    pairs = _pairs(cfg)
    rec_path = os.path.join(cfg.out_dir, "recommendation.json")
    if not os.path.exists(rec_path):
        print("✗ нет recommendation.json — сначала `python research/run.py all`")
        return 2
    payload = json.load(open(rec_path, encoding="utf-8"))
    verdicts = (payload.get("recommendation") or {}).get("verdicts") or []
    if not verdicts:
        print("✗ в recommendation.json нет вердиктов")
        return 2
    bundle_path = os.path.join(cfg.out_dir, "bundle.json")
    env = _env_from_verdict(verdicts[0], bundle_path if os.path.exists(bundle_path) else None)
    print("• применяю настройки:", json.dumps(env, ensure_ascii=False))
    for k, v in env.items():
        os.environ[k] = str(v)
    print("• зеркальный прогон (baseline)...")
    for k in list(env):
        os.environ.pop(k, None)
    base = E.mirror_run(cfg, pairs, jobs=a.jobs, variant="baseline")
    for k, v in env.items():
        os.environ[k] = str(v)
    print("• зеркальный прогон (с настройками)...")
    tuned = E.mirror_run(cfg, pairs, jobs=a.jobs, variant="tuned")
    out = {}
    for name, df in (("baseline", base), ("tuned", tuned)):
        st = compute_stats(df.to_dict("records")) if not df.empty else None
        out[name] = st.as_dict() if st else {"n": 0}
        if st:
            print(f"  {name:9s}: n={st.n} WR={st.winrate}% (ниж. {st.wr_lo}%) "
                  f"exp={st.expectancy:+.3f}% PF={st.profit_factor} DD={st.max_dd_pp}пп")
    if not base.empty and not tuned.empty:
        bundle_p = bundle_path if os.path.exists(bundle_path) else None
        if bundle_p:
            from tvresearch.curated import load_bundle
            b = load_bundle(bundle_p)
            for name, df in (("baseline", base), ("tuned", tuned)):
                try:
                    f = apply_bundle(df, b)
                    st = compute_stats(f.to_dict("records")) if not f.empty else None
                    print(f"  {name:9s}+ИИ-фильтр: " +
                          (f"n={st.n} WR={st.winrate}% exp={st.expectancy:+.3f}%" if st else "нет сделок"))
                    out[f"{name}_curated"] = st.as_dict() if st else {"n": 0}
                except Exception as e:      # noqa: BLE001
                    print(f"  ! ИИ-фильтр не применён: {e}")
    json.dump(out, open(os.path.join(cfg.out_dir, "verify.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, default=str)
    print(f"\nРезультат: {os.path.join(cfg.out_dir, 'verify.json')}")
    return 0


def cmd_selftest(a) -> int:
    """Прогон всей цепочки на синтетике: проверяет механику, а не рынок."""
    a.data = os.path.join(HERE, "artifacts", "selftest_data")
    a.out = os.path.join(HERE, "artifacts", "selftest")
    a.symbols = a.symbols or "AAAUSDT,BBBUSDT"
    a.timeframes = a.timeframes or "15m"
    a.strategies = a.strategies or "rsi_reversal,momentum_breakout,supertrend,mean_reversion,rsi_macd_combo"
    a.jobs = a.jobs or 2
    cfg = build_cfg(a)
    print("• синтетические данные (только для проверки механики)")
    D.save_synthetic(cfg.data_dir, ["AAAUSDT", "BBBUSDT"], ["15m"], bars=a.bars, seed=cfg.seed)
    for fn in os.listdir(cfg.data_dir):
        print("   ", fn)
    pairs = _pairs(cfg)
    print("• извлечение сигналов")
    E.extract_all(cfg, pairs, jobs=a.jobs)
    signals = E.load_signals(cfg.out_dir)
    print(f"• сигналов: {len(signals)}")
    specs = SE.spec_grid([0.6, 1.0, 1.5], [0.6, 1.0, 1.5], be_triggers=(0.0, 0.5),
                         trail_starts=(0.0,))
    trades = E.resolve_all(cfg, specs, signals=signals, pairs=pairs)
    print(f"• сделок: {len(trades)}")
    if trades.empty:
        print("✗ сделок нет — синтетика не дала сигналов, это подозрительно")
        return 1
    variants = SE.best_variant(trades, cfg)
    print(variants.head(6).to_string(index=False))
    wf = SE.walk_forward(trades, cfg, folds=3, trials=40, seed=cfg.seed)
    print("\nwalk-forward:")
    for f in wf.get("folds") or []:
        print("  ", f)
    paths, rec, verdicts, breport = _train_meta_and_report(
        cfg, trades, variants, wf, os.path.join(cfg.out_dir, "bundle.json"), trials=60,
        note="ПРОГОН НА СИНТЕТИКЕ: проверка механики контура, выводы о рынке делать нельзя.")
    print("\nvariant diagnostics:")
    print(json.dumps(SE.target_diagnostics(trades), ensure_ascii=False, indent=2)[:1500])
    print("\nvolatility formula:", SE.volatility_formula(trades))
    print("\nмета-модель (train→test):")
    for part in ("train", "test"):
        for kind, s in (breport or {}).get(part, {}).items():
            print(f"   {part:5s} {kind:9s} n={s.get('n')} WR={s.get('winrate')}% "
                  f"exp={s.get('expectancy')}%")
    print(f"\nотчёт: {paths['markdown']}")
    print(f"бандл: {paths.get('bundle')}")
    return 0


# --------------------------------------------------------------------- #
# Разбор аргументов
# --------------------------------------------------------------------- #
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Исследовательский контур TradeVision")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_q = sub.add_parser("quality", help="проверить качество данных")
    add_common(p_q); p_q.set_defaults(func=cmd_quality)

    p_d = sub.add_parser("download", help="скачать свечи с Bybit (публичный API)")
    add_common(p_d); p_d.set_defaults(func=cmd_download)

    p_e = sub.add_parser("extract", help="прогнать стратегии и кэшировать сигналы")
    add_common(p_e); p_e.set_defaults(func=cmd_extract)

    p_s = sub.add_parser("sweep", help="перебрать TP/SL/выход")
    add_common(p_s)
    p_s.add_argument("--grid", action="store_true", help="широкая сетка TP×SL")
    p_s.set_defaults(func=cmd_sweep)

    p_a = sub.add_parser("all", help="полный прогон: данные → сигналы → перебор → отчёт")
    add_common(p_a)
    p_a.add_argument("--grid", action="store_true", help="широкая сетка TP×SL")
    p_a.add_argument("--mirror", action="store_true", help="плюс зеркальный прогон портала")
    p_a.add_argument("--trials", type=int, default=200, help="итераций поиска фильтров")
    p_a.add_argument("--folds", type=int, default=4, help="фолдов walk-forward")
    p_a.set_defaults(func=cmd_all)

    p_v = sub.add_parser("verify", help="приёмка найденных настроек движком портала")
    add_common(p_v); p_v.set_defaults(func=cmd_verify)

    p_t = sub.add_parser("selftest", help="самопроверка на синтетике (без биржи)")
    add_common(p_t)
    p_t.add_argument("--bars", type=int, default=2500)
    p_t.set_defaults(func=cmd_selftest)

    a = p.parse_args(argv)
    t0 = time.time()
    rc = a.func(a)
    print(f"\nвремя: {time.time() - t0:.0f}с")
    return rc


if __name__ == "__main__":
    sys.exit(main())
