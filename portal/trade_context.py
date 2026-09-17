"""
Персональный движок: полный контекст каждой сделки + «ДНК торговли» +
персонализация сигналов. Вся математика — локально (0 AI-токенов).

Для каждой сделки считаем (на свечах Bybit вокруг неё):
  - состояние индикаторов в момент входа (RSI, ATR, объём, тренд EMA21/55);
  - MAE/MFE — максимальные ходы цены ПРОТИВ и В ПОЛЬЗУ за время удержания;
  - R-мультик (pnl в долях дефолтного стопа), capture — сколько
    благоприятного движения реально забрано;
  - «вход по фитилю» — процентиль цены входа в диапазоне предыдущих 20 баров.

Всё схлопывается в ДНК: условные winrate по монете/стороне/удержанию/сессии/
RSI-зоне/режиму → из них вычисляется fit-оценка каждого live-сигнала.
"""
from __future__ import annotations

import json
import math
import os
import time
from collections import defaultdict
from typing import List, Optional

from portal_config import DATA_DIR, settings
from council import get_market
from strategies import indicators as ta

DNA_PATH = os.path.join(DATA_DIR, "trader_dna.json")
PERSONA_PATH = os.path.join(DATA_DIR, "persona.json")
ENRICH_LIMIT = 60          # сколько последних сделок обогащаем свечным контекстом
MARGIN_BEFORE = 24         # баров до входа (контекст графика в модалке)
MARGIN_AFTER = 8           # баров после выхода


def _f(x, d=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


# ---------------------------------------------------------------------- #
# Выбор таймфрейма под длительность сделки
# ---------------------------------------------------------------------- #
_TF_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
          "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def _tf_for(hold_ms: Optional[float]) -> str:
    h = hold_ms or 0
    if h < 45 * 60_000:
        return "5m"
    if h < 6 * 3600_000:
        return "15m"
    if h < 2 * 86_400_000:
        return "1h"
    return "4h"


def _rsi_band(rsi: float) -> str:
    if rsi < 30: return "<30 (перепроданность)"
    if rsi < 45: return "30-45"
    if rsi < 56: return "45-55 (нейтрал)"
    if rsi < 70: return "55-70"
    return ">70 (перекупленность)"


def _session_bucket(ts_ms: int) -> str:
    hour = int(time.strftime("%H", time.gmtime(ts_ms / 1000)))
    if hour < 6:
        return "ночь (00-06 UTC)"
    if hour < 12:
        return "утро (06-12 UTC)"
    if hour < 18:
        return "день (12-18 UTC)"
    return "вечер (18-24 UTC)"


def _ema(vals: List[float], period: int) -> float:
    if not vals:
        return 0.0
    k = 2 / (period + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def _regime(closes: List[float]) -> str:
    if len(closes) < 56:
        return "неизвестно"
    fast = _ema(closes[-60:], 21)
    slow = _ema(closes[-60:], 55)
    mad = abs(fast - slow) / closes[-1] * 100 if closes[-1] else 0
    if mad < 0.25:
        return "флэт/пила"
    return "тренд-лонг" if fast > slow else "тренд-шорт"


# ---------------------------------------------------------------------- #
# Обогащение одной сделки
# ---------------------------------------------------------------------- #
async def enrich_trade(t: dict) -> Optional[dict]:
    """Свечной контекст сделки. Возвращает dict или None (нет данных/времени)."""
    open_ts, close_ts = t.get("open_ts"), t.get("close_ts")
    if not open_ts or not close_ts or close_ts <= open_ts:
        return None
    tf = _tf_for(close_ts - open_ts)
    bar = _TF_MS[tf]
    start = open_ts - MARGIN_BEFORE * bar
    end = close_ts + MARGIN_AFTER * bar

    market = get_market()
    candles = await market.get_klines(t["symbol"], tf, 500, int(start), int(end))
    if not candles or len(candles) < 10:
        return None

    # индексы входа/выхода: ближайшие бары
    ei = min(range(len(candles)), key=lambda i: abs(candles[i].ts - open_ts))
    xi = min(range(len(candles)), key=lambda i: abs(candles[i].ts - close_ts))
    if xi < ei:
        ei, xi = xi, ei
    xi = max(xi, ei)

    entry = t.get("entry") or candles[ei].close
    # Санити-чек: цена сделки должна сходиться с графиком (иначе график —
    # учебные данные/другой источник и вся статистика будет мусором).
    ref = candles[ei].close
    if entry and ref and abs(entry - ref) / ref > 0.25:
        return None
    side = "long" if t.get("side", "").lower() in ("buy", "long") else "short"

    # индикаторы в момент входа (срез истории ДО входа)
    pre = candles[: ei + 1]
    summ = ta.compute_feature_summary(pre) or {}
    closes = [c.close for c in pre]
    regime = _regime(closes)

    # процентиль входа в диапазоне предыдущих 20 баров
    win = candles[max(0, ei - 20):ei + 1]
    lo = min(c.low for c in win)
    hi = max(c.high for c in win)
    entry_pctile = ((entry - lo) / (hi - lo) * 100) if hi > lo else 50.0

    # MAE / MFE по свечам удержания
    hold = candles[ei: xi + 1]
    if side == "long":
        mae = (min(c.low for c in hold) - entry) / entry * 100
        mfe = (max(c.high for c in hold) - entry) / entry * 100
    else:
        mae = (entry - max(c.high for c in hold)) / entry * 100
        mfe = (entry - min(c.low for c in hold)) / entry * 100
    pnl_pct = ((t["exit"] - entry) / entry * 100 if side == "long"
               else (entry - t["exit"]) / entry * 100) if t.get("exit") else 0.0
    capture = (pnl_pct / mfe * 100) if mfe > 0.05 else None

    sl_ref = settings.DEFAULT_SL_PCT or 1.0
    r_mult = pnl_pct / sl_ref

    return {
        "symbol": t["symbol"], "side": side, "tf": tf,
        "open_ts": open_ts, "close_ts": close_ts,
        "entry": entry, "exit": t.get("exit"),
        "pnl": round(t.get("pnl", 0.0), 4),
        "pnl_pct": round(pnl_pct, 3),
        "r_multiple": round(r_mult, 2),
        "mae_pct": round(mae, 3), "mfe_pct": round(mfe, 3),
        "mfe_capture": round(capture, 1) if capture is not None else None,
        "rsi": summ.get("rsi"), "atr_pct": summ.get("atr_pct"),
        "volume_ratio": summ.get("volume_ratio"),
        "rsi_band": _rsi_band(_f(summ.get("rsi"), 50)),
        "regime": regime,
        "entry_pctile": round(entry_pctile, 1),
        "session": _session_bucket(open_ts),
        "hold_hours": round((close_ts - open_ts) / 3600_000, 2),
    }


# ---------------------------------------------------------------------- #
# ДНК торговли
# ---------------------------------------------------------------------- #
def _agg(rows: List[dict]) -> dict:
    if not rows:
        return {"trades": 0}
    wins = [r for r in rows if r.get("pnl", 0) > 0]
    n = len(rows)
    return {
        "trades": n,
        "winrate": round(100 * len(wins) / n, 1),
        "pnl": round(sum(r.get("pnl", 0) for r in rows), 2),
        # «эффективный» winrate с поправкой на малые выборки (для fit-оценки)
        "wr_eff": round(100 * (len(wins) + 2) / (n + 4), 1),
    }


def _group(rows: List[dict], key_fn) -> dict:
    g = defaultdict(list)
    for r in rows:
        g[key_fn(r)].append(r)
    return {k: _agg(v) for k, v in sorted(g.items())}


def build_dna(trades: List[dict], enriched: List[dict], days: int) -> dict:
    by_symbol = _group(trades, lambda t: t["symbol"])
    side_map = {"Buy": "лонг", "Sell": "шорт", "long": "лонг", "short": "шорт"}
    by_side = _group(trades, lambda t: side_map.get(t.get("side", ""), "неизвестно"))
    by_hold = _group(trades, lambda t: (
        "<15 мин" if (t.get("close_ts") or 0) - (t.get("open_ts") or 0) < 900_000 else
        "15-60 мин" if (t.get("close_ts") or 0) - (t.get("open_ts") or 0) < 3600_000 else
        "1-4 ч" if (t.get("close_ts") or 0) - (t.get("open_ts") or 0) < 14_400_000 else ">4 ч"))

    by_rsi = _group(enriched, lambda r: r["rsi_band"])
    by_regime = _group(enriched, lambda r: r["regime"])
    by_session = _group(enriched, lambda r: r["session"])

    winners = [r for r in enriched if r["pnl"] > 0]
    losers = [r for r in enriched if r["pnl"] < 0]
    stop_q = {
        "avg_mae_winners": round(sum(-r["mae_pct"] for r in winners) / len(winners), 2) if winners else None,
        "avg_mae_losers": round(sum(-r["mae_pct"] for r in losers) / len(losers), 2) if losers else None,
        "avg_capture": round(sum(r["mfe_capture"] for r in winners if r["mfe_capture"] is not None) /
                             max(1, len([r for r in winners if r["mfe_capture"] is not None])), 1) if winners else None,
        "avg_entry_pctile_winners": round(sum(r["entry_pctile"] for r in winners) / len(winners), 1) if winners else None,
        "avg_entry_pctile_losers": round(sum(r["entry_pctile"] for r in losers) / len(losers), 1) if losers else None,
    }

    # поведенческие флаги
    behavior = {}
    ordered = sorted(trades, key=lambda t: t.get("close_ts") or 0)
    revenge = 0
    revenge_wins = 0
    for i in range(1, len(ordered)):
        prev, cur = ordered[i - 1], ordered[i]
        if prev.get("pnl", 0) < 0 and cur.get("open_ts") and prev.get("close_ts") and \
                0 < cur["open_ts"] - prev["close_ts"] < 30 * 60_000:
            revenge += 1
            revenge_wins += 1 if cur.get("pnl", 0) > 0 else 0
    behavior["revenge_trades"] = revenge
    behavior["revenge_winrate"] = round(100 * revenge_wins / revenge, 1) if revenge else None

    # сильные/слабые зоны (где выборка приличная)
    strengths, weaknesses = [], []
    def _zones(table, label):
        for k, v in table.items():
            if v.get("trades", 0) >= 3:
                (strengths if v["winrate"] >= 60 and v["pnl"] > 0 else
                 weaknesses if v["winrate"] < 45 or v["pnl"] < 0 else []).append(
                    {"zone": f"{label}: {k}", "winrate": v["winrate"], "pnl": v["pnl"], "trades": v["trades"]})
    _zones(by_symbol, "монета")
    _zones(by_side, "сторона")
    _zones(by_hold, "удержание")
    _zones(by_rsi, "RSI при входе")
    _zones(by_regime, "режим")
    strengths.sort(key=lambda z: -z["pnl"])
    weaknesses.sort(key=lambda z: z["pnl"])

    return {
        "built_at": int(time.time()),
        "days": days,
        "total_trades": len(trades),
        "enriched": len(enriched),
        "by_symbol": by_symbol,
        "by_side": by_side,
        "by_hold": by_hold,
        "by_rsi": by_rsi,
        "by_regime": by_regime,
        "by_session": by_session,
        "stop_quality": stop_q,
        "behavior": behavior,
        "strengths": strengths[:6],
        "weaknesses": weaknesses[:6],
        "deals": enriched,           # брифы последних сделок (для маркеров/модалки)
    }


# ---------------------------------------------------------------------- #
# Fit-оценка сигнала под ДНК (0 токенов)
# ---------------------------------------------------------------------- #
def fit_score(dna: Optional[dict], symbol: str, side: str, rsi: Optional[float] = None,
              regime: Optional[str] = None) -> dict:
    if not dna:
        return {"score": None, "warnings": ["профиль ещё не построен — откройте «Мои сделки» и нажмите «Построить ДНК»"]}

    warnings, notes = [], []
    parts, weights = [], []

    def _part(table, key, label, w):
        v = (table or {}).get(key)
        if not v or v.get("trades", 0) < 3:
            return
        wr = v.get("wr_eff", 50)
        parts.append(wr); weights.append(w)
        if wr < 42:
            warnings.append(f"{label} «{key}» у вас исторически в минусе (winrate {v['winrate']}%, {v['trades']} сделок)")
        elif wr > 62:
            notes.append(f"{label} «{key}» — ваша сильная зона ({v['winrate']}%)")

    _part(dna.get("by_symbol"), symbol, "монета", 3.0)
    _part(dna.get("by_side"), "лонг" if side == "LONG" else "шорт", "сторона", 1.5)
    if rsi is not None:
        _part(dna.get("by_rsi"), _rsi_band(rsi), "RSI-зона входа", 2.0)
    if regime:
        _part(dna.get("by_regime"), regime, "режим рынка", 1.5)

    if not parts:
        return {"score": None, "warnings": warnings or ["недостаточно данных для оценки"]}

    score = sum(p * w for p, w in zip(parts, weights)) / sum(weights)
    score = max(1, min(99, round(score)))
    return {"score": score, "warnings": warnings, "notes": notes}


# ---------------------------------------------------------------------- #
# Персистентность
# ---------------------------------------------------------------------- #
def save_dna(dna: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DNA_PATH, "w", encoding="utf-8") as f:
        json.dump(dna, f, ensure_ascii=False)


def load_dna() -> Optional[dict]:
    try:
        with open(DNA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def dna_mtime() -> float:
    try:
        return os.path.getmtime(DNA_PATH)
    except OSError:
        return 0.0


def save_persona(persona: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PERSONA_PATH, "w", encoding="utf-8") as f:
        json.dump(persona, f, ensure_ascii=False)


def load_persona() -> Optional[dict]:
    try:
        with open(PERSONA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------------- #
# Данные для графика сделки (модалка)
# ---------------------------------------------------------------------- #
async def deal_chart(symbol: str, side: str, open_ts: int, close_ts: int) -> dict:
    tf = _tf_for(close_ts - open_ts)
    bar = _TF_MS[tf]
    market = get_market()
    candles = await market.get_klines(symbol, tf, 500, int(open_ts - MARGIN_BEFORE * bar),
                                      int(close_ts + MARGIN_AFTER * bar))
    if not candles:
        raise ValueError("Нет свечей для этого периода (Bybit вернул пусто)")
    ei = min(range(len(candles)), key=lambda i: abs(candles[i].ts - open_ts))
    xi = min(range(len(candles)), key=lambda i: abs(candles[i].ts - close_ts))
    xi = max(xi, ei)
    entry = candles[ei].open
    hold = candles[ei:xi + 1]
    # Санити-чек: если передали «левую» цену (график учебный) — честно скажем
    ref_line = sum(c.close for c in candles[max(0, ei - 5):ei + 1]) / min(ei + 1, 5)
    if entry and ref_line and abs(entry - ref_line) / ref_line > 5.0:
        raise ValueError("цена входа сделки не сходится с графиком — вероятно, график из учебных данных (Bybit недоступен).")
    is_long = side.lower() in ("buy", "long")
    if is_long:
        mae_px = min(c.low for c in hold)
        mfe_px = max(c.high for c in hold)
    else:
        mae_px = max(c.high for c in hold)
        mfe_px = min(c.low for c in hold)
    pre = candles[:ei + 1]
    summ = ta.compute_feature_summary(pre) or {}
    return {
        "tf": tf,
        "candles": [{"time": int(c.ts / 1000), "open": c.open, "high": c.high,
                     "low": c.low, "close": c.close, "volume": c.volume} for c in candles],
        "entry_idx": ei, "exit_idx": xi,
        "entry_px": entry, "exit_px": candles[xi].close,
        "mae_px": mae_px, "mfe_px": mfe_px,
        "indicators": summ,
        "regime": _regime([c.close for c in pre]),
    }


# ---------------------------------------------------------------------- #
# Промпт AI-персоны (каждый агент видит всю картину)
# ---------------------------------------------------------------------- #
PERSONA_SYSTEM = """Ты — {name} в системе TradeVision Portal. Экспертиза: {expertise}.

Пользователь дал тебе ПОЛНУЮ статистику своей реальной торговли (ДНК торговли):
условные winrate по монетам/сторонам/удержанию/RSI-зонам/режимам рынка/сессиям,
качество стопов и фиксации профита (MAE/MFE), поведенческие флаги, а также
худшие и лучшие сделки с контекстом графика в момент входа.

Твоя задача — понять СУТЬ торговли этого человека именно со своей позиции и
выдать строгий JSON:
{{
  "essence": "2-3 предложения: кем является этот трейдер (стиль, характер)",
  "strengths": ["сильная сторона (1-3)"],
  "weaknesses": ["слабость, приводящая к потере денег (2-4)"],
  "signal_hints": ["какому ТИПУ сигналов с этого движка доверять можно, а какой для него опасен (2-4)"],
  "avoid": ["конкретная зона, которую лучше не трогать (1-3)"]
}}

Правила: конкретно и на русском; только выводы из данных; не выдумывай сделки.
"""


def build_persona_preamble(dna: dict) -> str:
    import json as _json
    slim = {k: v for k, v in dna.items() if k != "deals"}
    deals = dna.get("deals", [])
    worst = sorted(deals, key=lambda r: r["pnl"])[:5]
    best = sorted(deals, key=lambda r: -r["pnl"])[:5]
    payload = {
        "dna": slim,
        "worst_deals": worst,
        "best_deals": best,
    }
    return _json.dumps(payload, ensure_ascii=False)
