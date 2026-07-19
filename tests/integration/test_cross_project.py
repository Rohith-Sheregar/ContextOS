"""Integration tests for the CrossProjectAgent similarity detection."""
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from contextos.core.config import settings
from contextos.core.database import init_db
from contextos.core.memory_store import MemoryStore
from contextos.daemon.agents.cross_project import CrossProjectAgent


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    init_db()
    return tmp_path


@pytest.fixture
def seeded_store(monkeypatch, tmp_path, temp_db):
    monkeypatch.setattr(settings, "CHROMA_DIR", tmp_path / "chroma")
    monkeypatch.setattr(settings, "CROSS_PROJECT_MAX_DISTANCE", 1.5)
    monkeypatch.setattr(settings, "CROSS_PROJECT_TOP_K", 5)

    store = MemoryStore()
    assert store.enabled, "MemoryStore must be enabled with Phase 2 deps installed"

    store.store_summary(
        text="Refactored JWT authentication middleware to validate token expiry correctly",
        metadata={"project_name": "project-alpha", "session_id": "alpha-s1",
                  "timestamp": "2024-01-01T10:00:00Z", "summary_type": "final"},
    )
    store.store_summary(
        text="Ran Alembic database migrations for the new user preferences schema",
        metadata={"project_name": "project-beta", "session_id": "beta-s1",
                  "timestamp": "2024-01-02T10:00:00Z", "summary_type": "final"},
    )

    return store


def test_cross_project_match_fires_above_threshold(seeded_store, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CROSS_PROJECT_MATCH_RELATIVE_PATH", ".contextos/similar.md")

    agent = CrossProjectAgent(seeded_store)
    agent.check_for_similar_work(
        current_project="project-beta",
        current_summary="Implementing OAuth2 JWT token validation in the API gateway",
        project_root=tmp_path,
    )

    similar_path = tmp_path / ".contextos" / "similar.md"
    assert similar_path.exists(), "similar.md should be written when a cross-project match is found"

    content = similar_path.read_text(encoding="utf-8")
    assert "project-alpha" in content
    assert "ContextOS" in content


def test_cross_project_no_false_positive(seeded_store, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CROSS_PROJECT_MAX_DISTANCE", 0.01)
    monkeypatch.setattr(settings, "CROSS_PROJECT_MATCH_RELATIVE_PATH", ".contextos/similar.md")

    agent = CrossProjectAgent(seeded_store)
    agent.check_for_similar_work(
        current_project="project-beta",
        current_summary="Kubernetes autoscaler configuration for the canary deployment pipeline",
        project_root=tmp_path,
    )

    similar_path = tmp_path / ".contextos" / "similar.md"
    assert not similar_path.exists(), "similar.md should NOT be written when nothing matches"


def test_cross_project_ignores_same_project(seeded_store, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CROSS_PROJECT_MAX_DISTANCE", 1.5)
    monkeypatch.setattr(settings, "CROSS_PROJECT_MATCH_RELATIVE_PATH", ".contextos/similar.md")

    agent = CrossProjectAgent(seeded_store)
    agent.check_for_similar_work(
        current_project="project-alpha",
        current_summary="Refactored JWT authentication middleware to validate token expiry correctly",
        project_root=tmp_path,
    )

    similar_path = tmp_path / ".contextos" / "similar.md"
    if similar_path.exists():
        content = similar_path.read_text(encoding="utf-8")
        assert "### project-alpha" not in content, "Own-project matches must not appear as a source"


def test_cross_project_disabled_store_does_not_crash(tmp_path):
    disabled_store = MagicMock()
    disabled_store.enabled = False

    agent = CrossProjectAgent(disabled_store)
    agent.check_for_similar_work("my-project", "Some summary text", tmp_path)

    similar_path = tmp_path / ".contextos" / "similar.md"
    assert not similar_path.exists()
