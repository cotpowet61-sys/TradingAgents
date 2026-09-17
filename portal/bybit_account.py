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

# Смещение локальных часов относительно сервера Bybit (частая причина
# «invalid timestamp» / retCode 10002 — подпись умирает при рассинхроне >1с).
_time_offset_ms: float = 0.0
_time_synced_at: float = 0.0


async def _sync_time(force: bool = False) -> None:
    global _time_offset_ms, _time_synced_at
    now = time.time()
    if not force and now - _time_synced_at < 300:
        return
    local_ms = now * 1000
    async with httpx.AsyncClient(timeout=10) as cli:
        for base in BASES:
            try:
                r = await cli.get(base + "/v5/market/time")
                data = r.json()
                srv = None
                res = data.get("result") or {}
                if res.get("timeNano"):
                    srv = int(res["timeNano"]) / 1_000_000
                elif res.get("timeSecond"):
                    srv = float(res["timeSecond"]) * 1000
                if srv:
                    _time_offset_ms = srv - local_ms
                    _time_synced_at = now
                    return
            except Exception:
                continue
    # сервер недоступен — подписываем локальным временем (ошибка всплывёт как retCode)
    _time_offset_ms = 0.0
    _time_synced_at = now


def server_time_ms() -> int:
    return int(time.time() * 1000 + _time_offset_ms)


def _sign(ts: int, recv: int, payload: str) -> str:
    raw = f"{ts}{settings.BYBIT_API_KEY}{recv}{payload}"
    return hmac.new(settings.BYBIT_API_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()


async def _get(path: str, params: dict) -> dict:
    if not settings.has_bybit_keys():
        raise BybitAuthError("Не заданы read-only ключи Bybit — введите их в Настройках (раздел «Мои сделки»)")
    # убираем пустые
    params = {k: v for k, v in params.items() if v is not None and v != ""}
    query = "&".join(f"{k}={v}" for k, v in sorted((k, str(v)) for k, v in params.items()))

    await _sync_time()
    ts = server_time_ms()
    recv = 20000
    headers = {
        "X-BAPI-API-KEY": settings.BYBIT_API_KEY,
        "X-BAPI-SIGN": _sign(ts, recv, query),
        "X-BAPI-TIMESTAMP": str(ts),
        "X-BAPI-RECV-WINDOW": str(recv),
    }

    last_err: Optional[Exception] = None
    tried: List[str] = []
    bases = ([settings.BYBIT_BASE_URL] if settings.BYBIT_BASE_URL else []) + [b for b in BASES if b != settings.BYBIT_BASE_URL]
    async with httpx.AsyncClient(timeout=20) as cli:
        for base in bases:
            host = base.replace("https://", "")
            tried.append(host)
            try:
                r = await cli.get(base.rstrip("/") + path + ("?" + query if query else ""), headers=headers)
                data = r.json()
                if data.get("retCode") == 0:
                    return data.get("result") or {}
                rc = data.get("retCode")
                msg = data.get("retMsg") or ""
                # ошибка ключа/прав — повторять на другом хосте бессмысленно
                if rc in (10003, 10004, 10005, 33004):
                    hint = {
                        10003: "неверный API-ключ",
                        10004: "ошибка подписи (проверьте secret)",
                        10005: "у ключа нет нужных прав — включите на Bybit доступ к контрактам (Read-Only с 'Contracts') либо Unified Trading",
                        33004: "срок ключа истёк или отозван",
                    }.get(rc, "")
                    raise BybitAuthError(f"Bybit отклонил ключ: {msg} (код {rc}). {hint}")
                # рассинхрон времени — ресинхронизируемся один раз и повторим
                if rc == 10002:
                    await _sync_time(force=True)
                    ts2 = server_time_ms()
                    headers["X-BAPI-TIMESTAMP"] = str(ts2)
                    headers["X-BAPI-SIGN"] = _sign(ts2, recv, query)
                    r = await cli.get(base.rstrip("/") + path + ("?" + query if query else ""), headers=headers)
                    data = r.json()
                    if data.get("retCode") == 0:
                        return data.get("result") or {}
                    last_err = BybitAuthError(f"Bybit: {data.get('retMsg')} (код {data.get('retCode')})")
                    continue
                last_err = BybitAuthError(f"Bybit: {msg} (код {rc})")
            except BybitAuthError:
                raise
            except Exception as e:
                last_err = e
                continue
    raise BybitAuthError(
        f"Все хосты Bybit недоступны ({', '.join(tried)}). "
        f"Последняя ошибка: {last_err}. Похоже на блокировку сети/гео — попробуйте VPN "
        f"или другой интернет-канал, затем нажмите «Диагностика API» в Настройках."
    )


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


# ---------------------------------------------------------------------- #
# Диагностика для Настроек: сеть до хостов + проверка ключа
# ---------------------------------------------------------------------- #
async def diagnose() -> dict:
    hosts = []
    async with httpx.AsyncClient(timeout=10) as cli:
        for base in BASES:
            host = base.replace("https://", "")
            t0 = time.time()
            try:
                r = await cli.get(base + "/v5/market/time")
                lat = round((time.time() - t0) * 1000)
                ok = r.status_code == 200
                hosts.append({"host": host, "ok": ok, "latency_ms": lat,
                              "detail": "доступен" if ok else f"HTTP {r.status_code}"})
            except Exception as e:
                lat = round((time.time() - t0) * 1000)
                hosts.append({"host": host, "ok": False, "latency_ms": lat,
                              "detail": str(e)[:90]})

    out = {"hosts": hosts, "network_ok": any(h["ok"] for h in hosts), "key_check": None}
    if any(h["ok"] for h in hosts):
        await _sync_time(force=True)
        out["time_offset_ms"] = round(_time_offset_ms)
    if settings.has_bybit_keys() and out["network_ok"]:
        await _sync_time(force=True)
        ts = server_time_ms()
        recv = 20000
        query = "category=linear&limit=1"
        headers = {
            "X-BAPI-API-KEY": settings.BYBIT_API_KEY,
            "X-BAPI-SIGN": _sign(ts, recv, query),
            "X-BAPI-TIMESTAMP": str(ts),
            "X-BAPI-RECV-WINDOW": str(recv),
        }
        async with httpx.AsyncClient(timeout=15) as cli:
            for h in hosts:
                if not h["ok"]:
                    continue
                try:
                    r = await cli.get(f"https://{h['host']}/v5/position/closed-pnl?" + query, headers=headers)
                    data = r.json()
                    rc = data.get("retCode")
                    if rc == 0:
                        out["key_check"] = {"ok": True, "host": h["host"],
                                            "detail": "ключ принят, история позиций читается ✓"}
                    else:
                        out["key_check"] = {"ok": False, "host": h["host"], "ret_code": rc,
                                            "detail": data.get("retMsg", "")}
                    break
                except Exception as e:
                    continue
    if out["network_ok"] and not out["key_check"] and not settings.has_bybit_keys():
        out["key_check"] = {"ok": False, "detail": "ключи не введены"}

    # человеческие подсказки
    tips = []
    if not out["network_ok"]:
        tips.append("Ни один хост Bybit не отвечает — вероятна блокировка провайдером/страной. Включите VPN и повторите.")
    kc = out.get("key_check")
    if kc and not kc.get("ok"):
        rc = kc.get("ret_code")
        if rc == 10005:
            tips.append("Код 10005: у ключа нет прав на чтение деривативов. На Bybit в настройках ключа отметьте Contracts/Derivatives Read-only (или Unified Trading).")
        elif rc in (10003, 10004):
            tips.append("Ключ или secret неверны — скопируйте заново без пробелов.")
        elif rc == 10002:
            tips.append("Код 10002: рассинхрон времени — попробуйте ещё раз (синхронизация уже поправлена автоматически).")
        elif "не введены" in (kc.get("detail") or ""):
            tips.append("Введите read-only ключи и сохраните.")
    out["tips"] = tips
    return out
