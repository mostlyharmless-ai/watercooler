from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run_cli(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "watercooler.cli", *args], capture_output=True, text=True, cwd=cwd)


def test_set_status_and_ball(tmp_path: Path):
    # init
    cp = run_cli("init-thread", "topic1", "--threads-dir", str(tmp_path))
    assert cp.returncode == 0
    # set-status
    cp = run_cli("set-status", "topic1", "in-progress", "--threads-dir", str(tmp_path))
    assert cp.returncode == 0
    s = (tmp_path / "topic1.md").read_text(encoding="utf-8")
    assert "Status: IN-PROGRESS" in s
    # set-ball
    cp = run_cli("set-ball", "topic1", "claude", "--threads-dir", str(tmp_path))
    assert cp.returncode == 0
    s = (tmp_path / "topic1.md").read_text(encoding="utf-8")
    assert "Ball: claude" in s


def test_set_status_nonexistent_thread(tmp_path: Path):
    # Attempting to set status on nonexistent thread should fail
    cp = run_cli("set-status", "does-not-exist", "CLOSED", "--threads-dir", str(tmp_path))
    assert cp.returncode != 0
    assert "not found" in cp.stderr.lower() or "not found" in cp.stdout.lower()
    # Thread file should not be created
    assert not (tmp_path / "does-not-exist.md").exists()


def test_append_entry(tmp_path: Path):
    # init thread
    cp = run_cli("init-thread", "topic2", "--threads-dir", str(tmp_path))
    assert cp.returncode == 0
    # append - Phase 2: requires --agent, --role, --title
    cp = run_cli(
        "append-entry",
        "topic2",
        "--threads-dir",
        str(tmp_path),
        "--agent",
        "dev",
        "--role",
        "implementer",
        "--title",
        "Completed task",
        "--body",
        "Did the thing",
        "--status",
        "in-review",
    )
    assert cp.returncode == 0
    s = (tmp_path / "topic2.md").read_text(encoding="utf-8")
    assert "in-review" in s
    assert "Did the thing" in s
