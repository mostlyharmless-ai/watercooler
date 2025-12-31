from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run_cli(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "watercooler.cli", *args], capture_output=True, text=True, cwd=cwd)


def test_reindex_and_search(tmp_path: Path):
    # init threads and append content
    # Phase 2: append-entry requires --agent, --role, --title
    run_cli("init-thread", "alpha", "--threads-dir", str(tmp_path))
    run_cli("append-entry", "alpha", "--threads-dir", str(tmp_path), "--agent", "team", "--role", "planner", "--title", "Roadmap", "--body", "Discuss roadmap")
    run_cli("init-thread", "beta", "--threads-dir", str(tmp_path))
    run_cli("append-entry", "beta", "--threads-dir", str(tmp_path), "--agent", "team", "--role", "implementer", "--title", "Bugfix", "--body", "Fix bug 123")

    # reindex
    cp = run_cli("reindex", "--threads-dir", str(tmp_path))
    assert cp.returncode == 0
    idx = tmp_path / "index.md"
    assert idx.exists()
    txt = idx.read_text(encoding="utf-8")
    assert "alpha.md" in txt and "beta.md" in txt

    # search
    cp = run_cli("search", "roadmap", "--threads-dir", str(tmp_path))
    assert cp.returncode == 0
    assert ":" in cp.stdout and "alpha.md" in cp.stdout

