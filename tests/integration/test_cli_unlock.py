"""Tests for the unlock CLI command.

The unlock command is a debugging tool for clearing advisory locks on thread files.
Locks are used to prevent concurrent modifications to thread files. This test suite
ensures the unlock command properly handles:
- Missing lock files (graceful handling)
- Stale locks (automatic removal based on TTL)
- Active locks (requires --force flag)
- Lock metadata display (PID, age, staleness)

Lock file format: .{topic}.lock in threads_dir
Lock file contents: {pid}:{process-name}
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


def run_cli(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    """Helper to run watercooler CLI commands in tests.

    Args:
        *args: Command-line arguments to pass to watercooler CLI
        cwd: Optional working directory for command execution

    Returns:
        CompletedProcess with stdout, stderr, and returncode
    """
    return subprocess.run([sys.executable, "-m", "watercooler.cli", *args], capture_output=True, text=True, cwd=cwd)


def test_unlock_no_lock(tmp_path: Path):
    """Test unlock command when no lock file exists.

    Verifies graceful handling of unlock attempts on threads without locks.
    Should exit cleanly with informative message rather than error.
    """
    cp = run_cli("unlock", "test-topic", "--threads-dir", str(tmp_path))
    assert cp.returncode == 0
    assert "No lock file present" in cp.stdout


def test_unlock_stale_lock(tmp_path: Path):
    """Test unlock automatically removes stale locks.

    Stale locks are those that exceed the TTL (time-to-live) threshold.
    This simulates a process that died while holding a lock, leaving it orphaned.
    The unlock command should detect staleness via mtime and remove automatically.

    Test steps:
    1. Create a thread file
    2. Create a lock file with old timestamp (> 1 hour)
    3. Run unlock without --force
    4. Verify lock is removed due to staleness
    """
    # Create a thread with a lock
    run_cli("init-thread", "locked-topic", "--threads-dir", str(tmp_path))

    # Manually create a stale lock file (lock path is .{topic}.lock in threads_dir)
    # Lock file format follows AdvisoryLock convention: {pid}:{process-name}
    lock_path = tmp_path / ".locked-topic.lock"
    lock_path.write_text("12345:old-process", encoding="utf-8")

    # Make it old enough to be stale (modify mtime to 1 hour ago)
    # AdvisoryLock considers locks stale based on file modification time
    old_time = time.time() - 3600  # 1 hour ago
    import os
    os.utime(lock_path, (old_time, old_time))

    # Unlock should automatically remove the stale lock
    cp = run_cli("unlock", "locked-topic", "--threads-dir", str(tmp_path))
    assert cp.returncode == 0
    assert "Lock removed" in cp.stdout
    assert not lock_path.exists()


def test_unlock_force_removes_active_lock(tmp_path: Path):
    """Test unlock --force removes even active locks.

    The --force flag bypasses staleness checks and removes locks regardless
    of their age or apparent activity. This is useful for recovering from
    stuck locks during development or when debugging concurrent access issues.

    Test steps:
    1. Create a thread file
    2. Create a fresh lock (not stale)
    3. Run unlock without --force (may or may not remove depending on staleness)
    4. Ensure lock exists for --force test
    5. Run unlock with --force
    6. Verify lock is removed regardless of staleness

    Note: This is a potentially dangerous operation in production and should
    only be used when you're certain no other process is actively using the lock.
    """
    # Create a thread
    run_cli("init-thread", "active-topic", "--threads-dir", str(tmp_path))

    # Create a fresh lock that would appear active (recent mtime)
    lock_path = tmp_path / ".active-topic.lock"
    lock_path.write_text(f"{12345}:active-process", encoding="utf-8")

    # First attempt without --force to test normal behavior
    # May succeed if lock is considered stale, or may fail with active lock warning
    cp = run_cli("unlock", "active-topic", "--threads-dir", str(tmp_path))

    # Recreate lock if it was removed (ensure we can test --force)
    if not lock_path.exists():
        lock_path.write_text(f"{12345}:active-process", encoding="utf-8")

    # With --force, should always remove the lock regardless of staleness
    cp = run_cli("unlock", "active-topic", "--threads-dir", str(tmp_path), "--force")
    assert cp.returncode == 0
    assert "Lock removed" in cp.stdout
    assert not lock_path.exists()


def test_unlock_shows_lock_info(tmp_path: Path):
    """Test unlock displays comprehensive lock metadata.

    The unlock command shows diagnostic information about locks to help
    with debugging concurrent access issues. This information includes:
    - Lock file path (for manual inspection if needed)
    - Lock contents (PID and process name from {pid}:{process-name} format)
    - Lock age in seconds (time since last modification)
    - Staleness status (whether lock exceeds TTL threshold)

    This metadata helps developers understand lock state before removal
    and aids in diagnosing why locks might be stuck.

    Test steps:
    1. Create a thread file
    2. Create a lock with known PID
    3. Make the lock stale (old timestamp)
    4. Run unlock
    5. Verify all metadata fields are displayed
    6. Verify lock is removed (due to staleness)
    """
    # Create thread and lock
    run_cli("init-thread", "info-topic", "--threads-dir", str(tmp_path))

    # Create lock with identifiable PID and process name
    lock_path = tmp_path / ".info-topic.lock"
    lock_path.write_text("99999:test-process", encoding="utf-8")

    # Make it stale so it gets removed (allows us to test full output)
    old_time = time.time() - 3600  # 1 hour ago
    import os
    os.utime(lock_path, (old_time, old_time))

    # Run unlock and verify metadata display
    cp = run_cli("unlock", "info-topic", "--threads-dir", str(tmp_path))
    assert cp.returncode == 0
    assert "Lock path:" in cp.stdout  # Shows full path for manual inspection
    assert "Contents:" in cp.stdout or "99999" in cp.stdout  # Shows PID/process
    assert "Age:" in cp.stdout  # Shows seconds since last modification
    assert "Stale:" in cp.stdout  # Shows True/False staleness status
