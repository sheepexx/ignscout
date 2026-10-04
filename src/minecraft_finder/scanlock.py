"""Allow only one scan per database at a time.

Two scans in parallel would share the same per-IP rate limit and just provoke
HTTP 429s, and could check the same names twice. The lock is an OS-level file
lock, so it disappears by itself when the scanning process exits, even if it
crashes. Reading results (stats, export, the menu's "show") never needs it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import IO


def lock_path_for(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".scan.lock")


class ScanLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: IO[bytes] | None = None

    def acquire(self) -> bool:
        """Try to take the lock without waiting. Returns False if another scan holds it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> ScanLock:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def scan_running(db_path: Path) -> bool:
    """True while another process is scanning with this database."""
    path = lock_path_for(db_path)
    if not path.exists():
        return False
    lock = ScanLock(path)
    if lock.acquire():
        lock.release()
        return False
    return True
