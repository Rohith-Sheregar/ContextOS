import asyncio
import logging
from typing import List
import pyperclip

from contextos.daemon.queue import EventQueue

logger = logging.getLogger("contextos.watchers.clipboard")

class ClipboardWatcher:
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: EventQueue):
        self.loop = loop
        self.queue = queue
        self._task = None
        self._watch_paths = []
        self._last_content = ""

    def start(self, watch_paths: List[str]):
        """Starts monitoring the clipboard."""
        if self._task and not self._task.done():
            return

        self._watch_paths = watch_paths
        
        try:
            self._last_content = pyperclip.paste()
        except Exception as e:
            logger.warning(f"Could not read initial clipboard: {e}")
            self._last_content = ""

        self._task = self.loop.create_task(self._poll_clipboard())
        logger.info("ClipboardWatcher started.")

    async def _poll_clipboard(self):
        try:
            while True:
                await asyncio.sleep(1.0)
                try:
                    # pyperclip can block or raise exceptions on some systems
                    current_content = await asyncio.to_thread(pyperclip.paste)
                except Exception as e:
                    continue

                if current_content and current_content != self._last_content:
                    self._last_content = current_content
                    # Limit the size of clipboard text to avoid massive DB bloat
                    if len(current_content) > 10000:
                        current_content = current_content[:10000] + "\n...[truncated]"

                    # Dispatch event to the first tracked project (or global)
                    project_name = "Global"
                    if self._watch_paths:
                        import os
                        project_name = os.path.basename(os.path.abspath(self._watch_paths[0]))

                    data = {
                        "source": "clipboard",
                        "event_type": "copied",
                        "file_path": "clipboard",
                        "project_name": project_name,
                        "payload": {"text": current_content},
                    }
                    await self.queue.put(data)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"ClipboardWatcher loop failed: {e}")

    def stop(self):
        """Stops the clipboard monitoring."""
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("ClipboardWatcher stopped.")

    def is_alive(self) -> bool:
        return self._task is not None and not self._task.done()
