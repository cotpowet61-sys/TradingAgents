"""Кибербезопасность: заголовки, CSRF/Origin, лимиты, cookie, аудит, keyvault."""
from __future__ import annotations

import time

from conftest import fresh_env, load, make_client


def test_security_headers_present(monkeypatch):
    c, _ = make_client(monkeypatch)
    with c:
        r = c.get("/")
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert "Content-Security-Policy" in r.headers
        assert "frame-ancestors" in r.headers["Content-Security-Policy"]
        assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        # HSTS только за HTTPS (иначе браузер залипает на http-превью)
        assert "Strict-Transport-Security" not in r.headers
        r2 = c.get("/", headers={"x-forwarded-proto": "https"})
        assert "Strict-Transport-Security" in r2.headers


def test_api_responses_are_not_cached(monkeypatch):
    c, _ = make_client(monkeypatch)
    with c:
        r = c.get("/api/health")
        assert r.headers.get("Cache-Control") == "no-store, max-age=0"


def test_cross_origin_post_is_rejected(monkeypatch):
    """Cookie-сессии + чужой сайт = CSRF. Запрос с чужим Origin не проходит."""
    c, _ = make_client(monkeypatch)
    with c:
        r = c.post("/api/auth/login", json={"email": "x@example.com", "password": "whatever12"},
                   headers={"origin": "https://evil.example"})
        assert r.status_code == 403
        assert r.json()["detail"] == "cross_origin_forbidden"
        # свой Origin проходит (дальше — обычная проверка пароля)
        ok = c.post("/api/auth/login", json={"email": "x@example.com", "password": "whatever12"},
                    headers={"origin": "http://testserver"})
        assert ok.status_code == 401


def test_oversized_body_rejected(monkeypatch):
    c, settings = make_client(monkeypatch)
    with c:
        import importlib
        cfg = importlib.import_module("portal_config")
        big = b"x" * int(cfg.settings.MAX_BODY_MB * 1024 * 1024 + 1024)
        r = c.post("/api/portfolio/csv", content=big,
                   headers={"content-type": "text/plain"})
        assert r.status_code == 413


def test_password_policy_and_enumeration_safety(monkeypatch):
    c, _ = make_client(monkeypatch)
    with c:
        weak = c.post("/api/auth/register", json={"email": "w@example.com", "password": "password"})
        assert weak.status_code == 400 and weak.json()["detail"] == "weak_password"
        short = c.post("/api/auth/register", json={"email": "w@example.com", "password": "a1b2"})
        assert short.status_code == 400
        bad_mail = c.post("/api/auth/register", json={"email": "nope", "password": "strong-pass-1"})
        assert bad_mail.status_code == 400 and bad_mail.json()["detail"] == "invalid_email"
        ok = c.post("/api/auth/register", json={"email": "w@example.com", "password": "strong-pass-1"})
        assert ok.status_code == 200


def test_session_cookie_flags(monkeypatch):
    """HttpOnly + SameSite=Lax всегда, Secure — за HTTPS."""
    c, _ = make_client(monkeypatch)
    with c:
        r = c.post("/api/auth/register", json={"email": "c@example.com", "password": "strong-pass-1"})
        setc = r.headers.get("set-cookie", "")
        assert "HttpOnly" in setc and "SameSite=lax" in setc
        assert "Secure" not in setc                       # http-превью
    c2, _ = make_client(monkeypatch)
    with c2:
        r = c2.post("/api/auth/register", json={"email": "c2@example.com", "password": "strong-pass-1"},
                    headers={"x-forwarded-proto": "https"})
        assert "Secure" in r.headers.get("set-cookie", "")


def test_rate_limit_blocks_bruteforce_when_enabled(monkeypatch):
    """Лимит логина: 10 попыток / 5 мин, потом 429 с Retry-After."""
    c, _ = make_client(monkeypatch, disable_rate_limit=False)
    with c:
        c.post("/api/auth/register", json={"email": "rl@example.com", "password": "strong-pass-1"})
        codes = []
        for _ in range(12):
            rr = c.post("/api/auth/login", json={"email": "rl@example.com", "password": "wrong-pass-1"})
            codes.append(rr.status_code)
        assert 429 in codes
        assert rr.headers.get("Retry-After")


def test_rate_limit_survives_restart(monkeypatch):
    """Счётчики в БД, а не в памяти: рестарт процесса не сбрасывает защиту."""
    fresh_env(monkeypatch, saas=False, disable_rate_limit=False)
    security = load("security")
    for _ in range(3):
        assert security.rate_ok("k1", limit=3, window_s=300)[0] is True
    assert security.rate_ok("k1", limit=3, window_s=300)[0] is False
    # «рестарт»: те же настройки/БД, модуль перезагружен
    security2 = load("security")
    assert security2.rate_ok("k1", limit=3, window_s=300)[0] is False


def test_password_change_kills_other_sessions(monkeypatch):
    c, _ = make_client(monkeypatch)
    with c:
        c.post("/api/auth/register", json={"email": "p@example.com", "password": "strong-pass-1"})
        token_old = c.cookies.get("tav_session")
        r = c.post("/api/auth/password", json={"current": "strong-pass-1", "new": "another-pass-2"})
        assert r.status_code == 200
        assert c.get("/api/auth/me").status_code == 200          # текущая сессия жива
        authn = load("authn")
        assert authn.user_by_token(token_old) is None            # старая сессия убита


def test_reset_flow_end_to_end(monkeypatch):
    c, _ = make_client(monkeypatch)
    with c:
        c.post("/api/auth/register", json={"email": "r@example.com", "password": "strong-pass-1"})
        assert c.post("/api/auth/reset/request", json={"email": "r@example.com"}).status_code == 200
        # неизвестный адрес — тот же ответ (не раскрываем существование аккаунта)
        assert c.post("/api/auth/reset/request", json={"email": "ghost@example.com"}).status_code == 200
        db = load("db")
        row = db.q("SELECT * FROM password_resets ORDER BY created_ms DESC", (), one=True)
        assert row is not None and row["used"] == 0
        # токена в БД нет — только его хэш
        assert len(row["token_hash"]) == 64
        c.post("/api/auth/logout")
        # вход старым паролем невозможен после сброса
        token = "tok-" + "a" * 30
        import hashlib
        db.execute("UPDATE password_resets SET token_hash=? WHERE token_hash=?",
                   (hashlib.sha256(token.encode()).hexdigest(), row["token_hash"]))
        r = c.post("/api/auth/reset/confirm", json={"token": token, "password": "reset-pass-77"})
        assert r.status_code == 200
        assert c.post("/api/auth/login", json={"email": "r@example.com",
                                              "password": "reset-pass-77"}).status_code == 200
        # повторное использование токена — отказ
        assert c.post("/api/auth/reset/confirm", json={"token": token,
                                                       "password": "reset-pass-99"}).status_code == 400


def test_admin_api_requires_token_and_is_audited(monkeypatch):
    c, _ = make_client(monkeypatch, admin_token="admin-token-" + "z" * 20)
    with c:
        assert c.get("/api/admin/partners").status_code == 403
        assert c.get("/api/admin/partners", headers={"x-admin-token": "short"}).status_code == 403
        good = {"x-admin-token": "admin-token-" + "z" * 20}
        r = c.post("/api/admin/partners", json={"code": "PARTNER1", "pct": 30}, headers=good)
        assert r.status_code == 200 and r.json()["code"] == "PARTNER1"
        audit = c.get("/api/admin/audit", headers=good).json()["entries"]
        assert any(e["action"] == "partner_create" for e in audit)


def test_short_admin_token_is_useless(monkeypatch):
    """Токен короче 16 символов не принимается вообще (даже если совпал)."""
    c, _ = make_client(monkeypatch, admin_token="short")
    with c:
        assert c.get("/api/admin/partners", headers={"x-admin-token": "short"}).status_code == 403


def test_keyvault_requires_secret_and_roundtrips(monkeypatch):
    """Пустой/слабый PORTAL_AUTH_SECRET = отказ, а не «предсказуемый ключ»."""
    fresh_env(monkeypatch)
    portal_config = load("portal_config")
    object.__setattr__(portal_config.settings, "AUTH_SECRET", "")
    kv = load("keyvault")
    try:
        kv.encrypt("hello")
        raise AssertionError("должно было упасть без секрета")
    except kv.KeyvaultError:
        pass
    object.__setattr__(portal_config.settings, "AUTH_SECRET", "x" * 10)
    try:
        kv.encrypt("hello")
        raise AssertionError("короткий секрет должен отклоняться")
    except kv.KeyvaultError:
        pass
    object.__setattr__(portal_config.settings, "AUTH_SECRET", "y" * 40)
    blob = kv.encrypt("api-key\napi-secret")
    assert blob.startswith("v1:") and "api-key" not in blob
    assert kv.decrypt(blob) == "api-key\napi-secret"


def test_client_ip_ignores_forwarded_header_when_proxy_untrusted(monkeypatch):
    fresh_env(monkeypatch)
    portal_config = load("portal_config")
    security = load("security")
    object.__setattr__(portal_config.settings, "TRUST_PROXY_HEADERS", False)

    class _Req:
        headers = {"x-forwarded-for": "1.2.3.4"}
        client = type("C", (), {"host": "9.9.9.9"})()

    assert security.client_ip(_Req(), trust_proxy=False) == "9.9.9.9"
    assert security.client_ip(_Req(), trust_proxy=True) == "1.2.3.4"


def test_audit_log_records_actions(monkeypatch):
    c, _ = make_client(monkeypatch)
    with c:
        c.post("/api/auth/register", json={"email": "log@example.com", "password": "strong-pass-1"})
        db = load("db")
        rows = db.q("SELECT action, actor FROM audit ORDER BY id DESC")
        assert any(r["action"] == "register" and r["actor"] == "log@example.com" for r in rows)


def test_metrics_endpoint_is_gated_by_admin_token(monkeypatch):
    """Метрики: без токена — отказ, с токеном — сводка; в SaaS не режется гейтом."""
    c, _ = make_client(monkeypatch, admin_token="admin-token-" + "q" * 20)
    with c:
        assert c.get("/api/metrics").status_code == 403
        r = c.get("/api/metrics", headers={"x-admin-token": "admin-token-" + "q" * 20})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and "live" in body and "db" in body
        assert "users" in body["db"]


def test_origin_check_works_behind_reverse_proxy(monkeypatch):
    """За прокси Host может быть переписан: ориентируемся и на X-Forwarded-Host,
    и на SITE_URL. Иначе легитимный вход через превью/прод-домен падал бы с 403."""
    monkeypatch.setenv("SITE_URL", "https://tv.example.com")
    c, _ = make_client(monkeypatch)
    with c:
        # Host переписан прокси на внутренний, но Origin — публичный
        r = c.post("/api/auth/login", json={"email": "x@example.com", "password": "whatever12"},
                   headers={"origin": "https://tv.example.com", "host": "10.0.0.5:3005"})
        assert r.status_code == 401                     # дошли до проверки пароля, не 403
        r2 = c.post("/api/auth/login", json={"email": "x@example.com", "password": "whatever12"},
                    headers={"origin": "https://preview-abc.example",
                             "x-forwarded-host": "preview-abc.example",
                             "host": "10.0.0.5:3005"})
        assert r2.status_code == 401
        # а чужой домен по-прежнему отбивается
        r3 = c.post("/api/auth/login", json={"email": "x@example.com", "password": "whatever12"},
                    headers={"origin": "https://evil.example", "host": "10.0.0.5:3005"})
        assert r3.status_code == 403


def test_frame_ancestors_is_normalized(monkeypatch):
    """CSP-ключевые слова обязаны быть в кавычках, чужой домен — как есть.

    Значение из .env приходит без кавычек (dotenv их снимает), поэтому
    нормализуем: иначе браузер трактует `self` как имя хоста и ломает фрейминг.
    """
    fresh_env(monkeypatch)
    security = load("security")
    assert security.normalize_frame_ancestors("self") == "'self'"
    assert security.normalize_frame_ancestors("'self' https://partner.example") == \
        "'self' https://partner.example"
    assert security.normalize_frame_ancestors("NONE") == "'none'"
    assert security.normalize_frame_ancestors("") == "'self'"
