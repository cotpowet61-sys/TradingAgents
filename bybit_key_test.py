# -*- coding: utf-8 -*-
"""
bybit_key_test.py — автономный тестер ключей Bybit (read-only).

ЗАЧЕМ ЭТОТ ФАЙЛ
---------------
Диагностируем ошибку 10002 «invalid request, please check your server timestamp».
Причина: локальные часы компьютера расходятся с часами сервера Bybit, и подпись
запроса уходит с меткой времени «из будущего» или «из далёкого прошлого».

ОБХОД, КОТОРЫЙ ТУТ ПРОВЕРЯЕМ
----------------------------
Мы ВООБЩЕ не доверяем локальным часам: перед КАЖДЫМ подписанным запросом
скрипт берёт свежее время сервера Bybit (публичный эндпоинт /v5/market/time,
подпись не нужна) и подписывает запрос этим серверным временем минус небольшой
запас. Сервер не может отвергнуть свою же метку времени — 10002 исчезает.

Файл полностью автономен: ТОЛЬКО стандартная библиотека Python (3.8+),
ничего ставить не нужно. Ничего в проекте он не меняет.

КАК ЗАПУСТИТЬ (Windows / macOS / Linux)
---------------------------------------
  1. Положить файл рядом с папкой portal (в корень проекта) — ключи подхватятся
     из portal\\.env автоматически. Либо ничего не класть — скрипт спросит ключи.
  2. Открыть терминал в этой папке и выполнить:

       python bybit_key_test.py                 # полный тест
       python bybit_key_test.py --stress 20     # 20 подписанных запросов подряд
       python bybit_key_test.py --days 90       # история глубиной 90 дней
       python bybit_key_test.py --key XXX --secret YYY   # ключи явно

По итогам скрипт пишет ВЕРДИКТ: что работает, что нет и почему.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

HOSTS = [
    "https://api.bybit.com",
    "https://api2.bybit.com",
    "https://api.bytick.com",
    "https://api.bybit.eu",
]

RECV_WINDOW = "20000"          # широкое окно приёма (мс)
HTTP_TIMEOUT = 10              # сек
SIGN_MARGIN_MS = 1000          # подписываем серверным временем МИНУС 1 сек (запас)
WINDOW_MS = int(6.9 * 24 * 3600 * 1000)   # окно истории 6.9 суток (лимит Bybit = 7)

# ---------------------------------------------------------------------------
# Вывод
# ---------------------------------------------------------------------------

def _setup_console() -> None:
    """UTF-8 в консоли Windows, чтобы кириллица не превращалась в кашу."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def ok(msg: str) -> None:   print(f"  [OK]    {msg}")
def fail(msg: str) -> None: print(f"  [FAIL]  {msg}")
def info(msg: str) -> None: print(f"  [..]    {msg}")
def head(msg: str) -> None: print(f"\n=== {msg} " + "=" * max(2, 58 - len(msg)))


def utc_str(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + f".{ms % 1000:03d} UTC"


def mask(s: str) -> str:
    if not s:
        return "(пусто)"
    return s[:3] + "..." + s[-3:] if len(s) > 8 else "***"


# ---------------------------------------------------------------------------
# HTTP (urllib, без сторонних библиотек)
# ---------------------------------------------------------------------------

def http_get_json(url: str, headers: dict | None = None):
    """GET -> (status, json|None, rtt_ms, error_text). Тело читаем даже при 4xx/5xx."""
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        status = e.code
    except Exception as e:
        return None, None, (time.perf_counter() - t0) * 1000, f"{type(e).__name__}: {e}"
    rtt = (time.perf_counter() - t0) * 1000
    try:
        return status, json.loads(body), rtt, None
    except Exception:
        return status, None, rtt, f"не-JSON ответ: {body[:160]}"


# ---------------------------------------------------------------------------
# Время сервера Bybit — сердце обхода
# ---------------------------------------------------------------------------

def fetch_server_time(host: str):
    """Возвращает (server_ms, rtt_ms) или (None, err)."""
    status, data, rtt, err = http_get_json(host + "/v5/market/time")
    if err or not data:
        return None, err or f"HTTP {status}"
    if data.get("retCode") != 0:
        return None, f"retCode {data.get('retCode')}: {data.get('retMsg')}"
    res = data.get("result", {})
    # V5 отдаёт timeSecond и timeNano — собираем миллисекунды
    try:
        sec = int(res.get("timeSecond", "0"))
        nano = int(str(res.get("timeNano", "0"))[:9] or "0")
        return sec * 1000 + nano // 1_000_000, rtt
    except Exception as e:
        return None, f"странный формат времени: {res!r} ({e})"


def server_timestamp_for_sign(host: str):
    """Свежая серверная метка для подписи: server_now - запас.

    Подпись получается гарантированно «в прошлом» относительно сервера,
    поэтому 10002 по причине «метка из будущего» невозможен в принципе.
    """
    server_ms, rtt_or_err = fetch_server_time(host)
    if server_ms is None:
        raise RuntimeError(f"время сервера недоступно: {rtt_or_err}")
    return server_ms - SIGN_MARGIN_MS, server_ms, rtt_or_err


# ---------------------------------------------------------------------------
# Подписанный GET (Bybit V5)
# ---------------------------------------------------------------------------

def signed_get(host: str, path: str, params: dict, key: str, secret: str):
    """Подписывает запрос СВЕЖИМ серверным временем (обход привязки к локальным часам)."""
    ts, server_ms, rtt = server_timestamp_for_sign(host)
    query = urllib.parse.urlencode(params)
    payload = f"{ts}{key}{RECV_WINDOW}{query}"
    sign = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": key,
        "X-BAPI-SIGN": sign,
        "X-BAPI-TIMESTAMP": str(ts),
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "X-BAPI-SIGN-TYPE": "2",
    }
    url = f"{host}{path}?{query}" if query else host + path
    status, data, http_rtt, err = http_get_json(url, headers)
    return {
        "status": status, "data": data, "err": err,
        "ts_sent": ts, "server_ms": server_ms,
        "time_rtt": rtt, "http_rtt": http_rtt,
    }


def retcode_of(resp: dict):
    d = resp.get("data") or {}
    return d.get("retCode"), str(d.get("retMsg", ""))


# ---------------------------------------------------------------------------
# Ключи: аргументы -> переменные окружения -> .env -> ввод руками
# ---------------------------------------------------------------------------

def load_env_file(paths):
    found = {}
    for p in paths:
        if not os.path.isfile(p):
            continue
        info(f"читаю {p}")
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v and k not in found:
                    found[k] = v
        break
    return found


def resolve_keys(args):
    here = os.path.dirname(os.path.abspath(__file__))
    env = load_env_file([
        os.path.join(here, "portal", ".env"),
        os.path.join(here, ".env"),
        os.path.join(os.getcwd(), "portal", ".env"),
        os.path.join(os.getcwd(), ".env"),
    ])
    key = (args.key or os.environ.get("BYBIT_API_KEY") or env.get("BYBIT_API_KEY")
           or env.get("BYBIT_API_KEY_RO") or "").strip()
    secret = (args.secret or os.environ.get("BYBIT_API_SECRET") or env.get("BYBIT_API_SECRET")
              or env.get("BYBIT_API_SECRET_RO") or "").strip()
    if key and secret:
        return key, secret
    print("\nКлючи не найдены ни в аргументах, ни в .env. Введи вручную")
    print("(ничего страшного, что секрет не отображается при вводе — так задумано).")
    try:
        if not key:
            key = input("  API key: ").strip()
        if not secret:
            secret = getpass.getpass("  API secret (скрытый ввод): ").strip()
    except (EOFError, KeyboardInterrupt):
        pass
    return key, secret


# ---------------------------------------------------------------------------
# Шаги теста
# ---------------------------------------------------------------------------

def step_connectivity():
    head("ШАГ 1. Связь с Bybit и сравнение часов")
    working = []
    for host in HOSTS:
        server_ms, rtt_or_err = fetch_server_time(host)
        local_ms = int(time.time() * 1000)
        if server_ms is None:
            fail(f"{host} — недоступен ({rtt_or_err})")
            continue
        skew = local_ms - server_ms
        direction = "ЛОКАЛЬНЫЕ ЧАСЫ СПЕШАТ" if skew > 0 else "локальные часы отстают"
        ok(f"{host} — жив, RTT {rtt_or_err:.0f} мс")
        info(f"  сервер: {utc_str(server_ms)}")
        info(f"  локально: {utc_str(local_ms)}  |  сдвиг {abs(skew)} мс ({direction})")
        working.append((host, abs(skew), rtt_or_err))
    if not working:
        fail("Ни один хост Bybit не отвечает — вероятна гео-блокировка или нет сети.")
        info("Совет: включи VPN (НЛ → Bybit ограничивает) и перезапусти тест.")
        return None
    if any(sk > 500 for _, sk, _ in working):
        info("Сдвиг часов > 0.5 c — именно это ломало подпись раньше.")
        info("В этом тесте подпись идёт по СЕРВЕРНОМУ времени, поэтому сдвиг не страшен.")
    best = min(working, key=lambda x: x[2])[0]
    ok(f"Основной хост для дальнейших шагов: {best}")
    return best


def step_wallet(host, key, secret):
    head("ШАГ 2. Подписанный запрос: баланс (wallet-balance)")
    resp = signed_get(host, "/v5/account/wallet-balance",
                      {"accountType": "UNIFIED"}, key, secret)
    code, msg = retcode_of(resp)
    info(f"подписано меткой {utc_str(resp['ts_sent'])} (серверное время минус {SIGN_MARGIN_MS} мс)")
    if resp["err"]:
        fail(f"сеть: {resp['err']}");  return False
    if code == 0:
        ok("Подпись принята! retCode 0")
        try:
            acct = (resp["data"]["result"]["list"] or [{}])[0]
            coins = acct.get("coin", [])
            total = acct.get("totalEquity", "?")
            info(f"  equity аккаунта: {total} USD(E) | монет с балансом: {len(coins)}")
            for c in coins[:5]:
                info(f"    {c.get('coin')}: {c.get('walletBalance')}")
        except Exception:
            pass
        return True
    fail(f"retCode {code}: {msg}")
    hint = {
        10003: "API-ключ неверный/удалён — пересоздай ключ в Bybit → API Management.",
        10004: "Подпись не сошлась — почти наверняка перепутан/обрезан SECRET. Скопируй заново целиком.",
        10005: "У ключа нет нужного разрешения — нужен как минимум Read-only на Unified Trading.",
        10010: "IP не в белом списке ключа — либо убери привязку по IP, либо добавь текущий.",
        110088: "Аккаунт UTA 1.0: этот эндпоинт требует UTA 2.0. На историю сделок не влияет.",
        10002: "Метка времени отвергнута — НЕ ДОЛЖНО появляться в этом тесте; если появилось — пришли вывод мне.",
    }.get(code, "Смотри текст ошибки выше.")
    info(f"расшифровка: {hint}")
    return False


def step_positions(host, key, secret):
    head("ШАГ 3. Открытые позиции (position/list, linear)")
    for params in ({"category": "linear", "settleCoin": "USDT"}, {"category": "linear"}):
        resp = signed_get(host, "/v5/position/list", params, key, secret)
        code, msg = retcode_of(resp)
        if resp["err"]:
            fail(f"сеть: {resp['err']}");  return
        if code == 110088 and "settleCoin" in params:
            continue  # пробуем без settleCoin
        if code == 110088:
            info("retCode 110088: аккаунт UTA 1.0 — Bybit закрыл чтение позиций до апгрейда до UTA 2.0.")
            info("Это НЕ ошибка ключа и НЕ ошибка подписи. История сделок (шаг 4) работает без апгрейда.")
            info("Захочешь позиции: приложение Bybit → Профиль → Upgrade to UTA 2.0.")
            return
        if code != 0:
            fail(f"retCode {code}: {msg}");  return
        rows = (resp["data"].get("result") or {}).get("list", [])
        live = [r for r in rows if float(r.get("size") or 0) > 0]
        ok(f"позиций с объёмом: {len(live)}")
        for r in live[:8]:
            info(f"  {r.get('symbol')} {r.get('side')} size={r.get('size')} "
                 f"entry={r.get('avgPrice')} uPnL={r.get('unrealisedPnl')}")
        return


def step_history(host, key, secret, days):
    head(f"ШАГ 4. История сделок окнами по 6.9 сут (глубина {days} дн.)")
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    n_windows = max(1, -(-(end - start) // WINDOW_MS))
    info(f"окон к опросу: {n_windows} (лимит Bybit — 7 суток на запрос)")

    total_exec, total_pnl = 0, 0
    first_ts, last_ts = None, None
    w_start = start
    while w_start < end:
        w_end = min(w_start + WINDOW_MS, end)
        for label, path, params in (
            ("executions", "/v5/execution/list",
             {"category": "linear", "startTime": int(w_start), "endTime": int(w_end), "limit": 100}),
            ("closed-pnl", "/v5/position/closed-pnl",
             {"category": "linear", "startTime": int(w_start), "endTime": int(w_end), "limit": 100}),
        ):
            resp = signed_get(host, path, params, key, secret)
            code, msg = retcode_of(resp)
            if resp["err"] or code != 0:
                fail(f"{label} [{utc_str(w_start)[:10]}..{utc_str(w_end)[:10]}]: "
                     f"{resp['err'] or f'retCode {code}: {msg}'}")
                continue
            rows = (resp["data"].get("result") or {}).get("list", [])
            if label == "executions":
                total_exec += len(rows)
                for r in rows:
                    t = int(r.get("execTime", 0))
                    first_ts = t if first_ts is None else min(first_ts, t)
                    last_ts = t if last_ts is None else max(last_ts, t)
            else:
                total_pnl += len(rows)
        w_start = w_end + 1
    ok(f"исполнений (executions): {total_exec} | закрытых позиций (closed-pnl): {total_pnl}")
    if first_ts:
        info(f"  период первой..последней сделки: {utc_str(first_ts)[:16]} → {utc_str(last_ts)[:16]}")
    elif total_pnl > 0:
        info("  executions пуст, но closed-pnl есть — для аналитики (ДНК трейдера) этого достаточно.")
    elif days >= 7:
        info("  сделок в периоде нет — либо их правда не было, либо ключ читает другой аккаунт.")


def step_stress(host, key, secret, n):
    head(f"ШАГ 5. Стресс-тест подписи: {n} запросов подряд (10002 не должно быть)")
    codes = {}
    bad = []
    for i in range(1, n + 1):
        resp = signed_get(host, "/v5/account/wallet-balance",
                          {"accountType": "UNIFIED"}, key, secret)
        code, msg = retcode_of(resp)
        code = code if code is not None else f"NET:{resp['err']}"
        codes[code] = codes.get(code, 0) + 1
        if code != 0:
            bad.append((i, code, msg))
        print(f"\r  ... {i}/{n}", end="", flush=True)
    print()
    for c, cnt in sorted(codes.items(), key=lambda x: str(x[0])):
        (ok if c == 0 else fail)(f"retCode {c}: {cnt} раз(а)")
    if bad:
        for i, c, m in bad[:5]:
            info(f"  попытка #{i}: {c} {m}")
    return codes.get(0, 0) == n


# ---------------------------------------------------------------------------

def main() -> int:
    _setup_console()
    ap = argparse.ArgumentParser(description="Автономный тестер ключей Bybit (read-only)")
    ap.add_argument("--key", help="API key (иначе берётся из portal/.env или спросит)")
    ap.add_argument("--secret", help="API secret")
    ap.add_argument("--days", type=int, default=30, help="глубина истории, дней (по умолч. 30)")
    ap.add_argument("--stress", type=int, default=0, metavar="N",
                    help="сделать N подписанных запросов подряд (проверка на 10002)")
    ap.add_argument("--no-pause", action="store_true", help="не ждать Enter в конце")
    args = ap.parse_args()

    print("╔" + "═" * 62 + "╗")
    print("║   BYBIT KEY TESTER — обход ошибки 10002 (серверная подпись)    ║")
    print("╚" + "═" * 62 + "╝")
    info(f"время запуска локальное: {utc_str(int(time.time()*1000))}")

    host = step_connectivity()
    if not host:
        if not args.no_pause:
            input("\nНажми Enter для выхода...")
        return 2

    key, secret = resolve_keys(args)
    info(f"key: {mask(key)} | secret: {mask(secret)}")
    if not key or not secret:
        fail("Ключи не заданы — дальше проверять нечего.")
        if not args.no_pause:
            input("\nНажми Enter для выхода...")
        return 2

    auth_ok = step_wallet(host, key, secret)
    all_ok = auth_ok
    if auth_ok:
        step_positions(host, key, secret)
        step_history(host, key, secret, max(1, min(args.days, 400)))
        if args.stress > 0:
            all_ok = step_stress(host, key, secret, args.stress) and all_ok

    head("ВЕРДИКТ")
    if all_ok and auth_ok:
        ok("Ключи рабочие, подпись по серверному времени стабильна.")
        ok("10002 обойдена. Скинь этот вывод мне — встрою тот же механизм в портал.")
    elif auth_ok and not all_ok:
        ok("Ключ валиден, но стресс-тест нашёл нестабильность (см. выше).")
    else:
        fail("Подпись не принимается — чини по расшифровке в шаге 2 и перезапусти.")

    if not args.no_pause:
        input("\nНажми Enter для выхода...")
    return 0 if (all_ok and auth_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
