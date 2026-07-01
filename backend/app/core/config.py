import os
from pathlib import Path
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Base paths
    # backend/app/core/config.py -> backend/
    BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent
    DATA_DIR: Path = BASE_DIR.parent / "data"
    DB_PATH: Path = DATA_DIR / "contextos.db"
    TRANSCRIPT_DIR: Path = DATA_DIR / "transcripts"
    
    # Default directories to watch (default is the monorepo root)
    WATCH_PATHS: list[str] = []
    
    # Watcher polling intervals (seconds)
    GIT_POLL_INTERVAL: float = 30.0
    CLIPBOARD_POLL_INTERVAL: float = 1.0
    
    # Default ignore patterns (directories to ignore completely)
    IGNORE_PATTERNS: list[str] = [
        r"[\/\\]\.git[\/\\]",
        r"[\/\\]node_modules[\/\\]",
        r"[\/\\]__pycache__[\/\\]",
        r"[\/\\]\.venv[\/\\]",
        r"[\/\\]venv[\/\\]",
        r"[\/\\]\.gemini[\/\\]",
        r"[\/\\]\.pytest_cache[\/\\]",
        r"[\/\\]\.idea[\/\\]",
        r"[\/\\]\.vscode[\/\\]",
        r"\.pyc$",
        r"\.pyo$",
        r"\.pyd$",
        r"\.db$",
        r"\.sqlite$",
        r"\.db-journal$",
        r"\.db-wal$",
        r"\.db-shm$"
    ]
    
    # DB Pipeline Settings
    QUEUE_FLUSH_INTERVAL: float = 2.0  # Time in seconds between database flushes
    QUEUE_BATCH_SIZE: int = 100         # Maximum number of events to batch write at once
    
    # Groq & LLM Config (Phase 3)
    GROQ_API_KEY: str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()

# Ensure the data directories exist
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
settings.TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
