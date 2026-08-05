import json
import logging
import threading
from datetime import datetime, timezone

from contextos.core.config import settings
from contextos.core.database import get_db_conn
from contextos.daemon.agents.llm import LLMClient

logger = logging.getLogger("contextos.daemon.agents.summarizer")


class SummarizerAgent:
    def __init__(self):
        self.llm = LLMClient(
            role="summarizer",
            system_prompt="You are ContextOS, an invisible background developer assistant.",
            temperature=0.3,
            max_tokens=500,
        )
        self.provider = self.llm.provider
        self.api_key = self.llm.api_key
        self.memory_store = None
        self.cross_project_agent = None  # set by DaemonObserver after init
        if self.provider:
            logger.info("SummarizerAgent initialized with %s provider.", self.provider)
        else:
            logger.warning(
                "No LLM provider configured. Summaries will be generated from event data only."
            )

    def generate_mini_summary(self, project_name: str, session_id: str, since_time: datetime):
        """Fetches events since a given time, generates a summary via LLM, and stores it."""
        now = datetime.now(timezone.utc)

        try:
            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT source, event_type, file_path, payload, timestamp FROM events "
                    "WHERE project_name = ? AND timestamp >= ?",
                    (project_name, since_time.isoformat()),
                )
                events = cursor.fetchall()
        except Exception as e:
            logger.error("Failed to fetch events for mini-summary: %s", e)
            return

        if not events:
            logger.debug(
                "No events found for %s since %s. Skipping mini-summary.",
                project_name, since_time.isoformat(),
            )
            return

        events_text = self._format_events_for_llm(events)

        prompt = (
            f"You are a developer assistant. Here are the raw events from the developer's last 5 minutes "
            f"working on the project '{project_name}':\n\n{events_text}\n\n"
            f"Write a concise, 1-3 sentence summary of what the developer just did or what problem they "
            f"encountered. Do not use conversational filler, just the facts."
        )

        summary_text = self._call_llm(prompt)
        if not summary_text:
            summary_text = self._build_offline_summary(events)

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
                            json.dumps({"session_id": session_id, "text": summary_text}),
                        ),
                    )
                    conn.commit()
                logger.info("Generated mini-summary for %s.", project_name)

                if self.memory_store:
                    self.memory_store.store_summary(
                        text=summary_text,
                        metadata={
                            "project_name": project_name,
                            "session_id": session_id,
                            "timestamp": now.isoformat(),
                            "summary_type": "mini",
                            "file_paths_touched": self._extract_file_paths(events),
                        },
                    )
                    # Fire cross-project check after embedding succeeds
                    if self.cross_project_agent:
                        project_root = None
                        with get_db_conn() as conn:
                            cursor = conn.cursor()
                            cursor.execute(
                                "SELECT path FROM projects WHERE name = ?", (project_name,)
                            )
                            path_row = cursor.fetchone()
                            if path_row:
                                from pathlib import Path
                                project_root = Path(path_row[0])
                        if not project_root:
                            logger.debug(
                                "Project '%s' has no mapped path in DB. Skipping CrossProject check.",
                                project_name,
                            )
                        else:
                            t = threading.Thread(
                                target=self.cross_project_agent.check_for_similar_work,
                                args=(project_name, summary_text, project_root),
                                daemon=True,
                            )
                            t.start()
            except Exception as e:
                logger.error("Failed to save mini-summary to database: %s", e)

    def generate_final_summary(self, project_name: str, session_id: str):
        """Fetches all mini summaries for a session and compiles a final Dev Diary narrative."""
        session_events = self._fetch_session_events(project_name, session_id)

        try:
            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT payload FROM events WHERE project_name = ? AND source = 'agent' AND event_type = 'mini_summary'",
                    (project_name,),
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
            logger.error("Failed to fetch mini-summaries for final summary: %s", e)
            return

        if not session_summaries:
            logger.debug(
                "No mini-summaries found for session %s. Generating from raw events instead.",
                session_id,
            )
            if not session_events:
                logger.debug(
                    "No raw events found for fallback summary for session %s.", session_id
                )
                return

            events_text = self._format_events_for_llm(session_events)
            prompt = (
                f"You are a developer assistant writing a Dev Diary. Here is a chronological list of actions "
                f"the developer took during their latest session on '{project_name}':\n\n{events_text}\n\n"
                f"Compile this into a cohesive, readable paragraph summarizing the entire session. Focus on the "
                f"high-level goals achieved and any major roadblocks solved."
            )
            summary_text = self._call_llm(prompt)
            if not summary_text:
                summary_text = self._build_offline_summary(session_events)
        else:
            summaries_text = "\n".join([f"- {text}" for text in session_summaries])
            prompt = (
                f"You are a developer assistant writing a Dev Diary. Here is a chronological list of actions "
                f"the developer took during their latest session on '{project_name}':\n\n{summaries_text}\n\n"
                f"Compile this into a cohesive, readable paragraph summarizing the entire session. Focus on the "
                f"high-level goals achieved and any major roadblocks solved."
            )
            summary_text = self._call_llm(prompt)
            if not summary_text:
                summary_text = self._build_offline_summary(session_events)

        if summary_text:
            try:
                with get_db_conn() as conn:
                    conn.execute(
                        "UPDATE sessions SET summary = ? WHERE session_id = ?",
                        (summary_text, session_id),
                    )
                    conn.commit()
                logger.info("Compiled final session narrative for %s.", project_name)

                if self.memory_store:
                    self.memory_store.store_summary(
                        text=summary_text,
                        metadata={
                            "project_name": project_name,
                            "session_id": session_id,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "summary_type": "final",
                            "file_paths_touched": self._extract_file_paths(session_events),
                        },
                    )
            except Exception as e:
                logger.error("Failed to save final summary to sessions table: %s", e)

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
            logger.error("Failed to fetch raw events for session %s: %s", session_id, e)
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
        result = self.llm.complete(prompt)
        if result.text:
            return result.text

        if result.error:
            logger.debug(
                "SummarizerAgent LLM call via %s failed: %s",
                result.provider,
                result.error,
            )
        return ""

    def _build_offline_summary(self, events) -> str:
        """When no LLM is available, build a structured summary from the raw
        events.  Not as polished as an LLM summary but still useful for recall."""
        files = set()
        sources = set()
        commits = []
        for row in events:
            if isinstance(row, dict):
                src, etype, fpath, payload_str = (
                    row.get("source", ""), row.get("event_type", ""),
                    row.get("file_path", ""), row.get("payload"),
                )
            else:
                src, etype, fpath, payload_str, _ = row
            sources.add(src)
            if fpath and fpath not in ("terminal", "summarizer"):
                files.add(fpath)
            if src == "git" and etype == "commit" and payload_str:
                try:
                    msg = json.loads(payload_str).get("message", "")
                    if msg:
                        commits.append(msg)
                except Exception:
                    pass

        parts = []
        if files:
            parts.append(f"Touched {len(files)} file(s): {', '.join(sorted(files)[:8])}")
        if commits:
            parts.append(f"Committed: {'; '.join(commits[:3])}")
        activity = ", ".join(sorted(sources - {'agent'}))
        if activity:
            parts.append(f"Activity sources: {activity}")
        return ". ".join(parts) if parts else ""
