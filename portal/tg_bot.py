"""
Telegram-бот портала (автономный процесс):
  python tg_bot.py

Два лица бота:
1. Для КЛИЕНТОВ (трафик-воронка): /start → короткий питч + кнопка «Открыть сайт».
   Ссылки t.me/бот не банятся рекламными сетями в отличие от crypto-лендингов.
2. Для ТРАФФЕРОВ: /start CODE (ивайт-линк от нас) → бинд тафгера→его tg →
   /stat показывает клики/реги/оплаты/заработок, /link даёт его ссылку.

Зависимостей нет — чистый httpx long-poll.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402

import db as _db  # noqa: E402  (инициализирует схему)
import partners  # noqa: E402
from portal_config import settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("portal.tgbot")

TOKEN = (settings.TG_BOT_TOKEN or "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
MS = 1000


def site_url() -> str:
    # домен фронта (SMB free tip: задайте SITE_URL=https://вашдомен в .env)
    return os.environ.get("SITE_URL", "https://your-domain.com")


async def _call(method: str, **payload):
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.post(f"{API}/{method}", json=payload)
        return r.json()


async def on_message(msg: dict) -> None:
    chat = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()
    tg_id = msg.get("from", {}).get("id", 0)
    if not chat:
        return

    if text.startswith("/start"):
        parts = text.split()
        code = parts[1].strip().upper() if len(parts) > 1 else ""
        if code:
            # бинд траффера: код должен существовать (создаёт владелец через /api/admin/partners)
            t = _db.q("SELECT * FROM traffickers WHERE code=? AND active=1", (code,), one=True)
            if t:
                _db.execute("UPDATE traffickers SET tg_id=? WHERE code=?", (tg_id, code))
                await _call("sendMessage", chat_id=chat,
                            text=(f"✅ Партнёрский код <b>{code}</b> привязан к вашему Telegram.\n\n"
                                  f"🔗 Ваша ссылка:\n{site_url()}/?t={code}\n\n"
                                  f"Статистика — /stat · Комиссия {t['pct']:.0f}% с каждой оплаты, "
                                  f"hold 7 дней, выплаты USDT еженедельно."),
                            parse_mode="HTML")
                return
            await _call("sendMessage", chat_id=chat,
                        text="❌ Такой партнёрский код не найден. Проверьте ссылку или напишите нам.")
            return
        # клиентская воронка
        kb = {"inline_keyboard": [
            [{"text": "🚀 Открыть платформу", "url": site_url()}],
            [{"text": "📊 Живой трек-рекорд", "url": site_url() + "/#track"}],
        ]}
        await _call("sendMessage", chat_id=chat,
                    text=("Привет! Это бот <b>{brand}</b> — AI-тренера трейдера.\n\n"
                          "🔍 Сканер 20+ стратегий, который сам отучается давать сливные сигналы\n"
                          "🧬 Разбор ВАШИХ сделок: найдём, где именно вы теряете\n"
                          "🤖 5 AI-агентов на подхвате\n"
                          "💵 Объём позиции под ваш депозит — в каждом сигнале\n\n"
                          "Доступ $100/мес, оплата USDT, активация за 1–2 минуты.").format(
                        brand=settings.BRAND_NAME),
                    parse_mode="HTML", reply_markup=kb)
        return

    if text.startswith("/stat"):
        t = _db.q("SELECT code FROM traffickers WHERE tg_id=?", (tg_id,), one=True)
        if not t:
            await _call("sendMessage", chat_id=chat,
                        text="Вы не привязаны к партнёрскому коду. Пришлите /start ВАШ_КОД (с ним вам дали ссылку).")
            return
        s = partners.partner_stat(t["code"])
        await _call("sendMessage", chat_id=chat,
                    text=(f"📊 <b>{s['code']}</b>\n"
                          f"Кликов: {s['clicks']} · Регистраций: {s['signups']} · Оплативших: {s['sales']}\n"
                          f"💰 Доступно к выплате: <b>${s['earned_available']:.2f}</b>\n"
                          f"⏳ В холде (7д): ${s['earned_pending']:.2f}\n"
                          f"✅ Выплачено: ${s['earned_paid']:.2f}\n\n"
                          f"Ваша ссылка: {site_url()}/?t={s['code']}"),
                    parse_mode="HTML")
        return

    if text.startswith("/link"):
        t = _db.q("SELECT code FROM traffickers WHERE tg_id=?", (tg_id,), one=True)
        if t:
            await _call("sendMessage", chat_id=chat, text=f"🔗 {site_url()}/?t={t['code']}")
        else:
            await _call("sendMessage", chat_id=chat, text="Сначала привяжите код: /start ВАШ_КОД")
        return

    await _call("sendMessage", chat_id=chat,
                text="Команды: /start — о платформе · /stat — статистика партнёра · /link — моя ссылка.")


async def main() -> None:
    if not TOKEN:
        log.error("TG_BOT_TOKEN пуст — задайте в portal/.env и перезапустите. Спим.")
        while True:
            await asyncio.sleep(3600)
    me = await _call("getMe")
    log.info("бот запущен: %s", (me.get("result") or {}).get("username"))
    offset = 0
    while True:
        try:
            async with httpx.AsyncClient(timeout=40) as cli:
                r = await cli.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 30})
            for upd in (r.json().get("result") or []):
                offset = upd["update_id"] + 1
                if "message" in upd:
                    try:
                        await on_message(upd["message"])
                    except Exception as e:  # noqa: BLE001
                        log.warning("on_message error: %s", e)
        except Exception as e:  # noqa: BLE001
            log.warning("poll error: %s", e)
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
