# -*- coding: utf-8 -*-
"""
Локальный бэктест-харнесс TradeVision: «как если бы страница сканера была открыта».

Режим А — полный прогон (нужен доступ к Bybit):
    python backtest_local.py --days 10 --tfs "5m" --tag week1

Режим Б — оффлайн-анализ уже готового отчёта (без сети, секунды):
    python backtest_local.py --analyze data/backtest_report_week1.json

Что делает режим А:
  1. Тянет РЕАЛЬНЫЕ свечи Bybit (synthetic = прогон не считается).
  2. Зеркальная лайв-логика: авто-селектор, min_conf, приоритет целей, кулдаун
     30 мин, разворот при +0.10 conf, одна сделка на (монета,TF), единый модуль
     фильтров signal_gates (стоп-листы, запретные зоны, breakeven/трейлинг).
  3. Разрезы (стратегия/сторона/монета/час/ratio-бакеты).
  4. Перебор «ручек» качества → таблица n/win/avg/PF по вариантам.
  5. JSON-отчёт + рекомендуемые строки .env.

Режим Б (--analyze): берёт trades_baseline из отчёта и ОФФЛАЙН ищет лучшую
КОМБИНАЦИЮ входных фильтров (стоп-листы, запретные зоны объёма/ATR/часов,
сторонние стоп-листы stype/SIDE) с нижними границами Уилсона — это и есть
кандидат в .env. Трейлинг/breakeven оффлайн не оценить (нужен путь цены) —
их меряем полным прогоном (варианты be_*/trail_*).

Честность: winrate ≥70 — цель, а не обещание. Конфиг-фильтры с 38% до 70%
не дотянут (см. отчёт); реальный механизм высокого winrate — breakeven/трейлинг
и/или снижение TP, измеряем цифрами, не верой.
"""
import argparse
import asyncio
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

from portal_config import settings            # noqa: E402
import signal_backtest as sb                   # noqa: E402
import signal_gates as G                       # noqa: E402
from strategies.registry import build_strategy  # noqa: E402
from strategies.selector import get_selector    # noqa: E402
from strategies import indicators as ta         # noqa: E402

COOLDOWN_S = 1800      # зеркало SIGNAL_COOLDOWN_SEC
FLIP_MARGIN = 0.10     # зеркало FLIP_CONF_MARGIN

DEFAULT_SYMS = ("BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,"
                "LINKUSDT,AVAXUSDT,NEARUSDT,ARBUSDT,OPUSDT,ATOMUSDT")

# Глобальные ручки, которые трогают варианты (снапшотим и откатываем).
KNOBS = ("MIN_CONFIDENCE", "RR_BINDING", "ATR_TP_MULT", "ATR_SL_MULT",
         "VOL_SEASON_GATE", "VOL_SEASON_MIN", "VOL_SEASON_STYPES", "STYPE_BLOCKLIST",
         "VOL_SEASON_BLOCK", "ATR_PCT_BLOCK", "HOURS_BLOCK",
         "BE_TRIGGER_PCT", "BE_OFFSET_PCT", "TRAIL_START_PCT", "TRAIL_PCT")


# --------------------------------------------------------------------------- #
# Зеркальный симулятор
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
            if not hit:
                # breakeven/трейлинг по экстремуму бара (зеркало live-тикера)
                G.update_stops(settings, open_t, c.high if side == "LONG" else c.low)
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
        entry = sig.entry or c.close
        try:
            summ = ta.compute_feature_summary(window) or {}
        except Exception:
            summ = {}
        # единые фильтры входа (зеркало лайва)
        if G.entry_block_reason(settings, stype=last_type, side=side_v, symbol=symbol,
                                hour_utc=(c.ts // 3_600_000) % 24,
                                vol_season=summ.get("vol_ratio_seasonal"),
                                atr_pct=summ.get("atr_pct")):
            continue
        # live-антиспам (чуть строже лайва: действует и после закрытия сделки)
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
                  "hi": entry, "lo": entry,
                  "rsi": summ.get("rsi"), "atr_pct": summ.get("atr_pct"),
                  "vol_ratio": summ.get("volume_ratio"),
                  "vol_ratio_seasonal": summ.get("vol_ratio_seasonal"),
                  "hour": time.strftime("%H", time.gmtime(c.ts / 1000))}
    return trades


# --------------------------------------------------------------------------- #
# Метрики
# --------------------------------------------------------------------------- #
def stat(rows: list) -> dict:
    if not rows:
        return {"n": 0, "winrate": None, "avg": None, "pf": None, "wlo": None}
    wins = [r for r in rows if r["pnl_pct"] > 0]
    gw = sum(r["pnl_pct"] for r in wins)
    gl = -sum(r["pnl_pct"] for r in rows if r["pnl_pct"] <= 0)
    wr = 100 * len(wins) / len(rows)
    return {"n": len(rows), "winrate": round(wr, 1),
            "avg": round(sum(r["pnl_pct"] for r in rows) / len(rows), 3),
            "pf": round(gw / gl, 2) if gl > 0 else (99.0 if gw > 0 else None),
            "wlo": _wilson_lo(wr, len(rows))}


def _wilson_lo(wr: float, n: int, z: float = 1.96) -> float:
    if not n:
        return 0.0
    p = wr / 100
    den = 1 + z * z / n
    return round(100 * (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / den, 1)


def cuts(rows: list):
    def by(fn, title):
        agg = {}
        for r in rows:
            agg.setdefault(fn(r), []).append(r)
        table = sorted(((k, stat(v)) for k, v in agg.items()),
                       key=lambda kv: (-(kv[1]["n"] or 0)))
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

    def stype_side(r):
        return f"{r['stype']}/{r['side'][0]}"

    return [by(lambda r: r["stype"], "by_strategy"),
            by(stype_side, "by_strategy_side"),
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
# Варианты
# --------------------------------------------------------------------------- #
def build_variants(baseline_trades: list, quick: bool = False) -> list:
    base_block = worst_strategies(baseline_trades)
    full = [
        ("baseline", {}),
        ("conf_0.60", {"MIN_CONFIDENCE": 0.60}),
        ("vol_season_1.3", {"VOL_SEASON_GATE": True, "VOL_SEASON_MIN": 1.3}),
        ("atr_1.6_1.2", {"RR_BINDING": False, "ATR_TP_MULT": 1.6, "ATR_SL_MULT": 1.2}),
        # — фильтры зон, замеренные на week1 —
        ("vol_block_1.0-2.0+3up", {"VOL_SEASON_BLOCK": "1.0-2.0,3.0-999"}),
        ("atr_block_0.6-1.0", {"ATR_PCT_BLOCK": "0.6-1.0"}),
        ("hours_block_3_11_19", {"HOURS_BLOCK": "3,11,19"}),
        # — механика выхода: высокий winrate честно —
        ("be_0.8_0.10", {"BE_TRIGGER_PCT": 0.8, "BE_OFFSET_PCT": 0.10}),
        ("be_1.0_0.15", {"BE_TRIGGER_PCT": 1.0, "BE_OFFSET_PCT": 0.15}),
        ("trail_1.3_0.6", {"TRAIL_START_PCT": 1.3, "TRAIL_PCT": 0.6}),
        ("be1.0+trail1.4_0.6", {"BE_TRIGGER_PCT": 1.0, "BE_OFFSET_PCT": 0.15,
                                "TRAIL_START_PCT": 1.4, "TRAIL_PCT": 0.6}),
    ]
    if base_block:
        full.insert(1, ("block_" + "+".join(base_block), {"STYPE_BLOCKLIST": ",".join(base_block)}))
        full.append(("combo", {"STYPE_BLOCKLIST": ",".join(base_block),
                               "VOL_SEASON_BLOCK": "1.0-2.0,3.0-999",
                               "ATR_PCT_BLOCK": "0.6-1.0",
                               "HOURS_BLOCK": "3,11,19",
                               "RR_BINDING": False, "ATR_TP_MULT": 1.6, "ATR_SL_MULT": 1.2}))
        full.append(("combo+be", {"STYPE_BLOCKLIST": ",".join(base_block),
                                  "VOL_SEASON_BLOCK": "1.0-2.0,3.0-999",
                                  "ATR_PCT_BLOCK": "0.6-1.0",
                                  "HOURS_BLOCK": "3,11,19",
                                  "RR_BINDING": False, "ATR_TP_MULT": 1.6, "ATR_SL_MULT": 1.2,
                                  "BE_TRIGGER_PCT": 1.0, "BE_OFFSET_PCT": 0.15,
                                  "TRAIL_START_PCT": 1.4, "TRAIL_PCT": 0.6}))
    if quick:
        keep = ("baseline",) + tuple(v[0] for v in full if v[0].startswith("block_")) + \
            ("be_1.0_0.15", "be1.0+trail1.4_0.6", "vol_block_1.0-2.0+3up", "combo", "combo+be")
        full = [v for v in full if v[0] in keep]
    return full


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
    print(f"    {name:<30s} n={s['n']:>4}  win={s['winrate']}%  avg={s['avg']}%  PF={s['pf']}  wlo={s['wlo']}%")
    return {"name": name, "patch": patch, "stats": s,
            "by_reason": _exits(trades), "trades": trades}


def _exits(rows):
    out = {}
    for r in rows:
        out[r["reason"]] = out.get(r["reason"], 0) + 1
    return out


# --------------------------------------------------------------------------- #
# Режим Б: оффлайн-анализ отчёта
# --------------------------------------------------------------------------- #
def analyze_report(path: str):
    d = json.load(open(path, encoding="utf-8"))
    tr = d.get("trades_baseline") or []
    if not tr:
        raise SystemExit("в отчёте нет trades_baseline")
    print(f"== analyze: {os.path.basename(path)} — {len(tr)} сделок baseline")
    print(f"   baseline: {stat(tr)}")

    VOL_Z = [(0, 0.7), (0.7, 1.0), (1.0, 1.3), (1.3, 1.6), (1.6, 2.0), (2.0, 3.0), (3.0, 999.0)]
    ATR_Z = [(0, 0.3), (0.3, 0.6), (0.6, 1.0), (1.0, 1.5), (1.5, 99)]

    def zstat(keyfn, zones):
        out = []
        for lo, hi in zones:
            rows = [t for t in tr if (lambda v: v is not None and lo <= v < hi)(keyfn(t))]
            out.append((lo, hi, stat(rows)))
        return out

    print("\n-- зоны сезонного объёма --")
    vol_block = []
    for lo, hi, s in zstat(lambda t: t.get("vol_ratio_seasonal"), VOL_Z):
        mark = ""
        if s["n"] >= 20 and (s["winrate"] or 100) < 36 and (s["avg"] or 0) < 0:
            vol_block.append(f"{lo}-{hi:g}"); mark = "  <== БЛОК"
        print(f"   [{lo:g},{hi:g}) n={s['n']:>3} win={s['winrate']}% avg={s['avg']}%{mark}")

    print("\n-- зоны ATR% --")
    atr_block = []
    for lo, hi, s in zstat(lambda t: t.get("atr_pct"), ATR_Z):
        mark = ""
        if s["n"] >= 20 and (s["winrate"] or 100) < 32 and (s["avg"] or 0) < 0:
            atr_block.append(f"{lo}-{hi:g}"); mark = "  <== БЛОК"
        print(f"   [{lo:g},{hi:g}) n={s['n']:>3} win={s['winrate']}% avg={s['avg']}%{mark}")

    print("\n-- часы UTC (худшие с n>=20) --")
    hours = sorted(((h, stat([t for t in tr if str(t.get("hour")) == h]))
                    for h in {str(t.get("hour")) for t in tr}), key=lambda kv: kv[1]["winrate"] or 0)
    hours_block = []
    for h, s in hours[:5]:
        mark = ""
        if s["n"] >= 20 and (s["winrate"] or 100) < 30 and (s["avg"] or 0) < 0:
            hours_block.append(h); mark = "  <== БЛОК"
        print(f"   {h}:00 n={s['n']:>3} win={s['winrate']}% avg={s['avg']}%{mark}")

    stypes = worst_strategies(tr)
    # сторонние блоки stype/SIDE (n>=15, win<=33, pf<0.9)
    agg = {}
    for t in tr:
        agg.setdefault((t["stype"], t["side"]), []).append(t)
    side_blocks = sorted(f"{k[0]}/{k[1]}" for k, v in agg.items()
                         if len(v) >= 15 and (stat(v)["winrate"] or 100) <= 33
                         and (stat(v)["pf"] or 9) < 0.9)
    if side_blocks:
        print("\n-- сторонние стоп-листы stype/SIDE --")
        for b in side_blocks:
            k = tuple(b.split("/"))
            print(f"   {b}: {stat(agg[k])}")

    # ---- комбинированная оценка оффлайн (только входные фильтры) ----
    def f_type(t): return t["stype"] in stypes or f"{t['stype']}/{t['side']}" in side_blocks
    def f_vol(t):
        v = t.get("vol_ratio_seasonal")
        return v is not None and any(lo <= v < hi for lo, hi in
                                     (map(lambda x: tuple(map(float, x.split('-'))), vol_block)))
    def f_atr(t):
        a = t.get("atr_pct")
        return a is not None and any(lo <= a < hi for lo, hi in
                                     (map(lambda x: tuple(map(float, x.split('-'))), atr_block)))
    def f_hour(t): return str(t.get("hour")) in set(hours_block)

    print("\n-- комбинации (оффлайн, входные фильтры) --")
    combos = {
        "stype_block": f_type,
        "vol_zones": f_vol,
        "atr_zones": f_atr,
        "hours": f_hour,
    }
    for name, f in combos.items():
        kept = [t for t in tr if not f(t)]
        print(f"   {name:22s} -> {stat(kept)}")
    kept = [t for t in tr if not (f_type(t) or f_vol(t) or f_atr(t) or f_hour(t))]
    s = stat(kept)
    print(f"   {'ВСЁ ВМЕСТЕ':22s} -> {s}")

    print("\n-- рекомендуемые строки .env (входные фильтры) --")
    if stypes:
        print(f"   STYPE_BLOCKLIST={','.join(stypes + side_blocks)}")
    if vol_block:
        print(f"   VOL_SEASON_BLOCK={','.join(vol_block)}")
    if atr_block:
        print(f"   ATR_PCT_BLOCK={','.join(atr_block)}")
    if hours_block:
        print(f"   HOURS_BLOCK={','.join(hours_block)}")
    print("   RR_BINDING=False\n   ATR_TP_MULT=1.6\n   ATR_SL_MULT=1.2")
    print("\nВНИМАНИЕ: breakeven/трейлинг оффлайн не оценить — меряются полным прогоном")
    print("(варианты be_*/be1.0+trail*). Если combo+be даст winrate ~60-70% при PF>=1.2,")
    print("включайте BE_TRIGGER_PCT/TRAIL_* в .env — это и есть честный путь к 70.")
    print("Предел конфиг-фильтров на ваших данных виден выше — не ждите от них 70.")


# --------------------------------------------------------------------------- #
# Режим А: загрузка + прогон
# --------------------------------------------------------------------------- #
async def fetch_all(syms, tfs, days) -> dict:
    cache = {}
    for sym in syms:
        for tf in tfs:
            days_eff = min(days, sb._MAX_DAYS.get(tf, 20))
            try:
                candles = await sb._load_history(sym, tf, days_eff)
                cache[(sym, tf)] = candles
                print(f"  [ok] {sym} {tf}: {len(candles)} баров ({days_eff} дн.)")
            except Exception as exc:
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
    variants = build_variants(base_trades, quick=args.quick)
    for name, patch in variants[1:]:
        results.append(run_variant(name, patch, cache, saved))

    print("\n-- разрезы baseline (где теряем) --")
    cuts_out = {}
    for title, table in cuts(base_trades):
        cuts_out[title] = [{"bucket": k, **v} for k, v in table]
        print(f"  [{title}]")
        for k, v in table[:12]:
            if (v["n"] or 0) >= 3:
                print(f"    {str(k):<22s} n={v['n']:>4} win={v['winrate']}% avg={v['avg']}%")

    cands = [r for r in results if r["stats"]["n"] >= 15]
    best = max(cands, key=lambda r: (r["stats"]["wlo"] or 0)) if cands else None
    print("\n-- итог --")
    print(f"  baseline winrate: {base['stats']['winrate']}% (n={base['stats']['n']})")
    rec_env = {}
    if best:
        print(f"  лучший вариант (по нижней границе Уилсона): {best['name']} → "
              f"win {best['stats']['winrate']}% (n={best['stats']['n']}, PF={best['stats']['pf']})")
        if best["patch"]:
            print("  рекомендуемые строки .env:")
            for k, v in best["patch"].items():
                print(f"    {k}={v}")
                rec_env[k] = v

    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    out_path = args.out or os.path.join(HERE, "data", f"backtest_report_{args.tag}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "tag": args.tag, "created": int(time.time()), "days": args.days,
            "symbols": syms, "tfs": tfs, "settings_snapshot": saved,
            "recommended_env": rec_env,
            "variants": [{k: r[k] for k in ("name", "patch", "stats", "by_reason")} for r in results],
            "cuts_baseline": cuts_out,
            "trades_baseline": base_trades,
            "trades_best": (best["trades"] if best else []),
            "note": "winrate≥70 — цель. Входные фильтры с 38% до 70 не дотягивают; "
                    "смотрите варианты be_*/be1.0+trail*: breakeven/трейлинг — "
                    "честный механизм высокого winrate.",
        }, f, ensure_ascii=False, indent=1)
    print(f"\nDONE за {time.time() - t0:.0f} c → {out_path}")
    print("Дальше: python backtest_local.py --analyze <этот файл> — оффлайн-подбор комбинации.")


def main():
    ap = argparse.ArgumentParser(description="TradeVision local backtest harness")
    ap.add_argument("--syms", default=os.environ.get("SIM_SYMS", DEFAULT_SYMS))
    ap.add_argument("--tfs", default=os.environ.get("SIM_TFS", "5m,1m"))
    ap.add_argument("--days", type=int, default=int(os.environ.get("SIM_DAYS", "7")))
    ap.add_argument("--tag", default=os.environ.get("SIM_TAG", "run"))
    ap.add_argument("--out", default="")
    ap.add_argument("--analyze", default="", help="оффлайн-анализ готового отчёта JSON")
    ap.add_argument("--quick", action="store_true", help="только ключевые варианты (быстрее)")
    args = ap.parse_args()
    if args.analyze:
        analyze_report(args.analyze)
        return
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
