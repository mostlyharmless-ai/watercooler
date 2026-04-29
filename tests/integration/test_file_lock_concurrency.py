"""Concurrency stress tests for ``watercooler_mcp.sync.file_lock``.

The file-lock primitive must serialise cross-process writes to a shared
file. The integration test spawns N worker processes each appending K
JSON lines under ``file_lock``; the post-condition is that the line
count equals N×K and every line parses as valid JSON.

Without the lock, concurrent appends on the same file produce torn
writes — partial lines from one writer interleave with another, and
``json.loads`` fails on those lines. This test fails today (without
file_lock) and passes after.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import pytest

from watercooler_mcp.sync.file_lock import (
    FileLockError,
    FileLockUnsupportedError,
    file_lock,
)


def _worker_append(target: str, lock: str, worker_id: int, n_writes: int) -> None:
    """Worker process: append n_writes lines to target under file_lock."""
    target_path = Path(target)
    lock_path = Path(lock)
    for i in range(n_writes):
        with file_lock(lock_path, timeout=30.0):
            with open(target_path, "a") as f:
                payload = json.dumps({"worker": worker_id, "i": i})
                # Build a deliberately long line so an interleaved write
                # without a lock would be detectable by truncated JSON.
                f.write(payload + " " * 256 + "\n")
                f.flush()
                os.fsync(f.fileno())


def test_concurrent_writers_produce_no_interleaved_lines(tmp_path: Path) -> None:
    """N processes appending K lines each → exactly N×K valid JSON lines."""
    target = tmp_path / "writes.jsonl"
    lock = tmp_path / "writes.lock"
    n_workers = 8
    n_writes = 50

    procs = [
        mp.Process(target=_worker_append, args=(str(target), str(lock), w, n_writes))
        for w in range(n_workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
        assert p.exitcode == 0, f"worker exited with {p.exitcode}"

    lines = target.read_text().splitlines()
    assert (
        len(lines) == n_workers * n_writes
    ), f"expected {n_workers * n_writes} lines, got {len(lines)}"

    seen: set[tuple[int, int]] = set()
    for line in lines:
        data = json.loads(line)
        assert {"worker", "i"} <= set(data.keys())
        seen.add((data["worker"], data["i"]))
    assert len(seen) == n_workers * n_writes


def _worker_hold_lock(lock: str, hold_s: float, ready_path: str) -> None:
    """Worker that holds the lock for hold_s seconds, signalling readiness."""
    with file_lock(Path(lock), timeout=5.0):
        Path(ready_path).touch()
        time.sleep(hold_s)


def test_timeout_raises_filelockerror(tmp_path: Path) -> None:
    """A second acquirer with a short timeout fails fast while the lock is held."""
    lock = tmp_path / "exclusive.lock"
    ready = tmp_path / "ready.flag"

    holder = mp.Process(target=_worker_hold_lock, args=(str(lock), 2.0, str(ready)))
    holder.start()
    try:
        deadline = time.monotonic() + 5.0
        while not ready.exists():
            if time.monotonic() > deadline:
                pytest.fail("holder process never acquired the lock")
            time.sleep(0.05)

        with pytest.raises(FileLockError):
            with file_lock(lock, timeout=0.1):
                pytest.fail("should not have acquired lock while held")
    finally:
        holder.join(timeout=10)


def test_lock_release_on_exception(tmp_path: Path) -> None:
    """Lock is released when the with-block raises."""
    lock = tmp_path / "release.lock"
    with pytest.raises(RuntimeError):
        with file_lock(lock):
            raise RuntimeError("boom")
    # Re-acquire immediately — no lingering hold.
    with file_lock(lock, timeout=0.5):
        pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only mode bits")
def test_lock_file_created_with_0600_permissions(tmp_path: Path) -> None:
    """Lock file is created with restrictive permissions on POSIX."""
    lock = tmp_path / "perms.lock"
    with file_lock(lock):
        assert lock.exists()
        mode = lock.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


@pytest.mark.skipif(sys.platform == "win32", reason="umask is POSIX-specific")
def test_lock_file_perms_independent_of_umask(tmp_path: Path) -> None:
    """The lock primitive must produce 0o600 even under a hostile umask.

    ``os.open`` mode bits are masked by the calling process umask. A
    umask of ``0o277`` would otherwise create the lock as ``0o400``,
    causing every subsequent O_RDWR open from another process to
    fail with EACCES — turning a contention retry into a hard
    OSError. The primitive applies an explicit ``os.chmod`` after
    open to keep the 0o600 invariant umask-independent.
    """
    import os as _os

    lock = tmp_path / "umask_perms.lock"
    saved_umask = _os.umask(0o277)
    try:
        with file_lock(lock):
            assert lock.exists()
            mode = lock.stat().st_mode & 0o777
            assert mode == 0o600, f"expected 0o600 under umask 0o277, got {oct(mode)}"
    finally:
        _os.umask(saved_umask)


def test_filelockerror_is_oserror(tmp_path: Path) -> None:
    """FileLockError inherits OSError so callers catching OSError still work."""
    assert issubclass(FileLockError, OSError)
    assert issubclass(FileLockUnsupportedError, OSError)


def _worker_lockless_append(target: str, worker_id: int, n_writes: int) -> None:
    """Worker that writes WITHOUT the lock — used to demonstrate the failure."""
    target_path = Path(target)
    for i in range(n_writes):
        with open(target_path, "a") as f:
            payload = json.dumps({"worker": worker_id, "i": i})
            f.write(payload + " " * 256 + "\n")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="lockless interleaving demo is POSIX-specific",
)
def test_baseline_lockless_produces_torn_writes_or_loss(tmp_path: Path) -> None:
    """Demonstrates the failure mode this primitive is preventing.

    Two-fold pre-condition for the file_lock test above:
      (a) the locked path produces N×K parseable lines.
      (b) the unlocked path can lose lines or produce invalid JSON.

    Marked as a sanity check rather than a strict assertion — kernel
    write buffering may still happen to land cleanly under load. We
    simply assert the locked variant ALWAYS holds, which is what the
    primary test guarantees.
    """
    target = tmp_path / "baseline.jsonl"
    n_workers = 8
    n_writes = 50

    procs = [
        mp.Process(target=_worker_lockless_append, args=(str(target), w, n_writes))
        for w in range(n_workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0

    # Best-effort check; not a strict test invariant. The strict
    # invariant is established by the locked counterpart above.
    lines = target.read_text().splitlines()
    parseable = 0
    for line in lines:
        try:
            json.loads(line)
            parseable += 1
        except json.JSONDecodeError:
            pass
    # Document the actual outcome for future debugging without failing
    # the suite when the kernel happened to serialise our small writes.
    print(
        f"baseline lockless: {len(lines)} lines, {parseable} parseable, "
        f"expected {n_workers * n_writes}"
    )
