"""
Read-only клиент Bybit v5 для раздела «Мои сделки».

Нужен API-ключ с правом ТОЛЬКО НА ЧТЕНИЕ (Bybit → API Management → Create key →
Read-only). Платформа никогда не отправляет торговые запросы — здесь только GET.
Подпись: HMAC-SHA256(timestamp + api_key + recv_window + queryString).
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import List, Optional

import httpx

from portal_config import settings


class BybitAuthError(Exception):
    pass


BASES = [
    "https://api.bybit.com",
    "https://api2.bybit.com",
    "https://api.bytick.com",
    "https://api.bybit.eu",
]


def _sign(ts: int, recv: int, payload: str) -> str:
    raw = f"{ts}{settings.BYBIT_API_KEY}{recv}{payload}"
    return hmac.new(settings.BYBIT_API_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()


async def _get(path: str, params: dict) -> dict:
    if not settings.has_bybit_keys():
        raise BybitAuthError("Не заданы read-only ключи Bybit — введите их в Настройках (раздел «Мои сделки»)")
    # убираем пустые
    params = {k: v for k, v in params.items() if v is not None and v != ""}
    query = "&".join(f"{k}={v}" for k, v in sorted((k, str(v)) for k, v in params.items()))
    ts = int(time.time() * 1000)
    recv = 20000
    headers = {
        "X-BAPI-API-KEY": settings.BYBIT_API_KEY,
        "X-BAPI-SIGN": _sign(ts, recv, query),
        "X-BAPI-TIMESTAMP": str(ts),
        "X-BAPI-RECV-WINDOW": str(recv),
    }

    last_err: Optional[Exception] = None
    bases = ([settings.BYBIT_BASE_URL] if settings.BYBIT_BASE_URL else []) + [b for b in BASES if b != settings.BYBIT_BASE_URL]
    async with httpx.AsyncClient(timeout=20) as cli:
        for base in bases:
            try:
                r = await cli.get(base.rstrip("/") + path + ("?" + query if query else ""), headers=headers)
                data = r.json()
                if data.get("retCode") == 0:
                    return data.get("result") or {}
                # ошибка ключа/прав — повторять на другом хосте бессмысленно
                if data.get("retCode") in (10003, 10004, 10005, 33004):
                    raise BybitAuthError(f"Bybit отклонил ключ: {data.get('retMsg')} (код {data.get('retCode')})")
                last_err = BybitAuthError(f"Bybit: {data.get('retMsg')} (код {data.get('retCode')})")
            except BybitAuthError:
                raise
            except Exception as e:
                last_err = e
                continue
    raise BybitAuthError(f"Все хосты Bybit недоступны ({last_err})")


async def fetch_executions(days: int = 90, category: str = "linear", max_pages: int = 10) -> List[dict]:
    """История исполнений (fills) за период. limit<=100, пагинация курсором."""
    end = int(time.time() * 1000)
    start = end - days * 24 * 3600 * 1000
    out: List[dict] = []
    cursor = None
    for _ in range(max_pages):
        res = await _get("/v5/execution/list", {
            "category": category, "startTime": start, "endTime": end,
            "limit": 100, "cursor": cursor,
        })
        out.extend(res.get("list") or [])
        cursor = res.get("nextPageCursor")
        if not cursor:
            break
    return out


async def fetch_closed_pnl(days: int = 90, category: str = "linear", max_pages: int = 10) -> List[dict]:
    """Закрытые позиции с PnL (главный источник для статистики)."""
    end = int(time.time() * 1000)
    start = end - days * 24 * 3600 * 1000
    out: List[dict] = []
    cursor = None
    for _ in range(max_pages):
        res = await _get("/v5/position/closed-pnl", {
            "category": category, "startTime": start, "endTime": end,
            "limit": 100, "cursor": cursor,
        })
        out.extend(res.get("list") or [])
        cursor = res.get("nextPageCursor")
        if not cursor:
            break
    return out


async def fetch_wallet() -> dict:
    """Баланс кошелька (UNIFIED). Опционально — для сводки."""
    return await _get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
