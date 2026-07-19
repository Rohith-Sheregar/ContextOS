import pytest

from contextos.core.config import settings
from contextos.core.memory_store import MemoryStore
from contextos.daemon.agents.query import QueryAgent


@pytest.fixture
def temp_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(settings, "CHROMA_DIR", tmp_path / "chroma")
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "")

    from contextos.core.database import init_db
    init_db()

    store = MemoryStore()
    assert store.enabled, "MemoryStore should be enabled with Phase 2 dependencies installed"

    assert store.store_summary(
        text="Refactored the authentication middleware to use JWT tokens instead of session cookies",
        metadata={
            "project_name": "webapp",
            "session_id": "s1",
            "timestamp": "2024-01-01T12:00:00Z",
            "summary_type": "final",
        },
    )
    assert store.store_summary(
        text="Added kubernetes deployment manifests for the worker service",
        metadata={
            "project_name": "infra",
            "session_id": "s2",
            "timestamp": "2024-01-02T12:00:00Z",
            "summary_type": "final",
        },
    )

    return QueryAgent(store)


def test_ask_returns_cited_answer(temp_agent):
    result = temp_agent.ask("What did I do with JWT tokens?")

    assert "answer" in result
    assert "sources" in result
    assert len(result["sources"]) > 0
    assert "s1" in [source["session_id"] for source in result["sources"]]
    assert "webapp" in result["answer"]


def test_ask_respects_project_filter(temp_agent):
    result = temp_agent.ask("deployment manifests", project_name="infra")

    assert result["sources"]
    assert all(source["project_name"] == "infra" for source in result["sources"])


def test_ask_with_no_relevant_memories(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "empty.db")
    monkeypatch.setattr(settings, "CHROMA_DIR", tmp_path / "chroma_empty")
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "")

    from contextos.core.database import init_db
    init_db()

    store = MemoryStore()
    assert store.enabled
    agent = QueryAgent(store)

    result = agent.ask("How do I deploy to kubernetes?")

    assert result["answer"] == "No relevant memories found in ContextOS."
    assert len(result["sources"]) == 0
