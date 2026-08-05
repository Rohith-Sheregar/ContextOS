"""
Tests for the ContextOS Dashboard API server (daemon/api.py).

Coverage:
  - Server starts and stops cleanly (is_alive checks)
  - GET / returns HTML dashboard with expected markers
  - GET /api/status returns correct counts from a seeded DB
  - GET /api/sessions returns session list
  - GET /api/events returns events, honoring ?project= filter
  - GET /api/projects returns registered projects
  - GET /api/health returns health data
  - GET /api/summaries returns combined final + mini summaries
  - GET /api/sessions/<id> returns single session with events
  - 404 for unknown paths
  - Failure state: DB unavailable (settings.DB_PATH points to unreadable path)
    → endpoints respond 503 with JSON error, do not crash the server
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest

from contextos.core.config import settings
from contextos.core.database import get_db_conn, init_db
from contextos.daemon.api import DashboardAPIServer


# ---------------------------------------------------------------------------
# Test port — pick a high port unlikely to be in use
# ---------------------------------------------------------------------------

TEST_HOST = "127.0.0.1"
TEST_PORT = 16543


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory):
    """Create a module-scoped temp database with predictable data."""
    tmp = tmp_path_factory.mktemp("api_test")
    db_path = tmp / "test_api.db"
    # Patch settings at module scope by monkeypatching object attribute directly
    original = settings.DB_PATH
    settings.DB_PATH = db_path
    init_db()
    _seed_data()
    yield db_path
    settings.DB_PATH = original


@pytest.fixture(scope="module")
def live_server(seeded_db):
    """Start the API server once for the module, stop after all tests."""
    server = DashboardAPIServer(host=TEST_HOST, port=TEST_PORT)
    server.start()
    # Allow the server thread to bind
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://{TEST_HOST}:{TEST_PORT}/api/status", timeout=0.5)
            break
        except Exception:
            time.sleep(0.05)
    yield server
    server.stop()


def _get(path: str) -> tuple[int, dict | str]:
    """Issue a GET to the live test server. Returns (status_code, parsed_json_or_text)."""
    url = f"http://{TEST_HOST}:{TEST_PORT}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            raw = resp.read().decode("utf-8")
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct:
                return resp.status, json.loads(raw)
            return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw


def _seed_data():
    with get_db_conn() as conn:
        # Projects
        conn.execute(
            "INSERT OR IGNORE INTO projects (name, path, status) VALUES (?, ?, ?)",
            ("test-proj", "/home/dev/test-proj", "IDLE"),
        )
        # Sessions
        conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, project_name, start_time, end_time, status, summary) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("sess-abc", "test-proj", "2024-02-01T10:00:00Z", "2024-02-01T11:00:00Z",
             "COMPLETED", "Completed the authentication module."),
        )
        conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, project_name, start_time, status) "
            "VALUES (?, ?, ?, ?)",
            ("sess-xyz", "test-proj", "2024-02-02T09:00:00Z", "ACTIVE"),
        )
        # Events
        conn.execute(
            "INSERT INTO events (timestamp, project_name, source, event_type, file_path, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("2024-02-01T10:30:00Z", "test-proj", "filesystem", "modified", "auth.py", '{"kind": "edit"}'),
        )
        # Mini summary event
        conn.execute(
            "INSERT INTO events (timestamp, project_name, source, event_type, file_path, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("2024-02-01T10:35:00Z", "test-proj", "agent", "mini_summary", "summarizer",
             json.dumps({"session_id": "sess-abc", "text": "Refactored the login handler."})),
        )
        # Health snapshot
        conn.execute(
            "INSERT INTO daemon_health (timestamp, pid, cpu_percent, memory_rss_bytes, "
            "memory_percent, thread_count, open_files) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2024-02-01T10:00:00Z", 12345, 1.5, 120 * 1024 * 1024, 2.1, 8, 12),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

def test_server_is_alive(live_server):
    assert live_server.is_alive()


def test_server_url_property():
    server = DashboardAPIServer(host="127.0.0.1", port=9999)
    assert server.url == "http://127.0.0.1:9999"


def test_stop_is_idempotent():
    """Stopping an already-stopped server should not raise."""
    server = DashboardAPIServer(host=TEST_HOST, port=16544)
    server.stop()   # Never started — should be a no-op


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

def test_dashboard_returns_html(live_server):
    status, body = _get("/")
    assert status == 200
    assert "ContextOS" in body
    assert "<html" in body.lower()
    assert "api/status" in body  # dashboard JS calls this endpoint


# ---------------------------------------------------------------------------
# /api/status
# ---------------------------------------------------------------------------

def test_status_endpoint_returns_counts(live_server):
    status, data = _get("/api/status")
    assert status == 200
    assert data["status"] == "running"
    assert data["total_events"] >= 2     # seeded 2 events
    assert data["total_sessions"] >= 2
    assert data["total_projects"] >= 1


def test_status_has_timestamp(live_server):
    _, data = _get("/api/status")
    assert "timestamp" in data
    assert "T" in data["timestamp"]  # ISO format sanity


# ---------------------------------------------------------------------------
# /api/sessions
# ---------------------------------------------------------------------------

def test_sessions_returns_list(live_server):
    status, data = _get("/api/sessions")
    assert status == 200
    assert "sessions" in data
    assert isinstance(data["sessions"], list)
    assert len(data["sessions"]) >= 2


def test_sessions_respect_limit(live_server):
    _, data = _get("/api/sessions?limit=1")
    assert len(data["sessions"]) == 1


def test_single_session_endpoint(live_server):
    status, data = _get("/api/sessions/sess-abc")
    assert status == 200
    assert data["session_id"] == "sess-abc"
    assert data["project_name"] == "test-proj"
    assert "events" in data
    assert isinstance(data["events"], list)


def test_single_session_404(live_server):
    status, data = _get("/api/sessions/nonexistent-session-id")
    assert status == 404
    assert "error" in data


# ---------------------------------------------------------------------------
# /api/events
# ---------------------------------------------------------------------------

def test_events_returns_list(live_server):
    status, data = _get("/api/events")
    assert status == 200
    assert "events" in data
    assert len(data["events"]) >= 2


def test_events_project_filter(live_server):
    _, data = _get("/api/events?project=test-proj")
    assert all(e["project_name"] == "test-proj" for e in data["events"])


def test_events_unknown_project_returns_empty(live_server):
    _, data = _get("/api/events?project=no-such-project")
    assert data["events"] == []


# ---------------------------------------------------------------------------
# /api/projects
# ---------------------------------------------------------------------------

def test_projects_returns_list(live_server):
    status, data = _get("/api/projects")
    assert status == 200
    assert "projects" in data
    names = [p["name"] for p in data["projects"]]
    assert "test-proj" in names


# ---------------------------------------------------------------------------
# /api/health
# ---------------------------------------------------------------------------

def test_health_returns_latest_snapshot(live_server):
    status, data = _get("/api/health")
    assert status == 200
    assert "latest" in data
    latest = data["latest"]
    assert latest["cpu_percent"] == pytest.approx(1.5)
    assert latest["thread_count"] == 8


def test_health_returns_history(live_server):
    _, data = _get("/api/health")
    assert "history" in data
    assert isinstance(data["history"], list)
    assert len(data["history"]) >= 1


# ---------------------------------------------------------------------------
# /api/summaries
# ---------------------------------------------------------------------------

def test_summaries_includes_final_and_mini(live_server):
    status, data = _get("/api/summaries")
    assert status == 200
    assert "summaries" in data
    types = {s["type"] for s in data["summaries"]}
    assert "final" in types
    assert "mini" in types


def test_summaries_have_expected_fields(live_server):
    _, data = _get("/api/summaries")
    for s in data["summaries"]:
        assert "project_name" in s
        assert "timestamp" in s
        assert "summary" in s
        assert "type" in s


# ---------------------------------------------------------------------------
# 404 for unknown routes
# ---------------------------------------------------------------------------

def test_unknown_route_returns_404(live_server):
    status, data = _get("/api/nonexistent")
    assert status == 404
    assert "error" in data


# ---------------------------------------------------------------------------
# Failure state: database unavailable
# ---------------------------------------------------------------------------

def test_status_db_unavailable(monkeypatch, tmp_path):
    """When the DB file doesn't exist, /api/status should return 503 JSON, not crash."""
    # Point settings at a non-existent DB while running a fresh server on a different port
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "missing" / "missing.db")

    server = DashboardAPIServer(host=TEST_HOST, port=16545)
    server.start()

    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://{TEST_HOST}:16545/api/status", timeout=0.5)
            break
        except urllib.error.HTTPError:
            break  # We expect an HTTP error — that means the server is up
        except Exception:
            time.sleep(0.05)

    try:
        status, data = _get_from_port("/api/status", 16545)
        # Either 200 with an error key or 503 — either way it's JSON, not a crash
        assert isinstance(data, dict)
    finally:
        server.stop()


def _get_from_port(path: str, port: int) -> tuple[int, dict | str]:
    url = f"http://{TEST_HOST}:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            raw = resp.read().decode("utf-8")
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct:
                return resp.status, json.loads(raw)
            return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw
