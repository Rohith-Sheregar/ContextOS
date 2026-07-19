"""
Daemon entry point — spawned as a detached subprocess by `contextos start`.
This file is intentionally separate from cli.py so the import chain is clean.
"""
import asyncio
import logging
import sys
from pathlib import Path

from contextos.core.config import settings
from contextos.core.lockfile import DaemonLock, LockfileError
from contextos.daemon.observer import DaemonObserver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger("contextos.daemon")


async def main():
    with DaemonLock(settings.PID_FILE):
        observer = DaemonObserver()
        try:
            await observer.start()
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            logger.info("Daemon execution cancelled by system.")
        except KeyboardInterrupt:
            logger.info("Daemon execution interrupted by user.")
        finally:
            await observer.stop()


if __name__ == "__main__":
    logger.info("Initializing ContextOS Daemon Process...")
    try:
        asyncio.run(main())
    except LockfileError as e:
        logger.error(str(e))
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("ContextOS Daemon stopped via KeyboardInterrupt.")
