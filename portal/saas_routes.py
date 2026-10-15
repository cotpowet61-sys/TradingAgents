"""
SaaS-маршруты и gate-middleware: аккаунты, биллинг USDT, публичный трек,
строгий доступ на 30 дней. Включается SAAS_MODE=true (только через .env —
намеренно: чтобы нельзя было случайно запереть самого себя из UI).

Безопасность (весь контур денег/доступа):
  • сессии — cookie HttpOnly + Secure(за HTTPS) + SameSite=Lax, в БД только sha256 токена;
  • небезопасные методы принимаются только с нашего Origin (см. portal/security.py);
  • rate-limit на все чувствительные эндпоинты — в БД (живёт после рестарта, работает
    при нескольких воркерах), с заголовком Retry-After;
  • админ-API — только по x-admin-token, сравнение за постоянное время, все действия
    пишутся в audit;
  • ключи клиентов Bybit принимаются ТОЛЬКО read-only (проверяются на бирже) и
    хранятся зашифрованными (AES-256-GCM, keyvault).
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time

from fastapi import Body, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse

import authn
import billing
import db
import security
import signal_learner

log = logging.getLogger("portal.saas")
MS = 1000

# Эндпоинты, которым нужна активная подписка (при REQUIRE_SUB_FOR_AI=false
# доступ к AI даётся и без оплаты — например, на время триала).
AI_PATHS = ("/api/analyze/", "/api/portfolio/ai-review", "/api/signals/ai-curator",
            "/api/portfolio/persona/build")


def mount(app, settings, static_dir: str) -> None:
    """Регистрирует роуты и middleware. Вызывается из server.py на импорте."""
    trust_proxy = bool(getattr(settings, "TRUST_PROXY_HEADERS", True))

    # ------------------------------------------------------------------ #
    # Хелперы
    # ------------------------------------------------------------------ #
    def _user_of(request: Request):
        return authn.user_by_token(request.cookies.get(authn.COOKIE))

    def _is_https(request: Request) -> bool:
        proto = (request.headers.get("x-forwarded-proto")
                 or request.url.scheme or "http").split(",")[0].strip().lower()
        return proto == "https"

    def _set_session(request: Request, resp: JSONResponse, token: str):
        resp.set_cookie(authn.COOKIE, token, max_age=30 * 86400, httponly=True,
                        samesite="lax", secure=_is_https(request), path="/")

    def _clear_session(resp: JSONResponse):
        resp.delete_cookie(authn.COOKIE, path="/")

    def _ip(request: Request) -> str:
        return security.client_ip(request, trust_proxy)

    def _limited(key: str, limit: int, window_s: int):
        """(ответ-429 или None). Пишет Retry-After."""
        ok, retry = security.rate_ok(key, limit=limit, window_s=window_s)
        if ok:
            return None
        return JSONResponse({"detail": "too_many_requests", "retry_after": retry},
                            status_code=429, headers={"Retry-After": str(retry)})

    def _site() -> str:
        return (settings.SITE_URL or "").strip().rstrip("/") or ""

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #
    @app.post("/api/auth/register")
    async def auth_register(request: Request, payload: dict = Body(...)):
        r = _limited(f"reg:{_ip(request)}", 6, 3600)
        if r:
            return r
        try:
            user = authn.register(str(payload.get("email", "")), str(payload.get("password", "")),
                                  referred_by=(request.cookies.get("tav_ref") or ""))
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
        token = authn.create_session(user["id"])
        resp = JSONResponse({"ok": True, "email": user["email"]})
        _set_session(request, resp, token)
        db.execute("DELETE FROM invoices WHERE user_id=? AND status='pending'", (user["id"],))
        security.audit("register", actor=user["email"], ip=_ip(request),
                       detail=f"ref={user.get('referred_by') or '-'}")
        return resp

    @app.post("/api/auth/login")
    async def auth_login(request: Request, payload: dict = Body(...)):
        ip = _ip(request)
        r = _limited(f"login:{ip}", 10, 300)
        if r:
            return r
        email = str(payload.get("email", "")).strip().lower()
        r = _limited(f"login_em:{email}", 6, 300)
        if r:  # защита от перебора по одному аккаунту с разных IP
            return r
        user = authn.login(email, str(payload.get("password", "")))
        if not user:
            security.audit("login_fail", actor=email, ip=ip)
            return JSONResponse({"detail": "invalid_credentials"}, status_code=401)
        token = authn.create_session(user["id"])
        resp = JSONResponse({"ok": True, "email": user["email"]})
        _set_session(request, resp, token)
        security.audit("login", actor=user["email"], ip=ip)
        return resp

    @app.post("/api/auth/logout")
    async def auth_logout(request: Request):
        authn.logout(request.cookies.get(authn.COOKIE))
        resp = JSONResponse({"ok": True})
        _clear_session(resp)
        return resp

    @app.get("/api/auth/me")
    async def auth_me(request: Request):
        if not settings.SAAS_MODE:
            return {"enabled": False}
        user = _user_of(request)
        if not user:
            return JSONResponse({"detail": "auth_required"}, status_code=401)
        return {
            "enabled": True,
            "email": user["email"],
            "owner": authn.is_owner(user),
            "ref_code": user["ref_code"],
            "sub_active": authn.sub_active(user),
            "days_left": authn.sub_days_left(user),
            "sub_until": user["sub_until_ms"],
            "price": settings.PRICE_MONTH_USD,
            "brand": settings.BRAND_NAME,
            "telegram": settings.TELEGRAM_URL,
            "wallets": {"trc20": settings.WALLET_TRC20, "bep20": settings.WALLET_BEP20},
        }

    # ------------------------------------------------------------------ #
    # Смена пароля (пользователь знает старый) + выход со всех устройств
    # ------------------------------------------------------------------ #
    @app.post("/api/auth/password")
    async def auth_password(request: Request, payload: dict = Body(...)):
        user = _user_of(request)
        if not user:
            return JSONResponse({"detail": "auth_required"}, status_code=401)
        ip = _ip(request)
        r = _limited(f"pwd:{user['id']}", 6, 3600)
        if r:
            return r
        cur, new = str(payload.get("current", "")), str(payload.get("new", ""))
        if not authn.check_password(cur, user["pass_hex"], user["salt_hex"],
                                    int(user.get("iters") or authn.PBKDF2_ITERS)):
            security.audit("password_fail", actor=user["email"], ip=ip)
            return JSONResponse({"detail": "invalid_credentials"}, status_code=401)
        prob = security.password_problem(new)
        if prob:
            return JSONResponse({"detail": prob}, status_code=400)
        hex_, salt = authn.hash_password(new, iters=authn.PBKDF2_ITERS)
        db.execute("UPDATE users SET pass_hex=?, salt_hex=?, iters=? WHERE id=?",
                   (hex_, salt, authn.PBKDF2_ITERS, user["id"]))
        authn.kill_user_sessions(user["id"])          # все старые сессии — вон
        token = authn.create_session(user["id"])      # текущему устройству — новая
        resp = JSONResponse({"ok": True})
        _set_session(request, resp, token)
        security.audit("password_change", actor=user["email"], ip=ip)
        return resp

    # ------------------------------------------------------------------ #
    # Сброс пароля (забыли): токен в БД, ссылка уходит владельцам в Telegram.
    # Email-провайдера у проекта нет — поэтому доставка ручная, но безопасная.
    # ------------------------------------------------------------------ #
    @app.post("/api/auth/reset/request")
    async def auth_reset_request(request: Request, payload: dict = Body(default={})):
        ip = _ip(request)
        r = _limited(f"reset:{ip}", 5, 900)
        if r:
            return r
        email = str(payload.get("email", "")).strip().lower()
        user = db.q("SELECT * FROM users WHERE email=?", (email,), one=True) \
            if security.email_ok(email) else None
        if user:
            token = secrets.token_urlsafe(32)
            now = int(time.time() * MS)
            db.execute(
                "INSERT INTO password_resets(token_hash, user_id, created_ms, expires_ms, used)"
                " VALUES(?,?,?,?,0)",
                (authn_secret_hash(token), user["id"], now, now + 3600 * MS),
            )
            link = f"{_site()}/reset.html?token={token}" if _site() else f"/reset.html?token={token}"
            try:
                await billing.notify_admin(
                    f"🔑 Запрос сброса пароля\n{email}\nСсылка (действует 1 час):\n{link}\n"
                    f"IP: {ip}", settings)
            except Exception as e:  # noqa: BLE001
                log.warning("[saas] не удалось отправить ссылку сброса админам: %s", e)
            security.audit("reset_request", actor=email, ip=ip)
        # Ответ одинаковый всегда: не раскрываем, есть такой аккаунт или нет.
        return {"ok": True, "delivery": "telegram" if settings.ADMIN_TG_ID else "manual",
                "hint": "Если аккаунт существует, ссылка отправлена поддержке."}

    @app.post("/api/auth/reset/confirm")
    async def auth_reset_confirm(request: Request, payload: dict = Body(default={})):
        ip = _ip(request)
        r = _limited(f"reset_c:{ip}", 10, 900)
        if r:
            return r
        token = str(payload.get("token", "")).strip()
        new = str(payload.get("password", ""))
        prob = security.password_problem(new)
        if prob:
            return JSONResponse({"detail": prob}, status_code=400)
        if not token:
            return JSONResponse({"detail": "invalid_token"}, status_code=400)
        row = db.q("SELECT * FROM password_resets WHERE token_hash=? AND used=0 AND expires_ms>?",
                   (authn_secret_hash(token), int(time.time() * MS)), one=True)
        if not row:
            security.audit("reset_confirm_fail", ip=ip)
            return JSONResponse({"detail": "invalid_token"}, status_code=400)
        hex_, salt = authn.hash_password(new, iters=authn.PBKDF2_ITERS)
        db.execute("UPDATE users SET pass_hex=?, salt_hex=?, iters=? WHERE id=?",
                   (hex_, salt, authn.PBKDF2_ITERS, row["user_id"]))
        db.execute("UPDATE password_resets SET used=1 WHERE token_hash=?",
                   (authn_secret_hash(token),))
        authn.kill_user_sessions(row["user_id"])
        u = db.q("SELECT email FROM users WHERE id=?", (row["user_id"],), one=True) or {}
        security.audit("reset_done", actor=u.get("email", ""), ip=ip)
        return {"ok": True}

    # ------------------------------------------------------------------ #
    # Read-only ключи Bybit самого пользователя (нужны для «Мои сделки»)
    # ------------------------------------------------------------------ #
    @app.get("/api/user/bybit-keys")
    async def user_keys_get(request: Request):
        user = _user_of(request)
        if not user:
            return JSONResponse({"detail": "auth_required"}, status_code=401)
        enc = (user.get("user_keys_enc") or "").strip()
        if not enc:
            return {"connected": False}
        try:
            from keyvault import decrypt
            k, _sk = decrypt(enc).split("\n", 1)
            return {"connected": True, "read_only": True,
                    "masked": (k[:4] + "…" + k[-4:]) if len(k) > 8 else "•" * len(k)}
        except Exception:
            return {"connected": True, "read_only": True, "masked": "…"}

    @app.post("/api/user/bybit-keys")
    async def user_keys_set(request: Request, payload: dict = Body(...)):
        user = _user_of(request)
        if not user:
            return JSONResponse({"detail": "auth_required"}, status_code=401)
        ip = _ip(request)
        r = _limited(f"bkeys:{user['id']}", 8, 3600)
        if r:
            return r
        api_key = str(payload.get("api_key", "")).strip()
        api_secret = str(payload.get("api_secret", "")).strip()
        if len(api_key) < 8 or len(api_secret) < 8:
            return JSONResponse({"detail": "bad_keys"}, status_code=400)
        try:
            import bybit_account as ba
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"detail": f"bybit_client_unavailable: {e}"}, status_code=503)

        # Проверка НА БИРЖЕ: ключ обязан быть read-only. Иначе — отказ.
        tok = ba._KEY_OVERRIDE.set((api_key, api_secret))
        try:
            try:
                info = await ba._get("/v5/user/query-api", {})
            except ba.BybitAuthError as e:
                return JSONResponse({"detail": "key_rejected", "explain": str(e)[:300]},
                                    status_code=400)
            read_only = int(info.get("readOnly") or 0) == 1
            if not read_only:
                security.audit("key_reject_not_ro", actor=user["email"], ip=ip)
                return JSONResponse(
                    {"detail": "not_read_only",
                     "explain": "Ключ имеет права торговли/вывода. Мы принимаем ТОЛЬКО Read-Only: "
                                "Bybit → API Management → измените права ключа (снимите Trade/Withdraw) "
                                "или создайте новый Read-Only ключ."}, status_code=400)
        finally:
            ba._KEY_OVERRIDE.reset(tok)

        try:
            import keyvault
            enc = keyvault.encrypt(f"{api_key}\n{api_secret}")
        except Exception as e:  # noqa: BLE001 — нет AUTH_SECRET и т.п.
            return JSONResponse({"detail": "keyvault_unavailable", "explain": str(e)[:300]},
                                status_code=503)
        db.execute("UPDATE users SET user_keys_enc=? WHERE id=?", (enc, user["id"]))
        security.audit("keys_saved", actor=user["email"], ip=ip, detail="read-only подтверждён")
        return {"ok": True, "read_only": True}

    @app.delete("/api/user/bybit-keys")
    async def user_keys_del(request: Request):
        user = _user_of(request)
        if not user:
            return JSONResponse({"detail": "auth_required"}, status_code=401)
        db.execute("UPDATE users SET user_keys_enc='' WHERE id=?", (user["id"],))
        security.audit("keys_deleted", actor=user["email"], ip=_ip(request))
        return {"ok": True}

    # ------------------------------------------------------------------ #
    # Admin API (x-admin-token: создать/посмотреть коды, отметить выплату)
    # ------------------------------------------------------------------ #
    def _adm(request: Request) -> bool:
        tok = request.headers.get("x-admin-token", "") or ""
        expect = str(settings.ADMIN_TOKEN or "")
        if len(expect) < 16:        # короткий токен бесполезен как секрет
            return False
        return security.safe_eq(tok, expect)

    def _adm_gate(request: Request):
        ip = _ip(request)
        r = _limited(f"adm:{ip}", 30, 300)
        if r:
            return r
        if not _adm(request):
            security.audit("admin_denied", ip=ip, detail=request.url.path)
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        return None

    @app.post("/api/admin/partners")
    async def admin_create_partner(request: Request, payload: dict = Body(default={})):
        g = _adm_gate(request)
        if g:
            return g
        try:
            import partners as P
            out = P.create_partner(str(payload.get("code", "")),
                                   str(payload.get("label", "")),
                                   float(payload.get("pct", 30.0)))
            security.audit("partner_create", actor="admin", ip=_ip(request),
                           detail=f"code={out.get('code')} pct={out.get('pct')}")
            return out
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)

    @app.get("/api/admin/partners")
    async def admin_list_partners(request: Request):
        g = _adm_gate(request)
        if g:
            return g
        import partners as P
        return {"partners": P.list_partners()}

    @app.post("/api/admin/partner/pay")
    async def admin_pay_partner(request: Request, payload: dict = Body(...)):
        g = _adm_gate(request)
        if g:
            return g
        import partners as P
        code = str(payload.get("code", ""))
        n = P.mark_paid(code, str(payload.get("tx_ref", "")))
        security.audit("partner_pay", actor="admin", ip=_ip(request),
                       detail=f"code={code} entries={n} tx={payload.get('tx_ref','')}")
        return {"ok": True, "paid_entries": n}

    @app.get("/api/admin/audit")
    async def admin_audit(request: Request, limit: int = 200):
        g = _adm_gate(request)
        if g:
            return g
        return {"entries": security.audit_tail(max(1, min(int(limit), 1000)))}

    # ------------------------------------------------------------------ #
    # Billing
    # ------------------------------------------------------------------ #
    @app.post("/api/billing/invoice")
    async def billing_invoice(request: Request, payload: dict = Body(default={})):
        user = _user_of(request)
        if not user:
            return JSONResponse({"detail": "auth_required"}, status_code=401)
        ip = _ip(request)
        r = _limited(f"inv:{user['id']}", 20, 3600)
        if r:
            return r
        network = str(payload.get("network", "trc20")).lower()
        try:
            inv = billing.create_invoice(user["id"], network, settings.PRICE_MONTH_USD)
        except ValueError:
            return JSONResponse({"detail": "bad_network"}, status_code=400)
        except RuntimeError:
            return JSONResponse({"detail": "busy_try_later"}, status_code=503)
        addr = settings.WALLET_TRC20 if network == "trc20" else settings.WALLET_BEP20
        if not addr:
            return JSONResponse({"detail": "network_not_configured"}, status_code=503)
        view = billing.invoice_view(inv) or {}
        return {
            "amount": view["amount"],
            "network": network,
            "address": addr,
            "expires_at": view["expires_at"],
            "expires_in_sec": view["expires_in_sec"],
            "note": f"Отправьте РОВНО {view['amount']} {'.'.join(['USDT'])} — зачтём автоматически "
                    f"в течение 1–2 минут.",
        }

    @app.get("/api/billing/status")
    async def billing_status(request: Request):
        user = _user_of(request)
        if not user:
            return JSONResponse({"detail": "auth_required"}, status_code=401)
        inv = billing.current_invoice(user["id"])
        return {
            "sub_active": authn.sub_active(user),
            "days_left": authn.sub_days_left(user),
            "owner": authn.is_owner(user),
            "invoice": billing.invoice_view(inv),
            "price": settings.PRICE_MONTH_USD,
            "wallets": {"trc20": settings.WALLET_TRC20, "bep20": settings.WALLET_BEP20},
        }

    # ------------------------------------------------------------------ #
    # Публичный трек-рекорд (логин не нужен — это витрина)
    # ------------------------------------------------------------------ #
    @app.get("/api/public/track")
    async def public_track():
        return signal_learner.track_summary()

    # ------------------------------------------------------------------ #
    # Публичные/платёжные страницы (роуты файлов)
    # ------------------------------------------------------------------ #
    def _page(name: str):
        path = os.path.join(static_dir, name)
        return FileResponse(path) if os.path.exists(path) else JSONResponse(
            {"detail": "not_found"}, status_code=404)

    @app.get("/landing.html", include_in_schema=False)
    async def _landing_page():
        return _page("landing.html")

    @app.get("/pay.html", include_in_schema=False)
    async def _pay_page():
        return _page("pay.html")

    @app.get("/admin.html", include_in_schema=False)
    async def _admin_page():
        resp = _page("admin.html")
        if hasattr(resp, "headers"):
            resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        return resp

    @app.get("/reset.html", include_in_schema=False)
    async def _reset_page():
        return _page("reset.html")

    # методы GET+HEAD: HEAD нужен мониторингу/парсерам, иначе 405
    @app.api_route("/legal/{page}", methods=["GET", "HEAD"], include_in_schema=False)
    async def _legal_page(page: str):
        """Юр. документы (Impressum/Privacy/Terms) + метка «черновик».

        Пока в файле остались маркеры [[...]], документ НЕ считается готовым:
        в HTML добавляется красная плашка, а в заголовках — X-Legal-Draft: 1.
        Так пустой Impressum невозможно «случайно» опубликовать.
        """
        from fastapi.responses import HTMLResponse
        name = os.path.basename(page or "").replace(".html", "")
        if name not in ("imprint", "privacy", "terms"):
            return JSONResponse({"detail": "not_found"}, status_code=404)
        path = os.path.join(static_dir, "legal", f"{name}.html")
        if not os.path.exists(path):
            return JSONResponse({"detail": "not_found"}, status_code=404)
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
        draft = "[[" in html
        if draft:
            banner = ('<div style="position:sticky;top:0;z-index:9;background:#3a0d12;color:#ffb3ba;'
                      'border-bottom:1px solid #ff6b7a;padding:10px 16px;'
                      'font:13px/1.5 Inter,system-ui,sans-serif">'
                      '⚠️ ЧЕРНОВИК: в документе остались незаполненные поля [[...]]. '
                      'Заполните их перед публикацией — страница пока не закрывает '
                      'юридические требования.</div>')
            html = html.replace("<body>", "<body>" + banner, 1)
        return HTMLResponse(html, headers={"X-Legal-Draft": "1" if draft else "0",
                                          "X-Robots-Tag": "noindex"})

    @app.get("/robots.txt", include_in_schema=False)
    async def _robots():
        return PlainTextResponse(
            "User-agent: *\nDisallow: /app\nDisallow: /admin.html\nDisallow: /pay.html\n"
            "Disallow: /api/\nAllow: /\n")

    # ------------------------------------------------------------------ #
    # Gate-middleware (только при SAAS_MODE; в обычном режиме — прозрачен)
    # ------------------------------------------------------------------ #
    # /api/metrics сам проверяет x-admin-token (или владельца) — поэтому пропускаем
    # его через гейт: иначе мониторинг получал бы 401 «auth_required» в SaaS-режиме.
    PUBLIC_API = ("/api/auth/", "/api/public/", "/api/health", "/api/partner/",
                  "/api/admin/", "/api/billing/", "/api/metrics")
    REF_COOKIE = "tav_ref"

    @app.middleware("http")
    async def saas_gate(request: Request, call_next):
        # --- атрибуция трафферов: ?t=CODE на любой GET → хит + кука 7 дней (и в off-режиме тоже)
        ref_code = (request.query_params.get("t") or "").strip().upper()
        hit_ok = False
        if ref_code and request.method == "GET" and 3 <= len(ref_code) <= 32:
            import partners as _pt
            hit_ok = _pt.record_hit(ref_code, _ip(request), request.headers.get("user-agent", ""))

        if not settings.SAAS_MODE:
            resp = await call_next(request)
            if hit_ok:
                resp.set_cookie(REF_COOKIE, ref_code, max_age=7 * 86400,
                                httponly=True, samesite="lax", secure=_is_https(request))
            return resp

        p = request.url.path
        if p == "/admin.html":  # страница пустая; данные — только по x-admin-token в API
            resp = _page("admin.html")
        elif (p.startswith("/static") or p.startswith("/legal")
              or p in ("/favicon.ico", "/robots.txt", "/pay.html",
                       "/landing.html", "/reset.html")):
            # Публичные страницы: юр. документы, сброс пароля, лендинг, оплата.
            # Оплата и сброс сами разберутся с сессией, но должны открываться анонимно.
            resp = await call_next(request)
        elif any(p.startswith(pref) for pref in PUBLIC_API):
            resp = await call_next(request)
        else:
            user = authn.user_by_token(request.cookies.get(authn.COOKIE))
            if user:
                import llm_client as _llm
                _llm._SAAS_USER.set(user["id"])   # контекст для Community-AI квоты
            if not user:
                if p in ("/", "/index.html", "/landing.html"):
                    resp = _page("landing.html")
                elif p.startswith("/api/") or p.startswith("/ws"):
                    resp = JSONResponse({"detail": "auth_required"}, status_code=401)
                else:
                    resp = RedirectResponse("/")
            elif not authn.sub_active(user):
                trial_ai = (not settings.REQUIRE_SUB_FOR_AI) and any(
                    p.startswith(pref) for pref in AI_PATHS)
                # строгий срез: без активной подписки доступны только аккаунт,
                # биллинг и собственные read-only ключи (нужны для онбординга:
                # подтверждение ключей входит в шаг «подключите биржу»).
                if (trial_ai or p.startswith("/api/auth/") or p.startswith("/api/billing/")
                        or p.startswith("/api/user/") or p == "/pay.html"):
                    resp = await call_next(request)
                elif p in ("/", "/index.html"):
                    resp = RedirectResponse("/pay.html")
                elif p.startswith("/api/") or p.startswith("/ws"):
                    resp = JSONResponse({"detail": "payment_required", "pay_url": "/pay.html"},
                                        status_code=402)
                else:
                    resp = RedirectResponse("/pay.html")
            elif p.startswith("/api/portfolio"):
                # персональные данные — только с ключами самого юзера
                enc = (user.get("user_keys_enc") or "").strip()
                if not enc:
                    resp = JSONResponse({"detail": "connect_readonly_keys",
                                         "explain": "Подключите Read-Only ключи Bybit в кабинете — "
                                                    "«Мои сделки» строится из ВАШЕЙ истории."},
                                        status_code=409)
                else:
                    import bybit_account as _ba
                    try:
                        import keyvault
                        k, skey = keyvault.decrypt(enc).split("\n", 1)
                    except Exception:
                        resp = JSONResponse({"detail": "keys_broken",
                                             "explain": "Сохранённые ключи не расшифровались "
                                                        "(сменился PORTAL_AUTH_SECRET?). "
                                                        "Переподключите read-only ключи."},
                                            status_code=409)
                    else:
                        tok = _ba._KEY_OVERRIDE.set((k, skey))
                        try:
                            resp = await call_next(request)
                        finally:
                            _ba._KEY_OVERRIDE.reset(tok)
            else:
                resp = await call_next(request)

        if hit_ok:
            resp.set_cookie(REF_COOKIE, ref_code, max_age=7 * 86400,
                            httponly=True, samesite="lax", secure=_is_https(request))
        return resp

    # ------------------------------------------------------------------ #
    # Watchers
    # ------------------------------------------------------------------ #
    _tasks: list[asyncio.Task] = []

    @app.on_event("startup")
    async def _saas_startup():
        security.prune()
        if not settings.SAAS_MODE:
            return
        if settings.BILLING_WATCHER and (settings.WALLET_TRC20 or settings.WALLET_BEP20):
            _tasks.append(asyncio.create_task(billing.watcher_loop(settings, interval_s=45)))
        _tasks.append(asyncio.create_task(housekeeping_loop()))
        log.info("[saas] mode ON, brand=%s, price=$%s, приём: trc20=%s bep20=%s",
                 settings.BRAND_NAME, settings.PRICE_MONTH_USD,
                 bool(settings.WALLET_TRC20), bool(settings.WALLET_BEP20))

    @app.on_event("shutdown")
    async def _saas_shutdown():
        for t in _tasks:
            t.cancel()


async def housekeeping_loop(interval_s: int = 3600) -> None:
    """Раз в час: сессии/счётчики/аудит не растут бесконечно."""
    while True:
        try:
            authn.cleanup_sessions()
            security.prune()
            db.execute("DELETE FROM password_resets WHERE expires_ms < ?",
                       (int(time.time() * MS),))
        except Exception as e:  # noqa: BLE001
            log.warning("[saas] housekeeping: %s", e)
        await asyncio.sleep(interval_s)


def authn_secret_hash(token: str) -> str:
    """sha256 токена сброса — в БД не лежит сам токен."""
    import hashlib
    return hashlib.sha256((token or "").encode()).hexdigest()
