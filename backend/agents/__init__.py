"""Мультиагентная система: сканер, 5 AI-ролей, оркестратор, форвард-тест (live-сигналы)."""
from .orchestrator import Orchestrator, get_orchestrator

__all__ = ["Orchestrator", "get_orchestrator"]
