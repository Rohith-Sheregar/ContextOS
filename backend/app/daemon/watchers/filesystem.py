import os
import asyncio
import logging
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent
from backend.app.core.ignore import should_ignore_path
from backend.app.daemon.queue import EventQueue

logger = logging.getLogger("contextos.watchers.filesystem")

class FilesystemEventHandler(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: EventQueue, watch_path: str):
        self.loop = loop
        self.queue = queue
        self.watch_path = os.path.abspath(watch_path)
        self.project_name = os.path.basename(self.watch_path)

    def should_ignore(self, path: str) -> bool:
        return should_ignore_path(path)

    def dispatch_event(self, event: FileSystemEvent, event_type: str):
        # Ignore temp files or folders we want to ignore
        if self.should_ignore(event.src_path):
            return

        dest_path = getattr(event, "dest_path", None)
        if dest_path and self.should_ignore(dest_path):
            return

        # Prepare payload dictionary
        payload = {
            "is_directory": event.is_directory,
        }
        if dest_path:
            payload["dest_path"] = os.path.abspath(dest_path)

        data = {
            "source": "filesystem",
            "event_type": event_type,
            "file_path": os.path.abspath(event.src_path),
            "project_name": self.project_name,
            "payload": payload
        }

        # Safely schedule queue.put in the running asyncio loop from the watcher's thread
        asyncio.run_coroutine_threadsafe(self.queue.put(data), self.loop)

    def on_created(self, event):
        self.dispatch_event(event, "created")

    def on_modified(self, event):
        # watchdog triggers modify on directories too when children change.
        # We can ignore directory modified events to reduce noise.
        if event.is_directory:
            return
        self.dispatch_event(event, "modified")

    def on_deleted(self, event):
        self.dispatch_event(event, "deleted")

    def on_moved(self, event):
        self.dispatch_event(event, "moved")


class FilesystemWatcher:
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: EventQueue):
        self.loop = loop
        self.queue = queue
        self.observer = None
        self.handlers = []

    def start(self, watch_paths: list[str]):
        """Starts monitoring the specified list of directory paths."""
        if self.observer:
            return

        self.observer = Observer()
        for path in watch_paths:
            path_abs = os.path.abspath(path)
            if not os.path.exists(path_abs):
                logger.warning(f"Path does not exist, skipping watch: {path_abs}")
                continue
            if should_ignore_path(path_abs):
                logger.info(f"Path is ignored, skipping filesystem watch: {path_abs}")
                continue

            logger.info(f"Setting up filesystem watcher for: {path_abs}")
            handler = FilesystemEventHandler(self.loop, self.queue, path_abs)
            self.handlers.append(handler)
            self.observer.schedule(handler, path_abs, recursive=True)

        if not self.handlers:
            logger.warning("No filesystem paths were scheduled for watching.")
            self.observer = None
            return

        try:
            self.observer.start()
        except Exception:
            self.observer = None
            self.handlers = []
            logger.exception("FilesystemWatcher failed to start.")
            raise
        logger.info("FilesystemWatcher started.")

    def stop(self):
        """Stops the filesystem monitoring."""
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None
            self.handlers = []
            logger.info("FilesystemWatcher stopped.")

    def is_alive(self) -> bool:
        return bool(self.observer and self.observer.is_alive())
