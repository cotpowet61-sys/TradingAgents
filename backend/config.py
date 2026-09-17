"""
Центральная конфигурация TradingAgents.

Все параметры читаются из файла .env (в корне репозитория). Ключи и модели можно
менять «на лету» через вкладку «Настройки» в веб-интерфейсе — изменения применяются
сразу и сохраняются обратно в .env.
"""
from typing import Optional, Dict, Any, List
from pydantic_settings import BaseSettings
import os

# Корень проекта и путь к .env (для чтения и сохранения настроек)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")
ENV_EXAMPLE_PATH = os.path.join(PROJECT_ROOT, ".env.example")


class Settings(BaseSettings):
    # ------------------------------------------------------------------ #
    # 1) Сервер
    # ------------------------------------------------------------------ #
    HOST: str = "0.0.0.0"
    PORT: int = 3003
    DEBUG: bool = True

    # ------------------------------------------------------------------ #
    # 2) Биржа Bybit (публичные данные; торговые ключи необязательны)
    # ------------------------------------------------------------------ #
    BYBIT_API_KEY: str = ""
    BYBIT_API_SECRET: str = ""
    BYBIT_TESTNET: bool = False
    BYBIT_BASE_URL: str = "https://api.bybit.com"

    # ------------------------------------------------------------------ #
    # 3) AI-модели (5 ролей). Все идут через один OpenAI-совместимый
    #    endpoint (по умолчанию https://api.justcode.lol/v1).
    #    Если у роли пустой *_API_KEY — берётся глобальный AI_API_KEY.
    #    Если и он пуст — DEMO-режим (детерминированные ответы).
    # ------------------------------------------------------------------ #
    AI_API_KEY: str = ""
    AI_BASE_URL: str = "https://api.justcode.lol/v1"
    AI_TIMEOUT: float = 120.0
    AI_TEMPERATURE: float = 0.3
    AI_MAX_TOKENS: int = 1200

    # Роль: ведущий стратег
    STRATEGIST_MODEL: str = "gpt-6-astra"
    STRATEGIST_API_KEY: str = ""
    STRATEGIST_BASE_URL: str = ""

    # Роль: аналитик моментума/тренда
    MOMENTUM_MODEL: str = "gpt-5.6-sol"
    MOMENTUM_API_KEY: str = ""
    MOMENTUM_BASE_URL: str = ""

    # Роль: контрариан / сентимент
    CONTRARIAN_MODEL: str = "grok-4.5"
    CONTRARIAN_API_KEY: str = ""
    CONTRARIAN_BASE_URL: str = ""

    # Роль: макро/фундаментальный контекст
    MACRO_MODEL: str = "gemini-2.5-pro"
    MACRO_API_KEY: str = ""
    MACRO_BASE_URL: str = ""

    # Роль: риск-менеджер / финальный арбитр
    RISK_MODEL: str = "claude-fable-5"
    RISK_API_KEY: str = ""
    RISK_BASE_URL: str = ""

    # ------------------------------------------------------------------ #
    # 4) Риск-менеджмент и правила дэйтрейдинга
    # ------------------------------------------------------------------ #
    MAX_TRADES_PER_DAY: int = 30
    MIN_TP_PCT: float = 2.0
    MAX_HOLD_HOURS: float = 4.0
    DEFAULT_SL_PCT: float = 1.0
    TRADING_FEE_PCT: float = 0.055
    INITIAL_CAPITAL: float = 10000.0
    POSITION_SIZE_PCT: float = 10.0
    MIN_CONFIDENCE: float = 0.55
    ALLOW_LONG: bool = True
    ALLOW_SHORT: bool = True

    # ------------------------------------------------------------------ #
    # 5) Рынок / демо-режим
    # ------------------------------------------------------------------ #
    DEFAULT_SYMBOLS: str = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,AVAXUSDT,LINKUSDT,ADAUSDT,LTCUSDT,LSKUSDT"
    DEFAULT_TIMEFRAME: str = "5m"
    SCAN_INTERVAL_SECONDS: int = 30
    AUTO_SCAN: bool = True
    DEMO_FALLBACK: bool = True
    KLINE_LIMIT: int = 500

    # Минимальный суточный объём торгов (USD) для включения монеты в список
    MIN_DAILY_VOLUME_USD: float = 1_000_000.0
    # Сколько топ-монет (по объёму) сканировать в live-режиме.
    # 0 = использовать DEFAULT_SYMBOLS. >0 = топ-N монет по суточному объёму.
    SCAN_MAX_SYMBOLS: int = 0

    class Config:
        env_file = ENV_PATH
        case_sensitive = True
        extra = "ignore"

    # ------------------------------------------------------------------ #
    # Хелперы
    # ------------------------------------------------------------------ #
    @property
    def symbols(self) -> list[str]:
        return [s.strip() for s in self.DEFAULT_SYMBOLS.split(",") if s.strip()]

    # Имена ролей в порядке, удобном для UI
    ROLE_KEYS: List[str] = ["strategist", "momentum", "contrarian", "macro", "risk"]

    def model_role(self, role: str) -> Dict[str, Any]:
        """{provider, model, api_key, base_url} для роли."""
        mapping = {
            "strategist": (self.STRATEGIST_MODEL, self.STRATEGIST_API_KEY, self.STRATEGIST_BASE_URL),
            "momentum": (self.MOMENTUM_MODEL, self.MOMENTUM_API_KEY, self.MOMENTUM_BASE_URL),
            "contrarian": (self.CONTRARIAN_MODEL, self.CONTRARIAN_API_KEY, self.CONTRARIAN_BASE_URL),
            "macro": (self.MACRO_MODEL, self.MACRO_API_KEY, self.MACRO_BASE_URL),
            "risk": (self.RISK_MODEL, self.RISK_API_KEY, self.RISK_BASE_URL),
        }
        model, key, base_url = mapping[role]
        key = key or self.AI_API_KEY
        base_url = base_url or self.AI_BASE_URL
        # По умолчанию всё идёт через OpenAI-совместимый протокол.
        # Native Anthropic включается только явным префиксом 'anthropic/'.
        provider = "anthropic" if model.startswith("anthropic/") else "openai"
        return {"provider": provider, "model": model, "api_key": key, "base_url": base_url}

    def is_demo(self) -> bool:
        return not any(self.model_role(r)["api_key"] for r in self.ROLE_KEYS)

    # ------------------------------------------------------------------ #
    # Публичная конфигурация (ключи замаскированы) — для UI
    # ------------------------------------------------------------------ #
    def public_config(self) -> dict:
        return {
            "demo_mode": self.is_demo(),
            "base_url": self.AI_BASE_URL,
            "global_api_key": _mask(self.AI_API_KEY),
            "roles": [
                {
                    "key": r,
                    "model": self.model_role(r)["model"],
                    "has_key": bool(self.model_role(r)["api_key"]),
                    "api_key": _mask(self.model_role(r)["api_key"]),
                    "base_url": self.model_role(r)["base_url"],
                }
                for r in self.ROLE_KEYS
            ],
            "bybit": {
                "api_key": _mask(self.BYBIT_API_KEY),
                "has_key": bool(self.BYBIT_API_KEY),
                "testnet": self.BYBIT_TESTNET,
                "base_url": self.BYBIT_BASE_URL,
            },
            "rules": {
                "max_trades_per_day": self.MAX_TRADES_PER_DAY,
                "min_tp_pct": self.MIN_TP_PCT,
                "max_hold_hours": self.MAX_HOLD_HOURS,
                "default_sl_pct": self.DEFAULT_SL_PCT,
                "min_confidence": self.MIN_CONFIDENCE,
                "position_size_pct": self.POSITION_SIZE_PCT,
                "allow_long": self.ALLOW_LONG,
                "allow_short": self.ALLOW_SHORT,
            },
            "market": {
                "symbols": self.symbols,
                "timeframe": self.DEFAULT_TIMEFRAME,
                "scan_interval_seconds": self.SCAN_INTERVAL_SECONDS,
                "auto_scan": self.AUTO_SCAN,
                "demo_fallback": self.DEMO_FALLBACK,
                "min_daily_volume_usd": self.MIN_DAILY_VOLUME_USD,
                "scan_max_symbols": self.SCAN_MAX_SYMBOLS,
            },
        }

    # ------------------------------------------------------------------ #
    # Применение изменений «на лету» + сохранение в .env
    # ------------------------------------------------------------------ #
    _ENV_FIELDS = {
        # ключ UI -> (атрибут Settings, имя переменной в .env)
        "ai_api_key": ("AI_API_KEY", "AI_API_KEY"),
        "ai_base_url": ("AI_BASE_URL", "AI_BASE_URL"),
        "bybit_api_key": ("BYBIT_API_KEY", "BYBIT_API_KEY"),
        "bybit_api_secret": ("BYBIT_API_SECRET", "BYBIT_API_SECRET"),
        "bybit_testnet": ("BYBIT_TESTNET", "BYBIT_TESTNET"),
        "strategist_model": ("STRATEGIST_MODEL", "STRATEGIST_MODEL"),
        "strategist_api_key": ("STRATEGIST_API_KEY", "STRATEGIST_API_KEY"),
        "momentum_model": ("MOMENTUM_MODEL", "MOMENTUM_MODEL"),
        "momentum_api_key": ("MOMENTUM_API_KEY", "MOMENTUM_API_KEY"),
        "contrarian_model": ("CONTRARIAN_MODEL", "CONTRARIAN_MODEL"),
        "contrarian_api_key": ("CONTRARIAN_API_KEY", "CONTRARIAN_API_KEY"),
        "macro_model": ("MACRO_MODEL", "MACRO_MODEL"),
        "macro_api_key": ("MACRO_API_KEY", "MACRO_API_KEY"),
        "risk_model": ("RISK_MODEL", "RISK_MODEL"),
        "risk_api_key": ("RISK_API_KEY", "RISK_API_KEY"),
        "max_trades_per_day": ("MAX_TRADES_PER_DAY", "MAX_TRADES_PER_DAY"),
        "min_tp_pct": ("MIN_TP_PCT", "MIN_TP_PCT"),
        "max_hold_hours": ("MAX_HOLD_HOURS", "MAX_HOLD_HOURS"),
        "default_sl_pct": ("DEFAULT_SL_PCT", "DEFAULT_SL_PCT"),
        "min_confidence": ("MIN_CONFIDENCE", "MIN_CONFIDENCE"),
        "position_size_pct": ("POSITION_SIZE_PCT", "POSITION_SIZE_PCT"),
        "allow_long": ("ALLOW_LONG", "ALLOW_LONG"),
        "allow_short": ("ALLOW_SHORT", "ALLOW_SHORT"),
        "scan_interval_seconds": ("SCAN_INTERVAL_SECONDS", "SCAN_INTERVAL_SECONDS"),
        "auto_scan": ("AUTO_SCAN", "AUTO_SCAN"),
        "demo_fallback": ("DEMO_FALLBACK", "DEMO_FALLBACK"),
        "min_daily_volume_usd": ("MIN_DAILY_VOLUME_USD", "MIN_DAILY_VOLUME_USD"),
        "scan_max_symbols": ("SCAN_MAX_SYMBOLS", "SCAN_MAX_SYMBOLS"),
    }

    def apply_update(self, updates: Dict[str, Any]) -> Dict[str, str]:
        """Применяет обновления к настройкам и сохраняет в .env.
        Возвращает {env_key: новое_значение} для записи в файл.
        """
        applied: Dict[str, str] = {}
        for ui_key, value in updates.items():
            if ui_key not in self._ENV_FIELDS:
                continue
            attr, env_key = self._ENV_FIELDS[ui_key]
            # Приводим типы
            current = getattr(self, attr)
            if isinstance(current, bool):
                value = str(value).lower() in ("1", "true", "yes", "on")
            elif isinstance(current, int):
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
    """Обновляет только указанные ключи в .env, сохраняя остальное и комментарии."""
    try:
        if os.path.exists(ENV_PATH):
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
        else:
            # нет .env — копируем шаблон
            if os.path.exists(ENV_EXAMPLE_PATH):
                with open(ENV_EXAMPLE_PATH, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            else:
                lines = []

        existing = set()
        out: List[str] = []
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
        print(f"[config] не удалось сохранить .env: {e}")


settings = Settings()
