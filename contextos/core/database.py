import sqlite3
import json
import hashlib
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from typing import Callable, TypeVar
from contextos.core.config import settings

logger = logging.getLogger("contextos.database")
T = TypeVar("T")

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

CREATE TABLE IF NOT EXISTS memory_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT UNIQUE NOT NULL,
    text TEXT NOT NULL,
    metadata TEXT NOT NULL,
    project_name TEXT,
    session_id TEXT,
    timestamp TEXT,
    summary_type TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_documents_project ON memory_documents(project_name);
CREATE INDEX IF NOT EXISTS idx_memory_documents_session ON memory_documents(session_id);

CREATE TABLE IF NOT EXISTS llm_cache (
    prompt_hash TEXT PRIMARY KEY,
    prompt_preview TEXT NOT NULL,
    response TEXT NOT NULL,
    provider TEXT,
    created_at TEXT NOT NULL
);
"""

@contextmanager
def get_db_conn():
    """Provides a thread-safe SQLite connection context."""
    conn = sqlite3.connect(
        str(settings.DB_PATH),
        timeout=settings.SQLITE_TIMEOUT_SECONDS
    )
    conn.execute("PRAGMA foreign_keys = ON;")
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
                    operation_name, sleep_for, attempt, attempts,
                )
                time.sleep(sleep_for)
                continue
            raise

def init_db():
    """Initializes database schema and ensures data directory exists."""
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
    """Loads queued summaries that still need embedding, skipping items that
    have exceeded BACKFILL_MAX_RETRIES."""
    max_retries = settings.BACKFILL_MAX_RETRIES
    def _load():
        with get_db_conn() as conn:
            rows = conn.execute(
                """
                SELECT doc_id, text, metadata, retry_count
                FROM memory_backfill_queue
                WHERE retry_count < ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (max_retries, limit),
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


# ---------------------------------------------------------------------------
# LLM response cache
# ---------------------------------------------------------------------------

def _hash_prompt(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

def get_cached_llm_response(prompt: str) -> str | None:
    """Returns a cached LLM response for the given prompt, or None."""
    prompt_hash = _hash_prompt(prompt)
    try:
        with get_db_conn() as conn:
            row = conn.execute(
                "SELECT response FROM llm_cache WHERE prompt_hash = ?",
                (prompt_hash,),
            ).fetchone()
            if row:
                return row["response"]
    except Exception:
        pass  # cache miss is fine
    return None

def save_llm_response_cache(prompt: str, response: str, provider: str | None = None):
    """Caches an LLM response keyed by prompt hash."""
    prompt_hash = _hash_prompt(prompt)
    preview = prompt[:120].replace("\n", " ")
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db_conn() as conn:
            conn.execute(
                """
                INSERT INTO llm_cache (prompt_hash, prompt_preview, response, provider, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(prompt_hash) DO UPDATE SET
                    response = excluded.response,
                    provider = excluded.provider,
                    created_at = excluded.created_at
                """,
                (prompt_hash, preview, response, provider, now),
            )
            conn.commit()
    except Exception:
        logger.debug("Failed to cache LLM response (non-fatal).")


# ---------------------------------------------------------------------------
# Forget / cleanup
# ---------------------------------------------------------------------------

def delete_project_data(project_name: str) -> dict[str, int]:
    """Deletes all events, sessions, and vector docs for a project. Returns counts."""
    counts = {"events": 0, "sessions": 0, "vectors": 0}
    def _delete():
        with get_db_conn() as conn:
            counts["events"] = conn.execute(
                "SELECT COUNT(*) FROM events WHERE project_name = ?", (project_name,)
            ).fetchone()[0]
            conn.execute("DELETE FROM events WHERE project_name = ?", (project_name,))

            counts["sessions"] = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE project_name = ?", (project_name,)
            ).fetchone()[0]
            conn.execute("DELETE FROM sessions WHERE project_name = ?", (project_name,))

            try:
                # Delete vector docs and their embeddings
                doc_ids = conn.execute(
                    "SELECT id FROM memory_documents WHERE project_name = ?", (project_name,)
                ).fetchall()
                counts["vectors"] = len(doc_ids)
                for row in doc_ids:
                    conn.execute("DELETE FROM memory_vectors WHERE rowid = ?", (row["id"],))
                conn.execute("DELETE FROM memory_documents WHERE project_name = ?", (project_name,))
            except Exception:
                pass  # memory_vectors may not exist if sqlite-vec isn't loaded

            conn.commit()
    run_with_db_retry("delete_project_data", _delete)
    return counts

def delete_events_before(cutoff_iso: str) -> int:
    """Deletes events older than the given ISO timestamp. Returns count."""
    def _delete():
        with get_db_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE timestamp < ?", (cutoff_iso,)
            ).fetchone()[0]
            conn.execute("DELETE FROM events WHERE timestamp < ?", (cutoff_iso,))
            conn.commit()
            return count
    return run_with_db_retry("delete_events_before", _delete)
