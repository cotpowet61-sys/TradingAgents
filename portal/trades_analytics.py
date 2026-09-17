"""
Аналитика истории сделок пользователя + промпт для AI-аналитика портфеля.

Источники:
  1) Bybit API (read-only): /v5/position/closed-pnl + /v5/execution/list
  2) CSV-экспорт Bybit (файлы кладутся/загружаются в portal/data/)
"""
from __future__ import annotations

import csv
import io
import os
import time
from collections import defaultdict
from typing import List, Optional

from portal_config import settings, DATA_DIR
from bybit_account import fetch_closed_pnl, fetch_executions, BybitAuthError

CSV_PATH = os.path.join(DATA_DIR, "bybit_trades.csv")


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------- #
# Нормализация источников в единый список сделок
# ---------------------------------------------------------------------- #
def normalize_closed_pnl(rows: List[dict]) -> List[dict]:
    trades = []
    for r in rows:
        trades.append({
            "symbol": r.get("symbol"),
            "side": (r.get("side") or "").upper(),          # Buy=лонг, Sell=шорт
            "qty": _f(r.get("qty")),
            "entry": _f(r.get("avgEntryPrice")),
            "exit": _f(r.get("avgExitPrice")),
            "pnl": _f(r.get("closedPnl")),
            "open_ts": int(_f(r.get("createdTime"))) if r.get("createdTime") else None,
            "close_ts": int(_f(r.get("updatedTime"))) if r.get("updatedTime") else None,
            "leverage": _f(r.get("leverage"), 1.0),
            "source": "bybit_api",
        })
    return [t for t in trades if t["symbol"]]


def normalize_csv(text: str) -> List[dict]:
    """Парсим CSV-экспорт позиций/ордеров Bybit.
    Поддерживаем гибкий разбор заголовков (Bybit меняет их между версиями)."""
    trades = []
    reader = csv.DictReader(io.StringIO(text.replace("﻿", "")))
    if not reader.fieldnames:
        return []
    for row in reader:
        low = { (k or "").strip().lower(): (v or "").strip() for k, v in row.items() }
        def pick(*names):
            for n in names:
                if n in low and low[n] != "":
                    return low[n]
            return None
        sym = pick("symbol", "contracts", "contract", "instrument")
        side_raw = (pick("side", "direction") or "").lower()
        pnl = pick("closed pnl", "closedpnl", "realized pnl", "realised pnl", "pnl", "profit")
        entry = pick("entry price", "avg entry price", "avgentryprice", "open price")
        exitp = pick("exit price", "avg exit price", "avgexitprice", "close price")
        qty = pick("qty", "quantity", "size", "closed size", "filled qty")
        cts = pick("closed time", "updated time", "close time", "date", "time")
        if not sym:
            continue
        side = "Buy" if side_raw in ("buy", "long", "лонг") else "Sell" if side_raw in ("sell", "short", "шорт") else ""
        trades.append({
            "symbol": sym,
            "side": side,
            "qty": _f(qty),
            "entry": _f(entry),
            "exit": _f(exitp),
            "pnl": _f(pnl),
            "open_ts": None,
            "close_ts": _parse_ts(cts),
            "leverage": 1.0,
            "source": "csv",
        })
    return trades


def _parse_ts(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    s = s.strip()
    if s.isdigit():
        v = int(s)
        return v if v > 10**11 else v * 1000
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S UTC", "%d.%m.%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(time.mktime(time.strptime(s.replace(" UTC", ""), fmt.replace(" UTC", ""))) * 1000)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------- #
# Загрузка сделок
# ---------------------------------------------------------------------- #
async def load_trades(days: int = 90) -> dict:
    """Bybit API (если ключи есть) → иначе сохранённый CSV."""
    if settings.has_bybit_keys():
        closed = await fetch_closed_pnl(days=days)
        trades = normalize_closed_pnl(closed)
        return {"trades": trades, "source": "bybit_api", "count": len(trades)}

    if os.path.exists(CSV_PATH):
        with open(CSV_PATH, "r", encoding="utf-8") as f:
            trades = normalize_csv(f.read())
        return {"trades": trades, "source": "csv", "count": len(trades)}

    raise BybitAuthError(
        "Нет данных о сделках: введите read-only ключи Bybit в Настройках "
        "или загрузите CSV-экспорт на вкладке «Мои сделки»."
    )


def save_csv(text: str) -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CSV_PATH, "w", encoding="utf-8") as f:
        f.write(text)
    trades = normalize_csv(text)
    return {"saved": True, "count": len(trades)}


# ---------------------------------------------------------------------- #
# Статистика
# ---------------------------------------------------------------------- #
def compute_stats(trades: List[dict], days: int = 90) -> dict:
    if not trades:
        return {"empty": True, "count": 0}

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    total_pnl = sum(t["pnl"] for t in trades)
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))

    holds = []
    for t in trades:
        if t.get("open_ts") and t.get("close_ts") and t["close_ts"] > t["open_ts"]:
            holds.append((t["close_ts"] - t["open_ts"]) / 3600000.0)
    avg_hold = sum(holds) / len(holds) if holds else None

    by_symbol = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    for t in trades:
        s = by_symbol[t["symbol"]]
        s["trades"] += 1
        s["wins"] += 1 if t["pnl"] > 0 else 0
        s["pnl"] += t["pnl"]
    sym_rows = sorted(
        ([{"symbol": k, "trades": v["trades"],
           "winrate": round(100.0 * v["wins"] / v["trades"], 1),
           "pnl": round(v["pnl"], 2)} for k, v in by_symbol.items()]),
        key=lambda x: x["pnl"])

    # PnL по дням (для графика эквити)
    daily = defaultdict(float)
    for t in trades:
        ts = t.get("close_ts") or t.get("open_ts")
        if ts:
            day = time.strftime("%Y-%m-%d", time.gmtime(ts / 1000))
            daily[day] += t["pnl"]
    equity = []
    acc = 0.0
    for d in sorted(daily):
        acc += daily[d]
        equity.append({"time": d, "value": round(acc, 2)})

    # Поведенческие паттерны (простые эвристики, AI углубит)
    patterns = []
    if losses:
        avg_loss = sum(-t["pnl"] for t in losses) / len(losses)
        avg_win = (sum(t["pnl"] for t in wins) / len(wins)) if wins else 0
        if avg_win and avg_loss > avg_win * 1.5:
            patterns.append(f"Средний убыток ({avg_loss:.2f}) заметно больше среднего профита ({avg_win:.2f}) — похоже, убыточные сделки пересиживаются.")
    if sym_rows and sym_rows[0]["pnl"] < 0:
        patterns.append(f"Худшая монета: {sym_rows[0]['symbol']} ({sym_rows[0]['pnl']} USDT за период).")
    if len(trades) / max(days, 1) > settings.MAX_TRADES_PER_DAY:
        patterns.append("Среднее число сделок в день выше вашего лимита — возможна овертрейдинговость.")
    long_n = sum(1 for t in trades if t["side"] in ("Buy", "LONG"))
    short_n = sum(1 for t in trades if t["side"] in ("Sell", "SHORT"))
    if long_n + short_n:
        patterns.append(f"Баланс направлений: лонг {long_n} / шорт {short_n}.")

    best = max(trades, key=lambda t: t["pnl"])
    worst = min(trades, key=lambda t: t["pnl"])

    return {
        "empty": False,
        "count": len(trades),
        "days": days,
        "total_pnl": round(total_pnl, 2),
        "winrate": round(100.0 * len(wins) / len(trades), 1),
        "wins": len(wins),
        "losses": len(losses),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(-sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0,
        "avg_hold_hours": round(avg_hold, 2) if avg_hold else None,
        "per_day": round(len(trades) / max(days, 1), 1),
        "by_symbol": sym_rows,
        "equity": equity,
        "patterns": patterns,
        "best_trade": _brief(best),
        "worst_trade": _brief(worst),
    }


def _brief(t: dict) -> dict:
    return {
        "symbol": t["symbol"], "side": t["side"], "pnl": round(t["pnl"], 2),
        "entry": t["entry"], "exit": t["exit"],
        "closed": _fmt_ts(t.get("close_ts")),
    }


def _fmt_ts(ts: Optional[int]) -> Optional[str]:
    if not ts:
        return None
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts / 1000))


def recent_brief(trades: List[dict], n: int = 30) -> List[dict]:
    rows = sorted(trades, key=lambda t: t.get("close_ts") or 0, reverse=True)[:n]
    return [_brief(t) for t in rows]


# ---------------------------------------------------------------------- #
# Промпт для агента «Аналитик сделок»
# ---------------------------------------------------------------------- #
TRADE_ANALYST_SYSTEM = """Ты — 💼 Аналитик сделок в системе TradeVision Portal.

Тебе передали СТАТИСТИКУ и ПОСЛЕДНИЕ СДЕЛКИ реального торгового счёта пользователя на Bybit.
Твоя задача — холодный разбор: где человек зарабатывает и где систематически теряет,
какие повторяющиеся ошибки у него есть и что исправить в первую очередь.

ОТВЕЧАЙ СТРОГО JSON-объектом без пояснений:
{{
  "summary": "2-3 предложения: общая картина торговли пользователя",
  "strengths": ["что уже хорошо (1-3 пункта)"],
  "mistakes": ["конкретная повторяющаяся ошибка + на чём видно (3-6 пунктов)"],
  "recommendations": ["конкретное действие для исправления (3-6 пунктов)"],
  "watch_out": ["риск, который может убить депозит (1-2 пункта)"],
  "score": число 1..10 — общая дисциплина торговли,
  "focus_symbol": "тикер монеты, с которой стоит разобраться в первую очередь, или null"
}}

Правила:
- говори конкретно, на русском, без воды; опирайся на цифры из данных;
- если данных мало (мало сделок) — честно укажи это в summary;
- не выдумывай сделки, которых нет в данных.
"""


def build_trade_prompt(stats: dict, trades: List[dict]) -> List[dict]:
    import json
    slim = {k: v for k, v in stats.items() if k not in ("empty",)}
    payload = {
        "statistics": slim,
        "recent_trades": recent_brief(trades, 30),
        "rules": {
            "max_trades_per_day": settings.MAX_TRADES_PER_DAY,
            "min_tp_pct": settings.MIN_TP_PCT,
            "max_hold_hours": settings.MAX_HOLD_HOURS,
        },
    }
    return [
        {"role": "system", "content": TRADE_ANALYST_SYSTEM},
        {"role": "user", "content": "ДАННЫЕ ПО СЧЁТУ:\n" + json.dumps(payload, ensure_ascii=False) + "\n\nВыдай строгий JSON-разбор."},
    ]
