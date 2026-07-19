from datetime import datetime, timedelta, timezone

from contextos.core.config import settings
from contextos.core.database import get_db_conn, init_db
from contextos.daemon.orchestrator import SessionOrchestrator


class ImmediateExecutorLoop:
    def run_in_executor(self, executor, func, *args):
        func(*args)


def test_process_state_machine_starts_session_for_recent_event(monkeypatch, tmp_path):
    _configure_temp_db(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "SESSION_IDLE_TIMEOUT_SECONDS", 60)

    now = datetime.now(timezone.utc)
    _insert_event("demo", now)

    orchestrator = SessionOrchestrator(ImmediateExecutorLoop())
    orchestrator._process_state_machine()

    assert "demo" in orchestrator.active_sessions
    with get_db_conn() as conn:
        row = conn.execute("SELECT status FROM sessions WHERE project_name = ?", ("demo",)).fetchone()
    assert row["status"] == "ACTIVE"


def test_process_state_machine_completes_idle_session(monkeypatch, tmp_path):
    _configure_temp_db(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "SESSION_IDLE_TIMEOUT_SECONDS", 60)
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")

    session_id = "session-1"
    old_time = datetime.now(timezone.utc) - timedelta(seconds=120)
    with get_db_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, project_name, start_time, status) VALUES (?, ?, ?, ?)",
            (session_id, "demo", old_time.isoformat(), "ACTIVE"),
        )
        conn.commit()

    orchestrator = SessionOrchestrator(ImmediateExecutorLoop())
    orchestrator.active_sessions["demo"] = {
        "session_id": session_id,
        "last_event_time": old_time,
        "last_summary_time": old_time,
    }

    orchestrator._process_state_machine()

    assert "demo" not in orchestrator.active_sessions
    with get_db_conn() as conn:
        row = conn.execute(
            "SELECT status, end_time FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    assert row["status"] == "COMPLETED"
    assert row["end_time"] == old_time.isoformat()


def test_process_state_machine_triggers_due_mini_summary(monkeypatch, tmp_path):
    _configure_temp_db(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "SESSION_IDLE_TIMEOUT_SECONDS", 60)
    monkeypatch.setattr(settings, "MINI_SUMMARY_INTERVAL_SECONDS", 30)

    now = datetime.now(timezone.utc)
    calls = []
    orchestrator = SessionOrchestrator(ImmediateExecutorLoop())
    orchestrator.active_sessions["demo"] = {
        "session_id": "session-2",
        "last_event_time": now,
        "last_summary_time": now - timedelta(seconds=31),
    }
    orchestrator._trigger_mini_summary = lambda project, session: calls.append((project, session))

    orchestrator._process_state_machine()

    assert calls == [("demo", "session-2")]


def _configure_temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "contextos_test.db")
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    init_db()


def _insert_event(project_name: str, timestamp: datetime):
    with get_db_conn() as conn:
        conn.execute(
            """
            INSERT INTO events (timestamp, project_name, source, event_type, file_path, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (timestamp.isoformat(), project_name, "filesystem", "modified", "demo.py", "{}"),
        )
        conn.commit()
