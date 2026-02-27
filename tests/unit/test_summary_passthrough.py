"""Tests for summary data flow through MCP payloads.

Validates that:
- _entry_header_payload includes summary field
- _entry_full_payload includes summary alongside body
- _load_entries returns 3-tuple with summaries dict
- _list_threads returns 7-tuple with thread summary
- summary_only mode in read_thread and get_thread_entry_range
"""

from __future__ import annotations

import json
from textwrap import dedent

import pytest

from watercooler.thread_entries import ThreadEntry, parse_thread_entries
from watercooler_mcp.helpers import (
    _entry_header_payload,
    _entry_full_payload,
)
from watercooler_mcp import server, validation
from watercooler_mcp.config import ThreadContext


# ============================================================================
# Fixtures
# ============================================================================

_THREAD_TEXT = dedent(
    """\
    # summary-test — Summary Test Thread
    Status: OPEN
    Ball: Claude (caleb)
    Topic: summary-test
    Created: 2025-11-14T08:09:39Z

    ---
    Entry: Claude (caleb) 2025-11-14T08:09:39Z
    Role: planner
    Type: Plan
    Title: Initial planning

    Spec: planner-architecture
    Planning the feature.
    <!-- Entry-ID: 01SUMMARY0000000000000001 -->

    ---
    Entry: Claude (caleb) 2025-11-14T08:15:55Z
    Role: implementer
    Type: Note
    Title: Implementation started

    Spec: implementer-code
    Started implementing the feature.
    <!-- Entry-ID: 01SUMMARY0000000000000002 -->
    """
)


def _make_entry(**overrides) -> ThreadEntry:
    """Create a ThreadEntry with defaults."""
    defaults = {
        "index": 0,
        "header": "Entry: Agent 2025-01-01T00:00:00Z\nRole: pm\nType: Note\nTitle: Test",
        "body": "Test body content.",
        "agent": "Agent",
        "timestamp": "2025-01-01T00:00:00Z",
        "role": "pm",
        "entry_type": "Note",
        "title": "Test",
        "entry_id": "01TEST00000000000000000001",
        "start_line": 0,
        "end_line": 0,
        "start_offset": 0,
        "end_offset": 0,
    }
    defaults.update(overrides)
    return ThreadEntry(**defaults)


def _create_graph_data(threads_dir):
    """Create per-thread graph data matching _THREAD_TEXT content."""
    topic = "summary-test"
    graph_dir = threads_dir / "graph" / "baseline" / "threads" / topic
    graph_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "type": "thread",
        "topic": topic,
        "title": "Summary Test Thread",
        "status": "OPEN",
        "ball": "Claude (caleb)",
        "last_updated": "2025-11-14T08:15:55Z",
        "summary": "Thread for testing summary passthrough",
        "entry_count": 2,
    }
    (graph_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    entries = [
        {
            "entry_id": "01SUMMARY0000000000000001",
            "thread_topic": topic,
            "index": 0,
            "agent": "Claude (caleb)",
            "role": "planner",
            "entry_type": "Plan",
            "title": "Initial planning",
            "timestamp": "2025-11-14T08:09:39Z",
            "summary": "Initial planning for the feature",
            "body": "Spec: planner-architecture\nPlanning the feature.\n",
        },
        {
            "entry_id": "01SUMMARY0000000000000002",
            "thread_topic": topic,
            "index": 1,
            "agent": "Claude (caleb)",
            "role": "implementer",
            "entry_type": "Note",
            "title": "Implementation started",
            "timestamp": "2025-11-14T08:15:55Z",
            "summary": "Started implementing the feature",
            "body": "Spec: implementer-code\nStarted implementing the feature.\n",
        },
    ]
    lines = [json.dumps(e) for e in entries]
    (graph_dir / "entries.jsonl").write_text("\n".join(lines) + "\n")


@pytest.fixture
def patched_context(tmp_path, monkeypatch):
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    thread_path = threads_dir / "summary-test.md"
    thread_path.write_text(_THREAD_TEXT, encoding="utf-8")

    # Create graph data (source of truth for reads)
    _create_graph_data(threads_dir)

    context = ThreadContext(
        code_root=tmp_path,
        threads_dir=threads_dir,
        code_repo="test/repo",
        code_branch="main",
        code_commit="abc1234",
        code_remote="origin",
        explicit_dir=True,
    )

    monkeypatch.setattr(validation, "_require_context", lambda code_path: (None, context))
    monkeypatch.setattr(validation, "_dynamic_context_missing", lambda ctx: False)
    monkeypatch.setattr(validation, "_refresh_threads", lambda ctx: None)
    monkeypatch.setattr(validation, "_validate_thread_context", lambda code_path: (None, context))

    return context


# ============================================================================
# Payload helper tests
# ============================================================================


class TestEntryHeaderPayload:
    """Tests for _entry_header_payload summary support."""

    def test_includes_summary_when_provided(self):
        entry = _make_entry()
        payload = _entry_header_payload(entry, summary="OAuth2 implementation started.")
        assert payload["summary"] == "OAuth2 implementation started."

    def test_summary_empty_by_default(self):
        entry = _make_entry()
        payload = _entry_header_payload(entry)
        assert payload["summary"] == ""

    def test_no_vestigial_fields(self):
        entry = _make_entry()
        payload = _entry_header_payload(entry)
        assert "header" not in payload
        assert "start_line" not in payload
        assert "end_line" not in payload
        assert "start_offset" not in payload
        assert "end_offset" not in payload

    def test_structured_fields_present(self):
        entry = _make_entry(
            agent="Claude",
            timestamp="2025-06-01T12:00:00Z",
            role="planner",
            entry_type="Plan",
            title="Design auth",
        )
        payload = _entry_header_payload(entry, summary="Auth design")
        assert payload["agent"] == "Claude"
        assert payload["timestamp"] == "2025-06-01T12:00:00Z"
        assert payload["role"] == "planner"
        assert payload["type"] == "Plan"
        assert payload["title"] == "Design auth"
        assert payload["summary"] == "Auth design"


class TestEntryFullPayload:
    """Tests for _entry_full_payload summary support."""

    def test_includes_summary_and_body(self):
        entry = _make_entry(body="Full body text here.")
        payload = _entry_full_payload(entry, summary="Short summary.")
        assert payload["summary"] == "Short summary."
        assert payload["body"] == "Full body text here."

    def test_summary_empty_by_default(self):
        entry = _make_entry()
        payload = _entry_full_payload(entry)
        assert payload["summary"] == ""
        assert "body" in payload

    def test_no_markdown_field(self):
        entry = _make_entry()
        payload = _entry_full_payload(entry)
        assert "markdown" not in payload


# ============================================================================
# Tool-level summary tests (summary_only mode)
# ============================================================================


class TestReadThreadSummaryOnly:
    """Tests for read_thread with summary_only=True."""

    def test_summary_only_json(self, patched_context):
        output = server.read_thread.fn(
            topic="summary-test",
            code_path=".",
            format="json",
            summary_only=True,
        )
        payload = json.loads(output)
        assert payload["summary_only"] is True
        assert payload["entry_count"] == 2
        # Entries should have summary but no body
        for entry in payload["entries"]:
            assert "summary" in entry
            assert "body" not in entry

    def test_summary_only_markdown(self, patched_context):
        output = server.read_thread.fn(
            topic="summary-test",
            code_path=".",
            format="markdown",
            summary_only=True,
        )
        # Should be condensed view with entry index markers
        assert "summary-test" in output
        assert "[0]" in output
        assert "[1]" in output
        # Should NOT contain full entry bodies
        assert "Spec: planner-architecture" not in output

    def test_full_json_includes_summary_field(self, patched_context):
        output = server.read_thread.fn(
            topic="summary-test",
            code_path=".",
            format="json",
        )
        payload = json.loads(output)
        # Meta should include summary key
        assert "summary" in payload["meta"]
        # Entries should have both summary and body
        for entry in payload["entries"]:
            assert "summary" in entry
            assert "body" in entry


class TestEntryRangeSummaryOnly:
    """Tests for get_thread_entry_range with summary_only=True."""

    def test_summary_only_json(self, patched_context):
        result = server.get_thread_entry_range.fn(
            topic="summary-test",
            start_index=0,
            end_index=1,
            code_path=".",
            format="json",
            summary_only=True,
        )
        text = result.content[0].text
        payload = json.loads(text)
        assert payload["summary_only"] is True
        assert len(payload["entries"]) == 2
        for entry in payload["entries"]:
            assert "summary" in entry
            assert "body" not in entry

    def test_summary_only_markdown(self, patched_context):
        result = server.get_thread_entry_range.fn(
            topic="summary-test",
            start_index=0,
            end_index=1,
            code_path=".",
            format="markdown",
            summary_only=True,
        )
        text = result.content[0].text
        assert "[0]" in text
        assert "[1]" in text
        # Should NOT contain separator blocks or full bodies
        assert "---" not in text


class TestGetThreadEntryIncludesSummary:
    """Tests for get_thread_entry including summary in response."""

    def test_entry_has_summary_field(self, patched_context):
        result = server.get_thread_entry.fn(
            topic="summary-test",
            index=0,
            code_path=".",
            format="json",
        )
        text = result.content[0].text
        payload = json.loads(text)
        assert "summary" in payload["entry"]


class TestListThreadEntriesIncludesSummary:
    """Tests for list_thread_entries including summaries."""

    def test_entries_have_summary_field(self, patched_context):
        result = server.list_thread_entries.fn(
            topic="summary-test",
            code_path=".",
            format="json",
        )
        text = result.content[0].text
        payload = json.loads(text)
        for entry in payload["entries"]:
            assert "summary" in entry
            # header-only payload should not have body
            assert "body" not in entry
