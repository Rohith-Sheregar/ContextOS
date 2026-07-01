import sqlite3
import json
import logging
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime
from backend.app.core.config import settings

logger = logging.getLogger("contextos.database")

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
"""

@contextmanager
def get_db_conn():
    """Provides a thread-safe SQLite connection context."""
    conn = sqlite3.connect(
        str(settings.DB_PATH)
    )
    # Enable foreign keys
    conn.execute("PRAGMA foreign_keys = ON;")
    # Use WAL mode for better concurrency
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    except Exception as e:
        conn.rollback()
        logger.error(f"Database error transaction rolled back: {e}")
        raise
    finally:
        conn.close()

def init_db():
    """Initializes database schema and ensures data/ directory exist."""
    logger.info(f"Initializing database at {settings.DB_PATH}")
    with get_db_conn() as conn:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
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
            timestamp = datetime.utcnow().isoformat()
            
        records.append((
            timestamp,
            event.get("project_name"),
            event.get("source"),
            event.get("event_type"),
            event.get("file_path"),
            payload_str
        ))
        
    with get_db_conn() as conn:
        conn.executemany(query, records)
        conn.commit()
    logger.debug(f"Saved {len(events)} events to database.")
