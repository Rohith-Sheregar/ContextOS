import sqlite3
import json
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from typing import Callable, TypeVar
from backend.app.core.config import settings

logger = logging.getLogger("contextos.database")
T = TypeVar("T")

# DB Initialization Schema
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    path TEXT UNIQUE NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'IDLE'
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    source TEXT NOT NULL,
    event_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    project_name TEXT NOT NULL,
    payload TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    project_name TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT,
    status TEXT NOT NULL, -- 'ACTIVE' or 'COMPLETED'
    summary TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_name);

CREATE TABLE IF NOT EXISTS daemon_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    pid INTEGER NOT NULL,
    cpu_percent REAL,
    memory_rss_bytes INTEGER,
    memory_percent REAL,
    thread_count INTEGER,
    open_files INTEGER,
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_daemon_health_timestamp ON daemon_health(timestamp);

CREATE TABLE IF NOT EXISTS memory_backfill_queue (
    doc_id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    metadata TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_memory_backfill_created_at ON memory_backfill_queue(created_at);
"""

@contextmanager
def get_db_conn():
    """Provides a thread-safe SQLite connection context."""
    conn = sqlite3.connect(
        str(settings.DB_PATH),
        timeout=settings.SQLITE_TIMEOUT_SECONDS
    )
    # Enable foreign keys
    conn.execute("PRAGMA foreign_keys = ON;")
    # Use WAL mode for better concurrency
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute(f"PRAGMA busy_timeout = {int(settings.SQLITE_TIMEOUT_SECONDS * 1000)};")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    except Exception as e:
        conn.rollback()
        logger.error(f"Database error transaction rolled back: {e}")
        raise
    finally:
        conn.close()

def _is_sqlite_lock_error(error: Exception) -> bool:
    return isinstance(error, sqlite3.OperationalError) and "locked" in str(error).lower()

def run_with_db_retry(operation_name: str, operation: Callable[[], T]) -> T:
    """Retries short-lived SQLite lock contention without crashing daemon workers."""
    attempts = max(1, settings.SQLITE_WRITE_RETRY_ATTEMPTS)
    delay = max(0.0, settings.SQLITE_WRITE_RETRY_BACKOFF_SECONDS)

    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as e:
            if _is_sqlite_lock_error(e) and attempt < attempts:
                sleep_for = delay * attempt
                logger.warning(
                    "%s hit SQLite lock; retrying in %.2fs (%s/%s)",
                    operation_name,
                    sleep_for,
                    attempt,
                    attempts,
                )
                time.sleep(sleep_for)
                continue
            raise

def init_db():
    """Initializes database schema and ensures data/ directory exist."""
    logger.info(f"Initializing database at {settings.DB_PATH}")
    def _init():
        with get_db_conn() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.commit()
    run_with_db_retry("init_db", _init)
    logger.info("Database initialized successfully.")

def save_events_batch(events: list[dict]):
    """Bulk inserts a list of event dictionaries into the database."""
    if not events:
        return

    query = """
        INSERT INTO events (timestamp, project_name, source, event_type, file_path, payload)
        VALUES (?, ?, ?, ?, ?, ?)
    """

    records = []
    for event in events:
        # Convert payload dict/list to JSON string if it is not already a string
        payload = event.get("payload")
        if payload is not None and not isinstance(payload, str):
            payload_str = json.dumps(payload)
        else:
            payload_str = payload

        timestamp = event.get("timestamp")
        if isinstance(timestamp, datetime):
            timestamp = timestamp.isoformat()
        elif not timestamp:
            timestamp = datetime.now(timezone.utc).isoformat()

        records.append((
            timestamp,
            event.get("project_name"),
            event.get("source"),
            event.get("event_type"),
            event.get("file_path"),
            payload_str
        ))

    def _save():
        with get_db_conn() as conn:
            conn.executemany(query, records)
            conn.commit()
    run_with_db_retry("save_events_batch", _save)
    logger.debug(f"Saved {len(events)} events to database.")

def save_daemon_health(snapshot: dict):
    """Stores one daemon health telemetry row."""
    query = """
        INSERT INTO daemon_health (
            timestamp, pid, cpu_percent, memory_rss_bytes, memory_percent,
            thread_count, open_files, metadata
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """
    record = (
        snapshot.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        snapshot.get("pid"),
        snapshot.get("cpu_percent"),
        snapshot.get("memory_rss_bytes"),
        snapshot.get("memory_percent"),
        snapshot.get("thread_count"),
        snapshot.get("open_files"),
        json.dumps(snapshot.get("metadata", {})),
    )

    def _save():
        with get_db_conn() as conn:
            conn.execute(query, record)
            conn.commit()
    run_with_db_retry("save_daemon_health", _save)

def enqueue_memory_backfill(doc_id: str, text: str, metadata: dict, error: str | None = None):
    """Stores a summary for later embedding retry."""
    query = """
        INSERT INTO memory_backfill_queue (doc_id, text, metadata, created_at, last_error, retry_count)
        VALUES (?, ?, ?, ?, ?, 0)
        ON CONFLICT(doc_id) DO UPDATE SET
            text = excluded.text,
            metadata = excluded.metadata,
            last_error = excluded.last_error,
            retry_count = memory_backfill_queue.retry_count + 1
    """
    record = (
        doc_id,
        text,
        json.dumps(metadata),
        datetime.now(timezone.utc).isoformat(),
        error,
    )

    def _save():
        with get_db_conn() as conn:
            conn.execute(query, record)
            conn.commit()
    run_with_db_retry("enqueue_memory_backfill", _save)

def load_memory_backfill_items(limit: int = 100) -> list[dict]:
    """Loads queued summaries that still need embedding."""
    def _load():
        with get_db_conn() as conn:
            rows = conn.execute(
                """
                SELECT doc_id, text, metadata, retry_count
                FROM memory_backfill_queue
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                {
                    "doc_id": row["doc_id"],
                    "text": row["text"],
                    "metadata": json.loads(row["metadata"]),
                    "retry_count": row["retry_count"],
                }
                for row in rows
            ]
    return run_with_db_retry("load_memory_backfill_items", _load)

def delete_memory_backfill_item(doc_id: str):
    """Removes a successfully embedded queued summary."""
    def _delete():
        with get_db_conn() as conn:
            conn.execute("DELETE FROM memory_backfill_queue WHERE doc_id = ?", (doc_id,))
            conn.commit()
    run_with_db_retry("delete_memory_backfill_item", _delete)
