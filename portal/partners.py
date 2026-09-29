"""
Трафферы/партнёры: код-схема атрибуции и реферальные начисления.

  ?t=CODE → кука 7 дней → регистрация фиксирует referred_by → первая оплата
  (и каждое продление того же юзера) начисляет CODE-балансу: PRICE × pct%.
  Hold 7 дней → available → вывод вручную (владельцы шлют USDT, помечают paid).
"""
from __future__ import annotations

import secrets
import time
from typing import Optional

import db

MS = 1000
HOLD_MS = 7 * 86400 * MS
REF_COOKIE_DAYS = 7


def create_partner(code: str, label: str = "", pct: float = 30.0, secret: str | None = None) -> dict:
    code = (code or "").strip().upper().replace(" ", "")
    if not code or len(code) < 3 or len(code) > 32:
        raise ValueError("bad_code")
    if db.q("SELECT code FROM traffickers WHERE code=?", (code,), one=True):
        raise ValueError("code_taken")
    secret = secret or secrets.token_urlsafe(10)
    db.execute("INSERT INTO traffickers(code, label, secret, pct, created_ms) VALUES (?,?,?,?,?)",
               (code, label, secret, pct, int(time.time() * MS)))
    return {"code": code, "secret": secret, "pct": pct}


def record_hit(code: str, ip: str = "", ua: str = "") -> bool:
    """Клик по ссылке траффера (true = код реальный и активный)."""
    t = db.q("SELECT code FROM traffickers WHERE code=? AND active=1", ((code or "").strip().upper(),), one=True)
    if not t:
        return False
    db.execute("INSERT INTO hits(code, ip, ua, created_ms) VALUES (?,?,?,?)",
               (t["code"], ip[:64], ua[:300], int(time.time() * MS)))
    return True


def credit_payment(user: dict, price: float, tx_ref: str = "") -> Optional[dict]:
    """Юзер оплатил → начислить его трафферу (если есть)."""
    code = (user.get("referred_by") or "").strip()
    if not code:
        return None
    t = db.q("SELECT * FROM traffickers WHERE code=? AND active=1", (code,), one=True)
    if not t:
        return None
    amount = round(price * float(t["pct"]) / 100.0, 2)
    now = int(time.time() * MS)
    db.execute("INSERT INTO ledger(code, user_id, amount_usd, status, created_ms) VALUES (?,?,?,?,?)",
               (code, user["id"], amount, "pending", now))
    return {"code": code, "amount": amount, "hold_until": now + HOLD_MS, "pct": t["pct"]}


def release_due() -> int:
    """Перевести протухший hold в available. Вызывается сканером биллинга."""
    cur = db.execute(
        "UPDATE ledger SET status='available', released_ms=? WHERE status='pending' AND created_ms+?<?",
        (int(time.time() * MS), HOLD_MS, int(time.time() * MS)),
    )
    return cur


def mark_paid(code: str, tx_ref: str = "") -> int:
    """Владелец выплатил трафферу USDT → помечаем его available как paid."""
    db.execute("UPDATE ledger SET status='paid', tx_ref=? WHERE code=? AND status='available'",
               (tx_ref, code))
    rows = db.q("SELECT COUNT(*) AS n FROM ledger WHERE code=? AND status='paid'", (code,), one=True)
    return int(rows["n"])


def partner_stat(code: str) -> Optional[dict]:
    t = db.q("SELECT * FROM traffickers WHERE code=?", ((code or "").strip().upper(),), one=True)
    if not t:
        return None
    clicks = db.q("SELECT COUNT(*) AS n FROM hits WHERE code=?", (t["code"],), one=True)["n"]
    signups = db.q("SELECT COUNT(*) AS n FROM users WHERE referred_by=?", (t["code"],), one=True)["n"]
    sales = db.q("SELECT COUNT(DISTINCT user_id) AS n FROM ledger WHERE code=?", (t["code"],), one=True)["n"]
    sums = db.q(
        "SELECT status, COALESCE(SUM(amount_usd),0) AS s FROM ledger WHERE code=? GROUP BY status",
        (t["code"],),
    )
    agg = {r["status"]: round(float(r["s"]), 2) for r in sums}
    return {
        "code": t["code"], "label": t["label"], "pct": t["pct"], "active": bool(t["active"]),
        "clicks": clicks, "signups": signups, "sales": sales,
        "earned_pending": agg.get("pending", 0.0),
        "earned_available": agg.get("available", 0.0),
        "earned_paid": agg.get("paid", 0.0),
    }


def list_partners() -> list[dict]:
    return [partner_stat(r["code"]) for r in db.q("SELECT code FROM traffickers ORDER BY created_ms DESC")]
