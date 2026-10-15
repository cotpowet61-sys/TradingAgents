"""
Аккаунты и сессии (SaaS). Пароли — PBKDF2-HMAC-SHA256 (stdlib, 260k итераций,
соль на юзера). Сессии — случайный токен в HttpOnly-куке, в БД лежит только
sha256 токена → даже копия БД не даёт готовых сессий.

Секретов здесь нет: сервер не хранит ничего, что можно украсть деньгами.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Optional

import db
import security

MS = 1000
SESSION_MS = 30 * 86400 * MS
SESSION_IDLE_MS = 30 * 86400 * MS    # скользящее окно: продлеваем при активности
PBKDF2_ITERS = 260_000               # текущая стоимость; старые юзеры — по своей из БД
PBKDF2_MIN_ITERS = 100_000

COOKIE = "tav_session"

# быстрый кэш «мёртвых» токенов, чтобы не ходить в БД на каждый запрос
_dead: dict[str, float] = {}
_DEAD_TTL = 15.0


# --------------------------------------------------------------------- #
# Пароли
# --------------------------------------------------------------------- #
def hash_password(password: str, salt_hex: str | None = None,
                  iters: int = PBKDF2_ITERS) -> tuple[str, str]:
    """PBKDF2-HMAC-SHA256 с солью на пользователя.

    iters едет вместе с хэшем в БД: стоимость можно поднимать, не ломая
    существующие аккаунты (при совпадении пароля хэш тихо переписывается).
    """
    salt_hex = salt_hex or secrets.token_hex(16)
    iters = max(PBKDF2_MIN_ITERS, int(iters or PBKDF2_ITERS))
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), iters)
    return h.hex(), salt_hex


def check_password(password: str, pass_hex: str, salt_hex: str, iters: int = PBKDF2_ITERS) -> bool:
    calc, _ = hash_password(password, salt_hex, iters)
    return hmac.compare_digest(calc, pass_hex)


# --------------------------------------------------------------------- #
# Пользователи
# --------------------------------------------------------------------- #
def register(email: str, password: str, referred_by: str = "") -> dict:
    email = (email or "").strip().lower()
    if not security.email_ok(email):
        raise ValueError("invalid_email")
    prob = security.password_problem(password)
    if prob:
        raise ValueError(prob)
    if db.q("SELECT id FROM users WHERE email=?", (email,), one=True):
        raise ValueError("email_taken")
    pass_hex, salt_hex = hash_password(password, iters=PBKDF2_ITERS)
    now = int(time.time() * MS)
    ref = secrets.token_hex(4)
    if referred_by and not db.q("SELECT code FROM traffickers WHERE code=? AND active=1", (referred_by,), one=True):
        referred_by = ""  # атрибуция только на живых трафферов
    db.execute(
        "INSERT INTO users(email, pass_hex, salt_hex, created_ms, ref_code, referred_by, iters)"
        " VALUES (?,?,?,?,?,?,?)",
        (email, pass_hex, salt_hex, now, ref, referred_by, PBKDF2_ITERS),
    )
    return db.q("SELECT * FROM users WHERE email=?", (email,), one=True)


def login(email: str, password: str) -> Optional[dict]:
    email = (email or "").strip().lower()
    user = db.q("SELECT * FROM users WHERE email=?", (email,), one=True)
    # ветка фейковой проверки — сглаживает тайминг «нет такого email»
    iters = int((user or {}).get("iters") or PBKDF2_ITERS)
    ph = user["pass_hex"] if user else ("0" * 64)
    sh = user["salt_hex"] if user else (secrets.token_hex(16))
    ok = check_password(password or "", ph, sh, iters)
    if not ok or not user:
        return None
    if user["status"] != "active":
        return None
    # тихий апгрейд стоимости хэша, когда политика поднялась
    if iters < PBKDF2_ITERS:
        try:
            new_hex, new_salt = hash_password(password, iters=PBKDF2_ITERS)
            db.execute("UPDATE users SET pass_hex=?, salt_hex=?, iters=? WHERE id=?",
                       (new_hex, new_salt, PBKDF2_ITERS, user["id"]))
            user["pass_hex"], user["salt_hex"], user["iters"] = new_hex, new_salt, PBKDF2_ITERS
        except Exception:
            pass
    return user


# --------------------------------------------------------------------- #
# Сессии
# --------------------------------------------------------------------- #
def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time() * MS)
    db.execute(
        "INSERT INTO sessions(token_hash, user_id, created_ms, expires_ms, last_seen_ms)"
        " VALUES (?,?,?,?,?)",
        (hashlib.sha256(token.encode()).hexdigest(), user_id, now, now + SESSION_MS, now),
    )
    return token


def user_by_token(token: str | None) -> Optional[dict]:
    """Пользователь по cookie-токену.

    Скользящее окно: активная сессия продлевается, но не чаще раза в час.
    Мёртвые токены на 15 секунд кэшируются в памяти — иначе каждый запрос
    к /api/* бил бы в БД дважды.
    """
    if not token:
        return None
    th = hashlib.sha256(token.encode()).hexdigest()
    now_t = time.time()
    if _dead.get(th, 0) > now_t:
        return None
    now = int(now_t * MS)
    s = db.q("SELECT * FROM sessions WHERE token_hash=? AND expires_ms>?", (th, now), one=True)
    if not s:
        if len(_dead) > 5000:
            _dead.clear()
        _dead[th] = now_t + _DEAD_TTL
        return None
    user = db.q("SELECT * FROM users WHERE id=? AND status='active'", (s["user_id"],), one=True)
    if not user:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (th,))
        return None
    # продление окна активности (не чаще раза в час)
    if now - int(s.get("last_seen_ms") or 0) > 3600 * MS:
        try:
            db.execute("UPDATE sessions SET last_seen_ms=?, expires_ms=? WHERE token_hash=?",
                       (now, now + SESSION_IDLE_MS, th))
        except Exception:
            pass
    return user


def kill_user_sessions(user_id: int) -> int:
    """Выход со всех устройств (смена/сброс пароля и т.п.)."""
    try:
        return db.execute("DELETE FROM sessions WHERE user_id=?", (int(user_id),))
    except Exception:
        return 0


def logout(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))


def cleanup_sessions() -> None:
    db.execute("DELETE FROM sessions WHERE expires_ms<?", (int(time.time() * MS),))


# --------------------------------------------------------------------- #
# Подписка (строгая: ровно 30 дней, стекуется от текущей даты окончания)
# --------------------------------------------------------------------- #
MONTH_MS = 30 * 86400 * MS


def is_owner(user: dict) -> bool:
    """Владелец продукта (OWNER_EMAILS в .env) — вечная подписка."""
    if not user:
        return False
    try:
        from portal_config import settings as _s
        em = (user.get("email") or "").strip().lower()
        owners = {e.strip().lower() for e in (_s.OWNER_EMAILS or "").split(",") if e.strip()}
        return bool(em) and em in owners
    except Exception:
        return False


def sub_active(user: dict) -> bool:
    if not user:
        return False
    if is_owner(user):
        return True
    return int(user.get("sub_until_ms") or 0) > int(time.time() * MS)


def sub_days_left(user: dict) -> float:
    if is_owner(user):
        return 3650.0  # владельцу не показываем таймер — UI рисует ∞
    left = int(user.get("sub_until_ms") or 0) - int(time.time() * MS)
    return round(max(0.0, left / (86400 * MS)), 1)


def extend_subscription(user_id: int, months: int = 1) -> int:
    """Строгое продление: от now или от sub_until (что позже) + 30/30.. дней.
    Возвращает новый sub_until_ms."""
    now = int(time.time() * MS)
    u = db.q("SELECT sub_until_ms FROM users WHERE id=?", (user_id,), one=True)
    base = max(now, int(u["sub_until_ms"]) if u else 0)
    new_until = base + MONTH_MS * months
    db.execute("UPDATE users SET sub_until_ms=? WHERE id=?", (new_until, user_id))
    return new_until


# --------------------------------------------------------------------- #
# Rate-limit (персистентный, см. security.rate_ok)
# --------------------------------------------------------------------- #
def rate_ok(key: str, limit: int = 8, window_s: int = 300) -> bool:
    allowed, _ = security.rate_ok(key, limit=limit, window_s=window_s)
    return allowed


def rate_retry(key: str, limit: int = 8, window_s: int = 300) -> int:
    allowed, retry = security.rate_ok(key, limit=limit, window_s=window_s)
    return 0 if allowed else retry
