"""
Конфигурация TradeVision Portal — полностью независимая от основной платформы.

Свой .env рядом с этим файлом (portal/.env). Все настройки меняются «на лету»
из веб-интерфейса (вкладка «Настройки») и сохраняются обратно в файл.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from pydantic_settings import BaseSettings

PORTAL_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(PORTAL_DIR)
ENV_PATH = os.path.join(PORTAL_DIR, ".env")
ENV_EXAMPLE_PATH = os.path.join(PORTAL_DIR, ".env.example")
DATA_DIR = os.path.join(PORTAL_DIR, "data")


class PortalSettings(BaseSettings):
    # ------------------------------------------------------------------ #
    # Сервер
    # ------------------------------------------------------------------ #
    PORTAL_HOST: str = "0.0.0.0"
    PORTAL_PORT: int = 3005

    # ------------------------------------------------------------------ #
    # AI (justcode.lol / любой OpenAI-совместимый endpoint)
    # ------------------------------------------------------------------ #
    AI_API_KEY: str = ""
    AI_BASE_URL: str = "https://api.justcode.lol/v1"
    AI_TIMEOUT: float = 120.0
    AI_MAX_TOKENS: int = 1400
    AI_TEMPERATURE: float = 0.3

    # Модели по ролям (пусто = используется дефолт ниже). Меняются в UI
    # выбором из списка моделей endpoint'а.
    MODEL_STRATEGIST: str = "gpt-6-astra"
    MODEL_MOMENTUM: str = "gpt-5.6-sol"
    MODEL_CONTRARIAN: str = "grok-4.5"
    MODEL_MACRO: str = "gemini-2.5-pro"
    MODEL_RISK: str = "claude-fable-5"
    MODEL_PORTFOLIO: str = "gpt-6-astra"   # агент-аналитик ваших сделок

    # Отдельные ключи по ролям (пусто = глобальный AI_API_KEY)
    KEY_STRATEGIST: str = ""
    KEY_MOMENTUM: str = ""
    KEY_CONTRARIAN: str = ""
    KEY_MACRO: str = ""
    KEY_RISK: str = ""
    KEY_PORTFOLIO: str = ""

    # Индивидуальные выключатели ролей (выкл = не вызываем, токены не тратятся)
    EN_STRATEGIST: bool = True
    EN_MOMENTUM: bool = True
    EN_CONTRARIAN: bool = True
    EN_MACRO: bool = True
    EN_RISK: bool = True
    EN_PORTFOLIO: bool = True

    # ------------------------------------------------------------------ #
    # Bybit: публичные данные + (ОПЦИОНАЛЬНО) read-only ключи для сделок.
    # ВАЖНО: для анализа сделок создавайте ключ с правом ТОЛЬКО НА ЧТЕНИЕ.
    # ------------------------------------------------------------------ #
    BYBIT_BASE_URL: str = "https://api.bybit.com"
    BYBIT_API_KEY: str = ""           # read-only, для закрытых эндпоинтов истории
    BYBIT_API_SECRET: str = ""
    MARKET_CATEGORY: str = "linear"   # бессрочные USDT-перпetуалы; спот — запасной

    # ------------------------------------------------------------------ #
    # Live-сигналы (индикаторные, токены не тратятся)
    # ------------------------------------------------------------------ #
    WATCHLIST: str = "BTCUSDT,ETHUSDT,SOLUSDT,LSKUSDT"
    SCAN_INTERVAL_SECONDS: int = 30
    DEFAULT_TIMEFRAME: str = "5m"
    # Таймфреймы сканера через запятую (1m,3m,5m,15m,30m,1h,4h).
    # Пусто = только DEFAULT_TIMEFRAME.
    SIGNAL_TIMEFRAMES: str = "5m"
    MIN_CONFIDENCE: float = 0.55
    SIGNAL_STRATEGY: str = "auto"     # auto = авто-подбор по графику (как в анализе)
    # Звук/всплывашки хранятся на клиенте, но дефолты можно задать здесь
    NOTIFY_SOUND: bool = True
    NOTIFY_TOASTS: bool = True
    # Привязка правила TP≥2% / SL 1% (RR 2:1) к live-сигналам.
    # False = цели считаются по ATR свежего графика (сигналы «дышат» под волу).
    RR_BINDING: bool = True
    # Ручной риск-фактор: если TP и SL > 0 — сигналы получают именно эти цели
    # (приоритет над привязкой 2:1 и над ATR). 0 = выключено.
    MANUAL_TP_PCT: float = 0.0
    MANUAL_SL_PCT: float = 0.0
    MANUAL_HOLD_MIN: float = 0.0     # целевая длительность сделки, мин (0 = не задано)
    # Размер позиции под сигнал (для отображения): депозит/риск%/плечо
    DEPOSIT_USD: float = 1000.0
    RISK_PER_TRADE_PCT: float = 3.0  # доля депозита, которой рискуем в одной сделке
    LEVERAGE: float = 20.0           # плечо при расчёте объёма/маржи

    # ------------------------------------------------------------------ #
    # SaaS (мульти-пользовательский режим продажи подписки)
    # ------------------------------------------------------------------ #
    SAAS_MODE: bool = False          # включается ТОЛЬКО через .env (во избежание самоблокировки)
    BRAND_NAME: str = "TradeVision"
    PRICE_MONTH_USD: float = 100.0   # цена месяца доступа
    # Публичные адреса приёма оплаты (ТОЛЬКО адреса! Приватных ключей на сервере НЕТ)
    WALLET_TRC20: str = ""
    WALLET_BEP20: str = ""
    BSCSCAN_API_KEY: str = ""        # бесплатный ключ api.bscscan.com (для сканера BSC)
    TELEGRAM_URL: str = ""           # канал поддержки/комьюнити
    # Секрет подписи сессий. Если пуст — генерируется один раз и пишется в .env.
    AUTH_SECRET: str = ""            # env PORTAL_AUTH_SECRET

    # Правила дэйтрейдинга (для отображения и контекста агентов)
    # 0 = без лимита (пользователь снял границу 30/день).
    MAX_TRADES_PER_DAY: int = 0
    MIN_TP_PCT: float = 2.0
    MAX_HOLD_HOURS: float = 4.0
    DEFAULT_SL_PCT: float = 1.0

    class Config:
        env_file = ENV_PATH
        case_sensitive = True
        extra = "ignore"

    ROLE_KEYS: List[str] = ["strategist", "momentum", "contrarian", "macro", "risk", "portfolio"]

    # ------------------------------------------------------------------ #
    # Хелперы
    # ------------------------------------------------------------------ #
    @property
    def watchlist(self) -> List[str]:
        return [s.strip().upper() for s in self.WATCHLIST.split(",") if s.strip()]

    _VALID_TFS = ("1m", "3m", "5m", "15m", "30m", "1h", "4h")

    @property
    def signal_timeframes(self) -> List[str]:
        tfs = [t.strip() for t in (self.SIGNAL_TIMEFRAMES or "").split(",")
               if t.strip() in self._VALID_TFS]
        return tfs or [self.DEFAULT_TIMEFRAME]

    @property
    def manual_risk(self) -> bool:
        return self.MANUAL_TP_PCT > 0 and self.MANUAL_SL_PCT > 0

    def role_model(self, role: str) -> Dict[str, Any]:
        m = getattr(self, f"MODEL_{role.upper()}", "") or "gpt-6-astra"
        key = getattr(self, f"KEY_{role.upper()}", "") or self.AI_API_KEY
        return {"model": m, "api_key": key, "base_url": self.AI_BASE_URL}

    def role_enabled(self, role: str) -> bool:
        return bool(getattr(self, f"EN_{role.upper()}", True))

    def has_bybit_keys(self) -> bool:
        return bool(self.BYBIT_API_KEY and self.BYBIT_API_SECRET)

    def is_demo(self) -> bool:
        return not any(self.role_model(r)["api_key"] for r in self.ROLE_KEYS)

    # ------------------------------------------------------------------ #
    # Публичная конфигурация для UI (ключи замаскированы)
    # ------------------------------------------------------------------ #
    def public_config(self) -> dict:
        return {
            "demo_mode": self.is_demo(),
            "ai_base_url": self.AI_BASE_URL,
            "global_api_key": _mask(self.AI_API_KEY),
            "roles": [
                {
                    "key": r,
                    "model": self.role_model(r)["model"],
                    "api_key": _mask(self.role_model(r)["api_key"]),
                    "enabled": self.role_enabled(r),
                }
                for r in self.ROLE_KEYS
            ],
            "bybit": {
                "base_url": self.BYBIT_BASE_URL,
                "api_key": _mask(self.BYBIT_API_KEY),
                "has_keys": self.has_bybit_keys(),
            },
            "live": {
                "watchlist": self.watchlist,
                "scan_interval_seconds": self.SCAN_INTERVAL_SECONDS,
                "timeframe": self.DEFAULT_TIMEFRAME,
                "signal_timeframes": self.signal_timeframes,
                "min_confidence": self.MIN_CONFIDENCE,
                "signal_strategy": self.SIGNAL_STRATEGY,
                "notify_sound": self.NOTIFY_SOUND,
                "notify_toasts": self.NOTIFY_TOASTS,
                "rr_binding": self.RR_BINDING,
                "manual_risk": {
                    "enabled": self.manual_risk,
                    "tp_pct": self.MANUAL_TP_PCT,
                    "sl_pct": self.MANUAL_SL_PCT,
                    "hold_min": self.MANUAL_HOLD_MIN,
                },
                "risk_calc": {
                    "deposit_usd": self.DEPOSIT_USD,
                    "risk_per_trade_pct": self.RISK_PER_TRADE_PCT,
                    "leverage": self.LEVERAGE,
                },
            },
            "rules": {
                "max_trades_per_day": self.MAX_TRADES_PER_DAY,
                "min_tp_pct": self.MIN_TP_PCT,
                "max_hold_hours": self.MAX_HOLD_HOURS,
                "default_sl_pct": self.DEFAULT_SL_PCT,
            },
            "saas": {  # только публичные данные (адреса кошельков публичны по природе)
                "enabled": self.SAAS_MODE,
                "brand": self.BRAND_NAME,
                "price_month": self.PRICE_MONTH_USD,
                "wallets": {"trc20": self.WALLET_TRC20, "bep20": self.WALLET_BEP20},
                "telegram": self.TELEGRAM_URL,
            },
        }

    # ------------------------------------------------------------------ #
    # Применение изменений «на лету» + сохранение в portal/.env
    # ------------------------------------------------------------------ #
    _ENV_FIELDS = {
        "ai_api_key": ("AI_API_KEY", "AI_API_KEY"),
        "ai_base_url": ("AI_BASE_URL", "AI_BASE_URL"),
        "bybit_base_url": ("BYBIT_BASE_URL", "BYBIT_BASE_URL"),
        "bybit_api_key": ("BYBIT_API_KEY", "BYBIT_API_KEY"),
        "bybit_api_secret": ("BYBIT_API_SECRET", "BYBIT_API_SECRET"),
        "watchlist": ("WATCHLIST", "WATCHLIST"),
        "scan_interval_seconds": ("SCAN_INTERVAL_SECONDS", "SCAN_INTERVAL_SECONDS"),
        "default_timeframe": ("DEFAULT_TIMEFRAME", "DEFAULT_TIMEFRAME"),
        "signal_timeframes": ("SIGNAL_TIMEFRAMES", "SIGNAL_TIMEFRAMES"),
        "manual_tp_pct": ("MANUAL_TP_PCT", "MANUAL_TP_PCT"),
        "manual_sl_pct": ("MANUAL_SL_PCT", "MANUAL_SL_PCT"),
        "manual_hold_min": ("MANUAL_HOLD_MIN", "MANUAL_HOLD_MIN"),
        "deposit_usd": ("DEPOSIT_USD", "DEPOSIT_USD"),
        "risk_per_trade_pct": ("RISK_PER_TRADE_PCT", "RISK_PER_TRADE_PCT"),
        "leverage": ("LEVERAGE", "LEVERAGE"),
        "brand_name": ("BRAND_NAME", "BRAND_NAME"),
        "price_month_usd": ("PRICE_MONTH_USD", "PRICE_MONTH_USD"),
        "wallet_trc20": ("WALLET_TRC20", "WALLET_TRC20"),
        "wallet_bep20": ("WALLET_BEP20", "WALLET_BEP20"),
        "bscscan_api_key": ("BSCSCAN_API_KEY", "BSCSCAN_API_KEY"),
        "telegram_url": ("TELEGRAM_URL", "TELEGRAM_URL"),
        "min_confidence": ("MIN_CONFIDENCE", "MIN_CONFIDENCE"),
        "signal_strategy": ("SIGNAL_STRATEGY", "SIGNAL_STRATEGY"),
        "notify_sound": ("NOTIFY_SOUND", "NOTIFY_SOUND"),
        "notify_toasts": ("NOTIFY_TOASTS", "NOTIFY_TOASTS"),
        "rr_binding": ("RR_BINDING", "RR_BINDING"),
        "model_strategist": ("MODEL_STRATEGIST", "MODEL_STRATEGIST"),
        "model_momentum": ("MODEL_MOMENTUM", "MODEL_MOMENTUM"),
        "model_contrarian": ("MODEL_CONTRARIAN", "MODEL_CONTRARIAN"),
        "model_macro": ("MODEL_MACRO", "MODEL_MACRO"),
        "model_risk": ("MODEL_RISK", "MODEL_RISK"),
        "model_portfolio": ("MODEL_PORTFOLIO", "MODEL_PORTFOLIO"),
        "key_strategist": ("KEY_STRATEGIST", "KEY_STRATEGIST"),
        "key_momentum": ("KEY_MOMENTUM", "KEY_MOMENTUM"),
        "key_contrarian": ("KEY_CONTRARIAN", "KEY_CONTRARIAN"),
        "key_macro": ("KEY_MACRO", "KEY_MACRO"),
        "key_risk": ("KEY_RISK", "KEY_RISK"),
        "key_portfolio": ("KEY_PORTFOLIO", "KEY_PORTFOLIO"),
        "en_strategist": ("EN_STRATEGIST", "EN_STRATEGIST"),
        "en_momentum": ("EN_MOMENTUM", "EN_MOMENTUM"),
        "en_contrarian": ("EN_CONTRARIAN", "EN_CONTRARIAN"),
        "en_macro": ("EN_MACRO", "EN_MACRO"),
        "en_risk": ("EN_RISK", "EN_RISK"),
        "en_portfolio": ("EN_PORTFOLIO", "EN_PORTFOLIO"),
        "max_trades_per_day": ("MAX_TRADES_PER_DAY", "MAX_TRADES_PER_DAY"),
        "min_tp_pct": ("MIN_TP_PCT", "MIN_TP_PCT"),
        "max_hold_hours": ("MAX_HOLD_HOURS", "MAX_HOLD_HOURS"),
        "default_sl_pct": ("DEFAULT_SL_PCT", "DEFAULT_SL_PCT"),
    }

    def apply_update(self, updates: Dict[str, Any]) -> Dict[str, str]:
        applied: Dict[str, str] = {}
        for ui_key, value in updates.items():
            if ui_key not in self._ENV_FIELDS:
                continue
            attr, env_key = self._ENV_FIELDS[ui_key]
            current = getattr(self, attr)
            if isinstance(current, bool):
                value = str(value).lower() in ("1", "true", "yes", "on")
            elif isinstance(current, int) and not isinstance(current, bool):
                value = int(value)
            elif isinstance(current, float):
                value = float(value)
            setattr(self, attr, value)
            applied[env_key] = _env_repr(value)
        if applied:
            _persist_env(applied)
        return applied


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return key[:4] + "…" + key[-4:]


def _env_repr(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _persist_env(updates: Dict[str, str]) -> None:
    """Обновляет указанные ключи в portal/.env, сохраняя остальное."""
    try:
        ensure_env_file()
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        existing = set()
        out = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in updates:
                    out.append(f"{key}={updates[key]}\n")
                    existing.add(key)
                    continue
            out.append(line)
        for key, val in updates.items():
            if key not in existing:
                out.append(f"{key}={val}\n")
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.writelines(out)
    except Exception as e:
        print(f"[portal-config] не удалось сохранить .env: {e}")


def ensure_env_file() -> None:
    """Создаёт portal/.env из шаблона, если файла нет."""
    if os.path.exists(ENV_PATH):
        return
    try:
        if os.path.exists(ENV_EXAMPLE_PATH):
            with open(ENV_EXAMPLE_PATH, "r", encoding="utf-8") as f:
                content = f.read()
        else:
            content = ""
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[portal-config] создан .env: {ENV_PATH}")
    except Exception as e:
        print(f"[portal-config] не удалось создать .env: {e}")


settings = PortalSettings()
