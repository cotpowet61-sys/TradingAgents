"""
Публичные рыночные данные Bybit (v5 REST) с синтетическим fallback.

Когда сеть/биржи недоступны (или включён DEMO_FALLBACK), генерируются
детерминированные свечи OHLCV на основе геометрического случайного блуждания,
чтобы платформа (сканер, бэктест, сигналы) работала «из коробки».
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, asdict
from typing import List, Optional

import httpx

from config import settings


@dataclass
class Candle:
    ts: int          # unix timestamp (мс), время открытия
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_dict(self) -> dict:
        return asdict(self)


class MarketData:
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._cache: dict = {}          # (symbol, interval) -> (fetched_at, [Candle])
        self._live_ok: Optional[bool] = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def aclose(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ------------------------------------------------------------------ #
    # Кэш
    # ------------------------------------------------------------------ #
    def _cache_get(self, symbol: str, interval: str) -> Optional[List[Candle]]:
        entry = self._cache.get((symbol, interval))
        if not entry:
            return None
        fetched_at, candles = entry
        if time.time() - fetched_at < 15:  # 15 сек TTL
            return candles
        return None

    def _cache_set(self, symbol: str, interval: str, candles: List[Candle]):
        self._cache[(symbol, interval)] = (time.time(), candles)

    # ------------------------------------------------------------------ #
    # Klines (OHLCV)
    # ------------------------------------------------------------------ #
    async def get_klines(self, symbol: str, interval: str = None, limit: int = None) -> List[Candle]:
        interval = interval or settings.DEFAULT_TIMEFRAME
        limit = limit or settings.KLINE_LIMIT

        cached = self._cache_get(symbol, interval)
        if cached and len(cached) >= limit:
            return cached[-limit:]

        live = await self._fetch_klines_live(symbol, interval, limit)
        if live:
            self._live_ok = True
            self._cache_set(symbol, interval, live)
            return live

        self._live_ok = False
        candles = synthetic_klines(symbol, interval, limit)
        self._cache_set(symbol, interval, candles)
        return candles

    async def _fetch_klines_live(self, symbol: str, interval: str, limit: int) -> Optional[List[Candle]]:
        if not settings.DEMO_FALLBACK:
            return None
        try:
            url = settings.BYBIT_BASE_URL.rstrip("/") + "/v5/market/kline"
            params = {"category": "spot", "symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
            http = await self._http()
            resp = await http.get(url, params=params)
            if resp.status_code != 200:
                return None
            rows = resp.json().get("result", {}).get("list", [])
            if not rows:
                return None
            candles = []
            for r in rows:  # Bybit: [startTime, open, high, low, close, volume, turnover]
                candles.append(Candle(
                    ts=int(r[0]),
                    open=float(r[1]),
                    high=float(r[2]),
                    low=float(r[3]),
                    close=float(r[4]),
                    volume=float(r[5]),
                ))
            candles.sort(key=lambda c: c.ts)
            return candles
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # Тикеры (для панели «Горячие монеты»)
    # ------------------------------------------------------------------ #
    async def get_tickers(self, symbols: List[str] = None) -> List[dict]:
        symbols = symbols or settings.symbols
        live = await self._fetch_tickers_live(symbols)
        if live:
            return live
        return synthetic_tickers(symbols)

    async def _fetch_tickers_live(self, symbols: List[str]) -> Optional[List[dict]]:
        if not settings.DEMO_FALLBACK:
            return None
        try:
            url = settings.BYBIT_BASE_URL.rstrip("/") + "/v5/market/tickers"
            params = {"category": "spot"}
            http = await self._http()
            resp = await http.get(url, params=params)
            if resp.status_code != 200:
                return None
            rows = resp.json().get("result", {}).get("list", [])
            wanted = set(symbols)
            out = []
            for r in rows:
                sym = r.get("symbol")
                if sym not in wanted:
                    continue
                out.append({
                    "symbol": sym,
                    "last": float(r.get("lastPrice", 0)),
                    "change24h": float(r.get("price24hPcnt", 0)) * 100,
                    "volume24h": float(r.get("turnover24h", 0)),
                    "high24h": float(r.get("highPrice24h", 0)),
                    "low24h": float(r.get("lowPrice24h", 0)),
                })
            return out if out else None
        except Exception:
            return None


# ---------------------------------------------------------------------- #
# Синтетические данные (детерминированные по символу/времени)
# ---------------------------------------------------------------------- #
def _seed(symbol: str, interval: str) -> int:
    h = 0
    for ch in f"{symbol}:{interval}":
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h


def _interval_ms(interval: str) -> int:
    unit = interval[-1]
    n = int(interval[:-1])
    return n * {"m": 60_000, "h": 3_600_000, "d": 86_400_000}.get(unit, 60_000)


def synthetic_klines(symbol: str, interval: str, limit: int) -> List[Candle]:
    """Геометрическое случайное блуждание с трендом/циклами — реалистично для демо."""
    rng = random.Random(_seed(symbol, interval))
    step = _interval_ms(interval)
    now = int(time.time() * 1000)
    start = now - step * limit
    start = (start // step) * step

    base_price = 100 * (0.5 + (_seed(symbol, "price") % 1000) / 500.0)  # 50..250
    vol = 0.004 + (rng.random() * 0.012)  # волатильность за свечу

    candles: List[Candle] = []
    price = base_price
    trend = rng.uniform(-0.0004, 0.0004)  # слабый дрейф
    for i in range(limit):
        ts = start + i * step
        # Циклы (синусоиды) + шум — дают развороты, на которых срабатывают стратегии
        cycle = 0.006 * math.sin(i / 40.0) + 0.004 * math.sin(i / 13.0)
        drift = trend + cycle
        open_p = price
        close_p = open_p * math.exp(drift + rng.gauss(0, vol))
        high_p = max(open_p, close_p) * (1 + abs(rng.gauss(0, vol * 0.6)))
        low_p = min(open_p, close_p) * (1 - abs(rng.gauss(0, vol * 0.6)))
        volume = abs(rng.gauss(1, 0.35)) * (1 + 3 * abs(drift) / vol)
        candles.append(Candle(ts=ts, open=round(open_p, 6), high=round(high_p, 6),
                              low=round(low_p, 6), close=round(close_p, 6),
                              volume=round(volume * 1000, 2)))
        price = close_p
    return candles


def synthetic_tickers(symbols: List[str]) -> List[dict]:
    rng = random.Random(int(time.time() // 60))  # меняется раз в минуту
    out = []
    for s in symbols:
        seed = _seed(s, "tick")
        base = 50 + (seed % 10000) / 10.0
        last = base * (1 + rng.uniform(-0.03, 0.03))
        change = rng.uniform(-6, 6)
        vol24 = base * 1_000_000 * rng.uniform(0.2, 3.0)
        out.append({
            "symbol": s,
            "last": round(last, 4),
            "change24h": round(change, 2),
            "volume24h": round(vol24, 0),
            "high24h": round(last * (1 + abs(change) / 100), 4),
            "low24h": round(last * (1 - abs(change) / 100), 4),
        })
    return out
