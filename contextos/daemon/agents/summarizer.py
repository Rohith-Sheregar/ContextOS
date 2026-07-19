import json
import logging
import threading
import urllib.request
import urllib.error
from datetime import datetime, timezone

from contextos.core.config import settings
from contextos.core.database import get_db_conn

logger = logging.getLogger("contextos.daemon.agents.summarizer")

class SummarizerAgent:
    def __init__(self):
        self.api_key = settings.OPENROUTER_API_KEY
        self.memory_store = None
        self.cross_project_agent = None  # Injected by DaemonObserver after Phase 3 init
        if self.api_key and self.api_key != "PASTE_YOUR_OPENROUTER_KEY_HERE":
            logger.info("SummarizerAgent initialized with OpenRouter API key.")
        else:
            self.api_key = None
            logger.warning("OPENROUTER_API_KEY not configured. SummarizerAgent will operate in dummy mode.")

    def generate_mini_summary(self, project_name: str, session_id: str, since_time: datetime):
        """Fetches events since a given time, generates a summary via OpenRouter, and stores it."""
        now = datetime.now(timezone.utc)

        try:
            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT source, event_type, file_path, payload, timestamp FROM events WHERE project_name = ? AND timestamp >= ?",
                    (project_name, since_time.isoformat())
                )
                events = cursor.fetchall()
        except Exception as e:
            logger.error(f"Failed to fetch events for mini-summary: {e}")
            return

        if not events:
            logger.debug(f"No events found for {project_name} since {since_time.isoformat()}. Skipping mini-summary.")
            return

        events_text = self._format_events_for_llm(events)

        prompt = (
            f"You are a developer assistant. Here are the raw events from the developer's last 5 minutes "
            f"working on the project '{project_name}':\n\n{events_text}\n\n"
            f"Write a concise, 1-3 sentence summary of what the developer just did or what problem they "
            f"encountered. Do not use conversational filler, just the facts."
        )

        summary_text = self._call_llm(prompt)

        if summary_text:
            try:
                with get_db_conn() as conn:
                    conn.execute(
                        """
                        INSERT INTO events (timestamp, project_name, source, event_type, file_path, payload)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            now.isoformat(),
                            project_name,
                            "agent",
                            "mini_summary",
                            "summarizer",
                            json.dumps({"session_id": session_id, "text": summary_text})
                        )
                    )
                    conn.commit()
                logger.info(f"Generated mini-summary for {project_name}.")

                if self.memory_store:
                    self.memory_store.store_summary(
                        text=summary_text,
                        metadata={
                            "project_name": project_name,
                            "session_id": session_id,
                            "timestamp": now.isoformat(),
                            "summary_type": "mini",
                            "file_paths_touched": self._extract_file_paths(events),
                        }
                    )
                    # Fire cross-project check after embedding succeeds
                    if self.cross_project_agent:
                        project_root = None
                        with get_db_conn() as conn:
                            cursor = conn.cursor()
                            cursor.execute("SELECT path FROM projects WHERE name = ?", (project_name,))
                            path_row = cursor.fetchone()
                            if path_row:
                                from pathlib import Path
                                project_root = Path(path_row[0])
                        if not project_root:
                            logger.debug(f"Project '{project_name}' has no mapped path in DB. Skipping CrossProject check.")
                        else:
                            t = threading.Thread(
                                target=self.cross_project_agent.check_for_similar_work,
                                args=(project_name, summary_text, project_root),
                                daemon=True,
                            )
                            t.start()
            except Exception as e:
                logger.error(f"Failed to save mini-summary to database: {e}")

    def generate_final_summary(self, project_name: str, session_id: str):
        """Fetches all mini summaries for a session and compiles a final Dev Diary narrative."""
        session_events = self._fetch_session_events(project_name, session_id)

        try:
            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT payload FROM events WHERE project_name = ? AND source = 'agent' AND event_type = 'mini_summary'",
                    (project_name,)
                )
                rows = cursor.fetchall()

                session_summaries = []
                for row in rows:
                    if row[0]:
                        try:
                            payload = json.loads(row[0])
                            if payload.get("session_id") == session_id:
                                session_summaries.append(payload.get("text", ""))
                        except Exception:
                            pass

        except Exception as e:
            logger.error(f"Failed to fetch mini-summaries for final summary: {e}")
            return

        if not session_summaries:
            logger.debug(f"No mini-summaries found for session {session_id}. Generating from raw events instead.")
            if not session_events:
                logger.debug(f"No raw events found for fallback summary for session {session_id}.")
                return

            events_text = self._format_events_for_llm(session_events)
            prompt = (
                f"You are a developer assistant writing a Dev Diary. Here is a chronological list of actions "
                f"the developer took during their latest session on '{project_name}':\n\n{events_text}\n\n"
                f"Compile this into a cohesive, readable paragraph summarizing the entire session. Focus on the "
                f"high-level goals achieved and any major roadblocks solved."
            )
            summary_text = self._call_llm(prompt)
        else:
            summaries_text = "\n".join([f"- {text}" for text in session_summaries])
            prompt = (
                f"You are a developer assistant writing a Dev Diary. Here is a chronological list of actions "
                f"the developer took during their latest session on '{project_name}':\n\n{summaries_text}\n\n"
                f"Compile this into a cohesive, readable paragraph summarizing the entire session. Focus on the "
                f"high-level goals achieved and any major roadblocks solved."
            )
            summary_text = self._call_llm(prompt)

        if summary_text:
            try:
                with get_db_conn() as conn:
                    conn.execute(
                        "UPDATE sessions SET summary = ? WHERE session_id = ?",
                        (summary_text, session_id)
                    )
                    conn.commit()
                logger.info(f"Compiled final session narrative for {project_name}.")

                if self.memory_store:
                    self.memory_store.store_summary(
                        text=summary_text,
                        metadata={
                            "project_name": project_name,
                            "session_id": session_id,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "summary_type": "final",
                            "file_paths_touched": self._extract_file_paths(session_events),
                        }
                    )
            except Exception as e:
                logger.error(f"Failed to save final summary to sessions table: {e}")

    def _fetch_session_events(self, project_name: str, session_id: str):
        try:
            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT start_time, end_time FROM sessions WHERE session_id = ? AND project_name = ?",
                    (session_id, project_name),
                )
                row = cursor.fetchone()
                if not row:
                    return []

                start_time, end_time = row
                query = """
                    SELECT source, event_type, file_path, payload, timestamp
                    FROM events
                    WHERE project_name = ? AND timestamp >= ?
                """
                params = [project_name, start_time]
                if end_time:
                    query += " AND timestamp <= ?"
                    params.append(end_time)
                query += " ORDER BY timestamp ASC"
                cursor.execute(query, params)
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"Failed to fetch raw events for session {session_id}: {e}")
            return []

    def _format_events_for_llm(self, events) -> str:
        formatted = []
        for row in events:
            source, event_type, file_path, payload_str, timestamp = row
            line = f"[{timestamp}] {source.upper()} | {event_type.upper()}"

            if file_path and file_path not in ("terminal", "summarizer"):
                line += f" -> {file_path}"

            if source == "terminal" and payload_str:
                try:
                    payload = json.loads(payload_str)
                    content = payload.get("content", "").strip()
                    if content:
                        if len(content) > 1000:
                            content = content[:1000] + "... (truncated)"
                        line += f"\nTerminal Output:\n{content}"
                except Exception:
                    pass
            elif source == "git" and payload_str:
                try:
                    payload = json.loads(payload_str)
                    if "message" in payload:
                        line += f" | Message: '{payload['message']}'"
                except Exception:
                    pass

            formatted.append(line)

        return "\n".join(formatted)

    def _extract_file_paths(self, events) -> list[str]:
        paths = set()
        for row in events:
            if isinstance(row, dict):
                file_path = row.get("file_path")
            else:
                source, _, file_path, _, _ = row

            if file_path and file_path not in ("terminal", "summarizer"):
                paths.add(file_path)
        return list(paths)

    def _call_llm(self, prompt: str) -> str:
        if not self.api_key:
            logger.debug("OpenRouter API not configured. Returning dummy summary.")
            return "[Dummy Summary: The developer was working on the codebase.]"

        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:8000",
            "X-Title": "ContextOS Daemon",
        }
        data = {
            "model": "openai/gpt-oss-20b:free",
            "messages": [
                {"role": "system", "content": "You are ContextOS, an invisible background developer assistant."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 500,
        }

        try:
            req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
                return result["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            logger.error(f"OpenRouter API call failed: HTTP {e.code} - {body[:200]}")
            return "[Error: Failed to generate summary]"
        except Exception as e:
            logger.error(f"OpenRouter API call failed: {e}")
            return "[Error: Failed to generate summary]"
