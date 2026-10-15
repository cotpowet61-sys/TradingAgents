"""
Конфигурация TradeVision Portal — полностью независимая от основной платформы.

Свой .env рядом с этим файлом (portal/.env). Все настройки меняются «на лету»
из веб-интерфейса (вкладка «Настройки») и сохраняются обратно в файл.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings

PORTAL_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(PORTAL_DIR)
# Путь .env можно переопределить (тесты, второй инстанс, контейнер с volume).
ENV_PATH = os.environ.get("PORTAL_ENV_FILE") or os.path.join(PORTAL_DIR, ".env")
ENV_EXAMPLE_PATH = os.path.join(PORTAL_DIR, ".env.example")
# Каталог данных (БД, журналы, кэши) можно вынести (тесты, контейнер, отдельный диск).
DATA_DIR = os.environ.get("PORTAL_DATA_DIR") or os.path.join(PORTAL_DIR, "data")


class PortalSettings(BaseSettings):
    # ------------------------------------------------------------------ #
    # Сервер
    # ------------------------------------------------------------------ #
    # По умолчанию слушаем ТОЛЬКО localhost: наружу портал отдаёт reverse-proxy
    # (Caddy в deploy/). Прямое выставление в интернет = осознанное решение:
    # тогда обязательно TRUST_PROXY_HEADERS=false (см. комментарий у флага).
    PORTAL_HOST: str = "127.0.0.1"
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
    RR_BINDING: bool = False     # True = наследие: строгий дефолт TP 2% / SL 1%
    # Ручной риск-фактор: если TP и SL > 0 — сигналы получают именно эти цели
    # (приоритет над привязкой 2:1 и над ATR). 0 = выключено.
    MANUAL_TP_PCT: float = 0.0
    MANUAL_SL_PCT: float = 0.0
    MANUAL_HOLD_MIN: float = 0.0     # целевая длительность сделки, мин (0 = не задано)
    # Размер позиции под сигнал (для отображения): депозит/риск%/плечо
    DEPOSIT_USD: float = 1000.0
    RISK_PER_TRADE_PCT: float = 3.0  # доля депозита, которой рискуем в одной сделке
    LEVERAGE: float = 20.0           # плечо при расчёте объёма/маржи
    # ---- Квант-фильтры качества сигналов (по мотивам бэктест-харнесса) ----
    # Множители ATR-режима целей (когда RR_BINDING выкл): TP = ATR%×mult, SL = ATR%×mult.
    ATR_TP_MULT: float = 2.0
    ATR_SL_MULT: float = 1.2
    # Сезонный объёмный фильтр (Chen–Feng–Palomar: сезонная база точнее rolling-mean).
    # Сигнал пропускаем, только если объём бара выше «нормы этого часа UTC».
    VOL_SEASON_GATE: bool = False    # включаем после подтверждения бэктестом
    VOL_SEASON_MIN: float = 1.0
    VOL_SEASON_STYPES: str = ""      # пусто = все стратегии; иначе "momentum_breakout,rsi_swing"
    # Стоп-лист стратегий (из отчёта бэктеста): \"stype\" или \"stype:SYMBOL\" через запятую.
    STYPE_BLOCKLIST: str = ""
    # ^ формат: "stype", "stype:SYMBOL" или "stype/SIDE" (напр. supertrend/SHORT)
    # Запретные ДИАПАЗОНЫ сезонного объёма: "1.0-2.0,3.0-999". Пусто = не режем.
    # По week1: зоны 1.0-2.0 и >=3.0 — худшие (30-35% win, минусовой avg).
    VOL_SEASON_BLOCK: str = ""
    # Запретные диапазоны ATR% входа: "0.6-1.0". Пусто = не режем.
    ATR_PCT_BLOCK: str = ""
    # Лестница интервалов сканирования (сек) через запятую: 30,60,90.
    # Пусто = фиксированный SCAN_INTERVAL_SECONDS. Все настройки читаются
    # напрямую через поле — getattr-дефолт молча игнорировал бы .env.
    SCAN_CADENCE_SECONDS: str = ""
    TFS_PER_TICK: int = 1             # сколько ТФ смотреть за один проход (ротация)
    # Не выдавать сигналы/исходы, если биржа недоступна и данные синтетические:
    # «учебный» график не должен попадать ни в ленту, ни в обучение, ни в публичный трек.
    REQUIRE_REAL_DATA: bool = True
    # Запретные часы входа (UTC) через запятую: "3,11,19". Пусто = не режем.
    HOURS_BLOCK: str = ""
    # Breakeven: при движении в прибыль на BE_TRIGGER_PCT% стоп → вход + BE_OFFSET_PCT%.
    # 0 = выкл. Ключевой механизм высокого winrate (проигрыш → нулевой выход).
    BE_TRIGGER_PCT: float = 0.0
    BE_OFFSET_PCT: float = 0.15
    # Трейлинг: после TRAIL_START_PCT% прибыли стоп следует за ценой на TRAIL_PCT%.
    TRAIL_START_PCT: float = 0.0
    TRAIL_PCT: float = 0.6
    # ---- Адекватные рамки плавающих целей (против TP 0.001% / SL 10%) ----
    TP_PCT_MIN: float = 0.5
    TP_PCT_MAX: float = 3.0
    SL_PCT_MIN: float = 0.25
    SL_PCT_MAX: float = 2.5

    # ---- ИИ-фильтр сигналов (мета-модель из research/) ------------------ #
    # Бандл обучает исследовательский контур (research/run.py all → bundle.json),
    # портал только применяет его. Пока USE_CURATED=false — всё работает как раньше.
    USE_CURATED: bool = False
    CURATED_BUNDLE: str = ""          # путь к bundle.json (обычно research/artifacts/bundle.json)
    CURATED_SHADOW: bool = True       # true = ничего не блокируем, только считаем и логируем
    CURATED_MIN_P_WIN: float = 0.0    # 0 = брать порог из бандла
    CURATED_MIN_EV: float = 0.0       # 0 = брать порог ожидаемого PnL из бандла

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
    # Администрирование SaaS (только серверные, НЕ светятся в public())
    ADMIN_TOKEN: str = ""            # токен админ-API (партнёры/стата/выплаты)
    SITE_URL: str = ""               # домен фронта (для ссылок/бота/Origin-проверки)
    # Владельцы продукта: email(ы) через запятую — у них подписка всегда активна,
    # оплата не требуется, доступ ко всему продукту сразу после входа.
    OWNER_EMAILS: str = ""
    TG_BOT_TOKEN: str = ""           # токен TG-бота (@BotFather) — бот+уведомления о платежах
    ADMIN_TG_ID: str = ""            # ваш числовой telegram id (кому слать оповещения)
    # Секрет подписи сессий. Если пуст — генерируется один раз и пишется в .env.
    # В .env/деплое переменная исторически называется PORTAL_AUTH_SECRET, а поле —
    # AUTH_SECRET. Без явного алиаса значение из .env НЕ читалось: секрет генерировался
    # заново при каждом старте, и ключи клиентов становились нерасшифровываемыми.
    AUTH_SECRET: str = Field(default="", validation_alias=AliasChoices(
        "AUTH_SECRET", "PORTAL_AUTH_SECRET"))
    # Community-AI (SaaS): бесплатный провайдер для юзеров без своего ключа.
    # OpenRouter совместим с OpenAI API; ":free" модели стоят $0.
    FREE_AI_BASE_URL: str = "https://openrouter.ai/api/v1"
    FREE_AI_KEY: str = ""            # ключ OpenRouter (серверный, НЕ в public)
    FREE_AI_MODEL: str = "meta-llama/llama-3.3-70b-instruct:free"

    # ------------------------------------------------------------------ #
    # Безопасность / эксплуатация
    # ------------------------------------------------------------------ #
    SECURITY_HEADERS: bool = True     # CSP/nosniff/referrer-policy/frame-ancestors
    CSRF_ORIGIN_CHECK: bool = True    # небезопасные методы только с нашего Origin
    HSTS_ENABLED: bool = True         # Strict-Transport-Security (только за HTTPS)
    # Доверять X-Forwarded-For/Host можно ТОЛЬКО когда впереди свой прокси (Caddy).
    # Портал, выставленный в интернет напрямую, обязан иметь false — иначе IP и
    # rate-limit подделываются заголовком.
    TRUST_PROXY_HEADERS: bool = True
    UNSAFE_LOCAL: bool = False        # аварийный флаг: разрешить небезопасные конфигурации (только локально!)
    FRAME_ANCESTORS: str = "'self'"   # CSP frame-ancestors (напр. "'self' https://partner.example")
    ALLOWED_ORIGINS: str = ""         # доп. Origin'ы через запятую (партнёрские превью-домены)
    MAX_BODY_MB: float = 12.0         # лимит тела запроса (CSV сделок идёт одним куском)
    REQUIRE_SUB_FOR_AI: bool = True   # AI-эндпоинты недоступны без активной подписки (SaaS)
    # Подтверждение «кошелёк — только публичный адрес»: контур осознанно watch-only,
    # приватных ключей/seed на сервере быть не должно.
    TRC20_WATCH_ONLY_ACK: bool = False
    # Фоновый сканер можно выключить (тесты, CI, отдельный воркер-сканер).
    LIVE_SCANNER: bool = True
    # Наблюдатель за блокчейном (подтверждение оплат) — можно вынести в отдельный процесс.
    BILLING_WATCHER: bool = True

    # ---- Бэктест (анти-DoS: тяжёлые прогоны ограничены) ----
    BACKTEST_MAX_DAYS: int = 180      # максимум истории на один прогон
    BACKTEST_MAX_SYMBOLS: int = 6
    BACKTEST_MAX_TFS: int = 3
    BACKTEST_TIMEOUT_SEC: int = 180   # общий таймаут прогона

    # Правила дэйтрейдинга (для отображения и контекста агентов)
    # 0 = без лимита (пользователь снял границу 30/день).
    MAX_TRADES_PER_DAY: int = 0
    MIN_TP_PCT: float = 0.5  # правило «мин. TP 2%» снято: плавающие цели в рамках TP_PCT_*
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
                "curated_ai": {
                    "enabled": self.USE_CURATED,
                    "shadow": self.CURATED_SHADOW,
                    "bundle": _mask(self.CURATED_BUNDLE),
                    "bundle_exists": bool(self.CURATED_BUNDLE) and os.path.exists(self.CURATED_BUNDLE),
                    "min_p_win": self.CURATED_MIN_P_WIN,
                    "min_ev": self.CURATED_MIN_EV,
                },
                "quant_filters": {
                    "atr_tp_mult": self.ATR_TP_MULT,
                    "atr_sl_mult": self.ATR_SL_MULT,
                    "vol_season_gate": self.VOL_SEASON_GATE,
                    "vol_season_min": self.VOL_SEASON_MIN,
                    "vol_season_stypes": self.VOL_SEASON_STYPES,
                    "stype_blocklist": self.STYPE_BLOCKLIST,
                    "vol_season_block": self.VOL_SEASON_BLOCK,
                    "atr_pct_block": self.ATR_PCT_BLOCK,
                    "hours_block": self.HOURS_BLOCK,
                },
                "exit_engine": {
                    "be_trigger_pct": self.BE_TRIGGER_PCT,
                    "be_offset_pct": self.BE_OFFSET_PCT,
                    "trail_start_pct": self.TRAIL_START_PCT,
                    "trail_pct": self.TRAIL_PCT,
                },
                "target_bounds": {
                    "tp_pct_min": self.TP_PCT_MIN, "tp_pct_max": self.TP_PCT_MAX,
                    "sl_pct_min": self.SL_PCT_MIN, "sl_pct_max": self.SL_PCT_MAX,
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
        "atr_tp_mult": ("ATR_TP_MULT", "ATR_TP_MULT"),
        "atr_sl_mult": ("ATR_SL_MULT", "ATR_SL_MULT"),
        "vol_season_gate": ("VOL_SEASON_GATE", "VOL_SEASON_GATE"),
        "vol_season_min": ("VOL_SEASON_MIN", "VOL_SEASON_MIN"),
        "vol_season_stypes": ("VOL_SEASON_STYPES", "VOL_SEASON_STYPES"),
        "stype_blocklist": ("STYPE_BLOCKLIST", "STYPE_BLOCKLIST"),
        "vol_season_block": ("VOL_SEASON_BLOCK", "VOL_SEASON_BLOCK"),
        "atr_pct_block": ("ATR_PCT_BLOCK", "ATR_PCT_BLOCK"),
        "hours_block": ("HOURS_BLOCK", "HOURS_BLOCK"),
        "be_trigger_pct": ("BE_TRIGGER_PCT", "BE_TRIGGER_PCT"),
        "be_offset_pct": ("BE_OFFSET_PCT", "BE_OFFSET_PCT"),
        "trail_start_pct": ("TRAIL_START_PCT", "TRAIL_START_PCT"),
        "trail_pct": ("TRAIL_PCT", "TRAIL_PCT"),
        "use_curated": ("USE_CURATED", "USE_CURATED"),
        "curated_shadow": ("CURATED_SHADOW", "CURATED_SHADOW"),
        "curated_bundle": ("CURATED_BUNDLE", "CURATED_BUNDLE"),
        "curated_min_p_win": ("CURATED_MIN_P_WIN", "CURATED_MIN_P_WIN"),
        "curated_min_ev": ("CURATED_MIN_EV", "CURATED_MIN_EV"),
        "tp_pct_min": ("TP_PCT_MIN", "TP_PCT_MIN"),
        "tp_pct_max": ("TP_PCT_MAX", "TP_PCT_MAX"),
        "sl_pct_min": ("SL_PCT_MIN", "SL_PCT_MIN"),
        "sl_pct_max": ("SL_PCT_MAX", "SL_PCT_MAX"),
        "leverage": ("LEVERAGE", "LEVERAGE"),
        "brand_name": ("BRAND_NAME", "BRAND_NAME"),
        "owner_emails": ("OWNER_EMAILS", "OWNER_EMAILS"),
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


# ---------------------------------------------------------------------- #
# Проверка боевой конфигурации (вызывается на старте сервера)
# ---------------------------------------------------------------------- #
def ensure_secrets(s: "PortalSettings") -> list[str]:
    """Первичная генерация секретов, без которых сервис не поднимется.

    Зачем: в комментариях обещано «если AUTH_SECRET пуст — генерируется один раз и
    пишется в .env», но генерации не было, и развёрнутый «с нуля» сервер с
    SAAS_MODE=true просто отказывался стартовать. Хуже того — если бы секрет
    генерировался в памяти, ключи клиентов, зашифрованные в одном запуске,
    не расшифровались бы в следующем (PORTAL_AUTH_SECRET = ключ шифрования ключей).

    Поэтому: генерируем ОДИН раз, пишем в .env, возвращаем список сообщений.
    Существующие значения не перезаписываем никогда — иначе слетит расшифровка.
    """
    import secrets as _secrets
    messages: list[str] = []
    updates: dict[str, str] = {}
    secret = (getattr(s, "AUTH_SECRET", "") or "").strip()
    if len(secret) < 24:
        secret = _secrets.token_hex(32)
        updates["PORTAL_AUTH_SECRET"] = secret
        messages.append("AUTH_SECRET сгенерирован (64 hex) и записан в " + ENV_PATH
                        + " — СДЕЛАЙТЕ БЭКАП: без него не расшифруются ключи клиентов")
    token = (getattr(s, "ADMIN_TOKEN", "") or "").strip()
    if not token:
        token = _secrets.token_urlsafe(32)
        updates["ADMIN_TOKEN"] = token
        messages.append("ADMIN_TOKEN сгенерирован (32+ символа) и записан в " + ENV_PATH
                        + " — он же нужен для x-admin-token у /api/metrics и админ-API")
    if not updates:
        return messages
    try:
        _persist_env(updates)
    except Exception as e:      # noqa: BLE001 — .env может быть только для чтения
        messages.append(f"не удалось сохранить секреты в .env ({e}); "
                        f"пропишите их вручную, иначе после перезапуска доступы сломаются")
    # применяем в текущем процессе (файл уже обновлён, но объект настроек — в памяти)
    s.AUTH_SECRET = secret
    s.ADMIN_TOKEN = token
    return messages


def validate_startup(s: "PortalSettings") -> tuple[list[str], list[str]]:
    """→ (ошибки, предупреждения).

    Ошибка = конфигурация физически не может работать или небезопасна;
    сервер откажется стартовать (кроме явного UNSAFE_LOCAL=true — для локальной
    разработки). Предупреждение = продукт запустится, но часть функций молчит.
    """
    err: list[str] = []
    warn: list[str] = []

    if s.SAAS_MODE:
        if not (s.AUTH_SECRET or "").strip() or len(s.AUTH_SECRET.strip()) < 24:
            err.append("PORTAL_AUTH_SECRET пуст/короткий — сессии и шифрование ключей клиентов невозможны")
        if not (s.ADMIN_TOKEN or "").strip():
            err.append("ADMIN_TOKEN не задан — админ-API (партнёры/выплаты) недоступен")
        if not ((s.WALLET_TRC20 or "").strip() or (s.WALLET_BEP20 or "").strip()):
            err.append("Не задан ни один кошелёк приёма (WALLET_TRC20/WALLET_BEP20) — оплата невозможна, "
                       "а SAAS_MODE=true закроет доступ к терминалу до оплаты")
        if not (s.SITE_URL or "").strip():
            warn.append("SITE_URL не задан — ссылки в TG-боте и Origin-проверка будут неполными")
        if not (s.TELEGRAM_URL or "").strip():
            warn.append("TELEGRAM_URL не задан — кнопка поддержки на лендинге/в кабинете не появится")
        if not any(s.role_model(r)["api_key"] for r in s.ROLE_KEYS) and not (s.FREE_AI_KEY or "").strip():
            warn.append("Нет ни одного AI-ключа — агенты будут отвечать демо-вердиктами (продукт продаётся с AI!)")
    if s.USE_CURATED:
        if not (s.CURATED_BUNDLE or "").strip():
            warn.append("USE_CURATED=true, но CURATED_BUNDLE не задан — ИИ-фильтр включится только "
                        "после публикации бандла (research/run.py all)")
        elif not os.path.exists(s.CURATED_BUNDLE):
            warn.append(f"CURATED_BUNDLE={s.CURATED_BUNDLE} не найден — сигналы идут без ИИ-фильтра")
        try:
            import curated as _cur                      # noqa: F401
            _b = _cur.load_bundle(s.CURATED_BUNDLE)
            print(f"[portal-config] ИИ-фильтр: бандл ок ({len(_b['features'])} признаков, "
                  f"порог p={_b.get('threshold_win')}, обучен на {(_b.get('trained_on') or {}).get('train_n')} сделках, "
                  f"shadow={s.CURATED_SHADOW})")
        except Exception as e:      # noqa: BLE001
            warn.append(f"ИИ-фильтр не загрузился ({e}) — сигналы идут без него")
    if s.SAAS_MODE:
        if (s.WALLET_TRC20 or "").strip() and not s.TRC20_WATCH_ONLY_ACK:
            warn.append("WALLET_TRC20 задан: подтвердите, что это ПУБЛИЧНЫЙ адрес приёма, и поставьте "
                        "TRC20_WATCH_ONLY_ACK=true (приватные ключи на сервере хранить нельзя)")
    return err, warn


def enforce_startup(s: "PortalSettings") -> None:
    err, warn = validate_startup(s)
    for w in warn:
        print(f"[portal-config] ⚠️  {w}")
    if err:
        msg = "Конфигурация не готова к боевому запуску:\n  - " + "\n  - ".join(err)
        if s.UNSAFE_LOCAL:
            print(f"[portal-config] ⚠️  UNSAFE_LOCAL=true: запускаюсь несмотря на проблемы\n{msg}")
            return
        raise RuntimeError(msg + "\n(исправьте portal/.env или выставьте UNSAFE_LOCAL=true для локальной отладки)")


settings = PortalSettings()
