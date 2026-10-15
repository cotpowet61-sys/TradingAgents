# -*- coding: utf-8 -*-
"""
Связка «биржевая аналитика → подходящие стратегии» (то самое ИИ-совмещение).

Что делает модуль:
  1. Берёт отчёт бэктеста портала (`signal_backtest.run_signal_backtest`), который
     прогоняет ВСЕ стратегии по РЕАЛЬНЫМ свечам Bybit, и разбирает его на правила:
     какая стратегия/сторона на какой паре (монета, ТФ) реально зарабатывает.
  2. Подбирает TP/SL: перебирает цель и стоп по сетке, гоняя тот же бэктест, и
     выбирает вариант по ЭКОНОМИКЕ (ожидаемый PnL после издержек), а не по
     «красивому винрейту». Пол по TP гарантирует, что цель не микроскопическая.
  3. Складывает результат в `DATA_DIR/ranker_report.json` и отдаёт его
     live-движку (запрет плохих стратегий на конкретной паре, порог уверенности)
     и админ-UI (кнопка «Применить» — запись в portal/.env).

Почему так, а не «пусть ИИ сам торгует»: правила проверяемые и воспроизводимые.
Каждое решение здесь — это число сделок, винрейт с нижней границей Уилсона,
ожидание на сделку и профит-фактор. Ничего «на глазок» и никаких обещаний.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from typing import Dict, List, Optional, Tuple

from portal_config import DATA_DIR, settings

REPORT_PATH = os.path.join(DATA_DIR, "ranker_report.json")
MIN_TRADES = 8            # меньше — статистики нет, правило не строим
MIN_WILSON = 45.0         # нижняя граница винрейта (95%), ниже — не берём
TP_FLOOR_REL = 4.0        # TP должен быть ≥ 4× полных издержек сделки

_costs = {"fee_bps": 5.5, "slip_bps": 2.0}   # тейкер Bybit + проскальзывание


def round_trip_cost_pct() -> float:
    return 2 * (_costs["fee_bps"] + _costs["slip_bps"]) / 100.0


def tp_floor_pct() -> float:
    """«Не слишком маленький TP»: цель обязана перекрывать издержки с запасом."""
    return round(max(settings.TP_PCT_MIN, round_trip_cost_pct() * TP_FLOOR_REL), 2)


# --------------------------------------------------------------------- #
# Статистика
# --------------------------------------------------------------------- #
def wilson_lo(wins: int, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    p = wins / n
    den = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return 100.0 * max(0.0, (c - h) / den)


def stats_of(rows: List[dict], cost_pct: float = None) -> dict:
    """Метрики по сделкам (pnl_pct уже без издержек — вычитаем их сами)."""
    c = round_trip_cost_pct() if cost_pct is None else cost_pct
    n = len(rows)
    if not n:
        return {"n": 0, "winrate": None, "wilson_lo": 0.0, "avg": None, "pf": None,
                "timeout_share": None, "median_min": None}
    net = [float(r.get("pnl_pct") or 0.0) - c for r in rows]
    wins = [x for x in net if x > 0]
    losses = [x for x in net if x <= 0]
    gw, gl = sum(wins), -sum(losses)
    mins = sorted(float(r.get("duration_min") or 0) for r in rows)
    return {
        "n": n,
        "wins": len(wins),
        "winrate": round(100 * len(wins) / n, 1),
        "wilson_lo": round(wilson_lo(len(wins), n), 1),
        "avg": round(sum(net) / n, 3),
        "total": round(sum(net), 2),
        "pf": round(gw / gl, 2) if gl > 0 else (None if gw == 0 else 99.0),
        "timeout_share": round(100 * sum(1 for r in rows if r.get("reason") == "timeout") / n, 1),
        "median_min": round(mins[len(mins) // 2]),
    }


def group_rows(rows: List[dict], key_fn) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    for r in rows:
        out.setdefault(key_fn(r), []).append(r)
    return out


# --------------------------------------------------------------------- #
# Разбор отчёта бэктеста в правила
# --------------------------------------------------------------------- #
def analyze(report: dict, *, min_trades: int = MIN_TRADES,
            min_wilson: float = MIN_WILSON, cost_pct: float = None) -> dict:
    """Отчёт бэктеста → правила «что разрешено, что запрещено».

    Отчёт должен содержать `trades_all` (список сделок) — его отдаёт
    run_signal_backtest. Если его нет, работаем с `per_pair`/`by_strategy`.
    """
    rows = list(report.get("trades_all") or [])
    cost = round_trip_cost_pct() if cost_pct is None else cost_pct
    out: dict = {"generated_at": int(time.time()), "cost_pct": round(cost, 4),
                 "cost_note": "комиссия тейкера + проскальзывание, вычтено из PnL",
                 "tp_floor_pct": tp_floor_pct(), "pairs": {}, "global": {},
                 "blocked_sides": {}, "min_confidence": None, "notes": []}

    if not rows:
        out["notes"].append("в отчёте нет сделок — правила не построены")
        return out

    # 1) глобально по (стратегия, сторона)
    by_ss = group_rows(rows, lambda r: f"{r.get('stype') or r.get('strategy')}/{str(r.get('side'))[0]}")
    glob = {}
    for k, v in by_ss.items():
        glob[k] = stats_of(v, cost)
    out["global"] = dict(sorted(glob.items(), key=lambda kv: -(kv[1]["avg"] or -99)))
    out["blocked_sides"] = sorted(k for k, s in glob.items()
                                  if s["n"] >= min_trades and (s["avg"] or 0) <= 0)

    # 2) по парам (монета × ТФ): разрешённые и запрещённые стратегии
    by_pair = group_rows(rows, lambda r: f"{r.get('symbol')}:{r.get('tf')}")
    for pair, prows in by_pair.items():
        pss = group_rows(prows, lambda r: f"{r.get('stype') or r.get('strategy')}/{str(r.get('side'))[0]}")
        stats = {k: stats_of(v, cost) for k, v in pss.items()}
        allowed = sorted([k for k, s in stats.items()
                          if s["n"] >= min_trades and (s["avg"] or 0) > 0
                          and s["wilson_lo"] >= min_wilson],
                         key=lambda k: -(stats[k]["avg"] or 0))
        blocked = sorted([k for k, s in stats.items()
                          if s["n"] >= min_trades and (s["avg"] or 0) <= 0])
        best = allowed[0] if allowed else None
        out["pairs"][pair] = {
            "trades": len(prows), "overall": stats_of(prows, cost),
            "allowed": allowed, "blocked": blocked, "best": best,
            "by_strategy_side": dict(sorted(stats.items(), key=lambda kv: -(kv[1]["avg"] or -99))),
        }

    # 3) предлагаемый порог уверенности: максимум ожидания по срезам conf
    buckets = {"0.50-0.60": (0.50, 0.60), "0.60-0.70": (0.60, 0.70),
               "0.70-0.80": (0.70, 0.80), "0.80-1.00": (0.80, 1.01)}
    best_thr, best_avg, rows_n = None, None, 0
    for name, (lo, hi) in buckets.items():
        sel = [r for r in rows if lo <= float(r.get("conf") or 0) < hi]
        if len(sel) < min_trades:
            continue
        s = stats_of(sel, cost)
        if s["avg"] is None:
            continue
        # берём минимальный порог, у которого ожидание ≥ 0 и не хуже 0.6 от лучшего
        if (s["avg"] or 0) > 0 and (best_avg is None or s["avg"] > best_avg):
            best_avg, best_thr, rows_n = s["avg"], lo, len(sel)
    if best_thr is not None and best_thr > settings.MIN_CONFIDENCE:
        out["min_confidence"] = best_thr
        out["notes"].append(f"срез conf ≥ {best_thr:.2f}: ожидание {best_avg:+.3f}% на {rows_n} сделках")
    return out


# --------------------------------------------------------------------- #
# Автоподбор TP/SL
# --------------------------------------------------------------------- #
async def optimize_targets(symbols: List[str], tf: str, days: int = 30, *,
                           tp_grid: Optional[List[float]] = None,
                           sl_grid: Optional[List[float]] = None,
                           winrate_target: float = 75.0,
                           max_symbols: int = 4) -> dict:
    """Перебор TP/SL на реальных свечах Bybit.

    Критерий (в этом порядке):
      1) ожидание на сделку > 0 после издержек (иначе вариант не рассматривается);
      2) среди прибыльных — те, где винрейт ≥ цели, а если таких нет, максимум
         ожидания (честно помечаем `winrate_target_met=false`);
      3) TP не ниже пола (издержки × 4) — «не слишком маленький TP».

    Ничего не пишет в .env сам: возвращает отчёт, применение — отдельное действие
    оператора (кнопка в админке или CLI-флаг --apply).
    """
    import signal_backtest as sb
    floor = tp_floor_pct()
    tp_grid = [round(t, 2) for t in (tp_grid or [0.8, 1.0, 1.5, 2.0, 2.5]) if t >= floor]
    if not tp_grid:
        tp_grid = [floor]
    sl_grid = [round(s, 2) for s in (sl_grid or [0.5, 0.8, 1.0, 1.5, 2.0])]
    saved = (settings.MANUAL_TP_PCT, settings.MANUAL_SL_PCT, settings.RR_BINDING)
    results: List[dict] = []
    try:
        for tp in tp_grid:
            for sl in sl_grid:
                settings.MANUAL_TP_PCT, settings.MANUAL_SL_PCT = tp, sl
                settings.RR_BINDING = False       # ручной риск-фактор имеет приоритет
                try:
                    rep = await sb.run_signal_backtest(symbols, [tf], days,
                                                       max_symbols=max_symbols)
                except Exception as e:      # noqa: BLE001 — нет сети/данных: не роняем прогон
                    return {"error": f"{type(e).__name__}: {e}", "tp_floor_pct": floor,
                            "hint": "автоподбор требует доступа к Bybit (реальные свечи)"}
                rows = list(rep.get("trades_all") or [])
                st = stats_of(rows)
                results.append({"tp_pct": tp, "sl_pct": sl, "cost_pct": round(round_trip_cost_pct(), 4),
                                **st, "errors": rep.get("errors") or []})
    finally:
        settings.MANUAL_TP_PCT, settings.MANUAL_SL_PCT, settings.RR_BINDING = saved

    ok = [r for r in results if r["n"] >= 10 and (r["avg"] or -1) > 0]
    hit = [r for r in ok if (r["winrate"] or 0) >= winrate_target]
    pool = hit or ok
    best = max(pool, key=lambda r: (r["winrate"] or 0) if hit else (r["avg"] or -99),
               default=None)
    return {
        "tf": tf, "symbols": symbols[:max_symbols], "days": days,
        "winrate_target": winrate_target, "winrate_target_met": bool(hit),
        "tp_floor_pct": floor, "cost_pct": round(round_trip_cost_pct(), 4),
        "grid": sorted(results, key=lambda r: -(r["avg"] or -99)),
        "best": best,
        "notes": _opt_notes(results, hit, floor, winrate_target),
    }


def _opt_notes(results: List[dict], hit: List[dict], floor: float, target: float) -> List[str]:
    notes = []
    if not results:
        notes.append("сетка пуста — проверьте пол по TP")
    if not hit:
        profitable = [r for r in results if (r["avg"] or -1) > 0]
        if profitable:
            best_wr = max(r["winrate"] or 0 for r in profitable)
            notes.append(f"винрейт {target:g}% на этих данных не берётся: у прибыльных "
                         f"вариантов максимум {best_wr:.1f}%. Показываем лучший по ожиданию — "
                         f"это честный компромисс винрейт/риск, а не обещание.")
        else:
            notes.append("ни один вариант сетки не прибылен после издержек — "
                         "нужны другие фильтры входа, а не подбор целей")
    notes.append(f"пол по TP = {floor:g}% (издержки × {TP_FLOOR_REL:g}): цель обязана "
                 f"перекрывать комиссию и проскальзывание")
    return notes


# --------------------------------------------------------------------- #
# Хранение и применение
# --------------------------------------------------------------------- #
def save_report(rep: dict) -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = REPORT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REPORT_PATH)
    return REPORT_PATH


def load_report() -> dict:
    try:
        with open(REPORT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def pair_rules(report: Optional[dict] = None) -> dict:
    return ((report if report is not None else load_report()).get("rules") or {}).get("pairs") or {}


def blocked_for(symbol: str, tf: str) -> Tuple[set, set]:
    """(запрещённые, разрешённые) пары «стратегия/сторона» для конкретной пары."""
    rules = pair_rules().get(f"{symbol}:{tf}") or {}
    return set(rules.get("blocked") or []), set(rules.get("allowed") or [])


def pick_allowed(candidates: List[str], symbol: str, tf: str) -> Optional[str]:
    """Первый кандидат, который не заблокирован для этой пары (или None)."""
    banned, _allowed = blocked_for(symbol, tf)
    stypes = {c.split("/")[0] for c in banned}
    for stype in candidates or []:
        if stype not in stypes:
            return stype
    return None


def apply_to_env(rec: dict, *, approved_by: str = "") -> dict:
    """Записать рекомендованные настройки в portal/.env (только по решению оператора)."""
    import portal_config as pc
    from security import audit
    updates: Dict[str, str] = {}
    if rec.get("tp_pct") and rec.get("sl_pct"):
        updates["MANUAL_TP_PCT"] = str(round(float(rec["tp_pct"]), 3))
        updates["MANUAL_SL_PCT"] = str(round(float(rec["sl_pct"]), 3))
        updates["RR_BINDING"] = "false"
    if rec.get("min_confidence"):
        updates["MIN_CONFIDENCE"] = str(round(float(rec["min_confidence"]), 3))
    blocked = rec.get("blocked_sides") or []
    if blocked:
        updates["STYPE_BLOCKLIST"] = ",".join(sorted(blocked))
    if not updates:
        return {"applied": {}}
    pc._persist_env(updates)      # noqa: SLF001 — намеренно: единый путь записи .env
    # применяем на лету, чтобы не перезапускать сервис
    for key, val in updates.items():
        attr = {"MANUAL_TP_PCT": "MANUAL_TP_PCT", "MANUAL_SL_PCT": "MANUAL_SL_PCT",
                "RR_BINDING": "RR_BINDING", "MIN_CONFIDENCE": "MIN_CONFIDENCE",
                "STYPE_BLOCKLIST": "STYPE_BLOCKLIST"}.get(key)
        if not attr:
            continue
        cur = getattr(settings, attr)
        if isinstance(cur, bool):
            val = str(val).lower() in ("1", "true", "yes", "on")
        elif isinstance(cur, float):
            val = float(val)
        setattr(settings, attr, val)
    try:
        audit("ranker_apply", actor=approved_by[:64],
              detail=json.dumps(updates, ensure_ascii=False)[:400])
    except Exception:
        pass
    return {"applied": updates}


def summary_for_ui() -> dict:
    rep = load_report()
    rules = rep.get("rules") or {}
    return {
        "generated_at": rep.get("generated_at"),
        "tp_floor_pct": rules.get("tp_floor_pct"),
        "cost_pct": rules.get("cost_pct"),
        "min_confidence": rules.get("min_confidence"),
        "blocked_sides": rules.get("blocked_sides") or [],
        "pairs": {k: {"allowed": v.get("allowed")[:6], "blocked": v.get("blocked")[:6],
                      "trades": v.get("trades"), "avg": (v.get("overall") or {}).get("avg"),
                      "winrate": (v.get("overall") or {}).get("winrate")}
                  for k, v in list(rules.get("pairs", {}).items())[:40]},
        "optimize": rep.get("optimize"),
        "notes": rules.get("notes") or [],
    }


# --------------------------------------------------------------------- #
# CLI: python strategy_ranker.py --symbols ... --tf 15m [--apply]
# --------------------------------------------------------------------- #
async def _cli() -> int:      # pragma: no cover — ручной инструмент оператора
    import argparse
    p = argparse.ArgumentParser(description="Разбор сделок с биржи и подбор стратегий/целей")
    p.add_argument("--symbols", default=",".join(settings.watchlist))
    p.add_argument("--tf", default=settings.DEFAULT_TIMEFRAME)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--max-symbols", type=int, default=4)
    p.add_argument("--winrate-target", type=float, default=75.0)
    p.add_argument("--optimize", action="store_true", help="перебрать TP/SL (медленно, сеть)")
    p.add_argument("--apply", action="store_true", help="записать лучшее в portal/.env")
    a = p.parse_args()

    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    import signal_backtest as sb
    try:
        rep = await sb.run_signal_backtest(symbols, [a.tf], a.days, max_symbols=a.max_symbols)
    except Exception as e:      # noqa: BLE001
        print(f"✗ бэктест недоступен: {e}")
        return 2
    rules = analyze(rep)
    payload = {"generated_at": int(time.time()), "params": {"symbols": symbols, "tf": a.tf,
                                                           "days": a.days},
               "overall": rep.get("overall"), "rules": rules}
    if a.optimize:
        opt = await optimize_targets(symbols, a.tf, a.days,
                                     winrate_target=a.winrate_target,
                                     max_symbols=a.max_symbols)
        payload["optimize"] = opt
        print(json.dumps(opt.get("best"), ensure_ascii=False, indent=2))
        for n in opt.get("notes") or []:
            print("•", n)
    path = save_report(payload)
    print(f"✓ отчёт: {path}")
    print(json.dumps({k: payload["rules"][k] for k in ("tp_floor_pct", "min_confidence",
                                                       "blocked_sides")},
                     ensure_ascii=False, indent=2))
    if a.apply:
        best = (payload.get("optimize") or {}).get("best")
        target = dict(best or {})
        target.setdefault("min_confidence", rules.get("min_confidence"))
        target.setdefault("blocked_sides", rules.get("blocked_sides"))
        print("применяю:", apply_to_env(target, approved_by="cli"))
    return 0


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(asyncio.run(_cli()))
