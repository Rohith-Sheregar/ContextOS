import os
import asyncio
import logging
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from contextos.core.config import settings
from contextos.daemon.queue import EventQueue

logger = logging.getLogger("contextos.watchers.terminal")

class TranscriptEventHandler(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: EventQueue):
        self.loop = loop
        self.queue = queue
        self.file_positions = {}

    def process_file(self, file_path: str):
        if not file_path.endswith('.txt'):
            return

        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                last_pos = self.file_positions.get(file_path, 0)
                f.seek(last_pos)
                new_data = f.read()
                self.file_positions[file_path] = f.tell()

                if new_data:
                    event_data = {
                        "source": "terminal",
                        "event_type": "output",
                        "file_path": file_path,
                        "project_name": "global_terminal",
                        "payload": {"content": new_data},
                    }
                    asyncio.run_coroutine_threadsafe(self.queue.put(event_data), self.loop)
        except Exception as e:
            logger.error(f"Error reading transcript {file_path}: {e}")

    def on_created(self, event):
        if not event.is_directory:
            self.process_file(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self.process_file(event.src_path)


class TerminalWatcher:
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: EventQueue):
        self.loop = loop
        self.queue = queue
        self.observer = None
        self.handler = None

    def start(self):
        """Starts monitoring the transcript directory."""
        if self.observer:
            return

        transcript_dir = str(settings.TRANSCRIPT_DIR)

        self.observer = Observer()
        self.handler = TranscriptEventHandler(self.loop, self.queue)

        logger.info(f"Setting up terminal watcher for: {transcript_dir}")
        self.observer.schedule(self.handler, transcript_dir, recursive=False)
        try:
            self.observer.start()
        except Exception:
            self.observer = None
            self.handler = None
            logger.exception("TerminalWatcher failed to start.")
            raise
        logger.info("TerminalWatcher started.")

    def stop(self):
        """Stops the terminal monitoring."""
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None
            self.handler = None
            logger.info("TerminalWatcher stopped.")

    def is_alive(self) -> bool:
        return bool(self.observer and self.observer.is_alive())
