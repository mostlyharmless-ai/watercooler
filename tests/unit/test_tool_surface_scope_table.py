"""Enforcement tests for the hybrid tool-surface scope table.

Descriptor-discipline amendment A3 (Decision, thread
audit-transport-modes-hosted-db-2026-07): the scope table in
docs/AUTHENTICATION_HOSTED.md is the maintained descriptor surface for
the hybrid remote leg. These tests fail whenever a tool's actual
registration shape or ``code_path`` signature diverges from the table —
in either direction (a row the code no longer matches, or a tool the
table forgot).
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from watercooler_mcp.capabilities import (
    DAEMON_TOOL_NAMES,
    HYBRID_DISABLED_TOOL_NAMES,
    HYBRID_POOLED_READ_TOOLS,
    MIXED_TOOL_NAMES,
    REMOTE_CAPABLE_MEMORY_TOOL_NAMES,
)

_DOC = Path(__file__).resolve().parents[2] / "docs" / "AUTHENTICATION_HOSTED.md"
_SECTION = "### Tool-surface scope table (hybrid remote leg)"

# The two repo-scoped WRITE wrappers: they fail closed instead of using
# select_pool_client's boot fallback (PR #1062 review P1).
_FAIL_CLOSED_WRITES = frozenset(
    {"watercooler_bulk_index", "watercooler_graphiti_add_episode"}
)


def _table_rows() -> dict[str, tuple[str, str]]:
    """Parse the scope table: tool name -> (registration, scope source)."""
    text = _DOC.read_text(encoding="utf-8")
    assert _SECTION in text, (
        f"scope-table section heading {_SECTION!r} missing from {_DOC.name}"
    )
    section = text.split(_SECTION, 1)[1]
    rows: dict[str, tuple[str, str]] = {}
    for line in section.splitlines():
        m = re.match(r"^\|\s*`(watercooler_\w+)`\s*\|([^|]+)\|([^|]+)\|", line)
        if m:
            rows[m.group(1)] = (m.group(2).strip(), m.group(3).strip())
    assert rows, "no rows parsed from the scope table"
    return rows


def _impl_for(tool_name: str):
    """Return the local implementation function for *tool_name*."""
    from watercooler_mcp.tools import daemon as daemon_tools
    from watercooler_mcp.tools import decisions as decisions_tools
    from watercooler_mcp.tools import graph as graph_tools
    from watercooler_mcp.tools import memory as memory_tools

    if tool_name in memory_tools.TOOL_BUILDERS:
        return memory_tools.TOOL_BUILDERS[tool_name][0]
    if tool_name in graph_tools.TOOL_BUILDERS:
        return graph_tools.TOOL_BUILDERS[tool_name][0]
    if tool_name == "watercooler_list_decisions":
        return decisions_tools._list_decisions_impl
    daemon_impls = {
        "watercooler_daemon_status": daemon_tools._daemon_status_impl,
        "watercooler_daemon_findings": daemon_tools._daemon_findings_impl,
        "watercooler_pulse_snapshot": daemon_tools._pulse_snapshot_impl,
    }
    if tool_name in daemon_impls:
        return daemon_impls[tool_name]
    raise AssertionError(f"no known implementation for {tool_name}")


class TestScopeTableEnforcement:
    def test_mixed_rows_match_mixed_tool_names(self):
        rows = _table_rows()
        mixed_rows = {t for t, (reg, _) in rows.items() if reg == "mixed wrapper"}
        assert mixed_rows == set(MIXED_TOOL_NAMES), (
            "scope table 'mixed wrapper' rows diverge from MIXED_TOOL_NAMES; "
            "update docs/AUTHENTICATION_HOSTED.md alongside capabilities.py"
        )

    def test_disabled_rows_match_disabled_tool_names(self):
        rows = _table_rows()
        disabled_rows = {t for t, (reg, _) in rows.items() if reg == "disabled"}
        assert disabled_rows == set(HYBRID_DISABLED_TOOL_NAMES)

    def test_table_covers_every_remote_capable_tool(self):
        """No hybrid remote-capable tool may be missing from the table.

        The universe is the remote-capable memory + daemon tools plus the
        mixed tools whose remote leg lives outside those sets
        (watercooler_search / watercooler_list_decisions).
        """
        rows = _table_rows()
        expected = (
            REMOTE_CAPABLE_MEMORY_TOOL_NAMES
            | DAEMON_TOOL_NAMES
            | MIXED_TOOL_NAMES
        )
        assert set(rows) == set(expected), (
            f"table/tool-set divergence: missing={sorted(expected - set(rows))} "
            f"stale={sorted(set(rows) - expected)}"
        )

    @pytest.mark.parametrize(
        "tool_name",
        sorted(
            REMOTE_CAPABLE_MEMORY_TOOL_NAMES
            | DAEMON_TOOL_NAMES
            | MIXED_TOOL_NAMES
        ),
    )
    def test_scope_source_matches_code_path_signature(self, tool_name):
        """A 'per-call code_path' claim requires the parameter to exist;
        a 'no code_path param' claim requires it to be absent."""
        rows = _table_rows()
        _, scope = rows[tool_name]
        if scope == "—":
            return
        params = inspect.signature(_impl_for(tool_name)).parameters
        has_code_path = "code_path" in params
        if "no `code_path` param" in scope:
            assert not has_code_path, (
                f"{tool_name}: table claims no code_path but the "
                "implementation now has one — promote it to per-call "
                "routing and update the table"
            )
        elif "per-call `code_path`" in scope:
            assert has_code_path, (
                f"{tool_name}: table claims per-call code_path routing but "
                "the implementation has no code_path parameter"
            )
        else:
            raise AssertionError(
                f"{tool_name}: unrecognized scope-source cell {scope!r}"
            )

    def test_fail_closed_rows_are_the_write_wrappers(self):
        rows = _table_rows()
        fail_closed = {
            t for t, (_, scope) in rows.items() if "fail-closed write" in scope
        }
        assert fail_closed == set(_FAIL_CLOSED_WRITES)
        assert not fail_closed & HYBRID_POOLED_READ_TOOLS

    def test_boot_fallback_rows_are_reads(self):
        """Boot fallback is acceptable for READS ONLY (select_pool_client
        contract) — no fail-closed write may carry it, and every pooled
        read with a code_path parameter must."""
        rows = _table_rows()
        fallback = {
            t for t, (_, scope) in rows.items() if "boot fallback" in scope
        }
        assert not fallback & _FAIL_CLOSED_WRITES
        pooled_with_path = {
            t
            for t in HYBRID_POOLED_READ_TOOLS
            if "code_path" in inspect.signature(_impl_for(t)).parameters
        }
        assert pooled_with_path <= fallback
