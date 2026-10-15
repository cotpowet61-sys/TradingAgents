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


class KeyvaultError(Exception):
    """Ключ хранилища отсутствует/слабый — шифровать нечем, и врать нельзя."""


def _key() -> bytes:
    """Ключ хранилища = sha256(PORTAL_AUTH_SECRET).

    Секрет ОБЯЗАТЕЛЕН: раньше при пустом значении подставлялась строка
    «dev-insecure-...», и все ключи клиентов шифровались предсказуемым ключом.
    Теперь это жёсткая ошибка — лучше 503 на одном эндпоинте, чем утечка всех.
    """
    secret = (settings.AUTH_SECRET or "").strip()
    if not secret or secret == "dev-insecure-do-not-use-in-prod":
        raise KeyvaultError(
            "PORTAL_AUTH_SECRET не задан — задайте его в portal/.env "
            "(генерируется автоматически при включённом SAAS_MODE) и перезапустите портал"
        )
    if len(secret) < 24:
        raise KeyvaultError("PORTAL_AUTH_SECRET слишком короткий (нужно ≥24 символа)")
    # соль-разделитель: тот же секрет даёт разные ключи под разные задачи
    return hashlib.sha256(("tv-keyvault-v1:" + secret).encode()).digest()


_V1 = "v1:"          # маркер формата: позволяет менять схему ключа без потери данных


def _legacy_key() -> bytes:
    """Старая схема (до v1): sha256(secret) без разделителя — только для чтения
    ранее сохранённых блобов."""
    secret = (settings.AUTH_SECRET or "").strip()
    return hashlib.sha256(secret.encode()).digest()


def encrypt(plain: str) -> str:
    """'строка' -> 'v1:' + hex(nonce|шифртекст)"""
    g = AESGCM(_key())
    nonce = os.urandom(12)
    return _V1 + (nonce + g.encrypt(nonce, plain.encode(), None)).hex()


def needs_reencrypt(blob: str) -> bool:
    return bool(blob) and not str(blob).startswith(_V1)


def decrypt(blob_hex: str) -> str:
    """Расшифровка ключей юзера. Поддерживает и старый формат (без префикса)."""
    blob_hex = (blob_hex or "").strip()
    raw = bytes.fromhex(blob_hex[len(_V1):] if blob_hex.startswith(_V1) else blob_hex)
    key = _key() if blob_hex.startswith(_V1) else _legacy_key()
    g = AESGCM(key)
    return g.decrypt(raw[:12], raw[12:], None).decode()
