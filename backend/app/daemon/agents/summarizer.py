import json
import logging
from datetime import datetime, timezone
from groq import Groq

from backend.app.core.config import settings
from backend.app.core.database import get_db_conn

logger = logging.getLogger("contextos.daemon.agents.summarizer")

class SummarizerAgent:
    def __init__(self):
        self.client = None
        if settings.GROQ_API_KEY and settings.GROQ_API_KEY != "your_groq_api_key_here":
            try:
                self.client = Groq(api_key=settings.GROQ_API_KEY)
                logger.info("SummarizerAgent initialized with Groq API key.")
            except Exception as e:
                logger.error(f"Failed to initialize Groq client: {e}")
        else:
            logger.warning("GROQ_API_KEY not configured. SummarizerAgent will operate in dummy mode.")

    def generate_mini_summary(self, project_name: str, session_id: str, since_time: datetime):
        """Fetches events since a given time, generates a summary via Groq, and stores it."""
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

        # Format events into a text block
        events_text = self._format_events_for_llm(events)
        
        prompt = (
            f"You are a developer assistant. Here are the raw events from the developer's last 5 minutes "
            f"working on the project '{project_name}':\n\n{events_text}\n\n"
            f"Write a concise, 1-3 sentence summary of what the developer just did or what problem they "
            f"encountered. Do not use conversational filler, just the facts."
        )

        summary_text = self._call_llm(prompt)
        
        if summary_text:
            # Store the mini summary as a new event!
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
            except Exception as e:
                logger.error(f"Failed to save mini-summary to database: {e}")

    def generate_final_summary(self, project_name: str, session_id: str):
        """Fetches all mini summaries for a session and compiles a final Dev Diary narrative."""
        try:
            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT payload FROM events WHERE project_name = ? AND source = 'agent' AND event_type = 'mini_summary'",
                    (project_name,)
                )
                rows = cursor.fetchall()
                
                # Filter rows to just those matching our session_id
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
            try:
                with get_db_conn() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT source, event_type, file_path, payload, timestamp FROM events WHERE project_name = ?",
                        (project_name,)
                    )
                    raw_events = cursor.fetchall()
            except Exception as e:
                logger.error(f"Failed to fetch raw events for fallback summary: {e}")
                return
                
            events_text = self._format_events_for_llm(raw_events)
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
            except Exception as e:
                logger.error(f"Failed to save final summary to sessions table: {e}")

    def _format_events_for_llm(self, events) -> str:
        """Formats raw database events into a readable string for the LLM."""
        formatted = []
        for row in events:
            source, event_type, file_path, payload_str, timestamp = row
            line = f"[{timestamp}] {source.upper()} | {event_type.upper()}"
            
            if file_path and file_path != "terminal" and file_path != "summarizer":
                line += f" -> {file_path}"
                
            if source == "terminal" and payload_str:
                try:
                    payload = json.loads(payload_str)
                    content = payload.get("content", "").strip()
                    if content:
                        # Truncate terminal output if it's too huge
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

    def _call_llm(self, prompt: str) -> str:
        """Helper to call Groq API."""
        if not self.client:
            logger.debug("GROQ API not configured. Returning dummy summary.")
            return "[Dummy Summary: The developer was working on the codebase.]"
            
        try:
            completion = self.client.chat.completions.create(
                model="llama-3.3-70b-versatile", # Updated free tier model
                messages=[
                    {"role": "system", "content": "You are ContextOS, an invisible background developer assistant."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=500
            )
            return completion.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Groq API call failed: {e}")
            return "[Error: Failed to generate summary]"
