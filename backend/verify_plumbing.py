import os
import sys
import asyncio
import shutil
import logging
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("verify_plumbing")

# Set test-specific environment variables BEFORE importing config
test_data_dir = Path(__file__).resolve().parent / "test_data"
test_watch_dir = Path(__file__).resolve().parent / "test_watch_dir"

os.environ["DB_PATH"] = str(test_data_dir / "contextos_test.db")
os.environ["DATA_DIR"] = str(test_data_dir)

# Now import the settings and observer
from backend.app.core.config import settings
from backend.app.daemon.observer import DaemonObserver
from backend.app.core.database import get_db_conn

# Set watch paths to our temporary folder
settings.WATCH_PATHS = [str(test_watch_dir)]

async def run_verification():
    # Setup test directories
    test_data_dir.mkdir(parents=True, exist_ok=True)
    if test_watch_dir.exists():
        shutil.rmtree(test_watch_dir)
    test_watch_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Initializing observer...")
    observer = DaemonObserver()
    await observer.start()
    
    # Wait for observer to stabilize
    await asyncio.sleep(1)

    # Perform file operations
    test_file = test_watch_dir / "sample.txt"
    
    logger.info(f"Creating file: {test_file}")
    with open(test_file, "w") as f:
        f.write("Hello contextOS!")
    await asyncio.sleep(0.5)

    logger.info(f"Modifying file: {test_file}")
    with open(test_file, "a") as f:
        f.write("\nAdding lines to trigger modified event.")
    await asyncio.sleep(0.5)

    logger.info(f"Deleting file: {test_file}")
    os.remove(test_file)
    await asyncio.sleep(0.5)

    # Wait for the queue flush interval to database (default 2 seconds)
    logger.info("Waiting for queue flusher to write to database...")
    await asyncio.sleep(3)

    logger.info("Stopping observer...")
    await observer.stop()

    # Query events from sqlite database
    logger.info("Querying SQLite DB event logs...")
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, timestamp, source, event_type, file_path, payload FROM events ORDER BY id ASC")
        rows = cursor.fetchall()
        
    logger.info("=== VERIFICATION EVENT LOGS ===")
    for row in rows:
        logger.info(f"Event ID {row['id']}: [{row['timestamp']}] Source: {row['source']} | Type: {row['event_type']} | File: {row['file_path']} | Payload: {row['payload']}")
    logger.info("=================================")

    # Clean up test directories
    if test_watch_dir.exists():
        shutil.rmtree(test_watch_dir)
    if test_data_dir.exists():
        shutil.rmtree(test_data_dir)

    # Assert check
    event_types = [row['event_type'] for row in rows]
    if "created" in event_types and "modified" in event_types and "deleted" in event_types:
        logger.info("SUCCESS: All filesystem events (created, modified, deleted) successfully captured and persisted!")
        return True
    else:
        logger.error(f"FAILURE: Missing events. Captured event types: {event_types}")
        return False

if __name__ == "__main__":
    success = asyncio.run(run_verification())
    sys.exit(0 if success else 1)
