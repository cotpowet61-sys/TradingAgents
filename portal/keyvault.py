"""
Шифрование ВАЖНЫХ секретов юзеров (их read-only ключи Bybit) в нашей БД.
AES-256-GCM, ключ = sha256(PORTAL_AUTH_SECRET) — секрет живёт только в .env
сервера (не в БД, не в git). Украв БД без .env — злоумышленник получает шифртекст.

Правило контура: ключи юзеров ВАЛИДИРУЮТСЯ на read-only до сохранения
(см. saas_routes /api/user/bybit-keys) — трейдить/выводить мы физически не можем
и не будем (и по закону не должны).
"""
from __future__ import annotations

import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from portal_config import settings


def _key() -> bytes:
    secret = (settings.AUTH_SECRET or "").strip()
    if not secret:  # защита от «пустого» ключа в ранние дни
        secret = "dev-insecure-do-not-use-in-prod"
    return hashlib.sha256(secret.encode()).digest()


def encrypt(plain: str) -> str:
    """'строка' -> hex(nonce|cipher)"""
    g = AESGCM(_key())
    nonce = os.urandom(12)
    return (nonce + g.encrypt(nonce, plain.encode(), None)).hex()


def decrypt(blob_hex: str) -> str:
    raw = bytes.fromhex(blob_hex)
    g = AESGCM(_key())
    return g.decrypt(raw[:12], raw[12:], None).decode()
