"""
Биллинг USDT (крипто-оплата на СВОЙ кошелёк, TrustWallet и т.п.).

Принцип безопасности №1: на сервере лежат ТОЛЬКО публичные адреса получателя.
Приватные ключи/seed НИКОГДА не должны попасть ни в код, ни в .env, ни в БД.
Проверка оплаты — watch-only через публичные API проводников (Tronscan/BscScan).

Схема матчинга платежа → пользователя: уникальные центы суммы.
  Например цена $100.00; инвойсы этого часа: 100.00, 100.01, 100.02 …
  Совпадение сети + суммы + окна 24ч = платёж от этого юзера, включаем 30 дней.
100 одновременных «свободных центов» на сеть — запас на фазу 0..N; при
насыщении окно центов крутится дальше (старые pending инвойсы протухают).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

import authn
import db
import partners

log = logging.getLogger("portal.billing")

MS = 1000
INVOICE_MS = 24 * 3600 * MS
_HTTP_TIMEOUT = 20.0

# Контракты USDT
USDT_TRON_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_BSC_CONTRACT = "0x55d398326f99059F775485246999027B3197955"

NETWORKS = ("trc20", "bep20")


async def notify_admin(text: str, settings) -> None:
    """Оповещение владельцев в Telegram (бот токена/чат id из серверного .env)."""
    token = (getattr(settings, "TG_BOT_TOKEN", "") or "").strip()
    chat_id = (getattr(settings, "ADMIN_TG_ID", "") or "").strip()
    if not token or not chat_id:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            await cli.post(f"https://api.telegram.org/bot{token}/sendMessage",
                           json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
    except Exception as e:  # noqa: BLE001 — оповещение не должно ронять оплату
        log.warning("[billing] tg-notify failed: %s", e)


# --------------------------------------------------------------------- #
# Инвойсы
# --------------------------------------------------------------------- #
def _free_cents(network: str, price: float) -> float:
    """Найти свободную сумму price+0..99 центов среди живых pending инвойсов сети."""
    now = int(time.time() * MS)
    rows = db.q(
        "SELECT amount FROM invoices WHERE network=? AND status='pending' AND expires_ms>?",
        (network, now),
    )
    used = {r["amount"] for r in rows}
    for cents in range(100):
        amt = round(price + cents / 100.0, 2)
        if amt not in used:
            return amt
    raise RuntimeError("no_free_amount")   # >99 одновременных плательщиков — редкость фазы 0


def create_invoice(user_id: int, network: str, price: float) -> dict:
    if network not in NETWORKS:
        raise ValueError("bad_network")
    now = int(time.time() * MS)
    # переиспользуем живой pending-инвойс юзера (не плодим клоны по F5)
    cur = db.q(
        "SELECT * FROM invoices WHERE user_id=? AND network=? AND status='pending' AND expires_ms>? ORDER BY id DESC",
        (user_id, network, now), one=True,
    )
    if cur:
        return cur
    amount = _free_cents(network, price)
    db.execute(
        "INSERT INTO invoices(user_id, network, amount, status, created_ms, expires_ms) VALUES (?,?,?,?,?,?)",
        (user_id, network, amount, "pending", now, now + INVOICE_MS),
    )
    return db.q("SELECT * FROM invoices WHERE user_id=? AND network=? AND amount=? ORDER BY id DESC",
                (user_id, network, amount), one=True)


def current_invoice(user_id: int) -> Optional[dict]:
    now = int(time.time() * MS)
    inv = db.q(
        "SELECT * FROM invoices WHERE user_id=? AND status='pending' AND expires_ms>? ORDER BY id DESC",
        (user_id, now), one=True,
    )
    return inv


# --------------------------------------------------------------------- #
# Зачисление платежа (ядро — чистая функция матчинга)
# --------------------------------------------------------------------- #
def record_tx(network: str, tx_hash: str, to_address: str, amount: float, ms: int,
              expect_address: str) -> Optional[dict]:
    """Попытаться сматчить входящий трансфер на наш адрес с pending-инвойсом.
    Идемпотентно: tx_hash хранится в paid_tx и не зачисляется дважды.
    Возвращает обновлённый инвойс или None."""
    if network not in NETWORKS or not tx_hash:
        return None
    if expect_address and to_address and to_address.strip().lower() != expect_address.strip().lower():
        return None
    if db.q("SELECT tx_hash FROM paid_tx WHERE tx_hash=?", (tx_hash,), one=True):
        return None  # уже зачтён
    now = int(time.time() * MS)
    rows = db.q(
        "SELECT * FROM invoices WHERE network=? AND status='pending' AND expires_ms>? ORDER BY id ASC",
        (network, now - 5 * 60 * MS),  # 5-мин допуск на инвойс, созданный чуть раньше tx
    )
    for inv in rows:
        if abs(float(inv["amount"]) - float(amount)) < 0.001:
            # окно: транзакция не раньше создания инвойса (минус 5 мин дрифт)
            if ms and ms < int(inv["created_ms"]) - 5 * 60 * MS:
                continue
            db.execute("UPDATE invoices SET status='paid', tx_hash=? WHERE id=? AND status='pending'",
                       (tx_hash, inv["id"]))
            db.execute("INSERT INTO paid_tx(tx_hash, user_id, network, amount, ms) VALUES (?,?,?,?,?)",
                       (tx_hash, inv["user_id"], network, amount, ms or now))
            new_until = authn.extend_subscription(inv["user_id"], 1)
            user = db.q("SELECT * FROM users WHERE id=?", (inv["user_id"],), one=True) or {}
            credited = partners.credit_payment(user, float(amount), tx_ref=tx_hash)  # % трафферу
            log.info("[billing] оплата %s %.2f USDT от user#%s → доступ до %s%s", network, amount,
                     inv["user_id"], time.strftime("%Y-%m-%d", time.gmtime(new_until / 1000)),
                     f" | партнёр {credited['code']} +${credited['amount']}" if credited else "")
            return db.q("SELECT * FROM invoices WHERE id=?", (inv["id"],), one=True)
    return None


# --------------------------------------------------------------------- #
# Сканеры сетей (публичные API, watch-only)
# --------------------------------------------------------------------- #
async def _fetch_tron(client: httpx.AsyncClient, address: str) -> list[dict]:
    url = ("https://apilist.tronscanapi.com/api/token_trc20/transfers"
           f"?limit=50&start=0&sort=-timestamp&count=true&toAddress={address}"
           f"&contract_address={USDT_TRON_CONTRACT}")
    r = await client.get(url)
    r.raise_for_status()
    out = []
    for t in (r.json().get("token_transfers") or []):
        try:
            out.append({
                "tx_hash": t.get("transaction_id", ""),
                "to": t.get("to_address", ""),
                "amount": float(t.get("quant", 0)) / 1e6,
                "ms": int(t.get("block_ts", 0)),
            })
        except (TypeError, ValueError):
            continue
    return out


async def _fetch_bsc(client: httpx.AsyncClient, address: str, apikey: str) -> list[dict]:
    url = ("https://api.bscscan.com/api?module=account&action=tokentx"
           f"&contractaddress={USDT_BSC_CONTRACT}&address={address}"
           f"&page=1&offset=50&sort=desc&apikey={apikey}")
    r = await client.get(url)
    r.raise_for_status()
    data = r.json()
    if str(data.get("status")) != "1":
        return []
    out = []
    for t in (data.get("result") or []):
        try:
            out.append({
                "tx_hash": t.get("hash", ""),
                "to": t.get("to", ""),
                "amount": float(t.get("value", 0)) / 1e18,  # USDT на BSC: 18 decimals
                "ms": int(t.get("timeStamp", 0)) * 1000,
            })
        except (TypeError, ValueError):
            continue
    return out


async def scan_once(settings) -> dict:
    """Один проход по обеим сетям. Возвращает {'credited': n}."""
    credited = 0
    addrs = {"trc20": (settings.WALLET_TRC20 or "").strip(),
             "bep20": (settings.WALLET_BEP20 or "").strip()}
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        for net, addr in addrs.items():
            if not addr:
                continue
            try:
                txs = (await _fetch_tron(client, addr)) if net == "trc20" \
                    else (await _fetch_bsc(client, addr, settings.BSCSCAN_API_KEY or ""))
            except Exception as e:  # noqa: BLE001 — сеть/API не должны ронять сканер
                log.warning("[billing] %s scan error: %s", net, e)
                continue
            for t in txs:
                paid = record_tx(net, t["tx_hash"], t["to"], t["amount"], t["ms"], addr)
                if paid:
                    credited += 1
                    usr = db.q("SELECT email, referred_by FROM users WHERE id=?", (paid["user_id"],), one=True) or {}
                    ref = usr.get("referred_by") or ""
                    await notify_admin(
                        f"💰 Оплата {paid['amount']} USDT ({net})\n"
                        f"Юзер: {usr.get('email','?')} (#{paid['user_id']})\n"
                        + (f"Траффер: {ref}\n" if ref else "")
                        + f"Время: {time.strftime('%d.%m %H:%M UTC', time.gmtime())}",
                        settings,
                    )
    # закрываем протухшие инвойсы
    db.execute("UPDATE invoices SET status='expired' WHERE status='pending' AND expires_ms<?",
               (int(time.time() * MS),))
    partners.release_due()   # устаревший hold трафферам → к выплате
    return {"credited": credited}


async def watcher_loop(settings, interval_s: int = 45) -> None:
    """Фоновая задача фазы продаж: сканирует сети каждые interval_s секунд."""
    log.info("[billing] watcher запущен (каждые %ss)", interval_s)
    while True:
        try:
            res = await scan_once(settings)
            if res["credited"]:
                log.info("[billing] зачтено платежей: %s", res["credited"])
        except Exception as e:  # noqa: BLE001
            log.warning("[billing] watcher error: %s", e)
        await asyncio.sleep(interval_s)
