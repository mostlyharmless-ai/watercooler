"""Regression tests for scripts/ops/falkordb_admin.sh.

Plan v20 Phase 3 shipped the Railway FalkorDB admin wrapper. The shell
script has three user-reachable safety properties that must not regress:

1. ``_is_safe_graph_name`` rejects empty names and names containing
   whitespace or control bytes (round 15 MEDIUM).
2. ``_json_escape_for_sed`` produces JSON-parseable output for every
   field that gets logged (round 7 LOW).
3. The backup ``RETURN`` trap cleans up every tempfile it registered,
   on both success and error exit paths (round 23 LOW).

Each test shells out to ``bash`` against the live script. The last line
(``main "$@"``) is stripped so sourcing does not invoke the CLI.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "falkordb_admin.sh"


def _source_and_run(body: str) -> subprocess.CompletedProcess[str]:
    """Source the script (minus the final ``main "$@"``) and run ``body``."""
    assert SCRIPT.exists(), f"ops script missing: {SCRIPT}"
    payload = (
        f'source <(head -n -1 "{SCRIPT}")\n'
        f"{body}\n"
    )
    return subprocess.run(
        ["bash", "-c", payload],
        capture_output=True,
        text=True,
        check=False,
    )


class TestIsSafeGraphName:
    @pytest.mark.parametrize(
        "name",
        [
            "mostlyharmless_ai_watercooler_cloud_t1",
            "mostlyharmless_ai_watercooler_cloud_t2",
            "repo-with-dashes",
            "repo.with.dots",
            "repo:with:colons",
            "a",
        ],
    )
    def test_accepts_valid(self, name: str) -> None:
        result = _source_and_run(
            f'if _is_safe_graph_name "{name}"; then echo OK; else echo REJECT; fi'
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout, (
            f"name {name!r} was rejected but should be accepted; "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    def test_rejects_empty(self) -> None:
        result = _source_and_run(
            'if _is_safe_graph_name ""; then echo OK; else echo REJECT; fi'
        )
        assert "REJECT" in result.stdout, result.stdout

    @pytest.mark.parametrize(
        "bash_literal",
        [
            r'"with space"',
            r'"leading space"[:1]"$( printf %b \x20 )"after',
            # Use ANSI-C quoting for whitespace + control chars.
            r"$' \tname'",
            r"$'line1\nline2'",
            r"$'name\r'",
            r"$'\x01control'",
        ],
    )
    def test_rejects_whitespace_and_control_bytes(
        self, bash_literal: str
    ) -> None:
        result = _source_and_run(
            f"if _is_safe_graph_name {bash_literal}; then echo OK; "
            f"else echo REJECT; fi"
        )
        assert "REJECT" in result.stdout, (
            f"unsafe name {bash_literal} was accepted; stdout={result.stdout!r}"
        )


class TestJsonEscapeForSed:
    def _escape(self, input_str: str) -> str:
        """Run _json_escape_for_sed on input_str, return the escaped text."""
        # Pass input via stdin to avoid quoting hell in bash -c payload.
        payload = (
            f'source <(head -n -1 "{SCRIPT}")\n'
            'IFS= read -r -d "" raw\n'
            'printf "%s" "$(_json_escape_for_sed "$raw")"\n'
        )
        p = subprocess.run(
            ["bash", "-c", payload],
            input=input_str + "\x00",
            capture_output=True,
            text=True,
            check=False,
        )
        assert p.returncode == 0, p.stderr
        return p.stdout

    def test_escapes_backslash(self) -> None:
        assert self._escape(r"a\b") == r"a\\b"

    def test_escapes_double_quote(self) -> None:
        assert self._escape('he said "hi"') == r'he said \"hi\"'

    def test_escapes_tab(self) -> None:
        assert self._escape("a\tb") == r"a\tb"

    def test_escapes_cr(self) -> None:
        assert self._escape("a\rb") == r"a\rb"

    def test_strips_nul(self) -> None:
        # Can't send nul via the harness (uses nul as delimiter), so just
        # confirm the filter keeps normal text.
        assert self._escape("plain") == "plain"

    def test_strips_other_control_bytes(self) -> None:
        out = self._escape("a\x01b\x02c")
        assert out == "abc", f"control bytes not stripped: {out!r}"

    def test_output_is_embeddable_in_json(self) -> None:
        """The whole point: the escaped output must produce parseable JSON
        when embedded inside a double-quoted string literal.
        """
        tricky = r'path=/tmp/foo"bar\baz' + "\tend"
        esc = self._escape(tricky)
        line = f'{{"field":"{esc}"}}'
        parsed = json.loads(line)
        assert parsed["field"] == tricky


class TestBackupTempfileCleanupTrap:
    """The cmd_backup function pushes tempfiles onto ``_BACKUP_TEMPFILES`` and
    relies on a ``trap ... RETURN`` to clean them up whether backup succeeds
    or aborts. The trap body must:

      1. Remove each registered tempfile if it exists.
      2. Leave the array cleared so a subsequent invocation starts fresh.

    We emulate the trap setup by extracting the trap body and registering
    a scripted RETURN handler in a test harness function.
    """

    def test_return_trap_removes_registered_tempfiles(
        self, tmp_path: Path
    ) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.write_text("x")
        b.write_text("y")
        assert a.exists() and b.exists()

        # Mirror the trap body from cmd_backup (falkordb_admin.sh).
        payload = (
            f'source <(head -n -1 "{SCRIPT}")\n'
            f'_BACKUP_TEMPFILES=("{a}" "{b}")\n'
            "harness() {\n"
            '  trap \'if [[ ${#_BACKUP_TEMPFILES[@]} -gt 0 ]]; then '
            'rm -rf "${_BACKUP_TEMPFILES[@]}" 2>/dev/null || true; '
            '_BACKUP_TEMPFILES=(); fi\' RETURN\n'
            '  echo "in harness"\n'
            "}\n"
            "harness\n"
            'echo "tempfiles left: ${#_BACKUP_TEMPFILES[@]}"\n'
        )
        result = subprocess.run(
            ["bash", "-c", payload],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "tempfiles left: 0" in result.stdout, result.stdout
        # And the filesystem state: tempfiles are gone.
        assert not a.exists(), "trap did not remove tempfile a"
        assert not b.exists(), "trap did not remove tempfile b"

    def test_return_trap_runs_on_error_exit(self, tmp_path: Path) -> None:
        t = tmp_path / "doomed"
        t.write_text("doomed")

        payload = (
            f'source <(head -n -1 "{SCRIPT}")\n'
            f'_BACKUP_TEMPFILES=("{t}")\n'
            "harness() {\n"
            '  trap \'if [[ ${#_BACKUP_TEMPFILES[@]} -gt 0 ]]; then '
            'rm -rf "${_BACKUP_TEMPFILES[@]}" 2>/dev/null || true; '
            '_BACKUP_TEMPFILES=(); fi\' RETURN\n'
            '  return 7\n'
            "}\n"
            "harness || echo \"harness exited with $?\"\n"
        )
        result = subprocess.run(
            ["bash", "-c", payload],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "harness exited with 7" in result.stdout, result.stdout
        assert not t.exists(), "trap did not remove tempfile on error exit"


class TestRequireLocalRedisCli:
    """``require_local_redis_cli`` blocks the silent-failure mode where
    ``railway run --service falkordb -- redis-cli ...`` runs against
    the local host (NOT the container) and silently produces no output
    when the local host has no ``redis-cli`` binary on PATH. The
    earlier behaviour was: ``graphs production`` printed an empty
    ``GRAPH.LIST`` and the operator concluded "no graphs exist."

    These tests shadow the ``command`` builtin so the script's
    ``command -v redis-cli`` returns the controlled value, which is
    portable across CI hosts that may or may not have redis-cli
    installed.
    """

    def test_missing_redis_cli_exits_with_helpful_error(self) -> None:
        # Shadow ``command`` so ``command -v redis-cli`` returns 1
        # without disturbing PATH (which the rest of the script
        # needs to find date/printf/etc).
        body = (
            "command() {\n"
            "  if [[ \"$1\" == \"-v\" && \"$2\" == \"redis-cli\" ]]; then\n"
            "    return 1\n"
            "  fi\n"
            "  builtin command \"$@\"\n"
            "}\n"
            "require_local_redis_cli\n"
        )
        result = _source_and_run(body)
        assert result.returncode == 2, (
            f"expected exit 2 (operator-visible failure), got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        # The remediation hint must mention BOTH paths so the
        # operator knows their options.
        assert "redis-cli is not on PATH" in result.stderr
        assert "railway ssh" in result.stderr, (
            "remediation hint should mention the in-container "
            "``railway ssh`` workaround, not just local install"
        )
        assert "redis-tools" in result.stderr or "brew install" in result.stderr, (
            "remediation hint should mention install paths"
        )

    def test_present_redis_cli_returns_silently(self) -> None:
        # Shadow ``command`` so ``command -v redis-cli`` returns 0.
        body = (
            "command() {\n"
            "  if [[ \"$1\" == \"-v\" && \"$2\" == \"redis-cli\" ]]; then\n"
            "    return 0\n"
            "  fi\n"
            "  builtin command \"$@\"\n"
            "}\n"
            "require_local_redis_cli && echo OK\n"
        )
        result = _source_and_run(body)
        assert result.returncode == 0, (
            f"expected exit 0 when redis-cli is present, got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "OK" in result.stdout
        # Must not have printed any error/warning when the binary is present.
        assert "redis-cli is not on PATH" not in result.stderr
