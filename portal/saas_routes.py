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
            user = authn.register(str(payload.get("email", "")), str(payload.get("password", "")))
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
            "ref_code": user["ref_code"],
            "sub_active": authn.sub_active(user),
            "days_left": authn.sub_days_left(user),
            "sub_until": user["sub_until_ms"],
            "price": settings.PRICE_MONTH_USD,
            "wallets": {"trc20": settings.WALLET_TRC20, "bep20": settings.WALLET_BEP20},
        }

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

    # ------------------------------------------------------------------ #
    # Gate-middleware (только при SAAS_MODE; в обычном режиме — прозрачен)
    # ------------------------------------------------------------------ #
    PUBLIC_API = ("/api/auth/", "/api/public/", "/api/health")

    @app.middleware("http")
    async def saas_gate(request: Request, call_next):
        if not settings.SAAS_MODE:
            return await call_next(request)
        p = request.url.path
        if p.startswith("/static") or p == "/favicon.ico":
            return await call_next(request)
        if any(p.startswith(pref) for pref in PUBLIC_API):
            return await call_next(request)
        user = authn.user_by_token(request.cookies.get(authn.COOKIE))
        if not user:
            if p in ("/", "/index.html", "/landing.html"):
                return FileResponse(os.path.join(static_dir, "landing.html"))
            if p.startswith("/api/") or p.startswith("/ws"):
                return JSONResponse({"detail": "auth_required"}, status_code=401)
            return RedirectResponse("/")
        if not authn.sub_active(user):
            # строгий срез: без активной подписки — только биллинг/аккаунт
            if p.startswith("/api/billing/") or p.startswith("/api/auth/") or p in ("/pay.html",):
                return await call_next(request)
            if p in ("/", "/index.html"):
                return RedirectResponse("/pay.html")
            if p.startswith("/api/") or p.startswith("/ws"):
                return JSONResponse({"detail": "payment_required", "pay_url": "/pay.html"}, status_code=402)
            return RedirectResponse("/pay.html")
        if p in ("/landing.html", "/pay.html"):
            return RedirectResponse("/")
        return await call_next(request)

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
