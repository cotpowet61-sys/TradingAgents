# -*- coding: utf-8 -*-
"""
Обучаемый сканер: разбор ИСХОДОВ live-сигналов по каждой монете
и автоподстройка правил. 0 AI-токенов — чистая статистика.

Петля обучения:
  1. Сигнал выдан → живёт в реестре → закрывается (TP/SL/таймаут/разворот).
  2. Исход пишется в data/signal_outcomes.jsonl (монета/TF/сторона/стратегия/
     уверенность/итоговый pnl%/длительность).
  3. По каждой паре (монета, стратегия, сторона) считается статистика:
     - sample >= 12 и winrate < 30% и суммарный pnl < 0
         → BLOCK: сканер глушит эту комбинацию (монета так не торгуется);
     - sample >= 8 и winrate < 40%  → TIGHTEN: порог уверенности +0.07;
     - sample >= 8 и winrate >= 65% → BOOST: порог уверенности −0.05.
  4. Правила сохраняются в data/learned_rules.json и применяются
     в live_engine при каждом сканировании.

Это и есть «обучаемая стратегия под каждую монету»: чем дольше работает,
тем точнее фильтры. Всё локально, детерминированно, без расхода токенов.
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional

from portal_config import DATA_DIR

OUTCOMES_PATH = os.path.join(DATA_DIR, "signal_outcomes.jsonl")
RULES_PATH = os.path.join(DATA_DIR, "learned_rules.json")

MIN_SAMPLE = 8      # минимальная выборка для подстройки порога
HARD_SAMPLE = 12    # выборка для жёсткой блокировки
TIGHTEN_DELTA = 0.07
BOOST_DELTA = -0.05
BLOCK_WR = 30.0
TIGHTEN_WR = 40.0
BOOST_WR = 65.0
KEEP_LINES = 3000   # ротация журнала исходов


def record_outcome(ev: dict) -> None:
    """Журналирует исход сигнала (вызывается движком при каждом закрытии)."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        row = {
            "symbol": ev.get("symbol"),
            "tf": ev.get("timeframe"),
            "side": ev.get("side"),
            "stype": ev.get("stype") or ev.get("strategy") or "?",
            "conf": ev.get("conf"),
            "reason": ev.get("reason"),
            "pnl_pct": ev.get("pnl_pct"),
            "duration_min": ev.get("duration_min"),
            "ts": ev.get("ts") or int(time.time() * 1000),
        }
        with open(OUTCOMES_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        _rotate()
    except Exception:
        pass


def _rotate() -> None:
    try:
        with open(OUTCOMES_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > KEEP_LINES:
            with open(OUTCOMES_PATH, "w", encoding="utf-8") as f:
                f.writelines(lines[-KEEP_LINES // 2:])
    except Exception:
        pass


def read_outcomes() -> List[dict]:
    if not os.path.exists(OUTCOMES_PATH):
        return []
    out = []
    try:
        with open(OUTCOMES_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
    except Exception:
        pass
    return out


BLOCK_TTL_MS = 3 * 86_400_000  # блок живёт 3 дня, потом — проба (рынок меняется)


def compute_rules(outcomes: Optional[List[dict]] = None) -> Dict[str, List[dict]]:
    """→ {symbol: [правило, ...]}. Только реальные исходы (REFRESH не считаем)."""
    rows = [o for o in (outcomes if outcomes is not None else read_outcomes())
            if o.get("reason") in ("TP", "SL", "TIMEOUT", "REVERSED") and o.get("pnl_pct") is not None]
    groups: Dict[tuple, List[dict]] = {}
    for o in rows:
        groups.setdefault((o["symbol"], o.get("stype") or "?", o.get("side") or "?"), []).append(o)

    rules: Dict[str, List[dict]] = {}
    for (sym, stype, side), g in groups.items():
        n = len(g)
        wins = sum(1 for x in g if x["pnl_pct"] > 0)
        wr = round(100 * wins / n, 1)
        total = round(sum(x["pnl_pct"] for x in g), 2)
        rule = {"symbol": sym, "stype": stype, "side": side, "sample": n,
                "winrate": wr, "total_pnl_pct": total}
        if n >= HARD_SAMPLE and wr < BLOCK_WR and total < 0:
            rule.update({"action": "block", "conf_delta": None,
                         "expires_at": int(time.time() * 1000) + BLOCK_TTL_MS,
                         "note": f"{side} по «{stype}» глушится на 3 дня: wr {wr}% за {n} сигналов (Σ {total}%)"})
        elif n >= MIN_SAMPLE and wr < TIGHTEN_WR:
            rule.update({"action": "tighten", "conf_delta": TIGHTEN_DELTA,
                         "note": f"порог +{TIGHTEN_DELTA}: wr {wr}% за {n} сигналов"})
        elif n >= MIN_SAMPLE and wr >= BOOST_WR and total > 0:
            rule.update({"action": "boost", "conf_delta": BOOST_DELTA,
                         "note": f"порог {BOOST_DELTA}: wr {wr}% за {n} сигналов"})
        else:
            rule.update({"action": "watch", "conf_delta": 0.0,
                         "note": f"набираем выборку: {n} сигналов, wr {wr}%"})
        rules.setdefault(sym, []).append(rule)
    for lst in rules.values():
        lst.sort(key=lambda r: (r["action"] != "block", r["action"] != "tighten", -r["sample"]))
    return rules


def save_rules(rules: Dict[str, List[dict]]) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(RULES_PATH, "w", encoding="utf-8") as f:
            json.dump({"built_at": int(time.time() * 1000), "rules": rules}, f, ensure_ascii=False)
    except Exception:
        pass


def load_rules() -> Dict[str, List[dict]]:
    try:
        with open(RULES_PATH, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("rules") or {}
    except Exception:
        return {}


# кэш правил в памяти (перечитываем файлы только по mtime)
_rules_cache: Dict[str, List[dict]] = {}
_rules_mtime: float = 0.0


def fresh_rules() -> Dict[str, List[dict]]:
    global _rules_cache, _rules_mtime
    try:
        mt = os.path.getmtime(RULES_PATH)
    except OSError:
        mt = 0.0
    if mt and mt > _rules_mtime:
        _rules_cache = load_rules()
        _rules_mtime = time.time()
    elif not mt and not _rules_cache:
        _rules_cache = compute_rules()  # первый запуск: попробуем собрать из журнала
        if _rules_cache:
            save_rules(_rules_cache)
            _rules_mtime = time.time()
    return _rules_cache


def get_adjustment(symbol: str, stype: str, side: str) -> Optional[dict]:
    """Подстройка для конкретной комбинации (монета, стратегия, сторона)."""
    sym_rules = (fresh_rules() or {}).get(symbol) or []
    stype_l = (stype or "").lower()
    hit = None
    for r in sym_rules:
        rs = (r.get("stype") or "").lower()
        if (rs == stype_l or stype_l.startswith(rs) or rs.startswith(stype_l)) and r.get("side") == side:
            hit = r
            break
    if not hit:
        return None
    if hit["action"] == "block":
        exp = hit.get("expires_at") or 0
        if exp and int(time.time() * 1000) > exp:
            # TTL вышел → авто-проба с ужесточённым порогом (не молчим вечно)
            return {"blocked": False, "conf_delta": TIGHTEN_DELTA, "action": "probation",
                    "note": f"проба после 3-дневного блока (wr была {hit['winrate']}%) — порог +{TIGHTEN_DELTA}",
                    "sample": hit["sample"], "winrate": hit["winrate"]}
        return {"blocked": True, "note": hit["note"], "sample": hit["sample"], "winrate": hit["winrate"]}
    if hit["conf_delta"]:
        return {"blocked": False, "conf_delta": hit["conf_delta"], "action": hit["action"],
                "note": hit["note"], "sample": hit["sample"], "winrate": hit["winrate"]}
    return {"blocked": False, "conf_delta": 0.0, "action": "watch",
            "note": hit["note"], "sample": hit["sample"], "winrate": hit["winrate"]}


def learn_from_event(ev: dict) -> None:
    """Полный шаг петли: записать исход и пересчитать правила его монеты."""
    global _rules_cache, _rules_mtime
    if ev.get("reason") == "REFRESH":
        return
    record_outcome(ev)
    rules = compute_rules()
    save_rules(rules)
    _rules_cache = rules            # кэш обновляем сразу сами (mtime надёжен
    _rules_mtime = time.time()      # только для чужих изменений файла)


def reset(symbol: Optional[str] = None) -> dict:
    """Сброс обучения (всех монет или одной)."""
    outcomes = read_outcomes()
    if symbol:
        keep = [o for o in outcomes if o.get("symbol") != symbol]
        with open(OUTCOMES_PATH, "w", encoding="utf-8") as f:
            for o in keep:
                f.write(json.dumps(o, ensure_ascii=False) + "\n")
        removed = len(outcomes) - len(keep)
    else:
        open(OUTCOMES_PATH, "w").close()
        removed = len(outcomes)
    rules = compute_rules()
    save_rules(rules)
    global _rules_cache, _rules_mtime
    _rules_cache = rules
    try:
        _rules_mtime = os.path.getmtime(RULES_PATH)
    except OSError:
        _rules_mtime = 0.0
    return {"removed": removed, "symbols_left": list(rules.keys())}


def apply_actions(actions: List[dict], source: str = "ai-curator") -> List[dict]:
    """Применяет правки к правилам (от AI-куратора). ops: keep/unblock/block/tighten/boost."""
    allowed = {"keep", "unblock", "block", "tighten", "boost"}
    rules = fresh_rules() or compute_rules()
    applied = []
    now_ms = int(time.time() * 1000)
    for a in actions or []:
        op = (a.get("op") or "").lower()
        sym, side = (a.get("symbol") or "").upper(), (a.get("side") or "").upper()
        stype_l = (a.get("stype") or "").lower()
        if op not in allowed or not sym:
            continue
        lst = rules.get(sym) or []
        hit = next((r for r in lst if (r.get("stype") or "").lower() == stype_l
                    and (r.get("side") or "") == side), None)
        if not hit or op == "keep":
            continue
        reason = (a.get("reason") or "").strip()[:220]
        if op == "unblock":
            hit.update({"action": "watch", "conf_delta": 0.0, "expires_at": 0,
                        "note": f"снятие блока куратором: {reason or 'восстановление наблюдения'}"})
        elif op == "block":
            hit.update({"action": "block", "conf_delta": None,
                        "expires_at": now_ms + BLOCK_TTL_MS,
                        "note": f"блок куратора (3 дня): {reason or hit.get('note', '')}"})
        elif op == "tighten":
            hit.update({"action": "tighten", "conf_delta": TIGHTEN_DELTA,
                        "note": f"порог ↑ куратором: {reason or hit.get('note', '')}"})
        elif op == "boost":
            hit.update({"action": "boost", "conf_delta": BOOST_DELTA,
                        "note": f"порог ↓ куратором: {reason or hit.get('note', '')}"})
        hit["ai_curated"] = {"op": op, "at": now_ms, "reason": reason}
        applied.append({"symbol": sym, "stype": hit.get("stype"), "side": side, "op": op})
    if applied:
        save_rules(rules)
        global _rules_cache, _rules_mtime
        _rules_cache = rules
        _rules_mtime = time.time()
    return applied
