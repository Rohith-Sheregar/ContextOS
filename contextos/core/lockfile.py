import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


class LockfileError(RuntimeError):
    pass


class DaemonLock:
    """Single-process lock backed by an atomic PID file create."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: int | None = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        data = json.dumps(payload, sort_keys=True)

        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(self._fd, data.encode("utf-8"))
                return
            except FileExistsError:
                existing_pid = self._read_existing_pid()
                if existing_pid and self._pid_is_running(existing_pid):
                    raise LockfileError(
                        f"ContextOS daemon already appears to be running with PID {existing_pid}."
                    )
                self._remove_stale_lock()

    def release(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            if self._read_existing_pid() == os.getpid():
                self.path.unlink()
        except FileNotFoundError:
            pass

    def _read_existing_pid(self) -> int | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return int(payload.get("pid"))
        except Exception:
            return None

    def _pid_is_running(self, pid: int) -> bool:
        if pid == os.getpid():
            return True
        if psutil is None:
            return True
        try:
            process = psutil.Process(pid)
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
        except psutil.Error:
            return True

    def _remove_stale_lock(self):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
