"""Единый слой доступа к LLM-моделям (OpenRouter / OpenAI / Anthropic / xAI / Google)."""
from .client import LLMClient, LLMError, get_client

__all__ = ["LLMClient", "LLMError", "get_client"]
