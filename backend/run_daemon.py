import os
import sys
import asyncio
import logging

# Ensure project root is in sys.path to allow absolute imports of backend.*
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.app.daemon.observer import DaemonObserver

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger("contextos.daemon")

async def main():
    observer = DaemonObserver()
    try:
        await observer.start()
        # Sleep indefinitely to keep the daemon running
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
    except KeyboardInterrupt:
        logger.info("ContextOS Daemon stopped via KeyboardInterrupt.")
