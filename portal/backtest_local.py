# -*- coding: utf-8 -*-
"""
Локальный бэктест-харнесс TradeVision: «как если бы страница сканера была открыта».

Запуск на машине с доступом к Bybit (через venv портала):
    cd portal
    python backtest_local.py --syms BTCUSDT,ETHUSDT,SOLUSDT --days 7 --tfs 5m,1m

Что делает:
  1. Тянет РЕАЛЬНЫЕ свечи Bybit (synthetic-фолбэк = прогон не считается, как и в лайве).
  2. Прогоняет зеркальную лайв-логику (авто-селектор, min_conf, приоритет целей
     manual→ATR→strategy→default, кулдаун 30 мин, разворот только при +0.10 conf,
     одна сделка на (монета,TF)).
  3. Считает разрезы (стратегия/сторона/монета/час/ratio-бакеты) — где теряем.
  4. Перебирает «ручки» качества (conf floor, сезонный объём, ATR-множители,
     стоп-лист стратегий) и печатает таблицу вариантов с winrate/PF.
  5. Пишет JSON-отчёт для отправки разработчику и печатает рекомендуемые
     строки .env (никакой магии: только то, что замерено на ваших данных).

Честность: winrate ≥70 — цель, а не обещание. Если её даёт только узкий сегмент
(напр. conf≥0.70), отчёт покажет и общий winrate, и winrate сегмента.
"""
import argparse
import asyncio
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

from portal_config import settings            # noqa: E402
import signal_backtest as sb                   # noqa: E402
from strategies.registry import build_strategy  # noqa: E402
from strategies.selector import get_selector    # noqa: E402
from strategies import indicators as ta         # noqa: E402

COOLDOWN_S = 1800      # зеркало SIGNAL_COOLDOWN_SEC
FLIP_MARGIN = 0.10     # зеркало FLIP_CONF_MARGIN

DEFAULT_SYMS = ("BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,"
                "LINKUSDT,AVAXUSDT,NEARUSDT,ARBUSDT,OPUSDT,ATOMUSDT")

# Какие глобальные ручки варианты трогают — снапшотим и откатываем.
KNOBS = ("MIN_CONFIDENCE", "RR_BINDING", "ATR_TP_MULT", "ATR_SL_MULT",
         "VOL_SEASON_GATE", "VOL_SEASON_MIN", "VOL_SEASON_STYPES", "STYPE_BLOCKLIST")


# --------------------------------------------------------------------------- #
# Зеркальный симулятор (как live_engine.scan_once + _gate)
# --------------------------------------------------------------------------- #
def simulate(symbol: str, tf: str, candles) -> list:
    sel = get_selector()
    min_conf = settings.MIN_CONFIDENCE
    tf_min = sb._TF_MIN.get(tf, 5)
    hold_bars = max(1, int((settings.MANUAL_HOLD_MIN or sb.DEFAULT_HOLD_MIN) / tf_min))
    trades = []
    last_type, last_pick = None, -10 ** 9
    open_t = None
    last_side, last_conf, last_sig_ts = None, 0.0, 0
    vol_stypes = {x.strip() for x in (settings.VOL_SEASON_STYPES or "").split(",") if x.strip()}
    blocklist = {x.strip() for x in (settings.STYPE_BLOCKLIST or "").split(",") if x.strip()}

    n = len(candles)
    for i in range(max(sb.WARMUP, 60), n - 1):
        c = candles[i]
        if open_t is not None:
            e, side = open_t["entry"], open_t["side"]
            hit, exit_px, reason = False, None, None
            if side == "LONG":
                if c.low <= open_t["sl"]:
                    hit, exit_px, reason = True, open_t["sl"], "SL"
                elif c.high >= open_t["tp"]:
                    hit, exit_px, reason = True, open_t["tp"], "TP"
            else:
                if c.high >= open_t["sl"]:
                    hit, exit_px, reason = True, open_t["sl"], "SL"
                elif c.low <= open_t["tp"]:
                    hit, exit_px, reason = True, open_t["tp"], "TP"
            if not hit and i - open_t["i"] >= hold_bars:
                hit, exit_px, reason = True, c.close, "timeout"
            if hit:
                pnl = (exit_px - e) / e * 100
                if side == "SHORT":
                    pnl = -pnl
                open_t.update({"exit": exit_px, "pnl_pct": round(pnl, 3), "reason": reason,
                               "duration_min": round((i - open_t["i"]) * tf_min)})
                trades.append(open_t)
                open_t = None
            continue

        window = candles[max(0, i - sb.WINDOW + 1):i + 1]
        if i - last_pick >= sb.REPICK_EVERY or last_type is None:
            if settings.SIGNAL_STRATEGY not in (None, "", "auto"):
                last_type = settings.SIGNAL_STRATEGY
            else:
                try:
                    last_type, _cand, _src = sel.auto_pick(symbol, tf, window)
                except Exception:
                    last_type = "momentum_breakout"
            last_pick = i
        strat = build_strategy(last_type)
        if not strat:
            continue
        try:
            sig = strat.generate(window, len(window) - 1)
        except Exception:
            continue
        if not sig or sig.side.value == "NEUTRAL":
            continue
        if sig.confidence < min_conf:
            continue
        side_v = sig.side.value
        # стоп-лист стратегий (зеркало лайва)
        if last_type in blocklist or f"{last_type}:{symbol}" in blocklist:
            continue
        entry = sig.entry or c.close
        try:
            summ = ta.compute_feature_summary(window) or {}
        except Exception:
            summ = {}
        # сезонный объёмный фильтр (зеркало лайва)
        if settings.VOL_SEASON_GATE:
            vrs = summ.get("vol_ratio_seasonal")
            if vrs is not None and vrs < float(settings.VOL_SEASON_MIN) \
                    and (not vol_stypes or last_type in vol_stypes):
                continue
        # live-антиспам: 30 мин та же сторона; разворот — только при +0.10 conf.
        # (харнесс применяет эти правила и ПОСЛЕ закрытия сделки, т.е. чуть
        #  строже, чем лайв — занижает количество сделок, а не завышает winrate)
        if last_side == side_v and (c.ts - last_sig_ts) / 1000 < COOLDOWN_S:
            continue
        if last_side and last_side != side_v and sig.confidence < last_conf + FLIP_MARGIN:
            continue
        tg = sb._targets(entry, side_v, summ, sig.tp, sig.sl)
        last_side, last_conf, last_sig_ts = side_v, sig.confidence, c.ts
        open_t = {"symbol": symbol, "tf": tf, "side": side_v,
                  "strategy": getattr(strat, "name", last_type), "stype": last_type,
                  "i": i, "ts": c.ts, "entry": entry, "tp": tg["tp"], "sl": tg["sl"],
                  "mode": tg["mode"], "conf": round(sig.confidence, 3),
                  "rsi": summ.get("rsi"), "atr_pct": summ.get("atr_pct"),
                  "vol_ratio": summ.get("volume_ratio"),
                  "vol_ratio_seasonal": summ.get("vol_ratio_seasonal"),
                  "hour": time.strftime("%H", time.gmtime(c.ts / 1000))}
    return trades


# --------------------------------------------------------------------------- #
# Метрики / разрезы
# --------------------------------------------------------------------------- #
def stat(rows: list) -> dict:
    if not rows:
        return {"n": 0, "winrate": None, "avg": None, "pf": None}
    wins = [r for r in rows if r["pnl_pct"] > 0]
    gw = sum(r["pnl_pct"] for r in wins)
    gl = -sum(r["pnl_pct"] for r in rows if r["pnl_pct"] <= 0)
    return {"n": len(rows),
            "winrate": round(100 * len(wins) / len(rows), 1),
            "avg": round(sum(r["pnl_pct"] for r in rows) / len(rows), 3),
            "pf": round(gw / gl, 2) if gl > 0 else (99.0 if gw > 0 else None)}


def cuts(rows: list):
    def by(fn, title):
        agg = {}
        for r in rows:
            agg.setdefault(fn(r), []).append(r)
        table = [(k, stat(v)) for k, v in agg.items()]
        table.sort(key=lambda kv: (-(kv[1]["n"] or 0)))
        return title, table

    def conf_bucket(r):
        c = r.get("conf") or 0
        return f"{int(c * 10) * 0.1:.1f}-{(int(c * 10) + 1) * 0.1:.1f}"

    def vrs_bucket(r):
        v = r.get("vol_ratio_seasonal")
        if v is None:
            return "n/a"
        for edge in (0.7, 1.0, 1.3, 1.6, 2.0, 3.0):
            if v < edge:
                return f"<{edge}"
        return ">=3.0"

    def atr_bucket(r):
        a = r.get("atr_pct") or 0
        for edge in (0.3, 0.6, 1.0, 1.5, 2.5):
            if a < edge:
                return f"<{edge}"
        return ">=2.5"

    return [by(lambda r: r["stype"], "by_strategy"),
            by(lambda r: r["side"], "by_side"),
            by(lambda r: r["symbol"], "by_symbol"),
            by(lambda r: r["tf"], "by_tf"),
            by(lambda r: r["hour"], "by_hour_utc"),
            by(conf_bucket, "by_conf"),
            by(vrs_bucket, "by_vol_season_ratio"),
            by(atr_bucket, "by_atr_pct")]


def worst_strategies(rows: list, min_n: int = 6, max_wr: float = 40.0) -> list:
    agg = {}
    for r in rows:
        agg.setdefault(r["stype"], []).append(r)
    out = []
    for k, v in agg.items():
        s = stat(v)
        if s["n"] >= min_n and s["winrate"] is not None and s["winrate"] <= max_wr:
            out.append(k)
    return sorted(out)


# --------------------------------------------------------------------------- #
# Варианты-«ручки»
# --------------------------------------------------------------------------- #
def build_variants(baseline_trades: list) -> list:
    base_block = worst_strategies(baseline_trades)
    variants = [
        ("baseline", {}),
        ("conf_0.60", {"MIN_CONFIDENCE": 0.60}),
        ("conf_0.65", {"MIN_CONFIDENCE": 0.65}),
        ("conf_0.70", {"MIN_CONFIDENCE": 0.70}),
        ("vol_season_1.0", {"VOL_SEASON_GATE": True, "VOL_SEASON_MIN": 1.0}),
        ("vol_season_1.3", {"VOL_SEASON_GATE": True, "VOL_SEASON_MIN": 1.3}),
        ("vol_season_1.6", {"VOL_SEASON_GATE": True, "VOL_SEASON_MIN": 1.6}),
        ("atr_2.4_1.0", {"RR_BINDING": False, "ATR_TP_MULT": 2.4, "ATR_SL_MULT": 1.0}),
        ("atr_2.0_1.2", {"RR_BINDING": False, "ATR_TP_MULT": 2.0, "ATR_SL_MULT": 1.2}),
        ("atr_1.6_1.2", {"RR_BINDING": False, "ATR_TP_MULT": 1.6, "ATR_SL_MULT": 1.2}),
    ]
    if base_block:
        variants.append(("block_" + "+".join(base_block), {"STYPE_BLOCKLIST": ",".join(base_block)}))
    # комбо: лучшие одиночные рычаги собираем после прогона (в main)
    return variants


def run_variant(name, patch, cache, saved):
    trades, bars = [], 0
    for k in KNOBS:
        setattr(settings, k, saved[k])
    settings.__dict__.update(patch)
    try:
        for (sym, tf), candles in cache.items():
            trades.extend(simulate(sym, tf, candles))
            bars += len(candles)
    finally:
        for k in KNOBS:
            setattr(settings, k, saved[k])
    trades.sort(key=lambda r: r["ts"])
    s = stat(trades)
    print(f"    {name:<28s} n={s['n']:>4}  win={s['winrate']}%  avg={s['avg']}%  PF={s['pf']}")
    return {"name": name, "patch": patch, "stats": s,
            "by_reason": _exits(trades), "trades": trades}


def _exits(rows):
    out = {}
    for r in rows:
        out[r["reason"]] = out.get(r["reason"], 0) + 1
    return out


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
async def fetch_all(syms, tfs, days) -> dict:
    cache, errors = {}, []
    for sym in syms:
        for tf in tfs:
            days_eff = min(days, sb._MAX_DAYS.get(tf, 20))
            try:
                candles = await sb._load_history(sym, tf, days_eff)
                cache[(sym, tf)] = candles
                print(f"  [ok] {sym} {tf}: {len(candles)} баров ({days_eff} дн.)")
            except Exception as exc:
                errors.append(f"{sym} {tf}: {exc}")
                print(f"  [ERR] {sym} {tf}: {exc}")
    if not cache:
        raise SystemExit("Нет реальных данных Bybit — прогон не считаем. Включите сеть/VPN.")
    return cache


async def amain(args):
    t0 = time.time()
    syms = [s.strip().upper() for s in args.syms.split(",") if s.strip()]
    tfs = [t.strip() for t in args.tfs.split(",") if t.strip()]
    print(f"== TradeVision backtest_local: {len(syms)} монет × {len(tfs)} TF × {args.days} дн.")

    print("-- загрузка истории Bybit --")
    cache = await fetch_all(syms, tfs, args.days)

    saved = {k: getattr(settings, k) for k in KNOBS}

    print("-- baseline --")
    base = run_variant("baseline", {}, cache, saved)
    base_trades = base["trades"]

    print("-- варианты --")
    results = [base]
    for name, patch in build_variants(base_trades)[1:]:
        results.append(run_variant(name, patch, cache, saved))

    # комбо из двух лучших небазовых рычагов по среднему pnl (если они про разное)
    rest = [r for r in results[1:] if r["stats"]["n"] >= 8]
    rest.sort(key=lambda r: (r["stats"]["winrate"] or 0, r["stats"]["avg"] or 0), reverse=True)
    if len(rest) >= 2:
        combo_patch = dict(rest[0]["patch"])
        combo_patch.update(rest[1]["patch"])
        results.append(run_variant(f"combo[{rest[0]['name']}+{rest[1]['name']}]",
                                   combo_patch, cache, saved))

    print("\n-- разрезы baseline (где теряем) --")
    cuts_out = {}
    for title, table in cuts(base_trades):
        cuts_out[title] = [{"bucket": k, **v} for k, v in table]
        print(f"  [{title}]")
        for k, v in table[:12]:
            if (v["n"] or 0) >= 3:
                print(f"    {str(k):<22s} n={v['n']:>4} win={v['winrate']}% avg={v['avg']}%")

    # рекомендации: лучший вариант с достаточной выборкой
    cands = [r for r in results if r["stats"]["n"] >= 15]
    best = None
    if cands:
        best = max(cands, key=lambda r: (r["stats"]["winrate"] or 0))
    print("\n-- итог --")
    print(f"  baseline winrate: {base['stats']['winrate']}% (n={base['stats']['n']})")
    rec_env = {}
    if best:
        print(f"  лучший вариант: {best['name']} → win {best['stats']['winrate']}% "
              f"(n={best['stats']['n']}, PF={best['stats']['pf']})")
        if best["patch"]:
            print("  рекомендуемые строки .env:")
            for k, v in best["patch"].items():
                print(f"    {k}={v}")
                rec_env[k] = v
    else:
        print("  мало сделок (<15) для уверенных выводов — увеличьте --days/--syms.")

    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    out_path = args.out or os.path.join(HERE, "data", f"backtest_report_{args.tag}.json")
    report = {
        "tag": args.tag, "created": int(time.time()), "days": args.days,
        "symbols": syms, "tfs": tfs,
        "settings_snapshot": saved,
        "recommended_env": rec_env,
        "variants": [{k: r[k] for k in ("name", "patch", "stats", "by_reason")} for r in results],
        "cuts_baseline": cuts_out,
        "trades_baseline": base_trades,
        "trades_best": (best["trades"] if best else []),
        "note": "winrate≥70 — цель. Если её даёт только сегмент (conf≥0.70 / vol≥1.3), "
                "см. по строкам таблицы вариантов и разрезов; глобальная цифра — baseline.",
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"\nDONE за {time.time() - t0:.0f} c → {out_path}")
    print("Пришлите этот JSON разработчику для следующего витка улучшений.")


def main():
    ap = argparse.ArgumentParser(description="TradeVision local backtest harness")
    ap.add_argument("--syms", default=os.environ.get("SIM_SYMS", DEFAULT_SYMS))
    ap.add_argument("--tfs", default=os.environ.get("SIM_TFS", "5m,1m"))
    ap.add_argument("--days", type=int, default=int(os.environ.get("SIM_DAYS", "7")))
    ap.add_argument("--tag", default=os.environ.get("SIM_TAG", "run"))
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
