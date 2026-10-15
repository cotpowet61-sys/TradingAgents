"""Гейт SaaS: доступ строго по активной подписке, персональные данные — по ключам юзера."""
from __future__ import annotations

from conftest import make_client


def test_anonymous_is_kept_out(monkeypatch):
    c, _ = make_client(monkeypatch)
    with c:
        assert c.get("/", follow_redirects=False).status_code == 200      # лендинг открыт
        assert c.get("/api/klines/BTCUSDT").status_code == 401
        r = c.get("/app", follow_redirects=False)
        assert r.status_code in (302, 307) and r.headers["location"] == "/"


def test_paid_user_gets_terminal(monkeypatch):
    c, _ = make_client(monkeypatch)
    with c:
        c.post("/api/auth/register", json={"email": "sub@example.com", "password": "strong-pass-1"})
        import importlib, time
        authn = importlib.import_module("authn")
        db = importlib.import_module("db")
        uid = db.q("SELECT id FROM users WHERE email=?", ("sub@example.com",), one=True)["id"]
        authn.extend_subscription(uid, 1)
        assert c.get("/app").status_code == 200
        assert c.get("/api/health").status_code == 200


def test_private_data_requires_user_keys(monkeypatch):
    """«Мои сделки» без подключённых read-only ключей — 409 с понятным объяснением."""
    c, _ = make_client(monkeypatch)
    with c:
        c.post("/api/auth/register", json={"email": "nokeys@example.com", "password": "strong-pass-1"})
        import importlib
        db = importlib.import_module("db")
        authn = importlib.import_module("authn")
        uid = db.q("SELECT id FROM users WHERE email=?", ("nokeys@example.com",), one=True)["id"]
        authn.extend_subscription(uid, 1)
        r = c.get("/api/portfolio/overview")
        assert r.status_code == 409
        assert r.json()["detail"] == "connect_readonly_keys"


def test_public_track_is_open(monkeypatch):
    c, _ = make_client(monkeypatch)
    with c:
        r = c.get("/api/public/track")
        assert r.status_code == 200
        body = r.json()
        assert "all" in body and "note" in body


def test_saas_off_keeps_portal_open(monkeypatch):
    """Без SAAS_MODE портал — локальный инструмент владельца, гейта нет."""
    c, _ = make_client(monkeypatch, saas=False)
    with c:
        assert c.get("/app").status_code == 200
        assert c.get("/api/klines/BTCUSDT").status_code in (200, 503)


def test_health_reports_mode(monkeypatch):
    c, _ = make_client(monkeypatch)
    with c:
        h = c.get("/api/health").json()
        assert h["ok"] is True and h["saas"] is True
        assert "market_source" in h and "real_data_required" in h


def test_legal_pages_are_public_and_marked_as_draft(monkeypatch):
    """Юр. страницы открыты анонимно; с незаполненными [[...]] помечены как черновик."""
    c, _ = make_client(monkeypatch)
    with c:
        for page in ("imprint", "privacy", "terms"):
            r = c.get(f"/legal/{page}.html")
            assert r.status_code == 200, page
            assert r.headers.get("x-legal-draft") == "1"
            assert "ЧЕРНОВИК" in r.text
        assert c.get("/legal/unknown.html").status_code == 404


def test_reset_page_is_public(monkeypatch):
    c, _ = make_client(monkeypatch)
    with c:
        assert c.get("/reset.html").status_code == 200


def test_robots_disallows_private_areas(monkeypatch):
    c, _ = make_client(monkeypatch)
    with c:
        txt = c.get("/robots.txt").text
        assert "/app" in txt and "/admin.html" in txt and "/api/" in txt
