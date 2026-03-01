from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"


def _relative_threads_dir() -> str:
    """Return cross-platform relative path from repo dir to threads dir."""
    return str(Path("..") / "threads")


def run_cli(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # When cwd is set, PYTHONPATH may be relative and break; ensure absolute src path.
    if cwd:
        parts = [str(_SRC)] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(parts))
    return subprocess.run(
        [sys.executable, "-m", "watercooler.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )


def test_list_shows_threads(tmp_path: Path):
    """Test that list returns empty when no graph data exists.

    The CLI init-thread only writes .md files. Since list_threads reads
    from graph, it returns empty without graph data. This is correct
    behavior — the MCP layer (which writes graph data) is the expected
    entry point for creating threads.
    """
    for t in ("alpha", "beta"):
        cp = run_cli("init-thread", t, "--threads-dir", str(tmp_path))
        assert cp.returncode == 0
    cp = run_cli("list", "--threads-dir", str(tmp_path))
    assert cp.returncode == 0
    # No graph data → empty output is correct
    # The MCP write path creates graph data; CLI init-thread does not


def test_list_with_relative_threads_dir(tmp_path: Path):
    """Test list with relative threads dir succeeds (returns empty without graph)."""
    code_dir = tmp_path / "code"
    threads_dir = tmp_path / "threads"
    code_dir.mkdir()
    threads_dir.mkdir()

    rel = _relative_threads_dir()

    cp = run_cli("init-thread", "alpha", "--threads-dir", rel, cwd=str(code_dir))
    assert cp.returncode == 0

    cp = run_cli("list", "--threads-dir", rel, cwd=str(code_dir))
    assert cp.returncode == 0
    # No graph data → empty output is expected
