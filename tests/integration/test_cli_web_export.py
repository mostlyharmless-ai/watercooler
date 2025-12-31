from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run_cli(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "watercooler.cli", *args], capture_output=True, text=True, cwd=cwd)


def test_web_export_generates_html(tmp_path: Path):
    run_cli("init-thread", "web1", "--threads-dir", str(tmp_path))
    cp = run_cli("web-export", "--threads-dir", str(tmp_path))
    assert cp.returncode == 0
    out = tmp_path / "index.html"
    assert out.exists()
    html = out.read_text(encoding="utf-8").lower()
    assert "<table" in html and "web1.md" in html

