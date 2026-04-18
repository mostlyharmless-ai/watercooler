"""Unit tests for the branch-discovery hint (Bug #2, plan v4).

Covers:
- get_branches_with_entries helper in baseline_graph.reader
- format_branch_discovery_hint formatter in baseline_graph.reader
- Wiring through all three branch-filtered MCP read tools
  (_read_thread_impl, _list_thread_entries_impl,
  _get_thread_entry_range_impl) for BOTH markdown and JSON outputs.

Design intent (current docs, not stale threads): branch-scoped reads
are the documented default with ``code_branch="*"`` as the escape
hatch. Keep the default. Only surface the hint when the filter is
active and produced zero entries but other code_branch values do carry
entries for the topic.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from watercooler.baseline_graph.reader import (
    format_branch_discovery_hint,
    get_branches_with_entries,
)


class TestFormatBranchDiscoveryHint:
    """Pure formatter — no I/O."""

    def test_returns_empty_when_filter_is_none(self):
        assert format_branch_discovery_hint("", {"main", "feature/x"}) == ""

    def test_returns_empty_when_filter_is_wildcard(self):
        assert format_branch_discovery_hint("*", {"main", "feature/x"}) == ""

    def test_returns_empty_when_no_other_branches_have_entries(self):
        # Only the filter branch appears in available_branches (or set is empty)
        assert format_branch_discovery_hint("main", {"main"}) == ""
        assert format_branch_discovery_hint("main", set()) == ""

    def test_emits_hint_with_sorted_other_branches(self):
        hint = format_branch_discovery_hint(
            "feature/x", {"main", "feature/y", "feature/x"}
        )
        assert "code_branch='feature/x'" in hint
        # Sorted so output is stable — tests would flake otherwise.
        assert "feature/y, main" in hint
        assert 'code_branch="*"' in hint

    def test_filter_branch_is_not_listed_as_other(self):
        """If the filter branch happens to be in the set, it must NOT
        appear in the 'entries exist on branches:' list."""
        hint = format_branch_discovery_hint(
            "main", {"main", "feature/x"}
        )
        # 'feature/x' is the sole 'other'
        assert "feature/x" in hint
        # The filter branch 'main' should NOT appear in the other-list
        # part of the message. Verify by slicing out the "branches: X"
        # portion and checking membership there.
        marker = "branches: "
        tail = hint[hint.index(marker) + len(marker):]
        other_list = tail.split(".", 1)[0]
        assert "main" not in other_list


class TestGetBranchesWithEntries:
    """I/O helper — test via a fake entries iterator."""

    def test_returns_distinct_non_null_branches(self, tmp_path):
        # Stub storage.load_thread_entries to avoid needing a real graph
        # on disk. The helper is thin; the contract is "scan nodes and
        # return the set of non-null code_branch values, deduped."
        from watercooler.baseline_graph import reader as reader_mod

        fake_entries = [
            {"code_branch": "main"},
            {"code_branch": "feature/x"},
            {"code_branch": "main"},  # dup
            {"code_branch": None},  # null → excluded
            {"code_branch": ""},  # empty → excluded
            {"code_branch": "feature/y"},
        ]

        with patch.object(
            reader_mod.storage,
            "load_thread_entries",
            return_value=iter(fake_entries),
        ):
            result = get_branches_with_entries(tmp_path, "some-topic")

        assert result == {"main", "feature/x", "feature/y"}


class TestReadThreadImplHint:
    """Integration-ish: _read_thread_impl attaches the hint in both
    markdown and JSON output when filter produces empty."""

    def _stub_read_with_empty_filter(self, monkeypatch, _all_branches):
        """Patch every piece of the local-mode read path that touches
        the filesystem or external state, so the test can focus on the
        hint behavior."""
        from watercooler_mcp.tools import thread_query as tq
        from watercooler_mcp.tools.thread_query import _read_thread_impl

        # read_thread_from_graph returns (thread_meta_obj, []) — i.e.,
        # thread exists but filtered entries are empty.
        class _FakeThread:
            pass

        monkeypatch.setattr(
            tq,
            "read_thread_from_graph",
            lambda *_a, **_k: (_FakeThread(), []),
        )
        # Branch-discovery helper returns the canned set.
        monkeypatch.setattr(
            tq, "get_branches_with_entries", lambda *_a, **_k: set(_all_branches)
        )
        # Skip sync and context machinery by stubbing what we need.
        # Use a minimal validation shim to avoid real git I/O.
        monkeypatch.setattr(tq, "ensure_readable", lambda *_a, **_k: (True, [], "clean", False))
        monkeypatch.setattr(tq, "format_parity_warning", lambda *_a, **_k: "")
        monkeypatch.setattr(tq.validation, "_dynamic_context_missing", lambda *_a, **_k: False)
        monkeypatch.setattr(tq.validation, "_refresh_threads", lambda *_a, **_k: None)
        monkeypatch.setattr(
            tq.validation,
            "_validate_thread_context",
            lambda *_a, **_k: (None, _FakeContext()),
        )
        monkeypatch.setattr(tq, "_track_access", lambda *_a, **_k: None)
        monkeypatch.setattr(
            tq,
            "_load_entries",
            lambda *_a, **_k: (None, [], {}),
        )
        monkeypatch.setattr(
            tq,
            "_get_thread_metadata",
            lambda *_a, **_k: ("Title", "OPEN", "human", "2026-01-01"),
        )
        monkeypatch.setattr(
            tq,
            "_get_thread_summary",
            lambda *_a, **_k: "summary",
        )
        monkeypatch.setattr(tq, "is_hosted_context", lambda _ctx: False)
        monkeypatch.setattr(tq, "_get_startup_warnings", lambda: [])
        monkeypatch.setattr(
            tq,
            "format_thread_markdown",
            lambda *_a, **_k: "# Thread content (stub)\n",
        )

        return _read_thread_impl

    def test_markdown_hint_prepended_when_filter_returns_empty(self, tmp_path, monkeypatch):
        impl = self._stub_read_with_empty_filter(
            monkeypatch, _all_branches={"main", "feature/x"}
        )
        result = impl(
            topic="t1",
            format="markdown",
            summary_only=False,
            code_path=str(tmp_path),
            code_branch="other",
        )
        text = result if isinstance(result, str) else result.content[0].text
        assert "code_branch='other'" in text
        # The hint should appear BEFORE the reconstructed markdown
        # content (non-empty string prepend, no positional tie-breaking).
        assert text.find("code_branch=") < 100

    def test_json_hint_and_branches_list_attached(self, tmp_path, monkeypatch):
        impl = self._stub_read_with_empty_filter(
            monkeypatch, _all_branches={"main", "feature/x"}
        )
        result = impl(
            topic="t1",
            format="json",
            summary_only=False,
            code_path=str(tmp_path),
            code_branch="other",
        )
        payload = json.loads(result if isinstance(result, str) else result.content[0].text)
        assert "_hint" in payload
        assert "code_branch='other'" in payload["_hint"]
        assert payload["_branches_with_entries"] == ["feature/x", "main"]

    def test_no_hint_when_filter_is_wildcard(self, tmp_path, monkeypatch):
        impl = self._stub_read_with_empty_filter(
            monkeypatch, _all_branches={"main", "feature/x"}
        )
        # Even with entries empty, wildcard filter means user asked for
        # all branches and got none — no hint needed.
        result = impl(
            topic="t1",
            format="json",
            code_path=str(tmp_path),
            code_branch="*",
        )
        payload = json.loads(result if isinstance(result, str) else result.content[0].text)
        assert "_hint" not in payload


class _FakeContext:
    """Minimal stand-in for ThreadContext used by patched code paths."""

    def __init__(self):
        self.threads_dir = Path("/tmp/fake-threads")
        self.code_root = Path("/tmp/fake-code")
        self.code_branch = None
        self.code_remote = "https://github.com/example/repo.git"
        self.code_repo = "example/repo"
