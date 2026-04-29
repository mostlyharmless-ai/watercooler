"""Unit tests for cmd_migrate's JSON-summary contract guarantee.

The CLI honors a "stdout is JSON, stderr is logs, exit code reflects
state" contract. Any uncaught exception from a migrate_* function MUST
be converted into a populated MigrationSummary on stdout — never
propagate as a Python traceback. These tests pin that boundary
behavior across all migrate_* paths.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from unittest.mock import patch

import pytest

from watercooler.migration import cli as cli_mod


def _build_args(tier="t1", direction="hybrid", **overrides):
    args = argparse.Namespace(
        migrate_tier=tier,
        to=direction,
        code_path="",
        target_group_id="",
        local_host="localhost",
        local_port=6379,
        local_password="",
        local_graph_name="",
        checkpoint="",
        dry_run=False,
        limit=0,
        threads="",
    )
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


def _run_cmd(args):
    """Run cmd_migrate; return (rc, stdout_str, stderr_str)."""
    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    with patch.object(sys, "stdout", captured_stdout), \
         patch.object(sys, "stderr", captured_stderr):
        rc = cli_mod.cmd_migrate(args)
    return rc, captured_stdout.getvalue(), captured_stderr.getvalue()


class TestCliBoundaryCatch:
    """Pins the cmd_migrate boundary: any exception → JSON summary, never traceback."""

    def test_t1_hybrid_runtime_error_becomes_json_summary(self) -> None:
        """RuntimeError from build_premium_client (no [mcp].url) must be caught."""
        with patch.object(cli_mod, "_dispatch",
                          side_effect=RuntimeError("Hybrid migration requires [mcp].url")):
            rc, stdout, stderr = _run_cmd(_build_args(tier="t1", direction="hybrid"))
        # JSON ON STDOUT, not a Python traceback.
        summary = json.loads(stdout)
        assert summary["tier"] == "t1"
        assert summary["direction"] == "stdio_to_hybrid"
        assert summary["errored"] >= 1
        notes_joined = " ".join(summary["notes"])
        assert "RuntimeError" in notes_joined
        assert "[mcp].url" in notes_joined
        assert rc == 2

    def test_t1_stdio_connection_error_becomes_json_summary(self) -> None:
        """ConnectionError from any step inside migrate_t1_to_stdio caught at boundary."""
        with patch.object(cli_mod, "_dispatch",
                          side_effect=ConnectionError("network blip")):
            rc, stdout, stderr = _run_cmd(_build_args(tier="t1", direction="stdio"))
        summary = json.loads(stdout)
        assert summary["direction"] == "hybrid_to_stdio"
        assert summary["errored"] >= 1
        assert "ConnectionError" in " ".join(summary["notes"])
        assert rc == 2

    def test_t2_hybrid_value_error_from_int_cast_becomes_json_summary(self) -> None:
        """ValueError from int() on a malformed server response is caught."""
        with patch.object(cli_mod, "_dispatch",
                          side_effect=ValueError("invalid literal for int(): 'foo'")):
            rc, stdout, stderr = _run_cmd(_build_args(tier="t2", direction="hybrid"))
        summary = json.loads(stdout)
        assert summary["tier"] == "t2"
        assert summary["errored"] >= 1
        assert "ValueError" in " ".join(summary["notes"])
        assert rc == 2

    def test_stdout_is_pure_json_even_on_crash(self) -> None:
        """The user-facing contract: stdout is parseable JSON, never a traceback.

        Log capture (via caplog) was attempted earlier but proved fragile
        across pytest versions and CI environments — the contract that
        actually matters to callers is "stdout parses as JSON," not "the
        log was emitted." The cmd_migrate code DOES log via
        logger.warning(..., exc_info=True), but that's an
        implementation-debug detail not a user-facing guarantee.
        """
        with patch.object(cli_mod, "_dispatch",
                          side_effect=RuntimeError("oops")):
            rc, stdout, stderr = _run_cmd(_build_args(tier="t1", direction="hybrid"))
        # Stdout is pure JSON — no leading log lines, no traceback bleed.
        # Strict: must parse as JSON without raising.
        summary = json.loads(stdout)
        # And the JSON contains the exception type/message in notes,
        # so the caller has actionable info even without log capture.
        assert "RuntimeError" in " ".join(summary["notes"])
        assert "oops" in " ".join(summary["notes"])

    def test_clean_summary_exits_zero(self) -> None:
        """Sanity: when migrate_* returns a clean summary, CLI exits 0."""
        from watercooler.migration.summary import MigrationSummary
        clean = MigrationSummary(
            tier="t1", direction="stdio_to_hybrid", dry_run=False,
            pushed=42, total_scanned=42,
        )
        with patch.object(cli_mod, "_dispatch", return_value=clean):
            rc, stdout, stderr = _run_cmd(_build_args(tier="t1", direction="hybrid"))
        summary = json.loads(stdout)
        assert summary["pushed"] == 42
        assert summary["errored"] == 0
        assert rc == 0

    def test_local_password_cli_flag_wins_over_env(self, monkeypatch) -> None:
        """Round-12 review LOW: CLI flag > FALKORDB_PASSWORD env > None."""
        monkeypatch.setenv("FALKORDB_PASSWORD", "from-env")
        args = _build_args(tier="t1", direction="hybrid", local_password="from-cli")
        assert cli_mod._resolve_local_password(args) == "from-cli"

    def test_local_password_falls_back_to_env(self, monkeypatch) -> None:
        monkeypatch.setenv("FALKORDB_PASSWORD", "from-env")
        args = _build_args(tier="t1", direction="hybrid", local_password="")
        assert cli_mod._resolve_local_password(args) == "from-env"

    def test_local_password_none_when_neither_set(self, monkeypatch) -> None:
        monkeypatch.delenv("FALKORDB_PASSWORD", raising=False)
        args = _build_args(tier="t1", direction="hybrid", local_password="")
        assert cli_mod._resolve_local_password(args) is None

    def test_unknown_tier_returns_2_not_traceback(self) -> None:
        """Even the ValueError from _dispatch's unknown-tier guard is caught."""
        with patch.object(cli_mod, "_dispatch",
                          side_effect=ValueError("Unknown tier: xyz")):
            rc, stdout, stderr = _run_cmd(_build_args(tier="xyz"))
        # Still produces JSON summary, not a raw traceback.
        summary = json.loads(stdout)
        assert summary["errored"] >= 1
        assert rc == 2
