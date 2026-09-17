"""
Публичные рыночные данные Bybit (v5 REST) с синтетическим fallback.

Возможности:
  * OHLCV свечи: последние N свечей или произвольный диапазон дат (start/end, мс).
  * Тикеры: для заданных символов или ВСЕ spot-монеты с фильтром по суточному объёму.
  * Резервный хост api.bytick.com, если api.bybit.com недоступен.
  * Публичный статус источника данных (Bybit / учебные данные) и self-check ping().
  * Синтетический fallback (демо): детерминированные свечи и тикеры.

ВАЖНО: Bybit V5 принимает интервал свечи ТОЛЬКО в формате
  1, 3, 5, 15, 30, 60, 120, 240, 360, 720, D, W, M
Формат вида "5m"/"1h" возвращает retCode=10001 и пустой список —
поэтому переводим "5m" -> "5", "1h" -> "60", "1d" -> "D".
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, asdict
from typing import List, Optional

import httpx

from config import settings

MAX_KLINE_CANDLES = 10000   # защита от гигантских выборок
_PAGE_LIMIT = 1000          # максимум Bybit на один запрос
_BACKUP_HOST = "https://api.bytick.com"   # официальное зеркало API Bybit

# Наш формат -> формат Bybit V5
_BYBIT_INTERVAL_MAP = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
    "1d": "D", "1w": "W", "1M": "M",
}

# Общий статус источника данных (shared по всем экземплярам MarketData)
_STATUS = {
    "source": "unknown",      # bybit | synthetic | unknown
    "bybit_ok": None,         # True/False/None
    "host": None,             # какой хост ответил последним
    "last_error": None,       # текст последней ошибки (для диагностики в UI)
    "checked_at": 0.0,        # unix ts последнего живого ответа
}


def _bybit_interval(interval: str) -> Optional[str]:
    """"5m" -> "5", "1h" -> "60". Возвращает None, если формат неизвестен."""
    if not interval:
        return None
    if interval in _BYBIT_INTERVAL_MAP:
        return _BYBIT_INTERVAL_MAP[interval]
    if interval.isdigit() or interval in ("D", "W", "M"):
        return interval
    # "10m" -> "10" (вдруг Bybit знает), "7d" -> нет
    if interval.endswith("m") and interval[:-1].isdigit():
        return interval[:-1]
    return None


def data_status() -> dict:
    """Текущий статус подключения к Bybit (для /api/market/status)."""
    return {
        "source": _STATUS["source"],
        "bybit_ok": _STATUS["bybit_ok"],
        "host": _STATUS["host"],
        "last_error": _STATUS["last_error"],
        "checked_at": _STATUS["checked_at"],
    }


def _mark_live(host: str):
    _STATUS["bybit_ok"] = True
    _STATUS["host"] = host
    _STATUS["source"] = "bybit"
    _STATUS["last_error"] = None
    _STATUS["checked_at"] = time.time()


def _mark_dead(err: str):
    _STATUS["bybit_ok"] = False
    _STATUS["last_error"] = err
    _STATUS["checked_at"] = time.time()


def _mark_synthetic():
    # Последняя выданная порция данных — учебная (Bybit не ответил/выключен)
    _STATUS["source"] = "synthetic"


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
        self._all_tickers_cache: Optional[tuple] = None  # (fetched_at, [dict])

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=8.0)
        return self._client

    async def aclose(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ------------------------------------------------------------------ #
    # HTTP к Bybit: основной хост -> резервный (bytick). retCode=0 = успех.
    # ------------------------------------------------------------------ #
    def _hosts(self) -> List[str]:
        hosts = [settings.BYBIT_BASE_URL.rstrip("/")]
        if _BACKUP_HOST not in hosts:
            hosts.append(_BACKUP_HOST)
        return hosts

    async def _get_json(self, path: str, params: dict) -> Optional[dict]:
        """GET {host}{path}. Возвращает распарсенный JSON при retCode==0, иначе None.

        Сетевые/HTTP ошибки -> пробуем следующий хост.
        Ошибка параметров (retCode != 0) -> прекращаем (на другом хосте будет то же).
        """
        if not settings.DEMO_FALLBACK:
            return None  # живое подключение отключено настройками — только демо
        http = await self._http()
        last_err = "нет соединения"
        for host in self._hosts():
            url = host + path
            try:
                resp = await http.get(url, params=params)
            except Exception as e:
                last_err = f"{host}: {type(e).__name__} {e}"
                continue
            if resp.status_code != 200:
                last_err = f"{host}: HTTP {resp.status_code}"
                continue
            try:
                data = resp.json()
            except Exception:
                last_err = f"{host}: некорректный JSON"
                continue
            code = data.get("retCode")
            if code not in (None, 0):
                # ошибка параметров/символа — на другом хосте будет так же
                msg = data.get("retMsg") or f"retCode={code}"
                _mark_dead(f"Bybit API: {msg}")
                return None
            _mark_live(host)
            return data
        _mark_dead(last_err)
        return None

    # ------------------------------------------------------------------ #
    # Диагностика: жив ли Bybit прямо сейчас
    # ------------------------------------------------------------------ #
    async def ping(self) -> dict:
        t0 = time.time()
        data = await self._get_json("/v5/market/kline", {
            "category": "spot", "symbol": "BTCUSDT", "interval": "5", "limit": 1})
        latency = int((time.time() - t0) * 1000)
        ok = bool(data) and bool(data.get("result", {}).get("list"))
        status = data_status()
        status["latency_ms"] = latency
        status["ok"] = ok
        return status

    # ------------------------------------------------------------------ #
    # Кэш свечей
    # ------------------------------------------------------------------ #
    def _cache_get(self, symbol: str, interval: str) -> Optional[List[Candle]]:
        entry = self._cache.get((symbol, interval))
        if not entry:
            return None
        fetched_at, candles = entry
        if time.time() - fetched_at < 15:
            return candles
        return None

    def _cache_set(self, symbol: str, interval: str, candles: List[Candle]):
        self._cache[(symbol, interval)] = (time.time(), candles)

    # ------------------------------------------------------------------ #
    # Klines (OHLCV)
    # ------------------------------------------------------------------ #
    async def get_klines(self, symbol: str, interval: str = None, limit: int = None,
                         start_ts: Optional[int] = None, end_ts: Optional[int] = None) -> List[Candle]:
        interval = interval or settings.DEFAULT_TIMEFRAME
        limit = limit or settings.KLINE_LIMIT

        # Режим диапазона дат
        if start_ts is not None or end_ts is not None:
            return await self._get_klines_range(symbol, interval, start_ts, end_ts, limit)

        # Режим "последние N свечей"
        cached = self._cache_get(symbol, interval)
        if cached and len(cached) >= limit:
            return cached[-limit:]

        live = await self._fetch_klines_live(symbol, interval, limit)
        if live:
            self._cache_set(symbol, interval, live)
            return live

        _mark_synthetic()
        candles = synthetic_klines(symbol, interval, limit)
        self._cache_set(symbol, interval, candles)
        return candles

    async def _get_klines_range(self, symbol: str, interval: str,
                                start_ts: Optional[int], end_ts: Optional[int],
                                limit: int) -> List[Candle]:
        end = end_ts if end_ts is not None else int(time.time() * 1000)
        start = start_ts if start_ts is not None else end - _interval_ms(interval) * limit
        if end <= start:
            start = end - _interval_ms(interval)

        live = await self._fetch_klines_live_range(symbol, interval, start, end)
        if live:
            return live

        _mark_synthetic()
        return synthetic_klines_range(symbol, interval, start, end)

    async def _fetch_klines_live(self, symbol: str, interval: str, limit: int) -> Optional[List[Candle]]:
        bint = _bybit_interval(interval)
        if bint is None:
            return None
        data = await self._get_json("/v5/market/kline", {
            "category": "spot", "symbol": symbol, "interval": bint,
            "limit": min(limit, 1000)})
        if not data:
            return None
        rows = data.get("result", {}).get("list", [])
        if not rows:
            return None
        candles = _rows_to_candles(rows)
        candles.sort(key=lambda c: c.ts)
        return candles

    async def _fetch_klines_live_range(self, symbol: str, interval: str,
                                       start: int, end: int) -> Optional[List[Candle]]:
        """Пагинация по диапазону дат (до 1000 свечей за запрос)."""
        bint = _bybit_interval(interval)
        if bint is None:
            return None
        rows_all: List[list] = []
        cursor = end
        for _ in range(12):  # до 12 страниц ≈ 12000 свечей
            data = await self._get_json("/v5/market/kline", {
                "category": "spot", "symbol": symbol, "interval": bint,
                "start": start, "end": cursor, "limit": _PAGE_LIMIT})
            if not data:
                return None
            rows = data.get("result", {}).get("list", [])
            if not rows:
                break
            rows_all.extend(rows)
            oldest = min(int(r[0]) for r in rows)
            if oldest <= start:
                break
            nxt = oldest - 1
            if nxt >= cursor:
                break
            cursor = nxt

        if not rows_all:
            return None
        candles = []
        for r in rows_all:
            ts = int(r[0])
            if ts < start or ts > end:
                continue
            candles.append(Candle(ts=ts, open=float(r[1]), high=float(r[2]),
                                  low=float(r[3]), close=float(r[4]), volume=float(r[5])))
        candles.sort(key=lambda c: c.ts)
        return candles[:MAX_KLINE_CANDLES] if candles else None

    # ------------------------------------------------------------------ #
    # Тикеры
    # ------------------------------------------------------------------ #
    async def get_tickers(self, symbols: List[str] = None) -> List[dict]:
        symbols = symbols or settings.symbols
        live = await self._fetch_tickers_live(symbols)
        if live:
            return live
        _mark_synthetic()
        return synthetic_tickers(symbols)

    async def _fetch_tickers_live(self, symbols: List[str]) -> Optional[List[dict]]:
        data = await self._get_json("/v5/market/tickers", {"category": "spot"})
        if not data:
            return None
        rows = data.get("result", {}).get("list", [])
        wanted = set(symbols)
        out = []
        for r in rows:
            sym = r.get("symbol")
            if sym not in wanted:
                continue
            out.append(_ticker_dict(r))
        return out if out else None

    # Все spot-монеты с фильтром по суточному объёму
    async def get_all_tickers(self, min_volume_usd: Optional[float] = None) -> List[dict]:
        min_vol = min_volume_usd if min_volume_usd is not None else settings.MIN_DAILY_VOLUME_USD
        # кэш на 60 сек
        if self._all_tickers_cache and time.time() - self._all_tickers_cache[0] < 60:
            return [t for t in self._all_tickers_cache[1] if t["volume24h"] >= min_vol]

        live = await self._fetch_all_tickers_live()
        if live:
            self._all_tickers_cache = (time.time(), live)
            return [t for t in live if t["volume24h"] >= min_vol]

        _mark_synthetic()
        synthetic = synthetic_tickers(settings.symbols)
        return [t for t in synthetic if t["volume24h"] >= min_vol]

    async def _fetch_all_tickers_live(self) -> Optional[List[dict]]:
        data = await self._get_json("/v5/market/tickers", {"category": "spot"})
        if not data:
            return None
        rows = data.get("result", {}).get("list", [])
        out = []
        for r in rows:
            out.append(_ticker_dict(r))
        out.sort(key=lambda x: -x["volume24h"])
        return out if out else None


def _ticker_dict(r: dict) -> dict:
    return {
        "symbol": r.get("symbol"),
        "last": float(r.get("lastPrice", 0) or 0),
        "change24h": float(r.get("price24hPcnt", 0) or 0) * 100,
        "volume24h": float(r.get("turnover24h", 0) or 0),
        "high24h": float(r.get("highPrice24h", 0) or 0),
        "low24h": float(r.get("lowPrice24h", 0) or 0),
    }


def _rows_to_candles(rows: List[list]) -> List[Candle]:
    return [Candle(ts=int(r[0]), open=float(r[1]), high=float(r[2]),
                   low=float(r[3]), close=float(r[4]), volume=float(r[5])) for r in rows]


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


def _make_candle(rng, ts, price, vol, trend):
    cycle = 0.006 * math.sin((ts // 1) / 40.0) + 0.004 * math.sin((ts // 1) / 13.0)
    drift = trend + cycle
    o = price
    c = o * math.exp(drift + rng.gauss(0, vol))
    h = max(o, c) * (1 + abs(rng.gauss(0, vol * 0.6)))
    l = min(o, c) * (1 - abs(rng.gauss(0, vol * 0.6)))
    v = abs(rng.gauss(1, 0.35)) * (1 + 3 * abs(drift) / vol)
    return Candle(ts=ts, open=round(o, 6), high=round(h, 6),
                  low=round(l, 6), close=round(c, 6), volume=round(v * 1000, 2))


def synthetic_klines(symbol: str, interval: str, limit: int) -> List[Candle]:
    """Последние limit свечей (заканчивая текущим моментом)."""
    rng = random.Random(_seed(symbol, interval))
    step = _interval_ms(interval)
    now = int(time.time() * 1000)
    start = ((now - step * limit) // step) * step

    base_price = 100 * (0.5 + (_seed(symbol, "price") % 1000) / 500.0)
    vol = 0.004 + (rng.random() * 0.012)
    trend = rng.uniform(-0.0004, 0.0004)

    candles: List[Candle] = []
    price = base_price
    for i in range(limit):
        ts = start + i * step
        candle = _make_candle(rng, ts, price, vol, trend)
        candles.append(candle)
        price = candle.close
    return candles


def synthetic_klines_range(symbol: str, interval: str, start_ts: int, end_ts: int) -> List[Candle]:
    """Свечи в заданном диапазоне дат (для демо-режима)."""
    rng = random.Random(_seed(symbol, interval))
    step = _interval_ms(interval)
    start_ts = (start_ts // step) * step
    n = int((end_ts - start_ts) // step) + 1
    n = max(1, min(n, MAX_KLINE_CANDLES))

    base_price = 100 * (0.5 + (_seed(symbol, "price") % 1000) / 500.0)
    vol = 0.004 + (rng.random() * 0.012)
    trend = rng.uniform(-0.0004, 0.0004)

    candles: List[Candle] = []
    price = base_price
    for i in range(n):
        ts = start_ts + i * step
        candle = _make_candle(rng, ts, price, vol, trend)
        candles.append(candle)
        price = candle.close
    return candles


def synthetic_tickers(symbols: List[str]) -> List[dict]:
    rng = random.Random(int(time.time() // 60))  # меняется раз в минуту
    out = []
    for s in symbols:
        seed = _seed(s, "tick")
        base = 50 + (seed % 10000) / 10.0
        last = base * (1 + rng.uniform(-0.03, 0.03))
        change = rng.uniform(-6, 6)
        vol24 = base * 1_000_000 * rng.uniform(1.2, 3.0)  # гарантированно > 1M
        out.append({
            "symbol": s,
            "last": round(last, 4),
            "change24h": round(change, 2),
            "volume24h": round(vol24, 0),
            "high24h": round(last * (1 + abs(change) / 100), 4),
            "low24h": round(last * (1 - abs(change) / 100), 4),
        })
    out.sort(key=lambda x: -x["volume24h"])
    return out
