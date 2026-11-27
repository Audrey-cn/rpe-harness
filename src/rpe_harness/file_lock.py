"""Portable exclusive file locking (POSIX fcntl.flock + Windows msvcrt.locking)."""
from __future__ import annotations

import os

NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
NONBLOCK = getattr(os, 'O_NONBLOCK', 0)

if os.name == 'nt':
    import msvcrt

    def lock_exclusive(fd: int, *, blocking: bool = True) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        try:
            msvcrt.locking(fd, mode, 1)
        except OSError as exc:
            if not blocking:
                raise BlockingIOError(exc.errno, exc.strerror) from exc
            raise

    def unlock(fd: int) -> None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def lock_exclusive(fd: int, *, blocking: bool = True) -> None:
        flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, flags)

    def unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
