"""
SaaS-маршруты и gate-middleware: аккаунты, биллинг USDT, публичный трек,
строгий доступ на 30 дней. Включается SAAS_MODE=true (только через .env —
намеренно: чтобы нельзя было случайно запереть самого себя из UI).
"""
from __future__ import annotations

import asyncio
import os
import time

from fastapi import Body, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

import authn
import billing
import db
import signal_learner

MS = 1000


def mount(app, settings, static_dir: str) -> None:
    """Регистрирует роуты и middleware. Вызывается из server.py на импорте."""

    # секрет сессий: в пустом виде — генерим и дожимаем в .env (один раз за всё время)
    if not settings.AUTH_SECRET:
        import secrets as _sec
        token = _sec.token_urlsafe(32)
        object.__setattr__(settings, "AUTH_SECRET", token)
        try:
            env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
            with open(env_path, "a", encoding="utf-8") as f:
                f.write(f"\nPORTAL_AUTH_SECRET={token}\n")
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # Хелперы
    # ------------------------------------------------------------------ #
    def _user_of(request: Request):
        return authn.user_by_token(request.cookies.get(authn.COOKIE))

    def _set_session(resp: JSONResponse, token: str):
        resp.set_cookie(authn.COOKIE, token, max_age=30 * 86400,
                        httponly=True, samesite="lax")  # secure=True включаем за HTTPS-прокси на VPS

    def _ip(request: Request) -> str:
        fwd = request.headers.get("x-forwarded-for", "")
        return (fwd.split(",")[0].strip() if fwd else request.client.host if request.client else "?") or "?"

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #
    @app.post("/api/auth/register")
    async def auth_register(request: Request, payload: dict = Body(...)):
        if not authn.rate_ok(f"reg:{_ip(request)}", limit=8, window_s=300):
            return JSONResponse({"detail": "too_many_requests"}, status_code=429)
        try:
            user = authn.register(str(payload.get("email", "")), str(payload.get("password", "")),
                                  referred_by=(request.cookies.get("tav_ref") or ""))
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
        token = authn.create_session(user["id"])
        resp = JSONResponse({"ok": True, "email": user["email"]})
        _set_session(resp, token)
        db.execute("DELETE FROM invoices WHERE user_id=? AND status='pending'", (user["id"],))
        return resp

    @app.post("/api/auth/login")
    async def auth_login(request: Request, payload: dict = Body(...)):
        if not authn.rate_ok(f"login:{_ip(request)}", limit=10, window_s=300):
            return JSONResponse({"detail": "too_many_requests"}, status_code=429)
        user = authn.login(str(payload.get("email", "")), str(payload.get("password", "")))
        if not user:
            return JSONResponse({"detail": "invalid_credentials"}, status_code=401)
        token = authn.create_session(user["id"])
        resp = JSONResponse({"ok": True, "email": user["email"]})
        _set_session(resp, token)
        return resp

    @app.post("/api/auth/logout")
    async def auth_logout(request: Request):
        authn.logout(request.cookies.get(authn.COOKIE))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(authn.COOKIE)
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
    # Admin API (x-admin-token: создать/посмотреть коды, отметить выплату)
    # ------------------------------------------------------------------ #
    def _adm(request: Request) -> bool:
        import hmac as _h
        tok = request.headers.get("x-admin-token", "") or ""
        return bool(settings.ADMIN_TOKEN) and _h.compare_digest(tok, str(settings.ADMIN_TOKEN))

    @app.post("/api/admin/partners")
    async def admin_create_partner(request: Request, payload: dict = Body(default={})):
        if not _adm(request):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        try:
            import partners as P
            return P.create_partner(str(payload.get("code", "")),
                                    str(payload.get("label", "")),
                                    float(payload.get("pct", 30.0)))
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)

    @app.get("/api/admin/partners")
    async def admin_list_partners(request: Request):
        if not _adm(request):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        import partners as P
        return {"partners": P.list_partners()}

    @app.post("/api/admin/partner/pay")
    async def admin_pay_partner(request: Request, payload: dict = Body(...)):
        if not _adm(request):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        import partners as P
        n = P.mark_paid(str(payload.get("code", "")), str(payload.get("tx_ref", "")))
        return {"ok": True, "paid_entries": n}

    # ------------------------------------------------------------------ #
    # Billing
    # ------------------------------------------------------------------ #
    @app.post("/api/billing/invoice")
    async def billing_invoice(request: Request, payload: dict = Body(default={})):
        user = _user_of(request)
        if not user:
            return JSONResponse({"detail": "auth_required"}, status_code=401)
        network = str(payload.get("network", "trc20"))
        try:
            inv = billing.create_invoice(user["id"], network, settings.PRICE_MONTH_USD)
        except ValueError:
            return JSONResponse({"detail": "bad_network"}, status_code=400)
        except RuntimeError:
            return JSONResponse({"detail": "busy_try_later"}, status_code=503)
        addr = settings.WALLET_TRC20 if network == "trc20" else settings.WALLET_BEP20
        if not addr:
            return JSONResponse({"detail": "network_not_configured"}, status_code=503)
        return {
            "amount": inv["amount"],
            "network": network,
            "address": addr,
            "expires_at": inv["expires_at"],
            "note": f"Отправьте РОВНО {inv['amount']} USDT — зачтём автоматически в течение 1-2 минут.",
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
            "invoice": ({"amount": inv["amount"], "network": inv["network"],
                         "expires_at": inv["expires_at"]} if inv else None),
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
    @app.get("/landing.html", include_in_schema=False)
    async def _landing_page():
        return FileResponse(os.path.join(static_dir, "landing.html"))

    @app.get("/pay.html", include_in_schema=False)
    async def _pay_page():
        return FileResponse(os.path.join(static_dir, "pay.html"))

    @app.get("/admin.html", include_in_schema=False)
    async def _admin_page():
        return FileResponse(os.path.join(static_dir, "admin.html"))

    # ------------------------------------------------------------------ #
    # Gate-middleware (только при SAAS_MODE; в обычном режиме — прозрачен)
    # ------------------------------------------------------------------ #
    PUBLIC_API = ("/api/auth/", "/api/public/", "/api/health", "/api/partner/", "/api/admin/")
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
                resp.set_cookie(REF_COOKIE, ref_code, max_age=7 * 86400, httponly=True, samesite="lax")
            return resp

        p = request.url.path
        if p == "/admin.html":  # страница пустая; данные — только по x-admin-token в API
            resp = FileResponse(os.path.join(static_dir, "admin.html"))
        elif p.startswith("/static") or p == "/favicon.ico":
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
                    resp = FileResponse(os.path.join(static_dir, "landing.html"))
                elif p.startswith("/api/") or p.startswith("/ws"):
                    resp = JSONResponse({"detail": "auth_required"}, status_code=401)
                else:
                    resp = RedirectResponse("/")
            elif not authn.sub_active(user):
                # строгий срез: без активной подписки — только биллинг/аккаунт
                if p.startswith("/api/billing/") or p.startswith("/api/auth/") or p == "/pay.html":
                    resp = await call_next(request)
                elif p in ("/", "/index.html"):
                    resp = RedirectResponse("/pay.html")
                elif p.startswith("/api/") or p.startswith("/ws"):
                    resp = JSONResponse({"detail": "payment_required", "pay_url": "/pay.html"}, status_code=402)
                else:
                    resp = RedirectResponse("/pay.html")
            elif p.startswith("/api/portfolio"):
                # персональные данные — только с ключами самого юзера
                enc = (user.get("user_keys_enc") or "").strip()
                if not enc:
                    resp = JSONResponse({"detail": "connect_readonly_keys",
                                         "explain": "Подключите Read-Only ключи Bybit в кабинете — «Мои сделки» строится из ВАШЕЙ истории."},
                                        status_code=409)
                else:
                    import keyvault, bybit_account as _ba
                    k, skey = keyvault.decrypt(enc).split("\n", 1)
                    tok = _ba._KEY_OVERRIDE.set((k, skey))
                    try:
                        resp = await call_next(request)
                    finally:
                        _ba._KEY_OVERRIDE.reset(tok)
            else:
                resp = await call_next(request)

        if hit_ok:
            resp.set_cookie(REF_COOKIE, ref_code, max_age=7 * 86400, httponly=True, samesite="lax")
        return resp

    # ------------------------------------------------------------------ #
    # Watchers
    # ------------------------------------------------------------------ #
    _tasks: list[asyncio.Task] = []

    @app.on_event("startup")
    async def _saas_startup():
        if not settings.SAAS_MODE:
            return
        if settings.WALLET_TRC20 or settings.WALLET_BEP20:
            _tasks.append(asyncio.create_task(billing.watcher_loop(settings, interval_s=45)))
        import logging
        logging.getLogger("portal.saas").info(
            "[saas] mode ON, brand=%s, price=$%s, надёжный приём: trc20=%s bep20=%s",
            settings.BRAND_NAME, settings.PRICE_MONTH_USD,
            bool(settings.WALLET_TRC20), bool(settings.WALLET_BEP20))

    @app.on_event("shutdown")
    async def _saas_shutdown():
        for t in _tasks:
            t.cancel()
