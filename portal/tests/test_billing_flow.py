"""Денежный путь целиком: регистрация → инвойс → оплата → доступ.

Это регрессия на P0-дефект: раньше /api/billing/invoice и /api/billing/status
падали с KeyError 'expires_at' (в БД колонка expires_ms), то есть клиент
физически не мог оплатить подписку. Здесь путь проверяется по шагам.
"""
from __future__ import annotations

import time

from conftest import make_client


def _register(c, email="payer@example.com", password="strong-pass-123"):
    r = c.post("/api/auth/register", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r


def test_invoice_and_status_do_not_crash(monkeypatch):
    """Инвойс и статус отдают 200 и корректные поля (без 500)."""
    c, _ = make_client(monkeypatch)
    with c:
        _register(c)
        r = c.post("/api/billing/invoice", json={"network": "trc20"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["amount"] >= 100.0
        assert d["address"].startswith("T")
        assert d["expires_at"] > time.time() * 1000 - 60_000      # метка в будущем
        assert d["expires_in_sec"] > 0

        s = c.get("/api/billing/status")
        assert s.status_code == 200, s.text
        body = s.json()
        assert body["sub_active"] is False
        assert body["invoice"]["amount"] == d["amount"]
        assert body["invoice"]["expires_in_sec"] > 0


def test_invoice_amount_is_unique_per_user(monkeypatch):
    """Уникальные центы — идентификатор плательщика: суммы не повторяются."""
    c, _ = make_client(monkeypatch)
    with c:
        _register(c, "a@example.com")
        amount_a = c.post("/api/billing/invoice", json={"network": "trc20"}).json()["amount"]
        c.post("/api/auth/logout")
        _register(c, "b@example.com")
        amount_b = c.post("/api/billing/invoice", json={"network": "trc20"}).json()["amount"]
        assert amount_a != amount_b


def test_invoice_network_not_configured(monkeypatch):
    """Сеть без адреса приёма — понятная 503, а не падение."""
    c, _ = make_client(monkeypatch)
    with c:
        _register(c)
        r = c.post("/api/billing/invoice", json={"network": "bep20"})
        assert r.status_code == 503
        assert r.json()["detail"] == "network_not_configured"


def test_payment_activates_subscription_and_opens_terminal(monkeypatch):
    """Оплата по блокчейну: подписка включается, доступ к /app открывается."""
    c, server = make_client(monkeypatch)
    with c:
        _register(c)
        inv = c.post("/api/billing/invoice", json={"network": "trc20"}).json()

        # Гейт до оплаты: терминал закрыт, API отдаёт 402
        assert c.get("/app", follow_redirects=False).status_code in (302, 307)
        assert c.get("/api/klines/BTCUSDT").status_code == 402

        import importlib
        billing = importlib.import_module("billing")
        paid = billing.record_tx("trc20", "txhash-abc-1", inv["address"], inv["amount"],
                                 int(time.time() * 1000), inv["address"])
        assert paid is not None and paid["status"] == "paid"

        st = c.get("/api/billing/status").json()
        assert st["sub_active"] is True
        assert st["days_left"] > 29

        # повторная публикация той же транзакции не должна начислять второй месяц
        before = st["days_left"]
        assert billing.record_tx("trc20", "txhash-abc-1", inv["address"], inv["amount"],
                                 int(time.time() * 1000), inv["address"]) is None
        assert c.get("/api/billing/status").json()["days_left"] == before


def test_payment_wrong_amount_ignored(monkeypatch):
    """Чужая сумма (не из инвойса) не активирует доступ."""
    c, _ = make_client(monkeypatch)
    with c:
        _register(c)
        inv = c.post("/api/billing/invoice", json={"network": "trc20"}).json()
        import importlib
        billing = importlib.import_module("billing")
        assert billing.record_tx("trc20", "tx-2", inv["address"], inv["amount"] + 5,
                                 int(time.time() * 1000), inv["address"]) is None
        assert c.get("/api/billing/status").json()["sub_active"] is False


def test_payment_to_foreign_address_ignored(monkeypatch):
    c, _ = make_client(monkeypatch)
    with c:
        _register(c)
        inv = c.post("/api/billing/invoice", json={"network": "trc20"}).json()
        import importlib
        billing = importlib.import_module("billing")
        assert billing.record_tx("trc20", "tx-3", "TSomeoneElse", inv["amount"],
                                 int(time.time() * 1000), inv["address"]) is None


def test_owner_bypasses_payment(monkeypatch):
    """Владелец (OWNER_EMAILS) получает доступ без оплаты."""
    monkeypatch.setenv("OWNER_EMAILS", "boss@example.com")
    c, _ = make_client(monkeypatch)
    with c:
        _register(c, "boss@example.com")
        me = c.get("/api/auth/me").json()
        assert me["owner"] is True and me["sub_active"] is True
        assert c.get("/api/klines/BTCUSDT").status_code in (200, 503)  # ключи/сеть не нужны для проверки доступа
