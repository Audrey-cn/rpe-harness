"""Portable exclusive file locking (POSIX fcntl.flock + Windows msvcrt.locking)."""
from __future__ import annotations

import os

NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
NONBLOCK = getattr(os, 'O_NONBLOCK', 0)


def _fileno(target) -> int:
    return target if isinstance(target, int) else target.fileno()


if os.name == 'nt':
    import msvcrt

    def lock_exclusive(target, *, blocking: bool = True) -> None:
        fd = _fileno(target)
        os.lseek(fd, 0, os.SEEK_SET)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        try:
            msvcrt.locking(fd, mode, 1)
        except OSError as exc:
            if not blocking:
                raise BlockingIOError(exc.errno, exc.strerror) from exc
            raise

    def unlock(target) -> None:
        try:
            fd = _fileno(target)
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def lock_exclusive(target, *, blocking: bool = True) -> None:
        flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(_fileno(target), flags)

    def unlock(target) -> None:
        try:
            fcntl.flock(_fileno(target), fcntl.LOCK_UN)
        except OSError:
            pass
