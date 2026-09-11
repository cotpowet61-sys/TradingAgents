"""
Центральная конфигурация TradingAgents.

Все параметры читаются из переменных окружения (файл .env).
Разделы:
  1) Сервер
  2) Биржа (Bybit) — только публичные REST/WS, без торговых ключей
  3) AI-модели — 5 ролей (можно указать свой провайдер/ID для каждой)
  4) Риск-менеджмент (глобальные правила дэйтрейдинга)
  5) Режим демо (синтетические данные, когда нет сети/ключей)
"""
from typing import Optional, Dict, Any
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ------------------------------------------------------------------ #
    # 1) Сервер
    # ------------------------------------------------------------------ #
    HOST: str = "0.0.0.0"
    PORT: int = 3001
    DEBUG: bool = True

    # ------------------------------------------------------------------ #
    # 2) Биржа Bybit (публичные данные, торговые ключи НЕ обязательны)
    # ------------------------------------------------------------------ #
    BYBIT_API_KEY: str = ""
    BYBIT_API_SECRET: str = ""
    BYBIT_TESTNET: bool = False
    BYBIT_BASE_URL: str = "https://api.bybit.com"

    # ------------------------------------------------------------------ #
    # 3) AI-модели (5 ролей).
    #    Каждая роль = { provider, model, api_key, base_url }.
    #    Если api_key пуст — используется глобальный AI_API_KEY/AI_BASE_URL.
    #    Если и он пуст — включается демо-режим (детерминированные ответы),
    #    чтобы платформа работала «из коробки».
    # ------------------------------------------------------------------ #
    AI_API_KEY: str = ""          # глобальный ключ (например OpenRouter)
    AI_BASE_URL: str = "https://openrouter.ai/api/v1"
    AI_TIMEOUT: float = 120.0
    AI_TEMPERATURE: float = 0.3
    AI_MAX_TOKENS: int = 1200

    # Роль: ведущий стратег (строит план, объединяет мнения)
    STRATEGIST_MODEL: str = "openai/gpt-6-astra"
    STRATEGIST_API_KEY: str = ""
    STRATEGIST_BASE_URL: str = ""

    # Роль: аналитик моментума/тренда (точки входа)
    MOMENTUM_MODEL: str = "openai/gpt-5.6-sol"
    MOMENTUM_API_KEY: str = ""
    MOMENTUM_BASE_URL: str = ""

    # Роль: контрариан / сентимент (адвокат дьявола)
    CONTRARIAN_MODEL: str = "x-ai/grok-4.5"
    CONTRARIAN_API_KEY: str = ""
    CONTRARIAN_BASE_URL: str = ""

    # Роль: макро/фундаментальный контекст
    MACRO_MODEL: str = "google/gemini-2.5-pro"
    MACRO_API_KEY: str = ""
    MACRO_BASE_URL: str = ""

    # Роль: риск-менеджер / финальный арбитр (осторожный)
    RISK_MODEL: str = "anthropic/claude-fable-5"
    RISK_API_KEY: str = ""
    RISK_BASE_URL: str = ""

    # ------------------------------------------------------------------ #
    # 4) Риск-менеджмент и правила дэйтрейдинга (глобальные дефолты)
    # ------------------------------------------------------------------ #
    MAX_TRADES_PER_DAY: int = 30        # максимум сделок в день
    MIN_TP_PCT: float = 2.0             # минимальный Take-Profit, % движения цены
    MAX_HOLD_HOURS: float = 4.0         # максимальное время удержания сделки, часов
    DEFAULT_SL_PCT: float = 1.0         # дефолтный Stop-Loss (2:1 RR к TP=2%)
    TRADING_FEE_PCT: float = 0.055      # комиссия на сторону (Bybit taker ~0.055%)
    INITIAL_CAPITAL: float = 10000.0    # стартовый капитал для бэктеста
    POSITION_SIZE_PCT: float = 10.0     # % капитала на сделку
    MIN_CONFIDENCE: float = 0.55        # порог уверенности консенсуса для сигнала
    ALLOW_LONG: bool = True
    ALLOW_SHORT: bool = True

    # ------------------------------------------------------------------ #
    # 5) Рынок / демо-режим
    # ------------------------------------------------------------------ #
    DEFAULT_SYMBOLS: str = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,AVAXUSDT,LINKUSDT,ADAUSDT,LTCUSDT"
    DEFAULT_TIMEFRAME: str = "5m"
    SCAN_INTERVAL_SECONDS: int = 30     # период авто-сканирования рынка
    AUTO_SCAN: bool = True              # автоматический фоновый скан сигналов
    DEMO_FALLBACK: bool = True          # генерировать синтетику при отсутствии сети
    KLINE_LIMIT: int = 500              # сколько свечей загружать по умолчанию

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"

    # ------------------------------------------------------------------ #
    # Хелперы
    # ------------------------------------------------------------------ #
    @property
    def symbols(self) -> list[str]:
        return [s.strip() for s in self.DEFAULT_SYMBOLS.split(",") if s.strip()]

    def model_role(self, role: str) -> Dict[str, Any]:
        """Возвращает {provider, model, api_key, base_url} для роли.

        Роли: strategist, momentum, contrarian, macro, risk.
        """
        mapping = {
            "strategist": (self.STRATEGIST_MODEL, self.STRATEGIST_API_KEY, self.STRATEGIST_BASE_URL),
            "momentum": (self.MOMENTUM_MODEL, self.MOMENTUM_API_KEY, self.MOMENTUM_BASE_URL),
            "contrarian": (self.CONTRARIAN_MODEL, self.CONTRARIAN_API_KEY, self.CONTRARIAN_BASE_URL),
            "macro": (self.MACRO_MODEL, self.MACRO_API_KEY, self.MACRO_BASE_URL),
            "risk": (self.RISK_MODEL, self.RISK_API_KEY, self.RISK_BASE_URL),
        }
        model, key, base_url = mapping[role]
        # Fallback на глобальный ключ/базу
        key = key or self.AI_API_KEY
        base_url = base_url or self.AI_BASE_URL
        # Определяем провайдера по префиксу модели
        provider = "openai"
        if model.startswith("anthropic/"):
            provider = "anthropic"
        elif model.startswith("google/") or model.startswith("gemini"):
            provider = "google"
        return {"provider": provider, "model": model, "api_key": key, "base_url": base_url}

    def is_demo(self) -> bool:
        """Демо-режим: нет ни одного настроенного ключа."""
        for role in ("strategist", "momentum", "contrarian", "macro", "risk"):
            if self.model_role(role)["api_key"]:
                return False
        return True


settings = Settings()
