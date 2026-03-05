from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run_cli(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "watercooler.cli", *args], capture_output=True, text=True, cwd=cwd)


def test_say_and_ack(tmp_path: Path):
    # init
    cp = run_cli("init-thread", "topic3", "--threads-dir", str(tmp_path))
    assert cp.returncode == 0
    # say - Phase 2: requires --title
    cp = run_cli("say", "topic3", "--threads-dir", str(tmp_path), "--agent", "dev", "--title", "Update", "--body", "Heads-up")
    assert cp.returncode == 0
    s = (tmp_path / "topic3.md").read_text(encoding="utf-8")
    assert "Heads-up" in s
    # ack - Phase 2: --author → --agent
    cp = run_cli("ack", "topic3", "--threads-dir", str(tmp_path), "--agent", "dev")
    assert cp.returncode == 0
    s = (tmp_path / "topic3.md").read_text(encoding="utf-8")
    assert "ack" in s.lower()


def test_handoff(tmp_path: Path):
    # init with ball=codex
    cp = run_cli("init-thread", "topic4", "--threads-dir", str(tmp_path), "--ball", "codex")
    assert cp.returncode == 0
    s = (tmp_path / "topic4.md").read_text(encoding="utf-8")
    assert "codex" in s.lower()
    # handoff flips ball to counterpart (Claude by default)
    # Phase 2: --author → --agent
    cp = run_cli("handoff", "topic4", "--threads-dir", str(tmp_path), "--agent", "codex", "--note", "your turn")
    assert cp.returncode == 0
    s = (tmp_path / "topic4.md").read_text(encoding="utf-8")
    # Ball should be "Claude" (capitalized)
    assert "Ball: Claude" in s or "Ball: claude" in s
    assert "your turn" in s

