import asyncio
import logging
from typing import Callable, List, Dict, Any
from contextos.core.config import settings
from contextos.core.database import save_events_batch

logger = logging.getLogger("contextos.queue")

class EventQueue:
    def __init__(
        self,
        save_func: Callable[[list[dict]], None] = save_events_batch,
        flush_interval: float | None = None,
        batch_size: int | None = None,
    ):
        self._queue = asyncio.Queue()
        self._retry_buffer: List[Dict[str, Any]] = []
        self._worker_task = None
        self._running = False
        self._save_func = save_func
        self._flush_interval = flush_interval if flush_interval is not None else settings.QUEUE_FLUSH_INTERVAL
        self._batch_size = batch_size if batch_size is not None else settings.QUEUE_BATCH_SIZE

    async def put(self, event: Dict[str, Any]):
        """Pushes a new event into the asyncio queue."""
        await self._queue.put(event)
        logger.debug(f"Queued event: {event.get('event_type')} from source {event.get('source')}")

    def start(self):
        """Starts the background database flushing worker."""
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._flush_worker())
        logger.info("EventQueue background flusher worker started.")

    async def stop(self):
        """Gracefully stops the worker, ensuring remaining items are flushed."""
        if not self._running:
            return
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

        while self._retry_buffer or not self._queue.empty():
            flushed = await self._flush_remaining()
            if not flushed:
                break
        logger.info("EventQueue background flusher worker stopped and queue flushed.")

    async def _flush_worker(self):
        """Loops periodically to flush queued events to the database in batches."""
        while self._running:
            try:
                await asyncio.sleep(self._flush_interval)
                await self._flush_remaining()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in queue flusher worker loop: {e}")

    async def _flush_remaining(self) -> bool:
        """Flushes all currently buffered events in the queue to SQLite."""
        events: List[Dict[str, Any]] = []

        while self._retry_buffer and len(events) < self._batch_size:
            events.append(self._retry_buffer.pop(0))

        while not self._queue.empty() and len(events) < self._batch_size:
            try:
                event = self._queue.get_nowait()
                events.append(event)
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break

        if not events:
            return True

        try:
            await asyncio.to_thread(self._save_func, events)
            return True
        except Exception as e:
            self._retry_buffer = events + self._retry_buffer
            logger.error(f"Failed to flush {len(events)} events to database: {e}")
            return False
