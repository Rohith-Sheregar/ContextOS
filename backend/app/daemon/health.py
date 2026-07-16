import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from backend.app.core.config import settings
from backend.app.core.database import save_daemon_health

try:
    import psutil
except ImportError:  # pragma: no cover - dependency absence is logged at runtime.
    psutil = None

logger = logging.getLogger("contextos.daemon.health")


class HealthMonitor:
    def __init__(self):
        self._running = False
        self._worker_task: asyncio.Task | None = None
        self._process = psutil.Process(os.getpid()) if psutil else None
        if self._process:
            self._process.cpu_percent(interval=None)

    def start(self):
        if self._running:
            return
        if not self._process:
            logger.warning("psutil is not installed; daemon health telemetry is disabled.")
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._monitor_loop())
        logger.info("HealthMonitor started.")

    async def stop(self):
        if not self._running:
            return
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("HealthMonitor stopped.")

    async def _monitor_loop(self):
        interval = settings.HEALTH_LOG_INTERVAL_SECONDS
        while self._running:
            expected_wake = time.monotonic() + interval
            try:
                await asyncio.sleep(interval)
                drift = time.monotonic() - expected_wake
                if drift > settings.SLEEP_WAKE_DRIFT_SECONDS:
                    logger.info("Detected possible sleep/wake gap of %.1fs.", drift)
                await asyncio.to_thread(self._record_health)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("HealthMonitor loop failed; continuing.")

    def _record_health(self):
        if not self._process:
            return
        memory = self._process.memory_info()
        try:
            open_files = len(self._process.open_files())
        except Exception:
            open_files = None

        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pid": self._process.pid,
            "cpu_percent": self._process.cpu_percent(interval=None),
            "memory_rss_bytes": memory.rss,
            "memory_percent": self._process.memory_percent(),
            "thread_count": self._process.num_threads(),
            "open_files": open_files,
            "metadata": {"status": self._process.status()},
        }
        save_daemon_health(snapshot)
        logger.debug(
            "Daemon health: cpu=%.2f%% rss=%.1fMB",
            snapshot["cpu_percent"],
            snapshot["memory_rss_bytes"] / (1024 * 1024),
        )
