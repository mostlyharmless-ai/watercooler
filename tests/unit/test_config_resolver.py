import os
import shutil
import subprocess
from pathlib import Path

import pytest

from watercooler_mcp.config import resolve_thread_context


def _git_available() -> bool:
    return shutil.which("git") is not None


def test_resolve_thread_context_explicit_dir(tmp_path, monkeypatch):
    explicit_dir = tmp_path / ".watercooler"
    monkeypatch.setenv("WATERCOOLER_DIR", str(explicit_dir))
    monkeypatch.delenv("WATERCOOLER_THREADS_BASE", raising=False)
    monkeypatch.delenv("WATERCOOLER_THREADS_PATTERN", raising=False)
    monkeypatch.delenv("WATERCOOLER_GIT_REPO", raising=False)
    monkeypatch.delenv("WATERCOOLER_CODE_REPO", raising=False)

    context = resolve_thread_context()

    assert context.explicit_dir is True
    assert context.threads_dir == explicit_dir
