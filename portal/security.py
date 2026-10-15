"""
Контур безопасности Портала (единое место, чтобы не расползалось по роутам).

Что здесь:
  • security_headers()      — CSP/HSTS/nosniff/frame-ancestors и прочее на каждый ответ;
  • install(app, settings)  — middleware: проверка Origin (анти-CSRF), лимит размера тела,
                              добавление заголовков, no-store для API и страниц кабинета;
  • client_ip()             — IP с учётом доверия к X-Forwarded-For (за Caddy — можно, напрямую — нет);
  • rate_ok()/rate_reset()  — ПЕРСИСТЕНТНЫЙ rate-limit (таблица rate_limits), работает
                              и после рестарта, и при нескольких воркерах;
  • password_problem()      — политика паролей;
  • safe_eq()               — сравнение секретов за постоянное время;
  • audit()                 — журнал действий администратора/платежей (таблица audit).

Модуль не знает про UI и не хранит секретов — только правила и счётчики.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
from typing import Iterable, Optional, Tuple
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import JSONResponse

import db

log = logging.getLogger("portal.security")
MS = 1000

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
_PWD_COMMON = {
    "password", "12345678", "123456789", "qwerty123", "qwertyui", "11111111",
    "password1", "iloveyou", "admin123", "letmein1", "welcome1", "trade123",
    "trader123", "crypto123", "bitcoin1", "1234567890", "qwerty1234", "abc12345",
}
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,64}\.[^@\s]{2,24}$")


# --------------------------------------------------------------------- #
# IP / прокси
# --------------------------------------------------------------------- #
def client_ip(request: Request, trust_proxy: bool = True) -> str:
    """Реальный IP клиента.

    X-Forwarded-For учитываем ТОЛЬКО за доверенным прокси (Caddy ставит его сам).
    Если портал выставлен в интернет напрямую — заголовок подделывается тривиально,
    поэтому при TRUST_PROXY_HEADERS=false он игнорируется целиком.
    """
    if trust_proxy:
        fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if fwd:
            return fwd[:64]
        real = (request.headers.get("x-real-ip") or "").strip()
        if real:
            return real[:64]
    return (request.client.host if request.client else "?") or "?"


# --------------------------------------------------------------------- #
# Rate limit (персистентный)
# --------------------------------------------------------------------- #
def rate_ok(key: str, limit: int, window_s: int, cost: int = 1) -> Tuple[bool, int]:
    """Счётчик попыток в БД. Возвращает (разрешено, retry_after_sec).

    Работает при нескольких процессах/воркерах и не сбрасывается рестартом
    (в отличие от словаря в памяти).
    """
    if limit <= 0:
        return True, 0
    # Хук для тестов/нагрузочных прогонов: RATE_LIMIT_DISABLED=1 отключает счётчики.
    # В бою переменная не выставляется (см. deploy/.env шаблон).
    if os.environ.get("RATE_LIMIT_DISABLED", "").lower() in ("1", "true", "yes"):
        return True, 0
    key = (key or "?")[:200]
    now = int(time.time() * MS)
    start = now - int(window_s * MS)
    try:
        row = db.q("SELECT hits, window_start_ms FROM rate_limits WHERE key=?", (key,), one=True)
        if not row or int(row["window_start_ms"]) < start:
            db.execute(
                "INSERT INTO rate_limits(key, hits, window_start_ms) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET hits=?, window_start_ms=?",
                (key, cost, now, cost, now),
            )
            return True, 0
        if int(row["hits"]) + cost > limit:
            retry = int((int(row["window_start_ms"]) + window_s * MS - now) / MS) + 1
            return False, max(1, retry)
        db.execute("UPDATE rate_limits SET hits=? WHERE key=?", (int(row["hits"]) + cost, key))
        return True, 0
    except Exception as e:  # noqa: BLE001 — БД полетела: не блокируем сервис, но и не пропускаем бесконечно
        log.warning("[security] rate_ok сбой (%s) — пропускаем попытку", e)
        return True, 0


def rate_reset(key: str) -> None:
    try:
        db.execute("DELETE FROM rate_limits WHERE key=?", ((key or "?")[:200],))
    except Exception:
        pass


def prune() -> None:
    """Чистка старых счётчиков и аудита (вызывается на старте и раз в час)."""
    try:
        db.execute("DELETE FROM rate_limits WHERE window_start_ms < ?",
                   (int(time.time() * MS) - 7 * 86400 * MS,))
        db.execute("DELETE FROM audit WHERE ts_ms < ?",
                   (int(time.time() * MS) - 180 * 86400 * MS,))
    except Exception:
        pass


# --------------------------------------------------------------------- #
# Пароли / секреты
# --------------------------------------------------------------------- #
def email_ok(email: str) -> bool:
    return bool(_EMAIL_RE.match((email or "").strip().lower()))


def password_problem(password: str, min_len: int = 8) -> Optional[str]:
    """Вернёт код проблемы или None. Политика намеренно простая и понятная."""
    p = password or ""
    if len(p) < max(8, int(min_len)):
        return "weak_password"
    if p.lower() in _PWD_COMMON:
        return "weak_password"
    if len(set(p)) < 4:
        return "weak_password"
    return None


def safe_eq(a: str, b: str) -> bool:
    """Сравнение секретов за постоянное время + защита от пустых значений."""
    a = str(a or "")
    b = str(b or "")
    if not a or not b:
        return False
    return hmac.compare_digest(a, b)


def token_fingerprint(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()[:12]


# --------------------------------------------------------------------- #
# Аудит действий (деньги/админка)
# --------------------------------------------------------------------- #
def audit(action: str, actor: str = "", ip: str = "", detail: str = "") -> None:
    try:
        db.execute(
            "INSERT INTO audit(action, actor, ip, detail, ts_ms) VALUES(?,?,?,?,?)",
            (str(action)[:64], str(actor)[:120], str(ip)[:64], str(detail)[:400],
             int(time.time() * MS)),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("[security] audit сбой: %s", e)


def audit_tail(n: int = 200) -> list[dict]:
    try:
        return db.q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (int(n),))
    except Exception:
        return []


# --------------------------------------------------------------------- #
# Origin / CSRF
# --------------------------------------------------------------------- #
def _extra_origins(settings) -> set[str]:
    raw = (getattr(settings, "ALLOWED_ORIGINS", "") or "").replace(";", ",")
    out: set[str] = set()
    for part in raw.split(","):
        part = part.strip().rstrip("/").lower()
        if part:
            out.add(part)
    site = (getattr(settings, "SITE_URL", "") or "").strip().rstrip("/").lower()
    if site.startswith("http"):
        out.add(site)
    return out


def origin_allowed(request: Request, settings) -> bool:
    """Анти-CSRF: небезопасный метод должен прийти с нашего же хоста.

    Браузер ВСЕГДА присылает Origin на кросс-сайтовые POST — поэтому сравнения
    Origin с Host достаточно, чтобы отсечь чужой сайт, использующий наши cookie.
    Запросы без Origin/Referer (curl, боты, healthchecks) пропускаем: cookie
    у них нет, а CSRF без cookie невозможен.
    """
    if request.method in SAFE_METHODS:
        return True
    if not getattr(settings, "CSRF_ORIGIN_CHECK", True):
        return True
    origin = (request.headers.get("origin") or "").strip().lower()
    if not origin:
        ref = (request.headers.get("referer") or "").strip().lower()
        if ref:
            origin = f"{urlparse(ref).scheme}://{urlparse(ref).netloc}"
        else:
            return True
    o_host = urlparse(origin).netloc.lower()
    hosts = {
        (request.headers.get("host") or "").lower(),
        # прокси (Caddy/E2B) может переписать Host, но обязан оставить X-Forwarded-Host
        (request.headers.get("x-forwarded-host") or "").lower().split(",")[0].strip(),
    }
    hosts.discard("")
    if o_host and o_host in hosts:
        return True
    origin_norm = origin.rstrip("/")
    if origin_norm in _extra_origins(settings):
        return True
    # сравниваем и по хосту из SITE_URL/ALLOWED_ORIGINS (порты/схемы могут отличаться
    # при доступе по IP или через превью-домен)
    allowed_hosts = {urlparse(o).netloc.lower() for o in _extra_origins(settings)}
    if o_host and o_host in allowed_hosts:
        return True
    log.warning("[security] отклонён %s %s: origin=%s hosts=%s",
                request.method, request.url.path, origin, ",".join(sorted(hosts)))
    return False


# --------------------------------------------------------------------- #
# Заголовки
# --------------------------------------------------------------------- #
def normalize_frame_ancestors(spec: str) -> str:
    """Приводит frame-ancestors к валидному виду.

    Важно: в dotenv кавычки — синтаксис. Значение FRAME_ANCESTORS='self' в .env
    доедет как `self`, а в CSP ключевые слова обязаны быть в кавычках
    ('self'/'none') — иначе браузер прочитает их как имя хоста и запретит фрейминг.
    """
    parts = [p for p in (spec or "").split() if p]
    out = []
    for p in parts:
        low = p.lower().strip("'\"")
        if low in ("self", "none"):
            out.append(f"'{low}'")
        else:
            out.append(low)
    return " ".join(out) or "'self'"


def security_headers(settings, https: bool = False) -> dict:
    if not getattr(settings, "SECURITY_HEADERS", True):
        return {}
    frames = normalize_frame_ancestors(getattr(settings, "FRAME_ANCESTORS", "") or "")
    csp = (
        "default-src 'self'; "
        "base-uri 'self'; form-action 'self'; object-src 'none'; "
        "img-src 'self' data:; font-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "connect-src 'self' ws: wss:; frame-ancestors " + frames
    )
    h = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Content-Security-Policy": csp,
        "X-Frame-Options": "SAMEORIGIN" if "self" in frames else "DENY",
    }
    if getattr(settings, "HSTS_ENABLED", True) and https:
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return h


# --------------------------------------------------------------------- #
# Middleware
# --------------------------------------------------------------------- #
_HEADER_SKIP = ("/static/",)


def install(app, settings) -> None:
    """Навешивает middleware безопасности. Вызывается из server.py один раз."""

    max_body = int(float(getattr(settings, "MAX_BODY_MB", 12.0)) * 1024 * 1024)

    @app.middleware("http")
    async def security_mw(request: Request, call_next):
        # 1) размер тела (грубая защита от «залил 2 ГБ в CSV/JSON»)
        try:
            cl = int(request.headers.get("content-length") or 0)
        except ValueError:
            cl = 0
        if cl and cl > max_body:
            return JSONResponse({"detail": "payload_too_large", "limit_mb": max_body // (1024 * 1024)},
                                status_code=413)

        # 2) анти-CSRF
        if not origin_allowed(request, settings):
            return JSONResponse({"detail": "cross_origin_forbidden"}, status_code=403)

        resp = await call_next(request)

        # 3) заголовки
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
        for k, v in security_headers(settings, https=(proto == "https")).items():
            resp.headers.setdefault(k, v)
        # 4) API и кабинет не кэшируем (в кэше не должно быть чужих данных)
        p = request.url.path
        if p.startswith("/api/") or p in ("/app", "/pay.html", "/index.html"):
            resp.headers.setdefault("Cache-Control", "no-store, max-age=0")
        if p.startswith(_HEADER_SKIP):
            resp.headers.pop("Content-Security-Policy", None)  # статике CSP не нужен
        return resp
