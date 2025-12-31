from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run_cli(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "watercooler.cli", *args], capture_output=True, text=True, cwd=cwd)


def test_handoff_flips_ball(tmp_path: Path):
    cp = run_cli("init-thread", "handoff1", "--threads-dir", str(tmp_path), "--ball", "codex")
    assert cp.returncode == 0
    # handoff should flip ball to counterpart of codex (Claude per default mapping)
    # Updated for Phase 2: --author → --agent
    cp = run_cli("handoff", "handoff1", "--threads-dir", str(tmp_path), "--agent", "codex", "--note", "please take a look")
    assert cp.returncode == 0
    s = (tmp_path / "handoff1.md").read_text(encoding="utf-8")
    # Ball should be "Claude" (capitalized, may include user tag)
    assert "Ball: Claude" in s or "Ball: claude" in s
    assert "please take a look" in s


def test_handoff_initialises_missing_thread(tmp_path: Path):
    cp = run_cli("handoff", "brand-new-topic", "--threads-dir", str(tmp_path), "--agent", "codex")
    assert cp.returncode == 0
    content = (tmp_path / "brand-new-topic.md").read_text(encoding="utf-8")
    assert "Handoff to Claude" in content
    assert "Ball: Claude" in content or "Ball: claude" in content
