from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    # Bybit API
    BYBIT_API_KEY: str = ""
    BYBIT_API_SECRET: str = ""
    BYBIT_TESTNET: bool = False
    
    # AI API (единый ключ для всех моделей)
    AI_API_KEY: str = ""
    AI_BASE_URL: str = "https://api.openrouter.ai/api/v1"
    
    # Server settings
    PORT: int = 3001
    DEBUG: bool = True
    
    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()
