"""
Tests for the shared LLMClient and all three LLM-consuming agents
(SummarizerAgent, QueryAgent, ReentryAgent).

Coverage:
  - Ollama success path (mocked HTTP)
  - OpenRouter success path (mocked HTTP)
  - Gemini success path (mocked HTTP)
  - Missing API key → graceful degradation (no raise, no file write)
  - Ollama HTTP failure → graceful degradation
  - OpenRouter HTTP failure → graceful degradation
  - Provider auto-selection logic
  - SummarizerAgent dummy-summary when no provider
  - ReentryAgent silent skip when no provider
"""
from __future__ import annotations

import io
import json
import urllib.error
from http.client import HTTPMessage
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from contextos.core.config import settings
from contextos.daemon.agents.llm import LLMClient, LLMResult
from contextos.daemon.agents.reentry import ReentryAgent
from contextos.daemon.agents.summarizer import SummarizerAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ollama_response(content: str) -> MagicMock:
    """Return a mock urlopen context manager that yields an Ollama /api/chat response."""
    body = json.dumps({"message": {"content": content}}).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _make_openrouter_response(content: str) -> MagicMock:
    """Return a mock urlopen context manager that yields an OpenRouter response."""
    body = json.dumps({
        "choices": [{"message": {"content": content}}]
    }).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _make_gemini_response(content: str) -> MagicMock:
    """Return a mock urlopen context manager that yields a Gemini generateContent response."""
    body = json.dumps({
        "candidates": [{"content": {"parts": [{"text": content}]}}]
    }).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _make_http_error(code: int = 503) -> urllib.error.HTTPError:
    headers = HTTPMessage()
    return urllib.error.HTTPError(
        url="http://mock",
        code=code,
        msg="Service Unavailable",
        hdrs=headers,
        fp=io.BytesIO(b"Service Unavailable"),
    )


# ---------------------------------------------------------------------------
# LLMClient — provider selection
# ---------------------------------------------------------------------------

class TestLLMClientProviderSelection:

    def test_ollama_selected_when_provider_is_ollama(self, monkeypatch):
        monkeypatch.setattr(settings, "LLM_PROVIDER", "ollama")
        client = LLMClient(role="query", system_prompt="sys", temperature=0.2, max_tokens=100)
        assert client.provider == "ollama"

    def test_openrouter_selected_when_key_present_and_auto(self, monkeypatch):
        monkeypatch.setattr(settings, "LLM_PROVIDER", "auto")
        monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "sk-test-key")
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
        client = LLMClient(role="query", system_prompt="sys", temperature=0.2, max_tokens=100)
        assert client.provider == "openrouter"

    def test_gemini_selected_when_only_gemini_key_and_allow_gemini(self, monkeypatch):
        monkeypatch.setattr(settings, "LLM_PROVIDER", "auto")
        monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "")
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "AIza-test")
        client = LLMClient(
            role="reentry", system_prompt="sys", temperature=0.5, max_tokens=800,
            allow_gemini=True,
        )
        assert client.provider == "gemini"

    def test_no_provider_when_no_keys_and_auto(self, monkeypatch):
        monkeypatch.setattr(settings, "LLM_PROVIDER", "auto")
        monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "")
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
        client = LLMClient(role="query", system_prompt="sys", temperature=0.2, max_tokens=100)
        assert client.provider is None

    def test_none_provider_disables_llm(self, monkeypatch):
        monkeypatch.setattr(settings, "LLM_PROVIDER", "none")
        client = LLMClient(role="query", system_prompt="sys", temperature=0.2, max_tokens=100)
        result = client.complete("hello")
        assert result.text == ""
        assert result.error is not None


# ---------------------------------------------------------------------------
# LLMClient — Ollama success and failure
# ---------------------------------------------------------------------------

class TestLLMClientOllama:

    def test_ollama_success(self, monkeypatch):
        monkeypatch.setattr(settings, "LLM_PROVIDER", "ollama")
        monkeypatch.setattr(settings, "OLLAMA_MODEL", "llama3.2")
        monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://localhost:11434")

        client = LLMClient(role="query", system_prompt="sys", temperature=0.2, max_tokens=100)

        with patch("urllib.request.urlopen", return_value=_make_ollama_response("Hello from Ollama")):
            result = client.complete("What was I working on?")

        assert result.text == "Hello from Ollama"
        assert result.provider == "ollama"
        assert result.error is None

    def test_ollama_http_error_returns_graceful_result(self, monkeypatch):
        monkeypatch.setattr(settings, "LLM_PROVIDER", "ollama")
        monkeypatch.setattr(settings, "OLLAMA_MODEL", "llama3.2")
        monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://localhost:11434")

        client = LLMClient(role="query", system_prompt="sys", temperature=0.2, max_tokens=100)

        with patch("urllib.request.urlopen", side_effect=_make_http_error(503)):
            result = client.complete("What was I working on?")

        assert result.text == ""
        assert result.provider == "ollama"
        assert "503" in (result.error or "")

    def test_ollama_connection_refused_returns_graceful_result(self, monkeypatch):
        monkeypatch.setattr(settings, "LLM_PROVIDER", "ollama")
        monkeypatch.setattr(settings, "OLLAMA_MODEL", "llama3.2")
        monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://localhost:11434")

        client = LLMClient(role="query", system_prompt="sys", temperature=0.2, max_tokens=100)

        with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError("Connection refused")):
            result = client.complete("What was I working on?")

        assert result.text == ""
        assert result.provider == "ollama"
        assert result.error is not None

    def test_ollama_role_specific_model_is_used(self, monkeypatch):
        monkeypatch.setattr(settings, "LLM_PROVIDER", "ollama")
        monkeypatch.setattr(settings, "OLLAMA_MODEL", "llama3.2")
        monkeypatch.setattr(settings, "OLLAMA_QUERY_MODEL", "mistral:7b")
        monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://localhost:11434")

        client = LLMClient(role="query", system_prompt="sys", temperature=0.2, max_tokens=100)

        captured_data = []

        def _fake_urlopen(req, timeout=None):
            captured_data.append(json.loads(req.data.decode()))
            return _make_ollama_response("ok")

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            client.complete("test")

        assert captured_data[0]["model"] == "mistral:7b"


# ---------------------------------------------------------------------------
# LLMClient — OpenRouter success and failure
# ---------------------------------------------------------------------------

class TestLLMClientOpenRouter:

    def test_openrouter_success(self, monkeypatch):
        monkeypatch.setattr(settings, "LLM_PROVIDER", "openrouter")
        monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "sk-test-key")
        monkeypatch.setattr(settings, "OPENROUTER_MODEL", "openai/gpt-4o-mini")

        client = LLMClient(role="summarizer", system_prompt="sys", temperature=0.3, max_tokens=500)

        with patch("urllib.request.urlopen", return_value=_make_openrouter_response("Summary text")):
            result = client.complete("Summarize this.")

        assert result.text == "Summary text"
        assert result.provider == "openrouter"

    def test_openrouter_http_error_is_graceful(self, monkeypatch):
        monkeypatch.setattr(settings, "LLM_PROVIDER", "openrouter")
        monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "sk-test-key")

        client = LLMClient(role="summarizer", system_prompt="sys", temperature=0.3, max_tokens=500)

        with patch("urllib.request.urlopen", side_effect=_make_http_error(429)):
            result = client.complete("Summarize this.")

        assert result.text == ""
        assert "429" in (result.error or "")

    def test_openrouter_missing_key_returns_error(self, monkeypatch):
        monkeypatch.setattr(settings, "LLM_PROVIDER", "openrouter")
        monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "")

        client = LLMClient(role="summarizer", system_prompt="sys", temperature=0.3, max_tokens=500)
        # provider will be None because key is missing for explicit openrouter
        result = client.complete("test")
        assert result.text == ""


# ---------------------------------------------------------------------------
# LLMClient — Gemini
# ---------------------------------------------------------------------------

class TestLLMClientGemini:

    def test_gemini_success(self, monkeypatch):
        monkeypatch.setattr(settings, "LLM_PROVIDER", "gemini")
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "AIza-test-key")
        monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-2.0-flash")

        client = LLMClient(
            role="reentry", system_prompt="sys", temperature=0.5, max_tokens=800,
            allow_gemini=True,
        )

        with patch("urllib.request.urlopen", return_value=_make_gemini_response("Gemini output")):
            result = client.complete("Generate brief.")

        assert result.text == "Gemini output"
        assert result.provider == "gemini"


# ---------------------------------------------------------------------------
# SummarizerAgent — LLM routing and dummy fallback
# ---------------------------------------------------------------------------

class TestSummarizerAgent:

    def test_summarizer_uses_llm_client(self, monkeypatch):
        """SummarizerAgent._call_llm delegates to self.llm.complete."""
        monkeypatch.setattr(settings, "LLM_PROVIDER", "ollama")
        monkeypatch.setattr(settings, "OLLAMA_MODEL", "llama3.2")

        agent = SummarizerAgent()
        with patch.object(agent.llm, "complete", return_value=LLMResult("LLM result", "ollama")) as mock_complete:
            result = agent._call_llm("some prompt")

        mock_complete.assert_called_once_with("some prompt")
        assert result == "LLM result"

    def test_summarizer_returns_empty_when_no_provider(self, monkeypatch):
        """When LLM is unavailable, _call_llm returns empty without raising."""
        monkeypatch.setattr(settings, "LLM_PROVIDER", "none")
        monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "")
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "")

        agent = SummarizerAgent()
        assert agent.provider is None

        result = agent._call_llm("some prompt")
        assert result == ""

    def test_summarizer_returns_empty_on_ollama_failure(self, monkeypatch):
        """A failed Ollama call should return empty, not raise."""
        monkeypatch.setattr(settings, "LLM_PROVIDER", "ollama")
        monkeypatch.setattr(settings, "OLLAMA_MODEL", "llama3.2")

        agent = SummarizerAgent()
        with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError("refused")):
            result = agent._call_llm("some prompt")

        assert result == ""

    def test_summarizer_uses_offline_fallback_on_empty_llm_result(self, monkeypatch, tmp_path):
        """If LLM returns nothing, an offline summary is generated from events and inserted."""
        monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test.db")
        monkeypatch.setattr(settings, "LLM_PROVIDER", "none")
        monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "")
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "")

        from contextos.core.database import get_db_conn, init_db
        init_db()

        # Seed an event so the agent actually tries to generate
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        since = now - timedelta(minutes=6)

        with get_db_conn() as conn:
            conn.execute(
                "INSERT INTO events (timestamp, project_name, source, event_type, file_path, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (now.isoformat(), "myproj", "filesystem", "modified", "main.py", "{}"),
            )
            conn.commit()

        agent = SummarizerAgent()
        # Force _call_llm to return empty to test the guard
        with patch.object(agent, "_call_llm", return_value=""):
            agent.generate_mini_summary("myproj", "sess-1", since)

        with get_db_conn() as conn:
            row = conn.execute(
                "SELECT payload FROM events WHERE event_type = 'mini_summary'"
            ).fetchone()
        
        assert row is not None, "Offline fallback should have inserted a mini_summary"
        payload = json.loads(row[0])
        assert "main.py" in payload["text"]


# ---------------------------------------------------------------------------
# ReentryAgent — LLM routing and skip-on-no-key
# ---------------------------------------------------------------------------

class TestReentryAgent:

    def test_reentry_uses_llm_client(self, monkeypatch):
        """ReentryAgent._call_llm delegates to self.llm.complete."""
        monkeypatch.setattr(settings, "LLM_PROVIDER", "ollama")
        monkeypatch.setattr(settings, "OLLAMA_MODEL", "llama3.2")
        monkeypatch.setattr(settings, "OLLAMA_REENTRY_MODEL", "")

        agent = ReentryAgent()
        with patch.object(agent.llm, "complete", return_value=LLMResult("Brief text", "ollama")) as mock_complete:
            result = agent._call_llm("some prompt")

        mock_complete.assert_called_once()
        assert result == "Brief text"

    def test_reentry_no_provider_does_not_write_file(self, monkeypatch, tmp_path):
        """Without an API key or Ollama, no brief file should be written."""
        monkeypatch.setattr(settings, "LLM_PROVIDER", "none")
        monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "")
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "")

        agent = ReentryAgent()
        assert agent.provider is None

        agent.generate_brief("myproject", "Fixed the auth bug.", project_root=tmp_path)

        brief_path = tmp_path / ".contextos" / "brief.md"
        assert not brief_path.exists(), "No brief should be written without a provider"

    def test_reentry_ollama_success_writes_file(self, monkeypatch, tmp_path):
        """Ollama success path should write the brief to disk."""
        monkeypatch.setattr(settings, "LLM_PROVIDER", "ollama")
        monkeypatch.setattr(settings, "OLLAMA_MODEL", "llama3.2")
        monkeypatch.setattr(settings, "OLLAMA_REENTRY_MODEL", "")
        monkeypatch.setattr(settings, "REENTRY_BRIEF_RELATIVE_PATH", ".contextos/brief.md")

        agent = ReentryAgent()

        with patch("urllib.request.urlopen", return_value=_make_ollama_response("## Welcome back!\n\nYou were debugging.")):
            with patch.object(agent, "_get_git_diff", return_value="(no changes)"):
                agent.generate_brief("myproject", "Fixed auth.", project_root=tmp_path)

        brief_path = tmp_path / ".contextos" / "brief.md"
        assert brief_path.exists()
        content = brief_path.read_text(encoding="utf-8")
        assert "Welcome back" in content
        assert "ContextOS Auto-Generated" in content

    def test_reentry_ollama_failure_does_not_write_file(self, monkeypatch, tmp_path):
        """When Ollama fails, ReentryAgent should degrade silently (no file, no exception)."""
        monkeypatch.setattr(settings, "LLM_PROVIDER", "ollama")
        monkeypatch.setattr(settings, "OLLAMA_MODEL", "llama3.2")
        monkeypatch.setattr(settings, "REENTRY_BRIEF_RELATIVE_PATH", ".contextos/brief.md")

        agent = ReentryAgent()

        with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError("refused")):
            with patch.object(agent, "_get_git_diff", return_value="(no changes)"):
                # Must not raise
                agent.generate_brief("myproject", "Fixed auth.", project_root=tmp_path)

        brief_path = tmp_path / ".contextos" / "brief.md"
        assert not brief_path.exists(), "Brief should not be written if LLM call fails"

    def test_reentry_openrouter_success_writes_file(self, monkeypatch, tmp_path):
        """OpenRouter success path should write the brief to disk."""
        monkeypatch.setattr(settings, "LLM_PROVIDER", "openrouter")
        monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "sk-test-key")
        monkeypatch.setattr(settings, "REENTRY_BRIEF_RELATIVE_PATH", ".contextos/brief.md")

        agent = ReentryAgent()

        with patch("urllib.request.urlopen", return_value=_make_openrouter_response("## Welcome back!\n\nYou fixed the bug.")):
            with patch.object(agent, "_get_git_diff", return_value="(no changes)"):
                agent.generate_brief("myproject", "Fixed auth.", project_root=tmp_path)

        brief_path = tmp_path / ".contextos" / "brief.md"
        assert brief_path.exists()
        assert "Welcome back" in brief_path.read_text(encoding="utf-8")
