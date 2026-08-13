"""Root-level tests conftest — ensures the project root is on sys.path."""
import sys
from pathlib import Path

import pytest

from contextos.core.config import settings

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def disable_llm_cache_for_tests(monkeypatch):
    monkeypatch.setattr(settings, "LLM_CACHE_ENABLED", False)
