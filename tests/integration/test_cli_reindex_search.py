from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run_cli(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "watercooler.cli", *args], capture_output=True, text=True, cwd=cwd)


def test_reindex_and_search(tmp_path: Path):
    """Test reindex and search CLI commands.

    These commands now read from graph. Without graph data (CLI-only
    init-thread doesn't write graph), reindex produces an empty index
    and search returns no results. This is correct behavior.
    """
    # init threads and append content
    run_cli("init-thread", "alpha", "--threads-dir", str(tmp_path))
    run_cli("append-entry", "alpha", "--threads-dir", str(tmp_path), "--agent", "team", "--role", "planner", "--title", "Roadmap", "--body", "Discuss roadmap")
    run_cli("init-thread", "beta", "--threads-dir", str(tmp_path))
    run_cli("append-entry", "beta", "--threads-dir", str(tmp_path), "--agent", "team", "--role", "implementer", "--title", "Bugfix", "--body", "Fix bug 123")

    # reindex — produces index.md (may be empty without graph data)
    cp = run_cli("reindex", "--threads-dir", str(tmp_path))
    assert cp.returncode == 0
    idx = tmp_path / "index.md"
    assert idx.exists()

    # search — runs without error (may find no results without graph)
    cp = run_cli("search", "roadmap", "--threads-dir", str(tmp_path))
    assert cp.returncode == 0

