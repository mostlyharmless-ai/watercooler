"""MCP-boundary wrapper for watercooler_promote_candidate.

``_promote_candidate_impl`` signals failure by *returning* a ``❌``-prefixed
string (the repo-wide convention), which leaves the MCP result ``isError: false``
— so a client that only checks ``isError`` reads a failed L3 promotion as success
(a 200 with nothing written). ``_promote_candidate_tool`` is the registered
boundary that converts a ``❌`` return into a raised ``ToolError`` (FastMCP →
``isError: true``, message surfaced). These tests pin that conversion and the
success/edits pass-through, without standing up a baseline graph.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from watercooler_mcp.tools import promotion as promo

_ARGS = dict(
    candidate_entry_id="01CAND00000000000000000000",
    topic="bug-sync-push-silent-success",
    target_type="Learning",
    human_authorized_by="github:caleb",
    ctx=MagicMock(name="ctx"),
    code_path="/abs/repo",
    agent_func="Claude Code:claude-opus-4-8:implementer",
)


def test_error_return_is_raised_as_toolerror():
    sentinel = "❌ watercooler_promote_candidate: candidate entry … not found on thread."
    with patch.object(promo, "_promote_candidate_impl", return_value=sentinel) as impl:
        with pytest.raises(ToolError) as exc:
            promo._promote_candidate_tool(**_ARGS)
    assert "not found on thread" in str(exc.value)
    impl.assert_called_once()


def test_leading_whitespace_before_marker_still_raises():
    with patch.object(
        promo, "_promote_candidate_impl",
        return_value="\n  ❌ watercooler_promote_candidate: refusing — already promoted.",
    ):
        with pytest.raises(ToolError, match="already promoted"):
            promo._promote_candidate_tool(**_ARGS)


def test_success_string_passes_through():
    ok = "✅ Promoted candidate 01CAND… to Learning on thread 'bug-…'."
    with patch.object(promo, "_promote_candidate_impl", return_value=ok):
        result = promo._promote_candidate_tool(**_ARGS)
    assert result == ok


def test_edits_are_forwarded_to_impl():
    edits = {"lesson": "Always verify push parity after a write"}
    with patch.object(promo, "_promote_candidate_impl", return_value="✅ ok") as impl:
        promo._promote_candidate_tool(**_ARGS, edits=edits)
    assert impl.call_args.kwargs["edits"] == edits


def test_tool_description_preserved_from_impl():
    # The registered tool's description should be the impl's rich docstring
    # (documents args/semantics), not the thin wrapper's.
    assert promo._promote_candidate_tool.__doc__ == promo._promote_candidate_impl.__doc__
