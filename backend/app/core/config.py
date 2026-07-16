import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Base paths
    # backend/app/core/config.py -> backend/
    BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent
    DATA_DIR: Path = BASE_DIR.parent / "data"
    DB_PATH: Path = DATA_DIR / "contextos.db"
    TRANSCRIPT_DIR: Path = DATA_DIR / "transcripts"

    # Memory / Vector store
    CHROMA_DIR: Path = DATA_DIR / "chroma"
    EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"
    QUERY_TOP_K: int = 5
    QUERY_MAX_DISTANCE: float = 1.25

    # Default directories to watch (default is the monorepo root)
    WATCH_PATHS: list[str] = []

    # Watcher polling intervals (seconds)
    GIT_POLL_INTERVAL: float = 30.0
    GIT_POLL_MAX_INTERVAL: float = 900.0
    GIT_POLL_BACKOFF_FACTOR: float = 2.0
    CLIPBOARD_POLL_INTERVAL: float = 1.0
    WATCHER_HEALTH_CHECK_INTERVAL: float = 5.0
    WATCHER_RESTART_DELAY_SECONDS: float = 5.0
    SLEEP_WAKE_DRIFT_SECONDS: float = 120.0

    # Daemon runtime files
    PID_FILE: Path = DATA_DIR / "daemon.pid"

    # Hard ignores enforced at the watcher layer.
    IGNORE_DIR_NAMES: list[str] = [
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".idea",
        ".vscode",
        ".gemini",
        "build",
        "dist",
        ".next",
        ".nuxt",
        "coverage",
        "htmlcov",
        "target",
    ]

    # Additional ignore regexes for generated files and SQLite sidecars.
    IGNORE_PATTERNS: list[str] = [
        r"\.pyc$",
        r"\.pyo$",
        r"\.pyd$",
        r"\.log$",
        r"\.tmp$",
        r"\.swp$",
        r"\.db$",
        r"\.sqlite$",
        r"\.sqlite3$",
        r"\.db-journal$",
        r"\.db-wal$",
        r"\.db-shm$"
    ]

    # DB Pipeline Settings
    QUEUE_FLUSH_INTERVAL: float = 2.0  # Time in seconds between database flushes
    QUEUE_BATCH_SIZE: int = 100         # Maximum number of events to batch write at once
    SQLITE_TIMEOUT_SECONDS: float = 30.0
    SQLITE_WRITE_RETRY_ATTEMPTS: int = 5
    SQLITE_WRITE_RETRY_BACKOFF_SECONDS: float = 0.25

    # Health telemetry
    HEALTH_LOG_INTERVAL_SECONDS: float = 60.0

    # OpenRouter LLM Config (Phase 3)
    OPENROUTER_API_KEY: str = ""
    GEMINI_API_KEY: str = ""

    # Orchestrator Settings
    SESSION_IDLE_TIMEOUT_SECONDS: int = 60 # Set to 60s for rapid testing. Production should be 1800 (30 mins).
    MINI_SUMMARY_INTERVAL_SECONDS: int = 300 # 5 minutes

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parent.parent.parent / ".env"),
        extra="ignore"
    )

settings = Settings()

# Ensure the data directories exist
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
settings.TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
settings.CHROMA_DIR.mkdir(parents=True, exist_ok=True)
