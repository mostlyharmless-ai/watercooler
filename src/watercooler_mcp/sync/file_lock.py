"""Cross-process advisory file locking primitive.

Provides a uniform context manager for acquiring an exclusive (or shared)
advisory lock on a path. POSIX uses ``fcntl.flock``; Windows uses
``msvcrt.locking`` on a byte range of the open file.

Use this helper for:

- Cross-process appends to shared state files (findings JSONL).
- Read-then-rewrite cycles where another process might write between read
  and rewrite (compact, acknowledge).
- Per-directory singleton process locks.

NFS limitation: ``fcntl.flock`` semantics differ on NFS. Watercooler does
not support NFS-mounted state directories.

Usage::

    from watercooler_mcp.sync.file_lock import file_lock

    with file_lock(state_dir / ".findings.lock"):
        with open(findings_path, "a") as f:
            f.write(line)
"""

from __future__ import annotations

import errno
import logging
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


class FileLockError(OSError):
    """Raised when a file lock cannot be acquired within the timeout."""


class FileLockUnsupportedError(OSError):
    """Raised when neither fcntl (POSIX) nor msvcrt (Windows) is available."""


_USE_FCNTL = sys.platform != "win32"

_fcntl = None  # type: ignore[assignment]
_msvcrt = None  # type: ignore[assignment]

if _USE_FCNTL:
    try:
        import fcntl as _fcntl_module

        _fcntl = _fcntl_module
    except ImportError:
        pass
else:
    try:
        import msvcrt as _msvcrt_module

        _msvcrt = _msvcrt_module
    except ImportError:
        pass


@contextmanager
def file_lock(
    path: Path,
    *,
    exclusive: bool = True,
    timeout: float = 5.0,
    poll_interval: float = 0.05,
) -> Iterator[None]:
    """Acquire an advisory lock on *path* for the duration of the block.

    *path* may be the file you intend to write OR a sentinel like
    ``foo.lock``. Either works — the lock is on the open file descriptor.

    Args:
        path: File path to lock. Created with mode 0o600 if absent.
        exclusive: When True (default) acquire ``LOCK_EX``. When False,
            ``LOCK_SH`` (POSIX only — Windows is always exclusive).
        timeout: Maximum seconds to wait. ``FileLockError`` on expiry.
        poll_interval: Sleep between non-blocking retries.

    Yields:
        None. The with-block runs holding the lock.

    Raises:
        FileLockError: lock not acquired within *timeout*.
        FileLockUnsupportedError: neither fcntl nor msvcrt available.
        OSError: open() / lock() errors unrelated to contention.
    """
    if _USE_FCNTL:
        if _fcntl is None:
            raise FileLockUnsupportedError(
                "fcntl is unavailable; cross-process file locking not supported"
            )
        with _flock_posix(
            path, exclusive=exclusive, timeout=timeout, poll_interval=poll_interval
        ):
            yield
    else:
        if _msvcrt is None:
            raise FileLockUnsupportedError(
                "msvcrt is unavailable; cross-process file locking not supported"
            )
        with _flock_windows(
            path, exclusive=exclusive, timeout=timeout, poll_interval=poll_interval
        ):
            yield


@contextmanager
def _flock_posix(
    path: Path,
    *,
    exclusive: bool,
    timeout: float,
    poll_interval: float,
) -> Iterator[None]:
    assert _fcntl is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    op = (_fcntl.LOCK_EX if exclusive else _fcntl.LOCK_SH) | _fcntl.LOCK_NB
    deadline = time.monotonic() + max(timeout, 0.0)
    # ``fd = os.open(...)`` MUST happen inside the ``try`` block so a
    # BaseException (KeyboardInterrupt, SystemExit) raised between the
    # syscall returning and the assignment binding cannot leak the
    # descriptor. The ``fd = -1`` sentinel lets the finally distinguish
    # "open succeeded → close needed" from "open raised → nothing to
    # close" while still keeping the close path on every exit.
    fd = -1
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
        # ``os.open`` mode bits are subject to the process umask, so a
        # caller-side umask of 0o200 would create the lock file as
        # 0o400 and every subsequent O_RDWR open would EACCES. Force
        # the mode explicitly so the lock primitive is umask-
        # independent and the 0o600 invariant is honoured on first
        # creation.
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass
        while True:
            try:
                _fcntl.flock(fd, op)
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    raise FileLockError(
                        f"timeout acquiring lock on {path} after {timeout:.3f}s"
                    ) from e
                time.sleep(poll_interval)
        try:
            yield
        finally:
            try:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass


_WINDOWS_CONTENTION_ERRNOS = frozenset(
    {
        errno.EACCES,
        # Windows CRT exposes ``errno.EDEADLOCK`` (errno 36); POSIX-emulating
        # builds (Cygwin / MinGW) may expose ``errno.EDEADLK`` instead. Both
        # represent "lock acquisition failed after the platform's internal
        # retries" and should be treated as contention. Fall back to EACCES
        # on platforms that expose neither — the EACCES retry already
        # handles the same observable state.
        getattr(errno, "EDEADLOCK", getattr(errno, "EDEADLK", errno.EACCES)),
    }
)


@contextmanager
def _flock_windows(
    path: Path,
    *,
    exclusive: bool,
    timeout: float,
    poll_interval: float,
) -> Iterator[None]:
    # msvcrt always locks a byte range exclusively. The ``exclusive`` flag is
    # accepted for API parity but cannot grant shared semantics on Windows.
    #
    # Microsoft ``_locking()`` raises OSError with errno EACCES when the
    # range is held by another process and EDEADLOCK after 10 internal
    # retries fail. Other errnos (EBADF, EINVAL, ENOLCK) signal non-
    # contention failures and must surface immediately rather than
    # busy-wait the full timeout.
    del exclusive
    assert _msvcrt is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(timeout, 0.0)
    # ``fd = os.open(...)`` MUST happen inside the ``try`` block so a
    # BaseException raised between the syscall returning and the
    # assignment binding cannot leak the descriptor. ``fd = -1``
    # sentinel pattern; see POSIX path for full reasoning.
    fd = -1
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
        # Same umask-independence reasoning as the POSIX path. Windows'
        # NTFS permission semantics are different from POSIX, but the
        # 0o600 mode is still honoured by ``os.chmod`` on Cygwin / WSL
        # and is a no-op on native Windows.
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass
        while True:
            try:
                # ``LK_NBLCK`` targets the byte range starting at the
                # current file-position pointer. Symmetric with the
                # unlock seek below: lock and unlock must both reference
                # offset 0 so a future read inside the with-block that
                # advances the pointer cannot break a retry.
                os.lseek(fd, 0, os.SEEK_SET)
                _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
                break
            except OSError as e:
                if e.errno not in _WINDOWS_CONTENTION_ERRNOS:
                    raise
                if time.monotonic() >= deadline:
                    raise FileLockError(
                        f"timeout acquiring lock on {path} after {timeout:.3f}s"
                    ) from e
                time.sleep(poll_interval)
        try:
            yield
        finally:
            try:
                # ``LK_UNLCK`` targets the byte range starting at the
                # current file-position pointer. Acquire pinned offset 0
                # (the fd was just opened); if the with-block ever
                # advanced the offset, unlock would silently target the
                # wrong byte and the lock would persist until fd close.
                # Defensive seek keeps the invariant crash-proof.
                os.lseek(fd, 0, os.SEEK_SET)
                _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass


__all__ = ["file_lock", "FileLockError", "FileLockUnsupportedError"]
