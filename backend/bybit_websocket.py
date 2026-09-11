"""
WebSocket-подписка на рыночные данные Bybit (публичный стрим).

Используется как более дешёвая альтернатива периодическому REST-поллингу:
подписываемся на тикеры/свечи и обновляем кэш MarketData в реальном времени.

Замечание: live-движок по умолчанию работает через REST (проще и надёжнее).
Данный модуль — готовая заготовка для перехода на real-time данные.
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

import websockets

from config import settings


class BybitWebSocket:
    URL = "wss://stream.bybit.com/v5/public/spot"

    def __init__(self):
        self._ws = None
        self._subs: dict = {}
        self._on_message = None

    def set_handler(self, handler):
        """handler(event: dict) — вызывается для каждого пришедшего события."""
        self._on_message = handler

    async def connect(self):
        self._ws = await websockets.connect(self.URL)
        # возобновляем подписки
        for topic in list(self._subs):
            await self._subscribe(topic, self._subs[topic])

    async def _subscribe(self, topic: str, args: dict):
        await self._ws.send(json.dumps({"op": "subscribe", "args": [topic], **args}))

    async def subscribe_ticker(self, symbol: str):
        topic = f"tickers.{symbol}"
        self._subs[topic] = {}
        if self._ws:
            await self._subscribe(topic, {})

    async def subscribe_kline(self, symbol: str, interval: str):
        topic = f"kline.{interval}.{symbol}"
        self._subs[topic] = {}
        if self._ws:
            await self._subscribe(topic, {})

    async def listen(self):
        """Основной цикл приёма сообщений."""
        if not self._ws:
            await self.connect()
        try:
            async for raw in self._ws:
                event = json.loads(raw)
                if self._on_message:
                    self._on_message(event)
        except Exception:
            # авто-реконнект
            await asyncio.sleep(3)
            await self.connect()
            await self.listen()

    async def close(self):
        if self._ws:
            await self._ws.close()


# --- Обработчик событий Bybit -> кэш MarketData (пример использования) ---
def make_cache_updater(market):
    """Возвращает handler, который кладёт свечи из стрима в кэш MarketData."""
    def handler(event: dict):
        topic = event.get("topic", "")
        if topic.startswith("kline."):
            data = event.get("data", [])
            if not data:
                return
            bar = data[0]
            symbol = topic.split(".")[-1]
            from data.market_data import Candle
            candle = Candle(
                ts=int(bar["start"]), open=float(bar["open"]), high=float(bar["high"]),
                low=float(bar["low"]), close=float(bar["close"]), volume=float(bar.get("volume", 0)),
            )
            # Обновляем последнюю свечу в кэше, если она уже есть
            key = (symbol, settings.DEFAULT_TIMEFRAME)
            entry = market._cache.get(key)
            if entry:
                _, candles = entry
                if candles and candles[-1].ts == candle.ts:
                    candles[-1] = candle
                elif candles and candle.ts > candles[-1].ts:
                    candles.append(candle)
    return handler
