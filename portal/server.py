"""
TradeVision Portal — сервер (порт 3005, независим от основной платформы).

Запуск:  python portal/server.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

PORTAL_DIR = os.path.dirname(os.path.abspath(__file__))
if PORTAL_DIR not in sys.path:
    sys.path.insert(0, PORTAL_DIR)

from portal_config import settings, ensure_env_file, ENV_PATH          # noqa: E402
from council import (build_snapshot, get_verdict, view, consensus,     # noqa: E402
                     get_market, COUNCIL_ROLES, role_meta)              # noqa: E402
from llm_client import list_models, test_role, chat, LLMError          # noqa: E402
import trades_analytics as ta_mod                                      # noqa: E402
from bybit_account import BybitAuthError                               # noqa: E402
from live_engine import get_engine                                     # noqa: E402

STATIC_DIR = os.path.join(PORTAL_DIR, "static")

app = FastAPI(title="TradeVision Portal", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
async def _startup():
    ensure_env_file()
    os.makedirs(os.path.join(PORTAL_DIR, "data"), exist_ok=True)
    get_engine().start()
    print(f"[portal] TradeVision запущен → http://localhost:{settings.PORTAL_PORT}")


@app.on_event("shutdown")
async def _shutdown():
    await get_engine().stop()


# ---------------------------------------------------------------------- #
# Страница
# ---------------------------------------------------------------------- #
@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "demo_mode": settings.is_demo(),
        "bybit_keys": settings.has_bybit_keys(),
        "live": get_engine().state(),
    }


# ---------------------------------------------------------------------- #
# Данные рынка Bybit (публичные, без ключей)
# ---------------------------------------------------------------------- #
@app.get("/api/klines/{symbol}")
async def klines(symbol: str, interval: str = "5m", limit: int = 300):
    market = get_market()
    candles = await market.get_klines(symbol.upper(), interval, limit)
    from data.market_data import data_status
    st = data_status()
    rows = [{
        "time": int(c.ts / 1000),
        "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume,
    } for c in candles]
    return {
        "symbol": symbol.upper(), "interval": interval,
        "data_source": st.get("source"), "candles": rows,
        "empty": not bool(rows),
    }


@app.get("/api/tickers")
async def tickers(symbols: str = Query("")):
    market = get_market()
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()] or settings.watchlist
    rows = await market.get_tickers(syms)
    return {"tickers": rows}


@app.get("/api/market/status")
async def market_status():
    from data.market_data import data_status
    return data_status()


@app.post("/api/market/ping")
async def market_ping():
    market = get_market()
    return await market.ping()


@app.get("/api/symbols")
async def all_symbols():
    """Все монеты Bybit (для добавления в watchlist)."""
    market = get_market()
    rows = await market.get_all_tickers()
    return {"symbols": sorted([r["symbol"] for r in rows])}


# ---------------------------------------------------------------------- #
# Совет агентов (SSE-стрим)
# ---------------------------------------------------------------------- #
@app.get("/api/analyze/stream")
async def analyze_stream(symbol: str, timeframe: str = None):
    timeframe = timeframe or settings.DEFAULT_TIMEFRAME
    symbol = symbol.upper()

    async def gen():
        try:
            snap = await build_snapshot(symbol, timeframe)
            candles = snap.pop("candles", None)
            yield _ev("snapshot", {
                "symbol": symbol, "timeframe": timeframe,
                "price": snap.get("price"),
                "features": snap.get("features"),
                "strategy": {"name": snap.get("strategy"), "type": None},
                "strategy_auto": snap.get("strategy_auto"),
                "strategy_signal": snap.get("strategy_signal_obj"),
                "candles": len(candles) if candles else 0,
            })

            tasks = {asyncio.create_task(get_verdict(k, snap)): k for k in COUNCIL_ROLES}
            verdicts = {}
            pending = set(tasks.keys())
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for fut in done:
                    k = tasks[fut]
                    try:
                        v = fut.result()
                    except Exception as e:
                        v = {"side": "NEUTRAL", "confidence": 0.0, "reason": f"Ошибка: {e}",
                             "thinking": [], "risks": [], "error": True}
                    verdicts[k] = v
                    yield _ev("agent", view(k, v))

            cons = consensus(verdicts, snap.get("strategy_signal_obj"))
            yield _ev("consensus", {"consensus": cons})
            yield _ev("done", {})
        except Exception as e:
            yield _ev("error", {"message": str(e)[:300]})

    return StreamingResponse(gen(), media_type="text/event-stream")


def _ev(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------- #
# Мои сделки
# ---------------------------------------------------------------------- #
@app.get("/api/portfolio/overview")
async def portfolio_overview(days: int = 90, full: bool = False,
                             from_ts: int = 0, to_ts: int = 0):
    """full=true — полная перезагрузка (мимо кэша); from_ts/to_ts — ручной диапазон дат (мс);
    иначе — кэш + докачка только новых сделок."""
    try:
        data = await ta_mod.load_trades(days=days, force=full, from_ts=from_ts, to_ts=to_ts)
    except BybitAuthError as e:
        raise HTTPException(400, str(e))
    span = data.get("span_days", days)
    stats = ta_mod.compute_stats(data["trades"], days=span)
    return {
        "source": data["source"],
        "stats": stats,
        "recent": ta_mod.recent_brief(data["trades"], 25),
        "agents_enabled": settings.role_enabled("portfolio"),
        "cache": {"mode": data.get("cache", "csv" if data["source"] == "csv" else "full"),
                  "added": data.get("cache_added", 0),
                  "total": data.get("cache_total", data["count"])},
    }


# ---------------------------------------------------------------------- #
# «Моя стратегия» — правила, выведенные из ваших сделок (0 AI-токенов)
# ---------------------------------------------------------------------- #
@app.get("/api/portfolio/my-strategy")
async def my_strategy_get():
    import trade_context as tc
    ms = tc.load_my_strategy()
    if not ms:
        return {"empty": True,
                "hint": "Стратегия ещё не выведена — нажмите «🎯 Моя стратегия» на вкладке «Мои сделки»."}
    return ms


@app.post("/api/portfolio/my-strategy")
async def my_strategy_build(days: int = 90, from_ts: int = 0, to_ts: int = 0):
    """Выводит персональную стратегию из ДНК (нужна построенная ДНК за этот период)."""
    import trade_context as tc
    dna = tc.load_dna()
    if not dna:
        raise HTTPException(400, "Сначала постройте ДНК торговли (кнопка 🧬) — стратегия выводится из неё.")
    if not from_ts and dna.get("days") != days:
        raise HTTPException(400, f"ДНК построена за {dna.get('days')} дн., а выбрано {days} — "
                                 f"перестройте ДНК за этот период или выберите его снова.")
    try:
        data = await ta_mod.load_trades(days=days, from_ts=from_ts, to_ts=to_ts)
    except BybitAuthError as e:
        raise HTTPException(400, str(e))
    trades = data["trades"]
    if len(trades) < 5:
        raise HTTPException(400, f"Слишком мало сделок ({len(trades)}) для вывода стратегии — нужно хотя бы 5.")
    ms = tc.derive_my_strategy(dna, trades)
    tc.save_my_strategy(ms)
    return ms


@app.post("/api/portfolio/csv")
async def portfolio_csv(text: str = Body(..., media_type="text/plain")):
    if len(text) > 8_000_000:
        raise HTTPException(413, "Файл слишком большой (>8 МБ)")
    res = ta_mod.save_csv(text)
    if res["count"] == 0:
        res["hint"] = ("Сделки не распознаны. Нужен CSV из Bybit: Orders → Trade History "
                       "(или Position History) → Export. Убедитесь, что файл не Excel (.xlsx).")
    return res


@app.get("/api/portfolio/diagnose")
async def portfolio_diagnose():
    """Проверка: сеть до хостов Bybit + валидность read-only ключа."""
    from bybit_account import diagnose
    return await diagnose()


# ---------------------------------------------------------------------- #
# ДНК торговли + график сделки + AI-персона
# ---------------------------------------------------------------------- #
@app.get("/api/portfolio/dna")
async def portfolio_dna(days: int = 90, rebuild: bool = False,
                        from_ts: int = 0, to_ts: int = 0):
    import trade_context as tc
    if not rebuild:
        cached = tc.load_dna()
        if cached and cached.get("days") == days and not from_ts:
            return {"dna": cached, "cached": True}
    try:
        data = await ta_mod.load_trades(days=days, from_ts=from_ts, to_ts=to_ts)
    except BybitAuthError as e:
        raise HTTPException(400, str(e))
    trades = data["trades"]
    if not trades:
        raise HTTPException(400, "Сделок не найдено за этот период")

    recent = sorted(trades, key=lambda t: t.get("close_ts") or 0, reverse=True)[:tc.ENRICH_LIMIT]
    sem = asyncio.Semaphore(5)

    async def one(t):
        async with sem:
            try:
                return await tc.enrich_trade(t)
            except Exception:
                return None

    enriched = [r for r in await asyncio.gather(*(one(t) for t in recent)) if r]
    dna = tc.build_dna(trades, enriched, days)
    tc.save_dna(dna)
    return {"dna": dna, "cached": False}


@app.get("/api/portfolio/deal-chart")
async def portfolio_deal_chart(symbol: str, side: str, open_ts: int, close_ts: int):
    import trade_context as tc
    try:
        return await tc.deal_chart(symbol.upper(), side, open_ts, close_ts)
    except Exception as e:
        raise HTTPException(400, str(e)[:300])


@app.get("/api/portfolio/persona")
async def persona_get():
    import trade_context as tc
    return {"persona": tc.load_persona()}


@app.get("/api/portfolio/persona/build")
async def persona_build():
    """Каждый включённый агент анализирует ДНК → единая персона трейдера."""
    import trade_context as tc
    from llm_client import has_key, chat as llm_chat, parse_json_verdict
    from council import backend_roles

    dna = tc.load_dna()
    if not dna:
        raise HTTPException(400, "Сначала постройте ДНК своей торговли (кнопка «Построить ДНК»)")

    async def gen():
        preamble = tc.build_persona_preamble(dna)
        by_role = {}
        for role in COUNCIL_ROLES:
            meta = role_meta(role)
            if not settings.role_enabled(role):
                yield _ev("progress", {"role": role, "name": meta["name"], "status": "skipped"})
                continue
            if not has_key(role):
                yield _ev("progress", {"role": role, "name": meta["name"], "status": "no_key"})
                continue
            yield _ev("progress", {"role": role, "name": meta["name"], "status": "working"})
            r = backend_roles.ROLES.get(role)
            system = tc.PERSONA_SYSTEM.format(name=meta["name"], expertise=r.expertise if r else meta["name"])
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": "ДАННЫЕ О ТОРГОВЛЕ ПОЛЬЗОВАТЕЛЯ:\n" + preamble + "\n\nВыдай строгий JSON."},
            ]
            try:
                text = await llm_chat(role, messages, json_mode=True, max_tokens=900)
                verdict = parse_json_verdict(text)
                verdict["tokens_est"] = (sum(len(str(m["content"])) for m in messages) + len(text)) // 4
                by_role[role] = verdict
                yield _ev("progress", {"role": role, "name": meta["name"], "status": "done", "verdict": verdict})
            except Exception as e:
                yield _ev("progress", {"role": role, "name": meta["name"], "status": "error", "detail": str(e)[:160]})

        merged = {
            "essence": "",
            "strengths": [],
            "weaknesses": [],
            "signal_hints": [],
            "avoid": [],
        }
        for rk in ["strategist", "risk", "momentum", "contrarian", "macro"]:
            v = by_role.get(rk)
            if v and not merged["essence"] and v.get("essence"):
                merged["essence"] = v["essence"]
        for v in by_role.values():
            merged["strengths"] += [s for s in (v.get("strengths") or []) if s][:2]
            merged["weaknesses"] += [s for s in (v.get("weaknesses") or []) if s][:2]
            merged["signal_hints"] += [s for s in (v.get("signal_hints") or []) if s][:2]
            merged["avoid"] += [s for s in (v.get("avoid") or []) if s][:2]

        persona = {"built_at": int(time.time()), "by_role": by_role, "merged": merged}
        tc.save_persona(persona)
        yield _ev("persona", persona)
        yield _ev("done", {})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/portfolio/ai-review")
async def portfolio_ai_review(days: int = 90, from_ts: int = 0, to_ts: int = 0):
    if not settings.role_enabled("portfolio"):
        raise HTTPException(400, "Агент «Аналитик сделок» выключен в Настройках")
    from llm_client import chat as llm_chat, has_key, parse_json_verdict
    if not has_key("portfolio"):
        raise HTTPException(400, "Введите AI-ключ в Настройках, чтобы аналитик сделок заработал")
    try:
        data = await ta_mod.load_trades(days=days, from_ts=from_ts, to_ts=to_ts)
    except BybitAuthError as e:
        raise HTTPException(400, str(e))
    stats = ta_mod.compute_stats(data["trades"], days=data.get("span_days", days))
    if stats.get("empty"):
        raise HTTPException(400, "Сделок не найдено за этот период")

    async def gen():
        messages = ta_mod.build_trade_prompt(stats, data["trades"])
        yield _ev("status", {"text": "Аналитик разбирает вашу историю сделок…"})
        try:
            text = await llm_chat("portfolio", messages, json_mode=True, max_tokens=1600)
            review = parse_json_verdict(text)
            review["model"] = settings.role_model("portfolio")["model"]
            review["tokens_est"] = (sum(len(str(m["content"])) for m in messages) + len(text)) // 4
            yield _ev("review", review)
        except Exception as e:
            yield _ev("error", {"message": str(e)[:300]})
        yield _ev("done", {})

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------------------------------------------------------------- #
# Настройки
# ---------------------------------------------------------------------- #
@app.get("/api/config")
async def get_config():
    cfg = settings.public_config()
    cfg["role_meta"] = {r: role_meta(r) for r in settings.ROLE_KEYS}
    cfg["env_path"] = ENV_PATH
    return cfg


class ConfigUpdate(BaseModel):
    updates: dict


@app.post("/api/config")
async def update_config(body: ConfigUpdate):
    applied = settings.apply_update(body.updates or {})
    return {"applied": list(applied.keys()), "config": settings.public_config()}


@app.get("/api/config/test")
async def test_roles(role: str = "all"):
    roles = settings.ROLE_KEYS if role == "all" else [role]
    results = {}
    for r in roles:
        results[r] = await test_role(r)
    return {"results": results}


@app.get("/api/models")
async def models():
    try:
        ids = await list_models()
        return {"models": ids}
    except LLMError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Не удалось получить список моделей: {str(e)[:200]}")


# ---------------------------------------------------------------------- #
# Live
# ---------------------------------------------------------------------- #
@app.get("/api/live/state")
async def live_state():
    eng = get_engine()
    return {"state": eng.state(), "feed": eng.feed[-30:], "prices": eng.prices}


@app.get("/api/signals/learned")
async def signals_learned():
    """Самообучение сканера: правила по монетам (из исходов live-сигналов)."""
    import signal_learner as L
    rules = L.fresh_rules() or L.compute_rules()
    return {"rules": rules, "outcomes": len(L.read_outcomes()),
            "thresholds": {"min_sample": L.MIN_SAMPLE, "hard_sample": L.HARD_SAMPLE,
                           "tighten_wr": L.TIGHTEN_WR, "boost_wr": L.BOOST_WR, "block_wr": L.BLOCK_WR}}


@app.post("/api/signals/learned/reset")
async def signals_learned_reset(payload: dict = Body(default={})):
    import signal_learner as L
    return L.reset((payload.get("symbol") or "").strip().upper() or None)


@app.post("/api/signals/backtest")
async def signals_backtest(payload: dict = Body(default={})):
    """Бэктест live-сканера: те же монеты/ТФ/правила риска, только на истории.
    Body: {symbols: [...], tfs: [...], days: N}. 0 AI-токенов."""
    from signal_backtest import run_signal_backtest, BacktestError
    symbols = payload.get("symbols") or settings.watchlist
    tfs = payload.get("tfs") or settings.signal_timeframes
    days = int(payload.get("days") or 7)
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",") if s.strip()]
    if isinstance(tfs, str):
        tfs = [t.strip() for t in tfs.split(",") if t.strip()]
    days = max(1, min(days, 180))
    try:
        return await run_signal_backtest(symbols, tfs, days)
    except BacktestError as e:
        raise HTTPException(400, str(e))


@app.websocket("/ws")
async def ws(ws: WebSocket):
    eng = get_engine()
    await eng.add(ws)
    try:
        while True:
            # держим соединение; клиент может слать ping-текст
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        eng.drop(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.PORTAL_HOST, port=settings.PORTAL_PORT, log_level="info")
