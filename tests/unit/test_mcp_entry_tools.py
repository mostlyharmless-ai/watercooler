from __future__ import annotations

import json
from textwrap import dedent

import pytest

from watercooler_mcp import server, validation
from watercooler_mcp.config import ThreadContext


_THREAD_TEXT = dedent(
    """\
    # entry-access-tools — Thread
    Status: OPEN
    Ball: Codex (caleb)
    Topic: entry-access-tools
    Created: 2025-11-14T08:09:39Z

    ---
    Entry: Codex (caleb) 2025-11-14T08:09:39Z
    Role: planner
    Type: Plan
    Title: Plan: entry-level MCP tooling

    Spec: planner-architecture
    Line A
    <!-- Entry-ID: 01KA0PK97G9Q6AB0B17896Y1EB -->

    ---
    Entry: Codex (caleb) 2025-11-14T08:15:55Z
    Role: planner
    Type: Note
    Title: Closing: wrong repo context

    Spec: planner-architecture
    Another body line
    <!-- Entry-ID: 01KA0PYSR7X43QQ61H1BCR3S2S -->
    """
)


@pytest.fixture
def patched_context(tmp_path, monkeypatch):
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    thread_path = threads_dir / "entry-access-tools.md"
    thread_path.write_text(_THREAD_TEXT, encoding="utf-8")

    context = ThreadContext(
        code_root=tmp_path,
        threads_dir=threads_dir,
        threads_repo_url=None,
        code_repo="mostlyharmless-ai/watercooler-cloud",
        code_branch="main",
        code_commit="abc1234",
        code_remote="origin",
        threads_slug="watercooler-cloud",
        explicit_dir=True,
    )

    def fake_require_context(code_path: str):
        return (None, context)

    # Patch validation module directly (not server) to break circular import pattern
    monkeypatch.setattr(validation, "_require_context", fake_require_context)
    monkeypatch.setattr(validation, "_dynamic_context_missing", lambda ctx: False)
    monkeypatch.setattr(validation, "_refresh_threads", lambda ctx: None)

    return thread_path


def _extract_payload(result) -> dict:
    assert result.content, "ToolResult missing content"
    payload_text = result.content[0].text
    return json.loads(payload_text)


def _extract_text(result) -> str:
    assert result.content, "ToolResult missing content"
    return result.content[0].text


def test_list_thread_entries_returns_headers(patched_context):
    result = server.list_thread_entries.fn(topic="entry-access-tools", code_path=".")
    payload = _extract_payload(result)

    assert payload["entry_count"] == 2
    assert len(payload["entries"]) == 2
    first = payload["entries"][0]
    assert first["index"] == 0
    assert first["entry_id"] == "01KA0PK97G9Q6AB0B17896Y1EB"
    assert first["header"].startswith("Entry: Codex")
    assert "body" not in first


def test_get_thread_entry_by_index(patched_context):
    result = server.get_thread_entry.fn(topic="entry-access-tools", index=1, code_path=".")
    payload = _extract_payload(result)

    assert payload["index"] == 1
    entry = payload["entry"]
    assert entry["entry_id"] == "01KA0PYSR7X43QQ61H1BCR3S2S"
    assert "Another body line" in entry["body"]
    assert entry["markdown"].startswith("Entry: Codex (caleb)")


def test_get_thread_entry_by_id(patched_context):
    result = server.get_thread_entry.fn(
        topic="entry-access-tools",
        entry_id="01KA0PK97G9Q6AB0B17896Y1EB",
        code_path=".",
    )
    payload = _extract_payload(result)
    assert payload["index"] == 0
    assert payload["entry"]["entry_id"] == "01KA0PK97G9Q6AB0B17896Y1EB"


def test_get_thread_entry_index_id_mismatch(patched_context):
    """Test that an error is returned when index and entry_id point to different entries."""
    result = server.get_thread_entry.fn(
        topic="entry-access-tools",
        index=0,  # Points to first entry with ID 01KA0PK97G9Q6AB0B17896Y1EB
        entry_id="01KA0PYSR7X43QQ61H1BCR3S2S",  # ID of second entry (index 1)
        code_path=".",
    )
    text = _extract_text(result)
    assert "Error" in text
    assert "different entries" in text


def test_get_thread_entry_range_inclusive(patched_context):
    result = server.get_thread_entry_range.fn(
        topic="entry-access-tools",
        start_index=0,
        end_index=1,
        code_path=".",
    )
    payload = _extract_payload(result)

    assert payload["start_index"] == 0
    assert payload["end_index"] == 1
    assert len(payload["entries"]) == 2


def test_entry_range_handles_open_end(patched_context):
    result = server.get_thread_entry_range.fn(
        topic="entry-access-tools",
        start_index=1,
        end_index=None,
        code_path=".",
    )
    payload = _extract_payload(result)
    assert payload["start_index"] == 1
    assert payload["end_index"] == 1
    assert len(payload["entries"]) == 1


def test_invalid_index_returns_error(patched_context):
    result = server.get_thread_entry.fn(topic="entry-access-tools", index=5, code_path=".")
    error_text = result.content[0].text
    assert "out of range" in error_text


def test_invalid_range_returns_error(patched_context):
    result = server.get_thread_entry_range.fn(
        topic="entry-access-tools",
        start_index=5,
        end_index=6,
        code_path=".",
    )
    error_text = result.content[0].text
    assert "out of range" in error_text or "must be" in error_text


def test_list_thread_entries_markdown(patched_context):
    result = server.list_thread_entries.fn(
        topic="entry-access-tools",
        code_path=".",
        format="markdown",
    )
    text = _extract_text(result)
    assert "Entries for 'entry-access-tools'" in text
    assert "[0]" in text


def test_get_thread_entry_markdown(patched_context):
    result = server.get_thread_entry.fn(
        topic="entry-access-tools",
        index=0,
        code_path=".",
        format="markdown",
    )
    text = _extract_text(result)
    assert text.startswith("Entry: Codex (caleb)")
    assert "Line A" in text


def test_get_thread_entry_range_markdown(patched_context):
    result = server.get_thread_entry_range.fn(
        topic="entry-access-tools",
        start_index=0,
        end_index=1,
        code_path=".",
        format="markdown",
    )
    text = _extract_text(result)
    assert text.count("Entry:") == 2
    assert "---" in text


def test_read_thread_json(patched_context):
    output = server.read_thread.fn(
        topic="entry-access-tools",
        code_path=".",
        format="json",
    )
    payload = json.loads(output)
    assert payload["entry_count"] == 2
    assert payload["meta"]["status"] == "open"


def test_read_thread_markdown_default(patched_context):
    output = server.read_thread.fn(
        topic="entry-access-tools",
        code_path=".",
    )
    assert output.startswith("# entry-access-tools")


def test_read_thread_invalid_format(patched_context):
    output = server.read_thread.fn(
        topic="entry-access-tools",
        code_path=".",
        format="xml",
    )
    assert "unsupported format" in output.lower()
