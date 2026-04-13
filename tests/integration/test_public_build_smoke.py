"""Public-build smoke tests.

Verifies that the open-core build (without watercooler_memory installed) does
not crash or expose private package names to users.

These tests invoke the CLI as subprocesses so they exercise the real import
chain rather than mocked imports.  Mock-based tests miss transitive import
failures that only surface at import time.

Run this file explicitly:
    pytest tests/integration/test_public_build_smoke.py

It is also wired into the `public-build-verify` CI job which installs from
pyproject.public.toml without the memory extras.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys

import pytest


def _run_watercooler(*args: str) -> subprocess.CompletedProcess:
    """Run the watercooler CLI as a subprocess and return the result."""
    return subprocess.run(
        [sys.executable, "-m", "watercooler.cli", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


_MEMORY_INSTALLED = importlib.util.find_spec("watercooler_memory") is not None


@pytest.mark.skipif(
    _MEMORY_INSTALLED,
    reason="watercooler_memory is installed; test is for open-core build only",
)
class TestPublicBuildSmoke:
    """Smoke tests that run when watercooler_memory is NOT installed."""

    def test_help_exits_cleanly(self):
        """watercooler --help must exit 0 without ImportError."""
        result = _run_watercooler("--help")
        assert result.returncode == 0, (
            f"watercooler --help failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "ImportError" not in result.stderr
        assert "watercooler-cloud[memory]" not in result.stderr

    def test_memory_build_fails_with_clear_message(self):
        """watercooler memory build must produce a helpful message, not a traceback."""
        result = _run_watercooler("memory", "build")
        # SystemExit(message) exits non-zero
        assert result.returncode != 0, (
            "Expected non-zero exit for memory build without watercooler_memory"
        )
        combined = result.stdout + result.stderr
        assert "Traceback" not in combined, (
            f"Unexpected traceback:\n{combined}"
        )
        assert "ImportError" not in combined or "open-core" in combined.lower(), (
            f"Unexpected bare ImportError without helpful context:\n{combined}"
        )
        # Must emit the expected helpful message
        assert "open-core edition" in combined.lower(), (
            f"Expected 'open-core edition' in output:\n{combined}"
        )
        # Must not leak the private package name as an install hint
        assert "watercooler-cloud[memory]" not in combined, (
            f"Leaked private package name in error message:\n{combined}"
        )

    def test_memory_export_fails_with_clear_message(self):
        """watercooler memory export must produce a helpful message, not a traceback."""
        # Path is irrelevant — CLI exits before writing when memory package is absent.
        result = _run_watercooler("memory", "export", "--output", "/nonexistent/test_export.json")
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "Traceback" not in combined
        assert "open-core edition" in combined.lower(), (
            f"Expected 'open-core edition' in output:\n{combined}"
        )
        assert "watercooler-cloud[memory]" not in combined

    def test_memory_stats_fails_with_clear_message(self):
        """watercooler memory stats must produce a helpful message, not a traceback."""
        result = _run_watercooler("memory", "stats")
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "Traceback" not in combined
        assert "open-core edition" in combined.lower(), (
            f"Expected 'open-core edition' in output:\n{combined}"
        )
        assert "watercooler-cloud[memory]" not in combined
