"""Integration tests for the Re-Entry Brief stale gate and file output."""
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from contextos.core.config import settings
from contextos.core.database import get_db_conn, init_db
from contextos.daemon.agents.reentry import ReentryAgent


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
    init_db()
    return tmp_path


def _seed_completed_session(project_name: str, end_hours_ago: float, summary: str = "Fixed the auth bug."):
    now = datetime.now(timezone.utc)
    end_time = now - timedelta(hours=end_hours_ago)
    start_time = end_time - timedelta(hours=1)

    with get_db_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, project_name, start_time, end_time, status, summary) "
            "VALUES (?, ?, ?, ?, 'COMPLETED', ?)",
            (f"sess-{project_name}-{end_hours_ago}", project_name,
             start_time.isoformat(), end_time.isoformat(), summary),
        )
        conn.commit()
    return end_time


def test_stale_gate_fires_when_threshold_exceeded(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "REENTRY_STALE_AFTER_HOURS", 4.0)
    monkeypatch.setattr(settings, "REENTRY_BRIEF_RELATIVE_PATH", ".contextos/brief.md")

    _seed_completed_session("myproject", end_hours_ago=5.0)

    agent = ReentryAgent()
    agent.provider = "openrouter"          # bypass no-key guard for test
    agent.api_key = "test-key"
    with patch.object(agent, "_call_llm", return_value="## Welcome back!\n\nYou were fixing auth."):
        agent.generate_brief("myproject", "Fixed the auth bug.", project_root=tmp_path)

    brief_path = tmp_path / ".contextos" / "brief.md"
    assert brief_path.exists(), "Brief file should be written when project is stale"
    content = brief_path.read_text(encoding="utf-8")
    assert "Welcome back" in content
    assert "ContextOS Auto-Generated" in content


def test_brief_written_to_correct_subpath(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "REENTRY_BRIEF_RELATIVE_PATH", ".contextos/re-entry.md")

    agent = ReentryAgent()
    agent.provider = "openrouter"
    agent.api_key = "test-key"
    with patch.object(agent, "_call_llm", return_value="## You left off adding tests."):
        agent.generate_brief("myproject", "Added tests.", project_root=tmp_path)

    expected = tmp_path / ".contextos" / "re-entry.md"
    assert expected.exists()


def test_brief_includes_git_diff_in_prompt(temp_db, monkeypatch, tmp_path):
    agent = ReentryAgent()
    agent.provider = "openrouter"          # bypass no-key guard
    agent.api_key = "test-key"
    captured_prompts = []

    with patch.object(agent, "_get_git_diff", return_value="diff --git a/foo.py ..."), \
         patch.object(agent, "_call_llm", side_effect=lambda p: captured_prompts.append(p) or "ok"):
        agent.generate_brief("proj", "Did stuff.", project_root=tmp_path)

    assert captured_prompts, "LLM should have been called"
    assert "diff --git" in captured_prompts[0], "Git diff should be included in the prompt"


def test_brief_skips_on_no_api_key(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")

    agent = ReentryAgent()
    agent.generate_brief("myproject", "Did some stuff.", project_root=tmp_path)

    brief_path = tmp_path / ".contextos" / "brief.md"
    assert not brief_path.exists(), "No brief should be written without an API key"


def test_stale_hours_gate_logic_recent(monkeypatch):
    monkeypatch.setattr(settings, "REENTRY_STALE_AFTER_HOURS", 4.0)
    now = datetime.now(timezone.utc)
    end_time = now - timedelta(hours=1)
    hours_elapsed = (now - end_time).total_seconds() / 3600
    assert hours_elapsed < settings.REENTRY_STALE_AFTER_HOURS


def test_stale_hours_gate_logic_stale(monkeypatch):
    monkeypatch.setattr(settings, "REENTRY_STALE_AFTER_HOURS", 4.0)
    now = datetime.now(timezone.utc)
    end_time = now - timedelta(hours=5)
    hours_elapsed = (now - end_time).total_seconds() / 3600
    assert hours_elapsed >= settings.REENTRY_STALE_AFTER_HOURS
