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

# Подпись по СЕРВЕРНОМУ времени Bybit (проверено автономным тестером
# bybit_key_test.py на машине пользователя): локальным часам не доверяем
# вообще — на ПК сдвиг 0.5–3.5 с убивал подпись ошибкой 10002 сначала
# постоянно, а после ресинка — периодически (джиттер VPN). Механика:
# берём /v5/market/time (публичный), кэшируем на _CLOCK_TTL_SEC секунд и
# экстраполируем через time.monotonic() (не зависит от системных часов),
# подписываем меткой «серверное время минус запас» — метка всегда строго
# в прошлом относительно сервера, поэтому 10002 исключён по построению.
_srv_base_ms: float = 0.0   # серверная метка, снятая в момент калибровки
_srv_mono: float = 0.0      # time.monotonic() в тот же момент
_CLOCK_TTL_SEC: float = 4.0
_SIGN_MARGIN_MS: float = 1000.0

# ---------------------------------------------------------------------- #
# Per-user ключи (SaaS): контекстная подмена кредов на время запроса.
# В монопольном режиме None → берутся глобальные из настроек (как прежде).
# ---------------------------------------------------------------------- #
import contextvars
_KEY_OVERRIDE: contextvars.ContextVar = contextvars.ContextVar("bybit_creds", default=None)


def _creds():
    o = _KEY_OVERRIDE.get()
    if o:
        return o
    return settings.BYBIT_API_KEY, settings.BYBIT_API_SECRET


def use_user_keys(api_key: str, api_secret: str):
    """Контекст-менеджер: запрос выполняется от чужих (юзерских) ключей."""
    token = _KEY_OVERRIDE.set((api_key, api_secret))

    class _CM:
        def __enter__(self): return self
        def __exit__(self, *a): _KEY_OVERRIDE.reset(token)
    return _CM()


def _bases_ordered() -> List[str]:
    return ([settings.BYBIT_BASE_URL] if settings.BYBIT_BASE_URL else []) + \
        [b for b in BASES if b != settings.BYBIT_BASE_URL]


async def _server_now_ms(force: bool = False) -> float:
    """Текущее серверное время Bybit (мс). Кэш 4 с, дальше — свежий запрос."""
    global _srv_base_ms, _srv_mono
    now = time.monotonic()
    if not force and _srv_base_ms and (now - _srv_mono) < _CLOCK_TTL_SEC:
        return _srv_base_ms + (now - _srv_mono) * 1000
    async with httpx.AsyncClient(timeout=10) as cli:
        for base in _bases_ordered():
            try:
                r = await cli.get(base.rstrip("/") + "/v5/market/time")
                res = (r.json().get("result") or {})
                srv = None
                if res.get("timeNano"):
                    srv = int(res["timeNano"]) / 1_000_000
                elif res.get("timeSecond"):
                    srv = float(res["timeSecond"]) * 1000
                if srv:
                    _srv_base_ms = srv
                    _srv_mono = time.monotonic()
                    return srv
            except Exception:
                continue
    # Все хосты недоступны — подписываем локальным временем (10002 всплывёт честно)
    _srv_base_ms = 0.0
    return time.time() * 1000


def server_time_ms() -> int:
    """Последняя известная серверная метка (для диагностики/отображения)."""
    if _srv_base_ms:
        return int(_srv_base_ms + (time.monotonic() - _srv_mono) * 1000)
    return int(time.time() * 1000)


def _sign(ts: int, recv: int, payload: str) -> str:
    api_key, api_secret = _creds()
    raw = f"{ts}{api_key}{recv}{payload}"
    return hmac.new(api_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()


async def _get(path: str, params: dict) -> dict:
    api_key, api_secret = _creds()
    if not api_key or not api_secret:
        raise BybitAuthError("Не заданы read-only ключи Bybit — введите их в Настройках (раздел «Мои сделки»)")
    # убираем пустые
    params = {k: v for k, v in params.items() if v is not None and v != ""}
    query = "&".join(f"{k}={v}" for k, v in sorted((k, str(v)) for k, v in params.items()))
    recv = 20000

    last_err: Optional[Exception] = None
    tried: List[str] = []
    async with httpx.AsyncClient(timeout=20) as cli:
        for base in _bases_ordered():
            host = base.replace("https://", "")
            tried.append(host)
            for retry in (0, 1):  # retry=1 — только после 10002 с принудительной калибровкой
                try:
                    # СВЕЖАЯ серверная метка на каждую попытку: локальные часы
                    # не участвуют в подписи → 10002 исключён по построению.
                    ts = int(await _server_now_ms(force=retry > 0) - _SIGN_MARGIN_MS)
                    headers = {
                        "X-BAPI-API-KEY": api_key,
                        "X-BAPI-SIGN": _sign(ts, recv, query),
                        "X-BAPI-TIMESTAMP": str(ts),
                        "X-BAPI-RECV-WINDOW": str(recv),
                    }
                    r = await cli.get(base.rstrip("/") + path + ("?" + query if query else ""), headers=headers)
                    data = r.json()
                    if data.get("retCode") == 0:
                        return data.get("result") or {}
                    rc = data.get("retCode")
                    msg = data.get("retMsg") or ""
                    # ошибка ключа/прав — повторять бессмысленно
                    if rc in (10003, 10004, 10005, 33004):
                        hint = {
                            10003: "неверный API-ключ",
                            10004: "ошибка подписи (проверьте secret)",
                            10005: "у ключа нет нужных прав — включите на Bybit доступ к контрактам (Read-Only с 'Contracts') либо Unified Trading",
                            33004: "срок ключа истёк или отозван",
                        }.get(rc, "")
                        raise BybitAuthError(f"Bybit отклонил ключ: {msg} (код {rc}). {hint}")
                    if rc == 10002 and retry == 0:
                        continue  # перекалибруемся по серверному времени и повторим один раз
                    if rc == 110088:
                        raise BybitAuthError(
                            "Bybit требует апгрейд аккаунта до UTA 2.0 для этого эндпоинта "
                            "(код 110088). На чтение истории closed-pnl это не влияет; "
                            "апгрейд: приложение Bybit → Профиль → Upgrade to UTA 2.0."
                        )
                    # Любая другая логическая ошибка API одинакова на всех хостах —
                    # сразу показываем её как есть (это НЕ сеть).
                    raise BybitAuthError(f"Bybit API: {msg} (код {rc})")
                except BybitAuthError:
                    raise
                except Exception as e:
                    last_err = e
                    break  # сетевая ошибка → следующий хост
    raise BybitAuthError(
        f"Все хосты Bybit недоступны ({', '.join(tried)}). "
        f"Последняя ошибка: {last_err}. Похоже на блокировку сети/гео — попробуйте VPN "
        f"или другой интернет-канал, затем нажмите «Диагностика API» в Настройках."
    )


async def fetch_executions(days: int = 90, category: str = "linear", max_pages: int = 10) -> List[dict]:
    """История исполнений (fills) за период. Лимит Bybit — окно ≤7 дней,
    поэтому период нарезается на 6.9-дневные окна."""
    return await _fetch_windowed("/v5/execution/list", days, category, max_pages)


async def fetch_closed_pnl(days: int = 90, category: str = "linear", max_pages: int = 10) -> List[dict]:
    """Закрытые позиции с PnL (главный источник для статистики).
    Окно запроса у Bybit — не более 7 дней: нарезаем период."""
    return await _fetch_windowed("/v5/position/closed-pnl", days, category, max_pages)


async def _fetch_windowed(path: str, days: int, category: str, max_pages_per_window: int) -> List[dict]:
    days = max(1, min(days, 400))
    end = int(time.time() * 1000)
    start_all = end - days * 86_400_000
    win = int(6.9 * 86_400_000)  # чуть меньше лимита Bybit (7 дней)
    out: List[dict] = []
    w_end = float(end)
    while w_end > start_all:
        w_start = max(float(start_all), w_end - win)
        cursor = None
        for _ in range(max_pages_per_window):
            res = await _get(path, {
                "category": category, "startTime": int(w_start), "endTime": int(w_end),
                "limit": 100, "cursor": cursor,
            })
            out.extend(res.get("list") or [])
            cursor = res.get("nextPageCursor")
            if not cursor:
                break
        w_end = w_start
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
        srv = await _server_now_ms(force=True)
        out["time_offset_ms"] = round(srv - time.time() * 1000)
    if settings.has_bybit_keys() and out["network_ok"]:
        ts = int(await _server_now_ms() - _SIGN_MARGIN_MS)
        recv = 20000
        query = "category=linear&limit=1"
        headers = {
            "X-BAPI-API-KEY": api_key,
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
            tips.append("Код 10002: сервер отверг метку времени — повторите (подпись идёт по серверному времени, пришлите вывод bybit_key_test.py, если повторяется).")
        elif rc == 110088:
            tips.append("Код 110088: Bybit просит апгрейд до UTA 2.0 (Профиль → Upgrade to UTA 2.0). История closed-pnl читается и без апгрейда.")
        elif "не введены" in (kc.get("detail") or ""):
            tips.append("Введите read-only ключи и сохраните.")
    out["tips"] = tips
    return out
