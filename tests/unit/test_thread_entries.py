from __future__ import annotations

from textwrap import dedent

from watercooler.thread_entries import parse_thread_entries


def _sample_thread() -> str:
    return dedent(
        """\
        # sample-thread — Thread
        Status: OPEN
        Ball: Codex (caleb)
        Topic: sample-thread
        Created: 2025-01-01T00:00:00Z

        ---
        Entry: Codex (caleb) 2025-01-01T00:01:00Z
        Role: planner
        Type: Plan
        Title: First Entry

        Spec: planner
        Body line 1
        Body line 2
        <!-- Entry-ID: 01ABCDEF1234567890ABCDEFGH -->

        ---
        Entry: Claude (caleb) 2025-01-01T00:02:00Z
        Role: critic
        Type: Note
        Title: Second Entry

        Spec: critic
        Another body line
        <!-- Entry-ID: 01ABCDEF1234567890ABCDEFGJ -->
        """
    )


def test_parse_thread_entries_extracts_metadata() -> None:
    text = _sample_thread()
    entries = parse_thread_entries(text)

    assert len(entries) == 2

    first = entries[0]
    assert first.agent == "Codex (caleb)"
    assert first.timestamp == "2025-01-01T00:01:00Z"
    assert first.role == "planner"
    assert first.entry_type == "Plan"
    assert first.title == "First Entry"
    assert first.entry_id == "01ABCDEF1234567890ABCDEFGH"
    assert first.start_line == 8
    assert first.end_line == 18  # includes trailing blank + separator
    segment = text[first.start_offset:first.end_offset]
    assert "Entry: Codex (caleb)" in segment
    assert "<!-- Entry-ID: 01ABCDEF1234567890ABCDEFGH -->" in segment

    second = entries[1]
    assert second.index == 1
    assert second.entry_id == "01ABCDEF1234567890ABCDEFGJ"
    assert second.start_line > first.end_line
    assert "Another body line" in second.body


def test_parse_thread_entries_handles_missing_entries() -> None:
    text = dedent(
        """\
        # empty-thread — Thread
        Status: OPEN
        Ball: Codex (caleb)
        Topic: empty-thread
        Created: 2025-01-01T00:00:00Z
        """
    )
    assert parse_thread_entries(text) == []


def test_parse_thread_entries_ignores_entry_in_code_block() -> None:
    """Entry: lines inside code blocks should be ignored."""
    text = dedent(
        """\
        # code-block-thread — Thread
        Status: OPEN
        Ball: Agent
        Topic: code-block-thread
        Created: 2025-01-01T00:00:00Z

        ---
        Entry: RealAgent (user) 2025-01-01T00:01:00Z
        Role: implementer
        Type: Note
        Title: Real Entry

        Here's some code:
        ```
        Entry: FakeAgent (fake) 2025-01-01T00:02:00Z
        This is inside a code block
        ```
        <!-- Entry-ID: 01REAL000000000000000000 -->
        """
    )
    entries = parse_thread_entries(text)
    assert len(entries) == 1
    assert entries[0].agent == "RealAgent (user)"
    assert entries[0].entry_id == "01REAL000000000000000000"


def test_parse_thread_entries_handles_mixed_length_fences() -> None:
    """Code fences must match char AND length per CommonMark spec.

    A 4-backtick fence should not be closed by a 3-backtick fence.
    """
    text = dedent(
        """\
        # fence-thread — Thread
        Status: OPEN
        Ball: Agent
        Topic: fence-thread
        Created: 2025-01-01T00:00:00Z

        ---
        Entry: RealAgent (user) 2025-01-01T00:01:00Z
        Role: implementer
        Type: Note
        Title: Real Entry

        Here's a nested code block example:
        ````markdown
        This shows how to use code blocks:
        ```
        Entry: FakeEntry (fake) 2025-01-01T00:00:00Z
        This is still inside the outer 4-backtick fence
        ```
        Still inside because we need 4+ backticks to close
        ````
        After the proper close
        <!-- Entry-ID: 01REAL000000000000000000 -->
        """
    )
    entries = parse_thread_entries(text)
    # Should only find the real entry, not the fake one inside nested fences
    assert len(entries) == 1
    assert entries[0].agent == "RealAgent (user)"


def test_parse_thread_entries_handles_tilde_fences() -> None:
    """Tilde fences (~~~) should also work and not mix with backticks."""
    text = dedent(
        """\
        # tilde-thread — Thread
        Status: OPEN
        Ball: Agent
        Topic: tilde-thread
        Created: 2025-01-01T00:00:00Z

        ---
        Entry: RealAgent (user) 2025-01-01T00:01:00Z
        Role: implementer
        Type: Note
        Title: Real Entry

        Tilde fence:
        ~~~
        Entry: FakeAgent (fake) 2025-01-01T00:00:00Z
        Inside tilde fence
        ```
        This backtick doesn't close the tilde fence
        ~~~
        Outside now
        <!-- Entry-ID: 01REAL000000000000000000 -->
        """
    )
    entries = parse_thread_entries(text)
    assert len(entries) == 1
    assert entries[0].agent == "RealAgent (user)"


def test_parse_thread_entries_handles_iso8601_timestamp_variants() -> None:
    """Parser should handle various ISO 8601 timestamp formats.

    Regression test for timestamps with:
    - Fractional seconds (.ffffff)
    - Timezone offset (+00:00 instead of Z)
    - Both fractional seconds and offset
    """
    text = dedent(
        """\
        # timestamps-thread — Thread
        Status: OPEN
        Ball: Agent
        Topic: timestamps-thread
        Created: 2025-01-01T00:00:00Z

        ---
        Entry: Agent1 (user) 2025-01-01T00:01:00Z
        Role: planner
        Type: Note
        Title: Basic timestamp (Z suffix)

        Entry with basic Z suffix
        <!-- Entry-ID: 01ENTRY1000000000000000000 -->

        ---
        Entry: Agent2 (user) 2025-01-01T00:02:00.123456Z
        Role: planner
        Type: Note
        Title: Fractional seconds with Z

        Entry with fractional seconds
        <!-- Entry-ID: 01ENTRY2000000000000000000 -->

        ---
        Entry: Agent3 (user) 2025-01-01T00:03:00+00:00
        Role: planner
        Type: Note
        Title: Offset timezone

        Entry with +00:00 offset
        <!-- Entry-ID: 01ENTRY3000000000000000000 -->

        ---
        Entry: Agent4 (user) 2025-01-01T00:04:00.654321+00:00
        Role: planner
        Type: Note
        Title: Fractional plus offset

        Entry with both fractional seconds and offset
        <!-- Entry-ID: 01ENTRY4000000000000000000 -->
        """
    )
    entries = parse_thread_entries(text)

    assert len(entries) == 4, f"Expected 4 entries, got {len(entries)}"

    assert entries[0].timestamp == "2025-01-01T00:01:00Z"
    assert entries[0].entry_id == "01ENTRY1000000000000000000"

    assert entries[1].timestamp == "2025-01-01T00:02:00.123456Z"
    assert entries[1].entry_id == "01ENTRY2000000000000000000"

    assert entries[2].timestamp == "2025-01-01T00:03:00+00:00"
    assert entries[2].entry_id == "01ENTRY3000000000000000000"

    assert entries[3].timestamp == "2025-01-01T00:04:00.654321+00:00"
    assert entries[3].entry_id == "01ENTRY4000000000000000000"
