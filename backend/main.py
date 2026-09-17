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
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List

import uvicorn

from config import settings, ENV_PATH, ENV_EXAMPLE_PATH
from data.market_data import MarketData, data_status
from strategies.registry import build_strategy, STRATEGY_SCHEMAS
from strategies.selector import get_selector
from backtest.engine import run_backtest
from agents.orchestrator import get_orchestrator
from agents import roles
from live.engine import get_live_engine
from live.single import get_single_engine
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
    await get_single_engine().stop()


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
            "data_source": data_status()["source"],
            "candles": [c.to_dict() for c in candles]}


@app.get("/api/market/status")
async def market_status():
    """Источник данных: bybit | synthetic — бейдж в шапке фронта."""
    st = data_status()
    st["demo_fallback"] = settings.DEMO_FALLBACK
    return st


@app.post("/api/market/ping")
async def market_ping():
    """Живая проверка соединения с Bybit (пробует основной и резервный хост)."""
    return await market.ping()


# ---------------------------------------------------------------------- #
# Стратегии / бэктест
# ---------------------------------------------------------------------- #
class BacktestRequest(BaseModel):
    symbol: str
    strategy: str = "momentum_breakout"
    timeframe: str = "5m"
    params: Optional[dict] = None
    tp_pct: Optional[float] = None
    sl_pct: Optional[float] = None
    max_hold_hours: Optional[float] = None
    # Диапазон дат (необязательно). Формат "YYYY-MM-DD".
    # Без дат — автоматически последние 24 часа.
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


def _date_range(start_date: Optional[str], end_date: Optional[str]) -> tuple:
    """Выбор периода бэктеста. Без дат -> последние 24 часа.

    Количество свечей больше не задаётся вручную: объём данных вычисляется
    автоматически из периода и таймфрейма (range-запрос к Bybit с пагинацией).
    """
    start_ts = _parse_date(start_date)
    end_ts = _parse_date(end_date)
    if end_ts:
        end_ts += 86_400_000 - 1  # включить весь конечный день
    if start_ts is None and end_ts is None:
        now = int(time.time() * 1000)
        start_ts, end_ts = now - 86_400_000, now   # последние 24 часа
    return start_ts, end_ts


@app.post("/api/backtest")
async def backtest(req: BacktestRequest):
    strategy = build_strategy(req.strategy, req.params, req.tp_pct, req.sl_pct, req.max_hold_hours)
    if strategy is None:
        raise HTTPException(404, f"Стратегия '{req.strategy}' не найдена")

    start_ts, end_ts = _date_range(req.start_date, req.end_date)
    candles = await market.get_klines(req.symbol, req.timeframe, None, start_ts, end_ts)
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
    out["data_source"] = data_status()["source"]
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
# Бэктест сразу по ВСЕМ стратегиям (сравнение в один клик)
# ---------------------------------------------------------------------- #
class BacktestAllRequest(BaseModel):
    symbol: str
    timeframe: str = "5m"
    tp_pct: Optional[float] = None
    sl_pct: Optional[float] = None
    max_hold_hours: Optional[float] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None


@app.post("/api/backtest/all")
async def backtest_all(req: BacktestAllRequest):
    start_ts, end_ts = _date_range(req.start_date, req.end_date)
    candles = await market.get_klines(req.symbol, req.timeframe, None, start_ts, end_ts)
    if not candles:
        raise HTTPException(400, "Нет данных для бэктеста в заданном диапазоне")

    # tp/sl применяются одинаково ко всем стратегиям
    tp_pct = req.tp_pct if req.tp_pct is not None else settings.MIN_TP_PCT
    sl_pct = req.sl_pct if req.sl_pct is not None else settings.DEFAULT_SL_PCT

    results = []
    for schema in STRATEGY_SCHEMAS():
        strategy = build_strategy(schema["type"], tp_pct=req.tp_pct, sl_pct=req.sl_pct,
                                  max_hold_hours=req.max_hold_hours)
        try:
            r = run_backtest(strategy, candles, req.timeframe, req.max_hold_hours)
        except Exception:
            continue
        pf = r.profit_factor
        results.append({
            "type": schema["type"],
            "name": schema["name"],
            "trades": r.total_trades,
            "win_rate": r.win_rate,
            "pnl_pct": r.total_pnl_pct,
            "profit_factor": pf,
            "max_drawdown": r.max_drawdown_pct,
            "avg_win_pct": r.avg_win_pct,
            "avg_loss_pct": r.avg_loss_pct,
            "avg_hold_hours": r.avg_hold_hours,
            "end_1000": r.end_1000,
            "return_pct": r.return_pct,
        })

    # Сортировка: сначала по win rate среди стратегий с >=3 сделками, затем по PnL
    def _key(r):
        trades_ok = 1 if r["trades"] >= 3 else 0
        return (trades_ok, r["win_rate"], r["pnl_pct"])
    results.sort(key=_key, reverse=True)

    best = results[0] if results else None
    return {
        "symbol": req.symbol,
        "timeframe": req.timeframe,
        "candles": len(candles),
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "start_date": req.start_date,
        "end_date": req.end_date,
        "data_source": data_status()["source"],
        "best": best,
        "results": results,
    }


# ---------------------------------------------------------------------- #
# Оптимизация: прогон всех стратегий по символу, выбор лучших
# ---------------------------------------------------------------------- #
class OptimizeRequest(BaseModel):
    symbol: str
    timeframe: str = "5m"
    min_trades: int = 10
    min_win_rate: float = 55.0
    top_n: int = 5
    start_date: Optional[str] = None
    end_date: Optional[str] = None


@app.post("/api/optimize")
async def optimize(req: OptimizeRequest):
    selector = get_selector()
    start_ts, end_ts = _date_range(req.start_date, req.end_date)
    out = await selector.optimize(req.symbol, req.timeframe, 1000,
                                  req.min_trades, req.min_win_rate, req.top_n,
                                  start_ts=start_ts, end_ts=end_ts)
    out["data_source"] = data_status()["source"]
    return out


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
# Live 1-на-1: сигналы только по одной выбранной монете.
# Работает параллельно с общим live-движком; настройки меняются онлайн.
# ---------------------------------------------------------------------- #
class SingleConfigBody(BaseModel):
    symbol: Optional[str] = None
    timeframe: Optional[str] = None
    strategy_type: Optional[str] = None       # "auto" или type из /api/strategies
    params: Optional[dict] = None
    tp_pct: Optional[float] = None
    sl_pct: Optional[float] = None
    max_hold_hours: Optional[float] = None
    max_trades_per_day: Optional[int] = None
    position_size_pct: Optional[float] = None
    min_confidence: Optional[float] = None


@app.get("/api/live/single/state")
async def single_state():
    return get_single_engine().state()


@app.post("/api/live/single/start")
async def single_start(body: SingleConfigBody):
    eng = get_single_engine()
    if body.symbol is None:
        raise HTTPException(400, "Укажите symbol")
    applied = eng.apply_config(body.dict(exclude_none=True))
    await eng.start()
    return {"ok": True, "applied": applied, "state": eng.state()}


@app.post("/api/live/single/stop")
async def single_stop():
    eng = get_single_engine()
    await eng.stop()
    return {"ok": True, "state": eng.state()}


@app.post("/api/live/single/config")
async def single_config(body: SingleConfigBody):
    """Онлайн-применение настроек (без остановки цикла)."""
    eng = get_single_engine()
    applied = eng.apply_config(body.dict(exclude_none=True))
    return {"ok": True, "applied": applied, "state": eng.state()}


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
# Кодовый ассистент — агент, который пишет код для платформы
# (новые стратегии, правки интерфейса и т.д.)
# ---------------------------------------------------------------------- #
CODE_SYSTEM = """Ты — кодовый ассистент для платформы TradingAgents (Python/FastAPI backend + vanilla JS frontend).

Структура проекта:
- backend/strategies/builtin.py — стратегии (классы, наследники Strategy, метод generate(candles, index) -> Signal)
- backend/strategies/base.py — базовый класс Strategy и Signal (SignalSide.LONG/SHORT/NEUTRAL, метод self._build_signal(side, candles, index, confidence, reason))
- backend/strategies/indicators.py — индикаторы (sma, ema, rsi, atr, macd, bollinger, stochastic, adx, supertrend, cci, williams_r, mfi, ichimoku, zscore, pivot_levels)
- backend/strategies/registry.py — реестр (BUILTIN_STRATEGIES, build_strategy)
- frontend/index.html, frontend/css/style.css, frontend/js/app.js — интерфейс

ТВОЯ ЗАДАЧА: по запросу пользователя выдать ГОТОВЫЙ, рабочий код. Отвечай только кодом (или кодом + краткие инструкции), без лишних рассуждений. Если просят новую стратегию — верни полный класс Python. Если просят правку интерфейса — верни нужный фрагмент HTML/CSS/JS. Код должен быть самодостаточным и корректным."""


class CodeRequest(BaseModel):
    request: str
    role: str = "strategist"  # какая из 5 моделей пишет код
    task: str = "strategy"    # strategy | ui | general


@app.post("/api/code/assist")
async def code_assist(req: CodeRequest):
    if req.role not in settings.ROLE_KEYS:
        raise HTTPException(404, f"Неизвестная роль: {req.role}")
    client = get_client()
    user = (
        f"Задача ({req.task}):\n{req.request}\n\n"
        f"Дай только готовый код и, если нужно, 1-3 строки инструкции по подключению."
    )
    try:
        text = await client.chat(req.role, [
            {"role": "system", "content": CODE_SYSTEM},
            {"role": "user", "content": user},
        ], max_tokens=2500, temperature=0.2)
    except Exception as e:
        raise HTTPException(500, f"Ошибка модели: {e}")
    return {
        "code": text,
        "role": req.role,
        "model": settings.model_role(req.role)["model"],
        "demo": settings.model_role(req.role)["api_key"] == "",
        "tokens_est": {"prompt": len(CODE_SYSTEM + user) // 4, "completion": len(text) // 4},
    }


class CodeSaveRequest(BaseModel):
    filename: str   # относительный путь внутри репозитория, например backend/strategies/my_strategy.py
    content: str


@app.post("/api/code/save")
async def code_save(req: CodeSaveRequest):
    """Сохраняет сгенерированный код в файл (только .py/.js/.css/.html внутри репозитория)."""
    from config import PROJECT_ROOT
    fn = req.filename.replace("\\", "/").lstrip("/")
    # защита от выхода за пределы репозитория
    target = os.path.normpath(os.path.join(PROJECT_ROOT, fn))
    if not target.startswith(os.path.normpath(PROJECT_ROOT) + os.sep):
        raise HTTPException(400, "Недопустимый путь (выход за пределы проекта)")
    if not fn.endswith((".py", ".js", ".css", ".html", ".md")):
        raise HTTPException(400, "Разрешены только файлы .py/.js/.css/.html/.md")
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(req.content)
    except Exception as e:
        raise HTTPException(500, f"Не удалось сохранить: {e}")
    return {"ok": True, "path": fn}


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
