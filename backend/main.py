"""
TradingAgents — мультиагентная платформа дэйтрейдинга.

Основные возможности:
  * Стратегии (встроенные + настраиваемые параметры)
  * Бэктест любой стратегии на исторических данных (Bybit / синтетика)
  * Мультиагентный анализ (5 AI-ролей) и live-сигналы по WebSocket
  * Правила: <=30 сделок/день, TP>=2%, удержание<=4ч, лонг и шорт

Запуск:  python main.py   (или uvicorn main:app --host 0.0.0.0 --port 3002)
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List

import uvicorn

from config import settings, ENV_PATH, ENV_EXAMPLE_PATH
from data.market_data import MarketData
from strategies.registry import build_strategy, STRATEGY_SCHEMAS
from strategies.selector import get_selector
from backtest.engine import run_backtest
from agents.orchestrator import get_orchestrator
from agents import roles
from live.engine import get_live_engine
from llm.client import get_client


# ---------------------------------------------------------------------- #
# Жизненный цикл
# ---------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    live = get_live_engine()
    await live.start()
    yield
    await live.stop()


app = FastAPI(title="TradingAgents", version="2.0.0", lifespan=lifespan)

market = MarketData()


# ---------------------------------------------------------------------- #
# Статика (frontend) — обслуживается тем же сервером, что и API.
# Если frontend раздаёт отдельный nginx (docker-compose), эти пути пропускаются.
# ---------------------------------------------------------------------- #
_FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
_FRONTEND_AVAILABLE = os.path.isdir(_FRONTEND_DIR)


@app.get("/")
async def root():
    if _FRONTEND_AVAILABLE:
        return FileResponse(os.path.join(_FRONTEND_DIR, "index.html"))
    return JSONResponse({"name": "TradingAgents API", "docs": "/docs", "frontend": "на порту 8080 (nginx)"})


if _FRONTEND_AVAILABLE:
    app.mount("/css", StaticFiles(directory=os.path.join(_FRONTEND_DIR, "css")), name="css")
    app.mount("/js", StaticFiles(directory=os.path.join(_FRONTEND_DIR, "js")), name="js")


# ---------------------------------------------------------------------- #
# Справочные данные
# ---------------------------------------------------------------------- #
@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "version": app.version,
        "demo_mode": settings.is_demo(),
        "symbols": settings.symbols,
        "timeframe": settings.DEFAULT_TIMEFRAME,
        "rules": {
            "max_trades_per_day": settings.MAX_TRADES_PER_DAY,
            "min_tp_pct": settings.MIN_TP_PCT,
            "max_hold_hours": settings.MAX_HOLD_HOURS,
            "default_sl_pct": settings.DEFAULT_SL_PCT,
        },
    }


@app.get("/api/strategies")
async def list_strategies():
    return {"strategies": STRATEGY_SCHEMAS()}


@app.get("/api/symbols")
async def list_symbols():
    return {"symbols": settings.symbols, "timeframes": ["1m", "3m", "5m", "15m", "30m", "1h", "4h"]}


# ---------------------------------------------------------------------- #
# Рынок
# ---------------------------------------------------------------------- #
@app.get("/api/tickers")
async def tickers(symbols: Optional[str] = None, all: bool = False):
    # all=true — все spot-монеты с суточным объёмом >= MIN_DAILY_VOLUME_USD
    if all:
        data = await market.get_all_tickers()
        return {"tickers": data, "count": len(data),
                "min_volume_usd": settings.MIN_DAILY_VOLUME_USD}
    syms = [s.strip() for s in symbols.split(",")] if symbols else None
    data = await market.get_tickers(syms)
    return {"tickers": data}


@app.get("/api/market/symbols")
async def market_symbols(min_volume_usd: Optional[float] = None):
    """Все доступные монеты (по суточному объёму). Для выпадающих списков."""
    data = await market.get_all_tickers(min_volume_usd)
    return {"symbols": [t["symbol"] for t in data], "count": len(data),
            "min_volume_usd": min_volume_usd or settings.MIN_DAILY_VOLUME_USD}


@app.get("/api/klines/{symbol}")
async def klines(symbol: str, interval: str = Query(default=None), limit: int = Query(default=300)):
    candles = await market.get_klines(symbol, interval, limit)
    return {"symbol": symbol, "interval": interval or settings.DEFAULT_TIMEFRAME,
            "candles": [c.to_dict() for c in candles]}


# ---------------------------------------------------------------------- #
# Стратегии / бэктест
# ---------------------------------------------------------------------- #
class BacktestRequest(BaseModel):
    symbol: str
    strategy: str = "momentum_breakout"
    timeframe: str = "5m"
    limit: int = 500
    params: Optional[dict] = None
    tp_pct: Optional[float] = None
    sl_pct: Optional[float] = None
    max_hold_hours: Optional[float] = None
    # Диапазон дат (необязательно). Формат "YYYY-MM-DD".
    # Если заданы — используются вместо limit.
    start_date: Optional[str] = None
    end_date: Optional[str] = None


def _parse_date(s: Optional[str]) -> Optional[int]:
    """'YYYY-MM-DD' -> unix ms. Возвращает None, если строка пустая/некорректная."""
    if not s:
        return None
    try:
        d = datetime.datetime.fromisoformat(s.strip())
        return int(d.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


@app.post("/api/backtest")
async def backtest(req: BacktestRequest):
    strategy = build_strategy(req.strategy, req.params, req.tp_pct, req.sl_pct, req.max_hold_hours)
    if strategy is None:
        raise HTTPException(404, f"Стратегия '{req.strategy}' не найдена")

    start_ts = _parse_date(req.start_date)
    end_ts = _parse_date(req.end_date)
    if end_ts:
        end_ts += 86_400_000 - 1  # включить весь конечный день

    candles = await market.get_klines(req.symbol, req.timeframe, req.limit, start_ts, end_ts)
    if not candles:
        raise HTTPException(400, "Нет данных для бэктеста в заданном диапазоне")

    result = run_backtest(strategy, candles, req.timeframe, req.max_hold_hours)
    result.symbol = req.symbol
    out = result.to_dict()
    out["tp_pct"] = strategy.tp_pct
    out["sl_pct"] = strategy.sl_pct
    out["max_hold_hours"] = strategy.max_hold_hours
    out["start_date"] = req.start_date
    out["end_date"] = req.end_date
    # Свечи для отрисовки сделок на графике (не более 400 — для скорости)
    chart_candles = candles[-400:]
    out["candles"] = [c.to_dict() for c in chart_candles]
    # Сдвигаем индексы сделок под обрезанный массив свечей
    offset = len(candles) - len(chart_candles)
    for t in out["trades"]:
        t["chart_entry_index"] = t["entry_index"] - offset
        t["chart_exit_index"] = t["exit_index"] - offset
    return out


# ---------------------------------------------------------------------- #
# Оптимизация: прогон всех стратегий по символу, выбор лучших
# ---------------------------------------------------------------------- #
class OptimizeRequest(BaseModel):
    symbol: str
    timeframe: str = "5m"
    limit: int = 500
    min_trades: int = 10
    min_win_rate: float = 55.0
    top_n: int = 5
    start_date: Optional[str] = None
    end_date: Optional[str] = None


@app.post("/api/optimize")
async def optimize(req: OptimizeRequest):
    selector = get_selector()
    start_ts = _parse_date(req.start_date)
    end_ts = _parse_date(req.end_date)
    if end_ts:
        end_ts += 86_400_000 - 1
    return await selector.optimize(req.symbol, req.timeframe, req.limit,
                                   req.min_trades, req.min_win_rate, req.top_n,
                                   start_ts=start_ts, end_ts=end_ts)


@app.get("/api/optimize/profiles")
async def optimize_profiles():
    selector = get_selector()
    return {"profiles": selector.all_profiles()}


# ---------------------------------------------------------------------- #
# Мультиагентный анализ / live
# ---------------------------------------------------------------------- #
class AnalyzeRequest(BaseModel):
    symbol: str
    strategy: str = "momentum_breakout"
    timeframe: str = "5m"
    params: Optional[dict] = None


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    orch = get_orchestrator()
    return await orch.analyze(req.symbol, req.strategy, req.timeframe, req.params)


class ScanRequest(BaseModel):
    symbols: Optional[List[str]] = None
    strategy: str = "momentum_breakout"
    timeframe: Optional[str] = None


@app.post("/api/scan")
async def scan(req: Optional[ScanRequest] = None,
               symbols: Optional[List[str]] = None,
               strategy: str = "momentum_breakout",
               timeframe: str = None):
    # поддержка и query-параметров, и JSON-тела
    if req is not None:
        symbols = req.symbols if req.symbols else symbols
        strategy = req.strategy
        timeframe = req.timeframe or timeframe
    orch = get_orchestrator()
    results = await orch.scan_market(symbols, strategy, timeframe)
    return {"results": results}


@app.get("/api/live/state")
async def live_state():
    return get_live_engine().state()


# ---------------------------------------------------------------------- #
# Конфигурация (ключи/модели/правила) — редактирование из браузера
# ---------------------------------------------------------------------- #
@app.get("/api/config")
async def get_config():
    return {
        **settings.public_config(),
        "env_path": ENV_PATH,
        "env_exists": os.path.exists(ENV_PATH),
    }


class ConfigUpdate(BaseModel):
    updates: dict


@app.post("/api/config")
async def update_config(body: ConfigUpdate):
    try:
        applied = settings.apply_update(body.updates)
        return {"ok": True, "applied": list(applied.keys()), "config": settings.public_config()}
    except Exception as e:
        raise HTTPException(400, f"Ошибка сохранения: {e}")


@app.post("/api/config/test")
async def test_models(role: Optional[str] = Query(default=None)):
    """Проверка доступности моделей. role='all' или конкретная роль."""
    client = get_client()
    if role and role != "all":
        if role not in settings.ROLE_KEYS:
            raise HTTPException(404, f"Неизвестная роль: {role}")
        return {"results": {role: await client.test(role)}}
    results = {}
    for r in settings.ROLE_KEYS:
        results[r] = await client.test(r)
    return {"results": results, "demo_mode": settings.is_demo()}


# ---------------------------------------------------------------------- #
# Стриминг-анализ (SSE) — «ветки размышлений» агентов в реальном времени
# ---------------------------------------------------------------------- #
@app.get("/api/analyze/stream")
async def analyze_stream(symbol: str, strategy: str = "momentum_breakout",
                         timeframe: str = None):
    orch = get_orchestrator()

    async def gen():
        try:
            async for event, data in orch.analyze_stream(symbol, strategy, timeframe):
                yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'message': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------- #
# WebSocket — трансляция событий (сигналы, системные сообщения)
# ---------------------------------------------------------------------- #
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    live = get_live_engine()
    q = live.subscribe()

    # Приветствие + текущее состояние
    await websocket.send_json({"type": "hello", "state": live.state()})
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=15.0)
                await websocket.send_json(event)
            except asyncio.TimeoutError:
                # пинг для поддержания соединения
                await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        live.unsubscribe(q)


if __name__ == "__main__":
    uvicorn.run(app, host=settings.HOST, port=settings.PORT, log_level="info")
