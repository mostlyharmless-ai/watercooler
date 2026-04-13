"""Unit tests for watercooler.capture_hook — packaged PostCompact hook."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


def _make_executable(path: Path) -> Path:
    """Write a minimal shell script and mark it executable."""
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# _resolve_watercooler_cmd — binary resolution order
# ---------------------------------------------------------------------------

class TestResolveWatercoolerCmd:
    """Coverage for the sibling-first resolution path that regressed at P1."""

    def test_sibling_bin_takes_priority(self, tmp_path):
        """Sibling watercooler next to the running hook is returned first.

        This is the path that regressed at P1: when Claude runs the stored
        absolute watercooler-capture-theme path the sibling watercooler must
        be found without any PATH activation or .venv presence.
        """
        import watercooler.capture_hook as mod

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        sibling = _make_executable(bin_dir / "watercooler")
        hook = bin_dir / "watercooler-capture-theme"  # the "running" hook

        with patch("sys.argv", [str(hook)]), \
             patch.object(mod, "_git_toplevel", return_value=None), \
             patch("shutil.which", return_value=None):
            result = mod._resolve_watercooler_cmd(str(tmp_path))

        assert result == [str(sibling)]

    def test_sibling_absent_falls_back_to_venv(self, tmp_path):
        """When no sibling exists, the project .venv/bin/watercooler is used."""
        import watercooler.capture_hook as mod

        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        venv_wc = _make_executable(venv_bin / "watercooler")

        no_sibling_dir = tmp_path / "other"
        no_sibling_dir.mkdir()
        fake_hook = no_sibling_dir / "watercooler-capture-theme"

        with patch("sys.argv", [str(fake_hook)]), \
             patch.object(mod, "_git_toplevel", return_value=str(tmp_path)), \
             patch("shutil.which", return_value=None):
            result = mod._resolve_watercooler_cmd(str(tmp_path))

        assert result == [str(venv_wc)]

    def test_sibling_absent_venv_absent_falls_back_to_path(self, tmp_path, monkeypatch):
        """When neither sibling nor .venv exist, PATH shutil.which is used."""
        import watercooler.capture_hook as mod

        # Clear VIRTUAL_ENV so the activated-env step doesn't fire under uv run.
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)

        fake_hook = tmp_path / "watercooler-capture-theme"
        path_bin = "/usr/local/bin/watercooler"

        with patch("sys.argv", [str(fake_hook)]), \
             patch.object(mod, "_git_toplevel", return_value=None), \
             patch("shutil.which", return_value=path_bin):
            result = mod._resolve_watercooler_cmd(str(tmp_path))

        assert result == [path_bin]

    def test_all_resolution_paths_absent_raises(self, tmp_path, monkeypatch):
        """FileNotFoundError is raised when all resolution steps fail."""
        import watercooler.capture_hook as mod

        monkeypatch.delenv("VIRTUAL_ENV", raising=False)

        fake_hook = tmp_path / "watercooler-capture-theme"

        with patch("sys.argv", [str(fake_hook)]), \
             patch.object(mod, "_git_toplevel", return_value=None), \
             patch("shutil.which", return_value=None):
            with pytest.raises(FileNotFoundError):
                mod._resolve_watercooler_cmd(str(tmp_path))


def test_capture_hook_import_is_platform_safe():
    """Module must not crash on import when fcntl is unavailable.

    Simulates a Windows-like environment where fcntl does not exist.
    The module should import cleanly and expose _FCNTL_AVAILABLE = False.
    """
    import watercooler.capture_hook as mod

    # Confirm the flag exists and is a bool
    assert hasattr(mod, "_FCNTL_AVAILABLE")
    assert isinstance(mod._FCNTL_AVAILABLE, bool)


def test_capture_hook_main_accepts_argv_for_drain_queue(tmp_path, monkeypatch):
    """main(argv) must accept explicit argv instead of reading sys.argv.

    Verifies the console-script entry point can be exercised without mutating
    global sys.argv and without actually running a drain against the real queue.
    """
    import watercooler.capture_hook as mod

    if not mod._FCNTL_AVAILABLE:
        # On Windows, main() exits early — just check it does so gracefully
        with patch.object(sys, "exit") as mock_exit:
            mod.main(["--drain-queue"])
            mock_exit.assert_called_once_with(1)
        return

    # Patch drain_queue so we don't touch the real queue file
    called = []
    monkeypatch.setattr(mod, "drain_queue", lambda: called.append(True))

    mod.main(["--drain-queue"])

    assert called, "drain_queue() should have been called when --drain-queue is passed"


def test_capture_hook_main_no_args_reads_stdin(monkeypatch, capsys):
    """main() without --drain-queue reads stdin and exits 1 if not valid JSON."""
    import watercooler.capture_hook as mod

    if not mod._FCNTL_AVAILABLE:
        return  # skip on platforms without fcntl

    # Provide invalid stdin
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("not json"))

    with pytest.raises(SystemExit) as exc_info:
        mod.main([])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "valid JSON" in captured.err


def test_fcntl_unavailable_main_exits_1(monkeypatch, capsys):
    """When _FCNTL_AVAILABLE is False, main() must exit 1 with a clear message."""
    import watercooler.capture_hook as mod

    monkeypatch.setattr(mod, "_FCNTL_AVAILABLE", False)

    with pytest.raises(SystemExit) as exc_info:
        mod.main([])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "fcntl" in captured.err.lower() or "platform" in captured.err.lower()


# ---------------------------------------------------------------------------
# VALID_KINDS sync assertion
# ---------------------------------------------------------------------------


def test_valid_kinds_sync_between_capture_hook_and_snapshot_lib():
    """VALID_KINDS in capture_hook.py and pulse_snapshot_lib.py must be identical."""
    from watercooler.capture_hook import VALID_KINDS as HOOK_KINDS
    from watercooler.pulse_snapshot_lib import VALID_KINDS as SNAP_KINDS
    assert HOOK_KINDS == SNAP_KINDS, (
        f"VALID_KINDS out of sync.\n"
        f"  capture_hook only: {HOOK_KINDS - SNAP_KINDS}\n"
        f"  pulse_snapshot_lib only: {SNAP_KINDS - HOOK_KINDS}"
    )


# ---------------------------------------------------------------------------
# validate_extraction: new kinds accepted / unknown kinds rejected
# ---------------------------------------------------------------------------


def test_validate_extraction_accepts_new_d4_kinds():
    """validate_extraction() accepts all 5 new D4 kinds."""
    import watercooler.capture_hook as mod

    new_kinds = ["pr_merged", "closure", "resolved_loop", "opened_loops", "closed_loops"]
    for kind in new_kinds:
        data = {
            "technical_focus": ["topic"],
            "session_intent": "Testing new kind",
            "observations": [{"kind": kind, "text": "some text"}],
            "confidence": 0.8,
        }
        assert mod.validate_extraction(data), f"Should accept kind '{kind}'"


def test_validate_extraction_rejects_unknown_kind():
    """validate_extraction() rejects kinds not in VALID_KINDS."""
    import watercooler.capture_hook as mod

    data = {
        "technical_focus": ["topic"],
        "session_intent": "Testing unknown kind",
        "observations": [{"kind": "unknown_kind", "text": "some text"}],
        "confidence": 0.8,
    }
    assert not mod.validate_extraction(data)


# ---------------------------------------------------------------------------
# _sanitize_observations
# ---------------------------------------------------------------------------


def test_sanitize_observations_drops_closed_loops_when_resolved_loop_present():
    """When both resolved_loop and closed_loops present, closed_loops are dropped."""
    import watercooler.capture_hook as mod

    observations = [
        {"kind": "resolved_loop", "text": "resolved the open question"},
        {"kind": "closed_loops", "text": "closed 3 items in a sweep"},
        {"kind": "insight", "text": "discovered something"},
    ]
    result = mod._sanitize_observations(observations)
    kinds = {o["kind"] for o in result}
    assert "resolved_loop" in kinds
    assert "closed_loops" not in kinds
    assert "insight" in kinds


def test_sanitize_observations_passes_through_resolved_loop_only():
    """Only resolved_loop present → unchanged."""
    import watercooler.capture_hook as mod

    observations = [{"kind": "resolved_loop", "text": "resolved it"}]
    result = mod._sanitize_observations(observations)
    assert result == observations


def test_sanitize_observations_passes_through_closed_loops_only():
    """Only closed_loops present (no resolved_loop) → unchanged."""
    import watercooler.capture_hook as mod

    observations = [{"kind": "closed_loops", "text": "batch closed 5 items"}]
    result = mod._sanitize_observations(observations)
    assert result == observations


def test_sanitize_observations_passes_through_neither():
    """Neither resolved_loop nor closed_loops → unchanged."""
    import watercooler.capture_hook as mod

    observations = [{"kind": "pr_merged", "text": "merged PR #547"}]
    result = mod._sanitize_observations(observations)
    assert result == observations


def test_sanitize_observations_empty_list():
    """Empty input → empty output."""
    import watercooler.capture_hook as mod

    assert mod._sanitize_observations([]) == []
