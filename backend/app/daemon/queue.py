import asyncio
import logging
from typing import List, Dict, Any
from backend.app.core.config import settings
from backend.app.core.database import save_events_batch

logger = logging.getLogger("contextos.queue")

class EventQueue:
    def __init__(self):
        self._queue = asyncio.Queue()
        self._worker_task = None
        self._running = False

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
        
        # Final flush of all remaining events in the queue
        await self._flush_remaining()
        logger.info("EventQueue background flusher worker stopped and queue flushed.")

    async def _flush_worker(self):
        """Loops periodically to flush queued events to the database in batches."""
        while self._running:
            try:
                # Wait for flush interval
                await asyncio.sleep(settings.QUEUE_FLUSH_INTERVAL)
                await self._flush_remaining()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in queue flusher worker loop: {e}")

    async def _flush_remaining(self):
        """Flushes all currently buffered events in the queue to SQLite."""
        events: List[Dict[str, Any]] = []
        
        while not self._queue.empty() and len(events) < settings.QUEUE_BATCH_SIZE:
            try:
                event = self._queue.get_nowait()
                events.append(event)
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break
                
        if events:
            try:
                # Write to sqlite using to_thread to avoid blocking event loop
                await asyncio.to_thread(save_events_batch, events)
            except Exception as e:
                logger.error(f"Failed to flush {len(events)} events to database: {e}")
