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
        code_repo="mostlyharmless-ai/watercooler",
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
    result = server.list_thread_entries(topic="entry-access-tools", code_path=".")
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


def test_list_thread_entries_keyword_filter_matches_body(patched_context):
    """#325 — filter= narrows to entries whose body contains the keyword."""
    result = server.list_thread_entries(
        topic="entry-access-tools", code_path=".", filter="Line A"
    )
    payload = _extract_payload(result)
    assert payload["filter"] == "Line A"
    assert payload["entry_count"] == 1
    assert len(payload["entries"]) == 1
    assert payload["entries"][0]["index"] == 0


def test_list_thread_entries_keyword_filter_matches_title_caseless(patched_context):
    """#325 — the filter matches the entry title, case-insensitively."""
    result = server.list_thread_entries(
        topic="entry-access-tools", code_path=".", filter="closing"
    )
    payload = _extract_payload(result)
    assert payload["entry_count"] == 1
    assert payload["entries"][0]["index"] == 1


def test_list_thread_entries_keyword_filter_no_match(patched_context):
    result = server.list_thread_entries(
        topic="entry-access-tools", code_path=".", filter="zzz-no-such-keyword"
    )
    payload = _extract_payload(result)
    assert payload["entry_count"] == 0
    assert payload["entries"] == []


def test_list_thread_entries_no_filter_returns_all(patched_context):
    """A blank filter is a no-op — all entries returned, no filter field."""
    result = server.list_thread_entries(topic="entry-access-tools", code_path=".")
    payload = _extract_payload(result)
    assert payload["entry_count"] == 2
    assert "filter" not in payload


def test_list_thread_entries_hosted_markdown_echoes_filter(monkeypatch):
    """#325 — the hosted markdown path filters AND echoes [filter: ...] in the
    header, matching the local path (PR #826 review)."""
    from watercooler_mcp.tools import thread_query as tq

    class _E:
        def __init__(self, index, title, body):
            self.index = index
            self.title = title
            self.body = body
            self.entry_id = f"id{index}"
            self.timestamp = "2026-01-01T00:00:00Z"
            self.role = "implementer"
            self.entry_type = "Note"
            self.agent = "Tester"

        def __getattr__(self, _name):
            # Stub any other ThreadEntry field the header payload reads.
            return ""

    entries = [
        _E(0, "OAuth design", "body discussing oauth"),
        _E(1, "Unrelated note", "nothing relevant here"),
    ]
    fake_ctx = type("Ctx", (), {"code_branch": None, "code_repo": "x/y"})()

    monkeypatch.setattr(
        tq.validation, "_validate_thread_context", lambda _cp: (None, fake_ctx)
    )
    monkeypatch.setattr(tq, "is_hosted_context", lambda _c: True)
    monkeypatch.setattr(
        tq, "load_thread_entries_hosted", lambda _t: (None, entries)
    )

    result = tq._list_thread_entries_impl(
        topic="t", code_path=".", format="markdown", filter="oauth"
    )
    text = result.content[0].text
    assert "[filter: oauth]" in text
    assert "OAuth design" in text
    assert "Unrelated note" not in text


def test_get_thread_entry_by_index(patched_context):
    result = server.get_thread_entry(topic="entry-access-tools", index=1, code_path=".")
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
    result = server.get_thread_entry(
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
        server.get_thread_entry(
            topic="entry-access-tools",
            index=0,  # Points to first entry with ID 01KA0PK97G9Q6AB0B17896Y1EB
            entry_id="01KA0PYSR7X43QQ61H1BCR3S2S",  # ID of second entry (index 1)
            code_path=".",
        )
    assert "different entries" in str(exc_info.value)


def test_get_thread_entry_range_inclusive(patched_context):
    result = server.get_thread_entry(
        topic="entry-access-tools",
        index=0,
        to_index=1,
        code_path=".",
    )
    payload = _extract_payload(result)

    assert payload["start_index"] == 0
    assert payload["end_index"] == 1
    assert len(payload["entries"]) == 2


def test_entry_range_handles_open_end(patched_context):
    # Open-ended ranges are not exposed by the unified watercooler_get_thread_entry
    # tool (it requires an explicit to_index); the underlying range helper still
    # supports end_index=None and is exercised directly here.
    from watercooler_mcp.tools.thread_query import _get_thread_entry_range_impl

    result = _get_thread_entry_range_impl(
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
        server.get_thread_entry(topic="entry-access-tools", index=5, code_path=".")
    assert "out of range" in str(exc_info.value).lower()


def test_invalid_range_returns_error(patched_context):
    import pytest
    from watercooler_mcp.errors import IndexOutOfRangeError

    with pytest.raises(IndexOutOfRangeError) as exc_info:
        server.get_thread_entry(
            topic="entry-access-tools",
            index=5,
            to_index=6,
            code_path=".",
        )
    assert "out of range" in str(exc_info.value).lower() or "must be" in str(exc_info.value).lower()


def test_list_thread_entries_markdown(patched_context):
    result = server.list_thread_entries(
        topic="entry-access-tools",
        code_path=".",
        format="markdown",
    )
    text = _extract_text(result)
    assert "Entries for 'entry-access-tools'" in text
    assert "[0]" in text


def test_get_thread_entry_markdown(patched_context):
    result = server.get_thread_entry(
        topic="entry-access-tools",
        index=0,
        code_path=".",
        format="markdown",
    )
    text = _extract_text(result)
    assert text.startswith("Entry: Codex (caleb)")
    assert "Line A" in text


def test_get_thread_entry_range_markdown(patched_context):
    result = server.get_thread_entry(
        topic="entry-access-tools",
        index=0,
        to_index=1,
        code_path=".",
        format="markdown",
    )
    text = _extract_text(result)
    assert text.count("Entry:") == 2
    assert "---" in text


def test_read_thread_json(patched_context):
    output = server.read_thread(
        topic="entry-access-tools",
        code_path=".",
        format="json",
    )
    payload = json.loads(output)
    assert payload["entry_count"] == 2
    assert payload["meta"]["status"] == "OPEN"


def test_read_thread_markdown_default(patched_context):
    output = server.read_thread(
        topic="entry-access-tools",
        code_path=".",
    )
    assert output.startswith("# entry-access-tools")


def test_read_thread_invalid_format(patched_context):
    import pytest
    from watercooler_mcp.errors import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        server.read_thread(
            topic="entry-access-tools",
            code_path=".",
            format="xml",
        )
    assert "unsupported format" in str(exc_info.value).lower()
