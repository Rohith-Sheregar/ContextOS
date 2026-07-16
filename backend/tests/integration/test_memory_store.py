import json

import pytest

from backend.app.core.config import settings
from backend.app.core.database import get_db_conn, init_db, load_memory_backfill_items
from backend.app.core.memory_store import MemoryStore


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "contextos_test.db")
    init_db()
    return settings.DB_PATH


@pytest.fixture
def temp_memory_store(monkeypatch, tmp_path, temp_db):
    monkeypatch.setattr(settings, "CHROMA_DIR", tmp_path / "chroma")
    store = MemoryStore()
    assert store.enabled, "MemoryStore should be enabled with Phase 2 dependencies installed"

    fixtures = [
        {"session_id": "s1", "project": "webapp", "text": "Refactored the authentication middleware to use JWT tokens instead of session cookies"},
        {"session_id": "s2", "project": "webapp", "text": "Fixed a race condition in the event queue batching logic"},
        {"session_id": "s3", "project": "cli-tool", "text": "Added argument parsing for the deploy command"},
        {"session_id": "s4", "project": "webapp", "text": "Wrote unit tests for the user registration flow"},
        {"session_id": "s5", "project": "webapp", "text": "Debugged a memory leak in the WebSocket connection handler"},
    ]

    for idx, fixture in enumerate(fixtures):
        assert store.store_summary(
            text=fixture["text"],
            metadata={
                "project_name": fixture["project"],
                "session_id": fixture["session_id"],
                "timestamp": f"2024-01-0{idx + 1}T12:00:00Z",
                "summary_type": "final",
            },
        )

    return store


def test_query_retrieves_correct_session(temp_memory_store):
    result = temp_memory_store.query("authentication JWT session cookies")
    assert result, "Expected query results"
    assert result[0]["session_id"] == "s1"


def test_query_with_project_filter(temp_memory_store):
    result = temp_memory_store.query("command line arguments", project_name="cli-tool")
    assert result, "Expected query results"
    assert all(match["project_name"] == "cli-tool" for match in result)


def test_query_uses_distance_threshold(temp_memory_store):
    result = temp_memory_store.query("kubernetes cluster autoscaling", max_distance=0.01)
    assert result == []


def test_disabled_store_queues_summary_for_retry(temp_db):
    store = MemoryStore.__new__(MemoryStore)
    store.enabled = False
    store.collection = None

    stored = store.store_summary(
        "Queued summary",
        {
            "project_name": "demo",
            "session_id": "queued-session",
            "timestamp": "2024-01-01T00:00:00Z",
            "summary_type": "final",
        },
    )

    assert stored is False
    queued = load_memory_backfill_items()
    assert len(queued) == 1
    assert queued[0]["text"] == "Queued summary"
    assert queued[0]["metadata"]["session_id"] == "queued-session"


def test_get_session_context_is_project_scoped(temp_db):
    _seed_overlapping_sessions()
    store = MemoryStore.__new__(MemoryStore)

    context = store.get_session_context("session-demo")

    assert len(context) == 1
    assert context[0]["file_path"] == "demo.py"
    assert "other.py" not in {event["file_path"] for event in context}


def test_backfill_from_sqlite_indexes_existing_summaries(monkeypatch, tmp_path, temp_db):
    monkeypatch.setattr(settings, "CHROMA_DIR", tmp_path / "chroma_backfill")
    _seed_session_with_summary()

    store = MemoryStore()
    assert store.enabled, "MemoryStore should be enabled with Phase 2 dependencies installed"
    counts = store.backfill_from_sqlite()

    assert counts["sessions"] == 1
    result = store.query("sqlite retry handling", project_name="demo")
    assert result
    assert result[0]["session_id"] == "session-summary"


def _seed_overlapping_sessions():
    with get_db_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, project_name, start_time, end_time, status, summary) VALUES (?, ?, ?, ?, ?, ?)",
            ("session-demo", "demo", "2024-01-01T10:00:00+00:00", "2024-01-01T11:00:00+00:00", "COMPLETED", "Demo summary"),
        )
        conn.execute(
            "INSERT INTO sessions (session_id, project_name, start_time, end_time, status, summary) VALUES (?, ?, ?, ?, ?, ?)",
            ("session-other", "other", "2024-01-01T10:00:00+00:00", "2024-01-01T11:00:00+00:00", "COMPLETED", "Other summary"),
        )
        conn.execute(
            "INSERT INTO events (timestamp, source, event_type, file_path, project_name, payload) VALUES (?, ?, ?, ?, ?, ?)",
            ("2024-01-01T10:30:00+00:00", "filesystem", "modified", "demo.py", "demo", "{}"),
        )
        conn.execute(
            "INSERT INTO events (timestamp, source, event_type, file_path, project_name, payload) VALUES (?, ?, ?, ?, ?, ?)",
            ("2024-01-01T10:30:00+00:00", "filesystem", "modified", "other.py", "other", "{}"),
        )
        conn.commit()


def _seed_session_with_summary():
    with get_db_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, project_name, start_time, end_time, status, summary) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "session-summary",
                "demo",
                "2024-01-02T10:00:00+00:00",
                "2024-01-02T11:00:00+00:00",
                "COMPLETED",
                "Added sqlite retry handling and daemon health checks.",
            ),
        )
        conn.execute(
            "INSERT INTO events (timestamp, source, event_type, file_path, project_name, payload) VALUES (?, ?, ?, ?, ?, ?)",
            ("2024-01-02T10:30:00+00:00", "filesystem", "modified", "database.py", "demo", json.dumps({"kind": "edit"})),
        )
        conn.commit()
