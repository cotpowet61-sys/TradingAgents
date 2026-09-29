"""
SaaS-база портала: аккаунты, сессии, инвойсы оплаты.

По умолчанию — embedded SQLite (WAL) рядом с данными: portal/data/saas.db.
Его хватает на одном VPS до ~10k+ пользователей и нулевой зависимостью.
Если задана PORTAL_DATABASE_URL (postgres://…), включается Postgres через
psycopg3  →  pip install "psycopg[binary]"  (ставится на VPS при масштабе).

Безопасность: здесь НЕТ и не должно быть никаких секретов/приватных ключей —
только хэши паролей/токенов и публичные адреса платежей.
"""
from __future__ import annotations

import os
import sqlite3
import threading

DATABASE_URL = os.environ.get("PORTAL_DATABASE_URL", "").strip()
_USE_PG = bool(DATABASE_URL)
_pg = None
if _USE_PG:
    try:
        import psycopg  # type: ignore
        from psycopg.rows import dict_row  # type: ignore
        _pg = psycopg
        _dict_row = dict_row
    except ImportError:
        _USE_PG = False  # драйвера нет → тихий фолбэк на SQLite

PORTAL_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PORTAL_DB_PATH", os.path.join(PORTAL_DIR, "data", "saas.db"))
_lock = threading.RLock()

_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE,
  pass_hex TEXT NOT NULL,
  salt_hex TEXT NOT NULL,
  created_ms INTEGER NOT NULL,
  sub_until_ms INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  ref_code TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  created_ms INTEGER NOT NULL,
  expires_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  network TEXT NOT NULL,
  amount REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_ms INTEGER NOT NULL,
  expires_ms INTEGER NOT NULL,
  tx_hash TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS paid_tx (
  tx_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  network TEXT NOT NULL,
  amount REAL NOT NULL,
  ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inv_user ON invoices(user_id, status);
CREATE INDEX IF NOT EXISTS idx_inv_net ON invoices(network, status);
"""

_SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS users (
  id BIGSERIAL PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  pass_hex TEXT NOT NULL,
  salt_hex TEXT NOT NULL,
  created_ms BIGINT NOT NULL,
  sub_until_ms BIGINT NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  ref_code TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id BIGINT NOT NULL,
  created_ms BIGINT NOT NULL,
  expires_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL,
  network TEXT NOT NULL,
  amount DOUBLE PRECISION NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_ms BIGINT NOT NULL,
  expires_ms BIGINT NOT NULL,
  tx_hash TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS paid_tx (
  tx_hash TEXT PRIMARY KEY,
  user_id BIGINT NOT NULL,
  network TEXT NOT NULL,
  amount DOUBLE PRECISION NOT NULL,
  ms BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inv_user ON invoices(user_id, status);
CREATE INDEX IF NOT EXISTS idx_inv_net ON invoices(network, status);
"""


def _conn():
    if _USE_PG:
        return _pg.connect(DATABASE_URL, autocommit=True, row_factory=_dict_row)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_schema() -> None:
    ddl = _SCHEMA_PG if _USE_PG else _SCHEMA_SQLITE
    with _lock, _conn() as c:
        for stmt in [s.strip() for s in ddl.split(";") if s.strip()]:
            c.execute(stmt)


def q(sql: str, params: tuple = (), one: bool = False):
    """Выполнить запрос; вернуть список dict (или один dict/None при one=True)."""
    sql2 = sql.replace("?", "%s") if _USE_PG else sql
    with _lock, _conn() as c:
        cur = c.execute(sql2, params)
        rows = cur.fetchall()
        out = [dict(r) for r in rows]
        return (out[0] if out else None) if one else out


def execute(sql: str, params: tuple = ()) -> int:
    """INSERT/UPDATE/DELETE; вернуть rowcount (для PG autocommit включён)."""
    sql2 = sql.replace("?", "%s") if _USE_PG else sql
    with _lock, _conn() as c:
        cur = c.execute(sql2, params)
        return cur.rowcount


init_schema()
