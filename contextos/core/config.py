import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ------------------------------------------------------------------
    # Base paths
    # ------------------------------------------------------------------
    # Package root: contextos/core/config.py -> contextos/
    _PACKAGE_DIR: Path = Path(__file__).resolve().parent.parent

    # User-level home dir — all persistent data lives here, NOT inside the
    # watched project. This ensures the daemon's own files are never seen by
    # the filesystem watcher.
    CONTEXTOS_HOME: Path = Path.home() / ".contextos"
    DATA_DIR: Path = CONTEXTOS_HOME / "data"
    DB_PATH: Path = DATA_DIR / "contextos.db"
    TRANSCRIPT_DIR: Path = DATA_DIR / "transcripts"

    # Memory / Vector store
    # CHROMA_DIR is kept because older releases used it for the ONNX model
    # cache path. We still create the dir so the model downloader works.
    CHROMA_DIR: Path = DATA_DIR / "chroma"
    EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"
    EMBEDDING_DIMENSION: int = 384
    QUERY_TOP_K: int = 5
    QUERY_MAX_DISTANCE: float = 1.25

    # Cross-project & re-entry
    REENTRY_STALE_AFTER_HOURS: float = 4.0
    REENTRY_BRIEF_RELATIVE_PATH: str = ".contextos/brief.md"
    CROSS_PROJECT_TOP_K: int = 5
    CROSS_PROJECT_MAX_DISTANCE: float = 0.95
    CROSS_PROJECT_MATCH_RELATIVE_PATH: str = ".contextos/similar.md"

    # Default directories to watch (default: current working directory)
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
    LOG_FILE: Path = CONTEXTOS_HOME / "logs" / "daemon.log"

    # Hard ignores enforced at the watcher layer.
    IGNORE_DIR_NAMES: list[str] = [
        ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
        "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
        ".idea", ".vscode", ".gemini", "build", "dist", ".next", ".nuxt",
        "coverage", "htmlcov", "target",
    ]

    # Additional ignore regexes for generated files and SQLite sidecars.
    IGNORE_PATTERNS: list[str] = [
        r"\.pyc$", r"\.pyo$", r"\.pyd$", r"\.log$", r"\.tmp$", r"\.swp$",
        r"\.db$", r"\.sqlite$", r"\.sqlite3$", r"\.db-journal$",
        r"\.db-wal$", r"\.db-shm$",
    ]

    # DB Pipeline Settings
    QUEUE_FLUSH_INTERVAL: float = 2.0
    QUEUE_BATCH_SIZE: int = 100
    SQLITE_TIMEOUT_SECONDS: float = 30.0
    SQLITE_WRITE_RETRY_ATTEMPTS: int = 5
    SQLITE_WRITE_RETRY_BACKOFF_SECONDS: float = 0.25

    # Health telemetry
    HEALTH_LOG_INTERVAL_SECONDS: float = 60.0

    # LLM Config
    LLM_PROVIDER: str = "auto"  # auto, openrouter, ollama, gemini, none
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL: str = "openai/gpt-oss-20b:free"
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.0-flash"
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "llama3.2"
    OLLAMA_SUMMARIZER_MODEL: str = ""
    OLLAMA_QUERY_MODEL: str = ""
    OLLAMA_REENTRY_MODEL: str = ""
    LLM_TIMEOUT_SECONDS: float = 20.0
    LLM_CACHE_ENABLED: bool = True

    # Orchestrator Settings
    SESSION_IDLE_TIMEOUT_SECONDS: int = 1800   # 30 minutes (production default)
    MINI_SUMMARY_INTERVAL_SECONDS: int = 300   # 5 minutes

    # Backfill queue — cap retries so a broken summary doesn't loop forever
    BACKFILL_MAX_RETRIES: int = 10

    # Dashboard API
    DASHBOARD_ENABLED: bool = True
    DASHBOARD_HOST: str = "127.0.0.1"
    DASHBOARD_PORT: int = 6543

    model_config = SettingsConfigDict(
        # Look for .env in CONTEXTOS_HOME first, then CWD
        env_file=[
            str(Path.home() / ".contextos" / ".env"),
            ".env",
        ],
        extra="ignore",
    )


settings = Settings()

# Ensure all runtime directories exist at import time
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
settings.TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
settings.CHROMA_DIR.mkdir(parents=True, exist_ok=True)
settings.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
