import os
import asyncio
import logging
from contextos.core.config import settings
from contextos.core.database import init_db
from contextos.daemon.queue import EventQueue
from contextos.daemon.watchers.filesystem import FilesystemWatcher
from contextos.daemon.watchers.terminal import TerminalWatcher
from contextos.daemon.watchers.git import GitWatcher
from contextos.daemon.watchers.clipboard import ClipboardWatcher
from contextos.daemon.orchestrator import SessionOrchestrator
from contextos.daemon.health import HealthMonitor
from contextos.core.memory_store import MemoryStore
from contextos.daemon.agents.cross_project import CrossProjectAgent
from contextos.daemon.api import DashboardAPIServer

logger = logging.getLogger("contextos.daemon.observer")

class DaemonObserver:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.queue = EventQueue()
        self.fs_watcher = FilesystemWatcher(self.loop, self.queue)
        self.term_watcher = TerminalWatcher(self.loop, self.queue)
        self.git_watcher = GitWatcher(self.loop, self.queue)
        self.clipboard_watcher = ClipboardWatcher(self.loop, self.queue)
        self.memory_store = MemoryStore()
        self.cross_project_agent = CrossProjectAgent(self.memory_store)
        self.orchestrator = SessionOrchestrator(self.loop, self.memory_store)
        # Inject cross-project checks into the summarizer
        self.orchestrator.summarizer.cross_project_agent = self.cross_project_agent
        self.health_monitor = HealthMonitor()
        self.api_server = DashboardAPIServer(
            host=settings.DASHBOARD_HOST,
            port=settings.DASHBOARD_PORT,
        )
        self._supervisor_task = None
        self._watch_paths = []
        self.is_running = False

    async def start(self):
        if self.is_running:
            return

        logger.info("Starting ContextOS Daemon Observer...")

        await asyncio.to_thread(init_db)
        self.queue.start()

        watch_paths = list(settings.WATCH_PATHS)
        if not watch_paths:
            # Default: watch the current working directory
            watch_paths = [str(os.getcwd())]

        self._watch_paths = watch_paths
        logger.info(f"Paths to monitor: {watch_paths}")

        from contextos.core.database import get_db_conn, run_with_db_retry
        def _upsert_projects():
            with get_db_conn() as conn:
                for p in self._watch_paths:
                    p_abs = os.path.abspath(p)
                    p_name = os.path.basename(p_abs)
                    conn.execute(
                        "INSERT INTO projects (name, path) VALUES (?, ?) "
                        "ON CONFLICT(name) DO UPDATE SET path=excluded.path",
                        (p_name, p_abs)
                    )
                conn.commit()
        run_with_db_retry("upsert_projects", _upsert_projects)

        self._start_watchers()
        self.orchestrator.start()
        self.health_monitor.start()

        if settings.DASHBOARD_ENABLED:
            self.api_server.start()

        self.is_running = True
        self._supervisor_task = asyncio.create_task(self._supervise_watchers_loop())
        logger.info("ContextOS Daemon Observer is running.")

    async def stop(self):
        if not self.is_running:
            return

        logger.info("Stopping ContextOS Daemon Observer...")

        if self._supervisor_task:
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except asyncio.CancelledError:
                pass
            self._supervisor_task = None

        self.orchestrator.stop()
        self.api_server.stop()
        self.fs_watcher.stop()
        self.git_watcher.stop()
        self.term_watcher.stop()
        self.clipboard_watcher.stop()
        await self.health_monitor.stop()
        await self.queue.stop()

        self.is_running = False
        logger.info("ContextOS Daemon Observer stopped.")

    def _start_watchers(self):
        self._safe_start_watcher("filesystem", lambda: self.fs_watcher.start(self._watch_paths))
        self._safe_start_watcher("git", lambda: self.git_watcher.start(self._watch_paths))
        self._safe_start_watcher("terminal", self.term_watcher.start)
        self._safe_start_watcher("clipboard", lambda: self.clipboard_watcher.start(self._watch_paths))

    def _safe_start_watcher(self, name: str, start_func):
        try:
            start_func()
        except Exception:
            logger.exception("Failed to start %s watcher; supervisor will retry.", name)

    async def _supervise_watchers_loop(self):
        while self.is_running:
            try:
                await asyncio.sleep(settings.WATCHER_HEALTH_CHECK_INTERVAL)
                self._restart_dead_watchers()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Watcher supervisor failed; continuing.")

    def _restart_dead_watchers(self):
        if not self.fs_watcher.is_alive():
            logger.warning("Filesystem watcher is not alive; restarting.")
            self.fs_watcher.stop()
            self._safe_start_watcher("filesystem", lambda: self.fs_watcher.start(self._watch_paths))

        if self.git_watcher.repos and not self.git_watcher.is_alive():
            logger.warning("Git watcher loop is not alive; restarting.")
            self.git_watcher.stop()
            self._safe_start_watcher("git", lambda: self.git_watcher.start(self._watch_paths))

        if not self.term_watcher.is_alive():
            logger.warning("Terminal watcher is not alive; restarting.")
            self.term_watcher.stop()
            self._safe_start_watcher("terminal", self.term_watcher.start)

        if not self.clipboard_watcher.is_alive():
            logger.warning("Clipboard watcher is not alive; restarting.")
            self.clipboard_watcher.stop()
            self._safe_start_watcher("clipboard", lambda: self.clipboard_watcher.start(self._watch_paths))
