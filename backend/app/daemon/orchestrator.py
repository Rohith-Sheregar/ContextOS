import asyncio
import logging
import uuid
from datetime import datetime, timezone
from backend.app.core.config import settings
from backend.app.core.database import get_db_conn
from backend.app.daemon.agents.summarizer import SummarizerAgent

logger = logging.getLogger("contextos.daemon.orchestrator")

class SessionOrchestrator:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self._running = False
        self._worker_task = None
        self.summarizer = SummarizerAgent()

        
        # In-memory tracking of active sessions
        # project_name -> {"session_id": str, "last_event_time": datetime, "last_summary_time": datetime}
        self.active_sessions = {}

    def start(self):
        """Starts the background polling orchestrator."""
        if self._running:
            return
        
        # Load any currently active sessions from the database
        self._load_active_sessions()
        
        self._running = True
        self._worker_task = asyncio.create_task(self._orchestrator_loop())
        logger.info("SessionOrchestrator started.")

    def stop(self):
        """Stops the orchestrator gracefully."""
        if not self._running:
            return
            
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
        
        # Force end all active sessions immediately since the daemon is stopping
        for project_name in list(self.active_sessions.keys()):
            self._end_session(project_name)
            
        logger.info("SessionOrchestrator stopped.")

    def _load_active_sessions(self):
        """Loads any 'ACTIVE' sessions from the DB on startup."""
        try:
            with get_db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT session_id, project_name, start_time FROM sessions WHERE status = 'ACTIVE'")
                rows = cursor.fetchall()
                
                for row in rows:
                    session_id, project_name, start_time_str = row
                    
                    # Get the most recent event time for this project to seed the last_event_time
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
                        "last_summary_time": datetime.now(timezone.utc)
                    }
                    logger.info(f"Loaded active session {session_id} for {project_name}")
        except Exception as e:
            logger.error(f"Failed to load active sessions: {e}")

    async def _orchestrator_loop(self):
        """Periodically polls the event stream and manages lifecycle states."""
        # Polling every 5 seconds is lightweight enough
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
        """Checks for new events and timeouts."""
        now = datetime.now(timezone.utc)
        timeout_seconds = settings.SESSION_IDLE_TIMEOUT_SECONDS
        mini_summary_interval = settings.MINI_SUMMARY_INTERVAL_SECONDS
        
        # 1. Query the latest event time for ALL projects
        latest_events = {}
        try:
            with get_db_conn() as conn:
                cursor = conn.cursor()
                # Get the most recent event per project that occurred in the last hour
                # We limit time window to avoid full table scans over huge dbs
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

        # 2. Start new sessions (IDLE -> ACTIVE)
        for project_name, last_event_time in latest_events.items():
            if project_name not in self.active_sessions:
                # We saw an event for a project that has no active session. 
                # ONLY start one if the event is RECENT (hasn't already timed out).
                if (now - last_event_time).total_seconds() <= timeout_seconds:
                    self._start_session(project_name, last_event_time)
            else:
                # Update the last_event_time
                self.active_sessions[project_name]["last_event_time"] = max(
                    self.active_sessions[project_name]["last_event_time"],
                    last_event_time
                )

        # 3. Handle Timeouts (ACTIVE -> IDLE) & Agent Triggers
        for project_name in list(self.active_sessions.keys()):
            session_data = self.active_sessions[project_name]
            time_since_last_event = (now - session_data["last_event_time"]).total_seconds()
            
            if time_since_last_event > timeout_seconds:
                # Session has timed out!
                logger.info(f"Project '{project_name}' idle for > {timeout_seconds}s. Ending session.")
                self._end_session(project_name)
            else:
                # Session is still active. Should we trigger a mini-summary?
                time_since_last_summary = (now - session_data["last_summary_time"]).total_seconds()
                if time_since_last_summary >= mini_summary_interval:
                    self._trigger_mini_summary(project_name, session_data["session_id"])
                    session_data["last_summary_time"] = now

    def _start_session(self, project_name: str, start_time: datetime):
        """Transitions a project from IDLE to ACTIVE."""
        session_id = str(uuid.uuid4())
        
        try:
            with get_db_conn() as conn:
                conn.execute(
                    "INSERT INTO sessions (session_id, project_name, start_time, status) VALUES (?, ?, ?, ?)",
                    (session_id, project_name, start_time.isoformat(), "ACTIVE")
                )
                conn.commit()
                
            self.active_sessions[project_name] = {
                "session_id": session_id,
                "last_event_time": start_time,
                "last_summary_time": datetime.now(timezone.utc)
            }
            logger.info(f"Started new session {session_id} for '{project_name}'")
        except Exception as e:
            logger.error(f"Failed to start session for {project_name}: {e}")

    def _end_session(self, project_name: str):
        """Transitions a project from ACTIVE to IDLE and generates final summary."""
        session_data = self.active_sessions.pop(project_name, None)
        if not session_data:
            return
            
        session_id = session_data["session_id"]
        end_time = session_data["last_event_time"].isoformat()
        
        try:
            with get_db_conn() as conn:
                conn.execute(
                    "UPDATE sessions SET end_time = ?, status = ? WHERE session_id = ?",
                    (end_time, "COMPLETED", session_id)
                )
                conn.commit()
            
            logger.info(f"Completed session {session_id} for '{project_name}'")
            
            # Trigger final agent summary
            logger.info(f"Compiling final session narrative for {project_name} (Session {session_id})")
            # We run this in a thread to avoid blocking the orchestrator loop
            asyncio.create_task(asyncio.to_thread(
                self.summarizer.generate_final_summary, project_name, session_id
            ))
            
        except Exception as e:
            logger.error(f"Failed to end session for {project_name}: {e}")

    def _trigger_mini_summary(self, project_name: str, session_id: str):
        """Triggers the 5-minute periodic mini summary."""
        # Get the time since the last summary
        session_data = self.active_sessions.get(project_name)
        if not session_data:
            return
            
        since_time = session_data["last_summary_time"]
        logger.info(f"Triggering mini-summary for {project_name} (Session {session_id})")
        
        # Run in a thread
        asyncio.create_task(asyncio.to_thread(
            self.summarizer.generate_mini_summary, project_name, session_id, since_time
        ))
