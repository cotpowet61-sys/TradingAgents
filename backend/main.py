"""
TradingAgents — мультиагентная платформа дэйтрейдинга.

Основные возможности:
  * Стратегии (встроенные + настраиваемые параметры)
  * Бэктест любой стратегии на исторических данных (Bybit / синтетика)
  * Мультиагентный анализ (5 AI-ролей) и live-сигналы по WebSocket
  * Правила: <=30 сделок/день, TP>=2%, удержание<=4ч, лонг и шорт

Запуск:  python main.py   (или uvicorn main:app --host 0.0.0.0 --port 3001)
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List

import uvicorn

from config import settings
from data.market_data import MarketData
from strategies.registry import build_strategy, STRATEGY_SCHEMAS
from backtest.engine import run_backtest
from agents.orchestrator import get_orchestrator
from live.engine import get_live_engine


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
async def tickers(symbols: Optional[str] = None):
    syms = [s.strip() for s in symbols.split(",")] if symbols else None
    data = await market.get_tickers(syms)
    return {"tickers": data}


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


@app.post("/api/backtest")
async def backtest(req: BacktestRequest):
    strategy = build_strategy(req.strategy, req.params, req.tp_pct, req.sl_pct, req.max_hold_hours)
    if strategy is None:
        raise HTTPException(404, f"Стратегия '{req.strategy}' не найдена")
    candles = await market.get_klines(req.symbol, req.timeframe, req.limit)
    if not candles:
        raise HTTPException(400, "Нет данных для бэктеста")
    result = run_backtest(strategy, candles, req.timeframe, req.max_hold_hours)
    result.symbol = req.symbol
    out = result.to_dict()
    out["tp_pct"] = strategy.tp_pct
    out["sl_pct"] = strategy.sl_pct
    out["max_hold_hours"] = strategy.max_hold_hours
    return out


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
