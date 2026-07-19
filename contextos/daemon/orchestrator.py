import asyncio
import logging
import uuid
from datetime import datetime, timezone
from contextos.core.config import settings
from contextos.core.database import get_db_conn
from contextos.daemon.agents.summarizer import SummarizerAgent
from contextos.daemon.agents.reentry import ReentryAgent

logger = logging.getLogger("contextos.daemon.orchestrator")

class SessionOrchestrator:
    def __init__(self, loop: asyncio.AbstractEventLoop, memory_store=None):
        self.loop = loop
        self._running = False
        self._worker_task = None
        self.summarizer = SummarizerAgent()
        if memory_store:
            self.summarizer.memory_store = memory_store
        self.reentry_agent = ReentryAgent()

        # project_name -> {session_id, last_event_time, last_summary_time}
        self.active_sessions = {}

    def start(self):
        if self._running:
            return
        self._load_active_sessions()
        self._running = True
        self._worker_task = asyncio.create_task(self._orchestrator_loop())
        logger.info("SessionOrchestrator started.")

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
        for project_name in list(self.active_sessions.keys()):
            self._end_session(project_name)
        logger.info("SessionOrchestrator stopped.")

    def _load_active_sessions(self):
        try:
            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT session_id, project_name, start_time FROM sessions WHERE status = 'ACTIVE'")
                rows = cursor.fetchall()

                for row in rows:
                    session_id, project_name, start_time_str = row

                    cursor.execute("SELECT MAX(timestamp) FROM events WHERE project_name = ?", (project_name,))
                    max_ts_row = cursor.fetchone()

                    last_event_time = datetime.now(timezone.utc)
                    if max_ts_row and max_ts_row[0]:
                        try:
                            last_event_time = datetime.fromisoformat(max_ts_row[0])
                        except Exception:
                            pass

                    self.active_sessions[project_name] = {
                        "session_id": session_id,
                        "last_event_time": last_event_time,
                        "last_summary_time": datetime.now(timezone.utc),
                    }
                    logger.info(f"Loaded active session {session_id} for {project_name}")
        except Exception as e:
            logger.error(f"Failed to load active sessions: {e}")

    async def _orchestrator_loop(self):
        interval = 5.0
        while self._running:
            try:
                await asyncio.sleep(interval)
                await asyncio.to_thread(self._process_state_machine)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in Orchestrator loop: {e}")

    def _process_state_machine(self):
        now = datetime.now(timezone.utc)
        timeout_seconds = settings.SESSION_IDLE_TIMEOUT_SECONDS
        mini_summary_interval = settings.MINI_SUMMARY_INTERVAL_SECONDS

        latest_events = {}
        try:
            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT project_name, MAX(timestamp)
                    FROM events
                    WHERE timestamp > datetime('now', '-1 hour')
                    GROUP BY project_name
                """)
                for row in cursor.fetchall():
                    project_name, timestamp_str = row
                    if timestamp_str:
                        latest_events[project_name] = datetime.fromisoformat(timestamp_str).replace(tzinfo=timezone.utc)
        except Exception as e:
            logger.error(f"Failed to query latest events: {e}")
            return

        for project_name, last_event_time in latest_events.items():
            if project_name not in self.active_sessions:
                if (now - last_event_time).total_seconds() <= timeout_seconds:
                    self._start_session(project_name, last_event_time)
            else:
                self.active_sessions[project_name]["last_event_time"] = max(
                    self.active_sessions[project_name]["last_event_time"],
                    last_event_time,
                )

        for project_name in list(self.active_sessions.keys()):
            session_data = self.active_sessions[project_name]
            time_since_last_event = (now - session_data["last_event_time"]).total_seconds()

            if time_since_last_event > timeout_seconds:
                logger.info(f"Project '{project_name}' idle for > {timeout_seconds}s. Ending session.")
                self._end_session(project_name)
            else:
                time_since_last_summary = (now - session_data["last_summary_time"]).total_seconds()
                if time_since_last_summary >= mini_summary_interval:
                    self._trigger_mini_summary(project_name, session_data["session_id"])
                    session_data["last_summary_time"] = now

    def _start_session(self, project_name: str, start_time: datetime):
        session_id = str(uuid.uuid4())

        try:
            with get_db_conn() as conn:
                conn.execute(
                    "INSERT INTO sessions (session_id, project_name, start_time, status) VALUES (?, ?, ?, ?)",
                    (session_id, project_name, start_time.isoformat(), "ACTIVE"),
                )
                conn.commit()

            self.active_sessions[project_name] = {
                "session_id": session_id,
                "last_event_time": start_time,
                "last_summary_time": datetime.now(timezone.utc),
            }
            logger.info(f"Started new session {session_id} for '{project_name}'")

            # Check for a previous session and trigger Re-entry Brief (only if stale enough)
            now = datetime.now(timezone.utc)
            try:
                with get_db_conn() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT summary, end_time FROM sessions WHERE project_name = ? AND status = 'COMPLETED' ORDER BY end_time DESC LIMIT 1",
                        (project_name,),
                    )
                    row = cursor.fetchone()
                    if row and row[0] and row[1]:
                        last_summary = row[0]
                        last_end_time_str = row[1]
                        try:
                            last_end_time = datetime.fromisoformat(last_end_time_str)
                            if last_end_time.tzinfo is None:
                                last_end_time = last_end_time.replace(tzinfo=timezone.utc)
                        except ValueError:
                            last_end_time = None

                        hours_elapsed = (
                            (now - last_end_time).total_seconds() / 3600
                            if last_end_time else settings.REENTRY_STALE_AFTER_HOURS + 1
                        )

                        if (
                            hours_elapsed >= settings.REENTRY_STALE_AFTER_HOURS
                            and not last_summary.startswith("[Error:")
                        ):
                            logger.info(
                                f"Triggering Re-entry Brief for {project_name} "
                                f"(stale for {hours_elapsed:.1f}h)"
                            )
                            self.loop.run_in_executor(
                                None, self.reentry_agent.generate_brief, project_name, last_summary
                            )
                        else:
                            logger.debug(
                                f"Skipping Re-entry Brief for {project_name}: "
                                f"only {hours_elapsed:.1f}h since last session"
                            )
            except Exception as e:
                logger.error(f"Failed to fetch previous session for re-entry brief: {e}")

        except Exception as e:
            logger.error(f"Failed to start session for {project_name}: {e}")

    def _end_session(self, project_name: str):
        session_data = self.active_sessions.pop(project_name, None)
        if not session_data:
            return

        session_id = session_data["session_id"]
        end_time = session_data["last_event_time"].isoformat()

        try:
            with get_db_conn() as conn:
                conn.execute(
                    "UPDATE sessions SET end_time = ?, status = ? WHERE session_id = ?",
                    (end_time, "COMPLETED", session_id),
                )
                conn.commit()

            logger.info(f"Completed session {session_id} for '{project_name}'")
            self.loop.run_in_executor(
                None, self.summarizer.generate_final_summary, project_name, session_id
            )

        except Exception as e:
            logger.error(f"Failed to end session for {project_name}: {e}")

    def _trigger_mini_summary(self, project_name: str, session_id: str):
        session_data = self.active_sessions.get(project_name)
        if not session_data:
            return

        since_time = session_data["last_summary_time"]
        logger.info(f"Triggering mini-summary for {project_name} (Session {session_id})")

        self.loop.run_in_executor(
            None, self.summarizer.generate_mini_summary, project_name, session_id, since_time
        )
