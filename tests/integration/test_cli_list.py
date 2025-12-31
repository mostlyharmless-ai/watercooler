from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _relative_threads_dir() -> str:
    """Return cross-platform relative path from repo dir to threads dir."""
    return str(Path("..") / "threads")


def run_cli(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "watercooler.cli", *args], capture_output=True, text=True, cwd=cwd)


def test_list_shows_threads(tmp_path: Path):
    # init a couple threads
    for t in ("alpha", "beta"):
        cp = run_cli("init-thread", t, "--threads-dir", str(tmp_path))
        assert cp.returncode == 0
    cp = run_cli("list", "--threads-dir", str(tmp_path))
    assert cp.returncode == 0
    lines = [ln for ln in cp.stdout.splitlines() if ln.strip()]
    assert len(lines) >= 2
    # columns: updated, status, ball, NEW, title, path
    cols = lines[0].split("\t")
    assert len(cols) == 6
    assert cols[1] in {"open", "in-progress", "done"}


def test_list_with_relative_threads_dir(tmp_path: Path):
    code_dir = tmp_path / "code"
    threads_dir = tmp_path / "threads"
    code_dir.mkdir()
    threads_dir.mkdir()

    rel = _relative_threads_dir()

    cp = run_cli("init-thread", "alpha", "--threads-dir", rel, cwd=str(code_dir))
    assert cp.returncode == 0

    cp = run_cli("list", "--threads-dir", rel, cwd=str(code_dir))
    assert cp.returncode == 0
    assert "alpha" in cp.stdout
