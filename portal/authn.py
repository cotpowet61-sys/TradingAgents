"""
Аккаунты и сессии (SaaS). Пароли — PBKDF2-HMAC-SHA256 (stdlib, 260k итераций,
соль на юзера). Сессии — случайный токен в HttpOnly-куке, в БД лежит только
sha256 токена → даже копия БД не даёт готовых сессий.

Секретов здесь нет: сервер не хранит ничего, что можно украсть деньгами.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from typing import Optional

import db

MS = 1000
SESSION_MS = 30 * 86400 * MS
_PBKDF2_ITERS = 260_000
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,64}\.[^@\s]{2,24}$")

COOKIE = "tav_session"


# --------------------------------------------------------------------- #
# Пароли
# --------------------------------------------------------------------- #
def hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    salt_hex = salt_hex or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), _PBKDF2_ITERS)
    return h.hex(), salt_hex


def check_password(password: str, pass_hex: str, salt_hex: str) -> bool:
    calc, _ = hash_password(password, salt_hex)
    return hmac.compare_digest(calc, pass_hex)


# --------------------------------------------------------------------- #
# Пользователи
# --------------------------------------------------------------------- #
def register(email: str, password: str) -> dict:
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValueError("invalid_email")
    if len(password or "") < 6:
        raise ValueError("weak_password")
    if db.q("SELECT id FROM users WHERE email=?", (email,), one=True):
        raise ValueError("email_taken")
    pass_hex, salt_hex = hash_password(password)
    now = int(time.time() * MS)
    ref = secrets.token_hex(4)
    db.execute(
        "INSERT INTO users(email, pass_hex, salt_hex, created_ms, ref_code) VALUES (?,?,?,?,?)",
        (email, pass_hex, salt_hex, now, ref),
    )
    return db.q("SELECT * FROM users WHERE email=?", (email,), one=True)


def login(email: str, password: str) -> Optional[dict]:
    email = (email or "").strip().lower()
    user = db.q("SELECT * FROM users WHERE email=?", (email,), one=True)
    # ветка фейковой проверки — сглаживает тайминг «нет такого email»
    ph, sh = (user["pass_hex"], user["salt_hex"]) if user else ("0" * 64, secrets.token_hex(16))
    if not check_password(password or "", ph, sh) or not user:
        return None
    if user["status"] != "active":
        return None
    return user


# --------------------------------------------------------------------- #
# Сессии
# --------------------------------------------------------------------- #
def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time() * MS)
    db.execute(
        "INSERT INTO sessions(token_hash, user_id, created_ms, expires_ms) VALUES (?,?,?,?)",
        (hashlib.sha256(token.encode()).hexdigest(), user_id, now, now + SESSION_MS),
    )
    return token


def user_by_token(token: str | None) -> Optional[dict]:
    if not token:
        return None
    th = hashlib.sha256(token.encode()).hexdigest()
    now = int(time.time() * MS)
    s = db.q("SELECT * FROM sessions WHERE token_hash=? AND expires_ms>?", (th, now), one=True)
    if not s:
        return None
    user = db.q("SELECT * FROM users WHERE id=? AND status='active'", (s["user_id"],), one=True)
    return user


def logout(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))


def cleanup_sessions() -> None:
    db.execute("DELETE FROM sessions WHERE expires_ms<?", (int(time.time() * MS),))


# --------------------------------------------------------------------- #
# Подписка (строгая: ровно 30 дней, стекуется от текущей даты окончания)
# --------------------------------------------------------------------- #
MONTH_MS = 30 * 86400 * MS


def sub_active(user: dict) -> bool:
    return bool(user) and int(user.get("sub_until_ms") or 0) > int(time.time() * MS)


def sub_days_left(user: dict) -> float:
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
# Примитивный rate-limit для /auth (защита от перебора)
# --------------------------------------------------------------------- #
_rl: dict[str, list[float]] = {}
_rl_lock = threading.Lock()


def rate_ok(key: str, limit: int = 8, window_s: int = 300) -> bool:
    now = time.time()
    with _rl_lock:
        arr = [t for t in _rl.get(key, []) if now - t < window_s]
        if len(arr) >= limit:
            _rl[key] = arr
            return False
        arr.append(now)
        _rl[key] = arr
        if len(_rl) > 5000:  # не раздуваем память
            cutoff = now - window_s
            for k in [k for k, v in _rl.items() if not v or v[-1] < cutoff][:1000]:
                _rl.pop(k, None)
        return True
