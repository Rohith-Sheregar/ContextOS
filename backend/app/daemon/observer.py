import os
import asyncio
import logging
from backend.app.core.config import settings
from backend.app.core.database import init_db
from backend.app.daemon.queue import EventQueue
from backend.app.daemon.watchers.filesystem import FilesystemWatcher
from backend.app.daemon.watchers.terminal import TerminalWatcher
from backend.app.daemon.watchers.git import GitWatcher

logger = logging.getLogger("contextos.daemon.observer")

class DaemonObserver:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.queue = EventQueue()
        self.fs_watcher = FilesystemWatcher(self.loop, self.queue)
        self.term_watcher = TerminalWatcher(self.loop, self.queue)
        self.git_watcher = GitWatcher(self.loop, self.queue)
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
            
        logger.info(f"Paths to monitor: {watch_paths}")
        
        # Start filesystem watcher
        self.fs_watcher.start(watch_paths)
        
        # Start git watcher
        self.git_watcher.start(watch_paths)
        
        # Start terminal watcher
        self.term_watcher.start()
        
        self.is_running = True
        logger.info("ContextOS Daemon Observer is running.")

    async def stop(self):
        """Stops watchers and flushes queue data pipeline gracefully."""
        if not self.is_running:
            return
            
        logger.info("Stopping ContextOS Daemon Observer...")
        
        # Stop watchers first
        self.fs_watcher.stop()
        self.git_watcher.stop()
        self.term_watcher.stop()
        
        # Stop the queue flusher and ensure remaining events are written
        await self.queue.stop()
        
        self.is_running = False
        logger.info("ContextOS Daemon Observer stopped.")
