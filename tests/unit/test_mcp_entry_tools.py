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


def _create_graph_data(threads_dir):
    """Create per-thread graph data matching _THREAD_TEXT content."""
    topic = "entry-access-tools"
    graph_dir = threads_dir / "graph" / "baseline" / "threads" / topic
    graph_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "type": "thread",
        "topic": topic,
        "title": "entry-access-tools",
        "status": "OPEN",
        "ball": "Codex (caleb)",
        "last_updated": "2025-11-14T08:15:55Z",
        "summary": "",
        "entry_count": 2,
    }
    (graph_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    entries = [
        {
            "entry_id": "01KA0PK97G9Q6AB0B17896Y1EB",
            "thread_topic": topic,
            "index": 0,
            "agent": "Codex (caleb)",
            "role": "planner",
            "entry_type": "Plan",
            "title": "Plan: entry-level MCP tooling",
            "timestamp": "2025-11-14T08:09:39Z",
            "summary": "Plan for entry-level MCP tooling",
            "body": "Spec: planner-architecture\nLine A\n",
        },
        {
            "entry_id": "01KA0PYSR7X43QQ61H1BCR3S2S",
            "thread_topic": topic,
            "index": 1,
            "agent": "Codex (caleb)",
            "role": "planner",
            "entry_type": "Note",
            "title": "Closing: wrong repo context",
            "timestamp": "2025-11-14T08:15:55Z",
            "summary": "Closing: wrong repo context",
            "body": "Spec: planner-architecture\nAnother body line\n",
        },
    ]
    lines = [json.dumps(e) for e in entries]
    (graph_dir / "entries.jsonl").write_text("\n".join(lines) + "\n")


@pytest.fixture
def patched_context(tmp_path, monkeypatch):
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    thread_path = threads_dir / "entry-access-tools.md"
    thread_path.write_text(_THREAD_TEXT, encoding="utf-8")

    # Create graph data (source of truth for reads)
    _create_graph_data(threads_dir)

    context = ThreadContext(
        code_root=tmp_path,
        threads_dir=threads_dir,
        code_repo="mostlyharmless-ai/watercooler-cloud",
        code_branch="main",
        code_commit="abc1234",
        code_remote="origin",
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
    assert "summary" in first
    # Vestigial MD fields must not appear
    assert "header" not in first
    assert "start_line" not in first
    assert "end_line" not in first
    assert "start_offset" not in first
    assert "end_offset" not in first
    assert "body" not in first


def test_get_thread_entry_by_index(patched_context):
    result = server.get_thread_entry.fn(topic="entry-access-tools", index=1, code_path=".")
    payload = _extract_payload(result)

    assert payload["index"] == 1
    entry = payload["entry"]
    assert entry["entry_id"] == "01KA0PYSR7X43QQ61H1BCR3S2S"
    assert "Another body line" in entry["body"]
    assert "summary" in entry
    # Vestigial fields must not appear
    assert "markdown" not in entry
    assert "header" not in entry
    assert "start_line" not in entry


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
    """Test that an error is raised when index and entry_id point to different entries."""
    import pytest
    from watercooler_mcp.errors import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        server.get_thread_entry.fn(
            topic="entry-access-tools",
            index=0,  # Points to first entry with ID 01KA0PK97G9Q6AB0B17896Y1EB
            entry_id="01KA0PYSR7X43QQ61H1BCR3S2S",  # ID of second entry (index 1)
            code_path=".",
        )
    assert "different entries" in str(exc_info.value)


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
    import pytest
    from watercooler_mcp.errors import IndexOutOfRangeError

    with pytest.raises(IndexOutOfRangeError) as exc_info:
        server.get_thread_entry.fn(topic="entry-access-tools", index=5, code_path=".")
    assert "out of range" in str(exc_info.value).lower()


def test_invalid_range_returns_error(patched_context):
    import pytest
    from watercooler_mcp.errors import IndexOutOfRangeError

    with pytest.raises(IndexOutOfRangeError) as exc_info:
        server.get_thread_entry_range.fn(
            topic="entry-access-tools",
            start_index=5,
            end_index=6,
            code_path=".",
        )
    assert "out of range" in str(exc_info.value).lower() or "must be" in str(exc_info.value).lower()


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
    import pytest
    from watercooler_mcp.errors import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        server.read_thread.fn(
            topic="entry-access-tools",
            code_path=".",
            format="xml",
        )
    assert "unsupported format" in str(exc_info.value).lower()
