import os
import asyncio
import logging
from backend.app.core.config import settings
from backend.app.core.database import init_db
from backend.app.daemon.queue import EventQueue
from backend.app.daemon.watchers.filesystem import FilesystemWatcher
from backend.app.daemon.watchers.terminal import TerminalWatcher
from backend.app.daemon.watchers.git import GitWatcher
from backend.app.daemon.orchestrator import SessionOrchestrator
from backend.app.daemon.health import HealthMonitor
from backend.app.core.memory_store import MemoryStore

logger = logging.getLogger("contextos.daemon.observer")

class DaemonObserver:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.queue = EventQueue()
        self.fs_watcher = FilesystemWatcher(self.loop, self.queue)
        self.term_watcher = TerminalWatcher(self.loop, self.queue)
        self.git_watcher = GitWatcher(self.loop, self.queue)
        self.memory_store = MemoryStore()
        self.orchestrator = SessionOrchestrator(self.loop, self.memory_store)
        self.health_monitor = HealthMonitor()
        self._supervisor_task = None
        self._watch_paths = []
        self.is_running = False

    async def start(self):
        """Starts the ingestion queue, initializes database, and launches watchers."""
        if self.is_running:
            return

        logger.info("Starting ContextOS Daemon Observer...")

        # Initialize SQLite database schema
        await asyncio.to_thread(init_db)

        # Start queue database flusher
        self.queue.start()

        # Determine watch paths
        watch_paths = list(settings.WATCH_PATHS)
        if not watch_paths:
            # Default to the parent of backend/ (the monorepo directory)
            default_path = str(settings.BASE_DIR.parent.resolve())
            watch_paths = [default_path]

        self._watch_paths = watch_paths
        logger.info(f"Paths to monitor: {watch_paths}")

        # Start watchers; supervisor will retry any watcher that fails to start.
        self._start_watchers()

        # Start orchestrator
        self.orchestrator.start()

        # Start daemon health telemetry.
        self.health_monitor.start()

        self.is_running = True
        self._supervisor_task = asyncio.create_task(self._supervise_watchers_loop())
        logger.info("ContextOS Daemon Observer is running.")

    async def stop(self):
        """Stops watchers and flushes queue data pipeline gracefully."""
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

        # Stop orchestrator first so it wraps up sessions
        self.orchestrator.stop()

        # Stop watchers
        self.fs_watcher.stop()
        self.git_watcher.stop()
        self.term_watcher.stop()

        # Stop health telemetry before the process exits.
        await self.health_monitor.stop()

        # Stop the queue flusher and ensure remaining events are written
        await self.queue.stop()

        self.is_running = False
        logger.info("ContextOS Daemon Observer stopped.")

    def _start_watchers(self):
        self._safe_start_watcher("filesystem", lambda: self.fs_watcher.start(self._watch_paths))
        self._safe_start_watcher("git", lambda: self.git_watcher.start(self._watch_paths))
        self._safe_start_watcher("terminal", self.term_watcher.start)

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
