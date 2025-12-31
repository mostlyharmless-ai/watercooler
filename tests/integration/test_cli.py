"""CLI smoke tests - verify all commands exist and run without errors.

This test suite provides basic sanity checks that all CLI commands:
1. Are properly registered in the argument parser
2. Execute without crashing or returning error codes
3. Produce expected output formats

These are "smoke tests" - they verify commands work at a basic level,
while dedicated test files (test_cli_*.py) provide deeper testing of
each command's functionality.

Coverage:
- All 12 CLI commands (init-thread, append-entry, say, ack, handoff,
  set-status, set-ball, list, reindex, search, web-export, unlock)
- Help text generation
- Basic output format validation
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


TEST_THREADS_DIR = Path(__file__).parent / ".cli-threads"
TEST_THREADS_DIR.mkdir(exist_ok=True)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run watercooler CLI command and capture output.

    Args:
        *args: Command-line arguments to pass to watercooler

    Returns:
        CompletedProcess with stdout, stderr, and returncode
    """
    env = os.environ.copy()
    env["WATERCOOLER_DIR"] = str(TEST_THREADS_DIR)
    env.setdefault("WATERCOOLER_AUTO_BRANCH", "0")
    return subprocess.run(
        [sys.executable, "-m", "watercooler.cli", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_help_exits_zero():
    """Verify --help flag works and displays usage information."""
    cp = _run("--help")
    assert cp.returncode == 0
    assert "usage:" in cp.stdout.lower()


def test_all_commands_exist_and_exit_zero():
    """Smoke test: verify all CLI commands execute successfully.

    This test ensures that all 12 watercooler commands:
    1. Are registered in the CLI parser (don't produce "unknown command" errors)
    2. Execute with exit code 0 (no crashes or exceptions)
    3. Produce expected output patterns based on command type

    Commands tested:
    - Thread management: init-thread
    - Entry creation: append-entry, say, ack, handoff
    - Metadata updates: set-status, set-ball
    - Querying: list, search, reindex
    - Export: web-export
    - Debugging: unlock

    Note: This uses default threads directory (./watercooler) which is created
    in the working directory during test execution. Each command gets minimal
    required arguments to execute successfully.
    """
    # Test all CLI commands with minimal required arguments
    # Updated to include handoff and unlock commands for 100% coverage
    for cmd in [
        "init-thread topic",  # Creates thread file
        "append-entry topic --agent codex --role implementer --title Test --body note",  # Adds entry
        "say topic --title Note --body note",  # Quick note with auto ball-flip
        "ack topic",  # Acknowledge without ball flip
        "handoff topic --note test",  # Hand off to counterpart agent
        "set-status topic open",  # Update thread status
        "set-ball topic codex",  # Update ball ownership
        "reindex",  # Generate markdown index
        "list",  # List all threads
        "web-export",  # Generate HTML index
        "search roadmap",  # Search thread contents
        "unlock topic",  # Remove advisory lock (debugging)
    ]:
        parts = cmd.split()
        cp = _run(*parts)
        assert cp.returncode == 0, (cmd, cp.stdout, cp.stderr)

        # Verify expected output patterns based on command type
        if parts[0] in {"init-thread", "append-entry", "set-status", "set-ball", "say", "ack", "handoff"}:
            # Commands that modify thread files should print the .md file path
            assert ".md" in cp.stdout
        elif parts[0] == "list":
            # List command emits tab-separated rows; just ensure output is present
            assert len(cp.stdout.strip()) > 0
        elif parts[0] == "reindex":
            # Reindex command prints path to generated index.md file
            assert cp.stdout.strip().endswith("index.md")
        else:
            # Other commands (web-export, search, unlock) just verify they run
            assert cp.returncode == 0
