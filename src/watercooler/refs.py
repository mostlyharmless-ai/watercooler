"""Canonical, human-navigable reference format for thread entries.

A single citation token of the form ``<thread_topic>:<index> (<entry_id>)`` — e.g.
``agent-authority-ladder-proposal-2026-05-13:12 (01KS0JTK0RT4EC0M92PMX19XRA)``.

The ``thread_topic`` prefix is the slug callers pass back to ``watercooler_write``;
the index locates the entry within the thread; the ULID is the precise,
globally-unique handle (the same identifier used in the ``<!-- ULID -->``
provenance markers in docs). Readable for humans, unambiguous for agents.
"""

from __future__ import annotations


def format_entry_ref(
    thread_topic: str | None,
    index: int | None,
    entry_id: str | None,
) -> str | None:
    """Render the canonical entry reference, or ``None`` if under-specified.

    Args:
        thread_topic: Thread slug (the write-back handle).
        index: Entry position within the thread.
        entry_id: Entry ULID.

    Returns:
        ``"<thread_topic>:<index> (<entry_id>)"`` when all three components are
        present, otherwise ``None`` — so callers can omit the field for
        thread-level or non-entry hits (e.g. T2 entities) rather than emit a
        malformed token.
    """
    if not thread_topic or index is None or not entry_id:
        return None
    return f"{thread_topic}:{index} ({entry_id})"
