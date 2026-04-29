"""Unit tests for migration/_remote.py — sync wrappers + pagination iteration."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from watercooler.migration import _remote


def _async_return(value):
    """Build a fresh coroutine that resolves to *value*.

    A coroutine can only be awaited once, so each test that drives
    call_remote_tool through asyncio.run needs its own coroutine.
    """
    async def _coro():
        return value
    return _coro()


class TestCallRemoteToolErrorMessages:
    """Round-3 review LOW-1: clear error if invoked from running event loop."""

    def test_helpful_error_when_called_from_running_loop(self) -> None:
        """The cryptic 'asyncio.run() cannot be called from a running event loop'
        is replaced with a pointer at the underlying coroutine API."""

        async def _runner():
            client = MagicMock()
            client.call_tool_text = lambda *a, **kw: _async_return("ok")
            # We're inside an event loop now. call_remote_tool's asyncio.run()
            # should raise — and we expect the rewrapped message.
            with pytest.raises(RuntimeError, match="await client.call_tool_text"):
                _remote.call_remote_tool(client, "tname", {"k": "v"})

        asyncio.run(_runner())

    def test_normal_call_returns_text(self) -> None:
        """Sanity: from a fresh sync context, the wrapper just works."""
        client = MagicMock()
        client.call_tool_text = lambda *a, **kw: _async_return('{"ok": true}')
        out = _remote.call_remote_tool(client, "tname", {"k": "v"})
        assert out == '{"ok": true}'


class TestListRemoteEmbeddingsPaginationDataLoss:
    """Round-3 review MEDIUM-1: don't early-return on empty `entries` if next_cursor is set.

    A whole page can be all-null-embedding rows (filtered out server-side)
    yet still carry a non-empty next_cursor. Returning here would silently
    drop everything past the first all-null page.
    """

    def test_empty_entries_with_next_cursor_continues_iteration(self) -> None:
        """Three pages: page 1 has data, page 2 is all-filtered (empty entries +
        non-empty next_cursor), page 3 has data again. Iteration must reach page 3.
        """
        # Each successive call_remote_tool returns the next page's JSON.
        page_responses = [
            json.dumps({
                "entries": [
                    {"entry_id": "01A", "thread_topic": "t",
                     "embedding": [0.1] * 1024, "group_id": "g"}
                ],
                "next_cursor": "01A",
                "page_size": 1,
            }),
            json.dumps({
                # All filtered server-side; entries empty BUT cursor is set.
                "entries": [],
                "next_cursor": "01M",
                "page_size": 0,
            }),
            json.dumps({
                "entries": [
                    {"entry_id": "01Z", "thread_topic": "t",
                     "embedding": [0.2] * 1024, "group_id": "g"}
                ],
                "next_cursor": "",  # end of stream
                "page_size": 1,
            }),
        ]

        with patch.object(_remote, "call_remote_tool", side_effect=page_responses):
            out = list(_remote.list_remote_embeddings(
                MagicMock(),
                target_group_id="g",
            ))

        # MUST get BOTH 01A and 01Z — pre-fix it would stop at page 2 and miss 01Z.
        eids = [r.entry_id for r in out]
        assert eids == ["01A", "01Z"]

    def test_empty_entries_with_empty_cursor_terminates(self) -> None:
        """End-of-stream remains correct: empty entries + empty cursor → stop."""
        with patch.object(_remote, "call_remote_tool", return_value=json.dumps({
            "entries": [],
            "next_cursor": "",
        })):
            out = list(_remote.list_remote_embeddings(
                MagicMock(),
                target_group_id="g",
            ))
        assert out == []

    def test_error_in_payload_raises_transport_error(self) -> None:
        """Round-6 review HIGH-1: silent truncation must raise, not return.

        Pre-fix: server error response just `return`-ed from the generator.
        Caller's for-loop ended normally; summary.errored stayed 0; the
        user checkpointed a partial pull and didn't know to re-run.
        Now we raise so the caller sees the truncation explicitly.
        """
        with patch.object(_remote, "call_remote_tool", return_value=json.dumps({
            "error": "scope_resolution_failed",
        })):
            with pytest.raises(_remote.MigrationTransportError, match="scope_resolution_failed"):
                list(_remote.list_remote_embeddings(
                    MagicMock(),
                    target_group_id="g",
                ))

    def test_unparseable_response_raises_transport_error(self) -> None:
        """Round-6 review HIGH-1: unparseable response must raise, not return."""
        with patch.object(_remote, "call_remote_tool", return_value="<html>500</html>"):
            with pytest.raises(_remote.MigrationTransportError, match="Unparseable"):
                list(_remote.list_remote_embeddings(
                    MagicMock(),
                    target_group_id="g",
                ))

    def test_malformed_embedding_in_one_row_skips_that_row_continues_iteration(self) -> None:
        """Proactive (post-round-7 fresh-eyes review): one bad row must not kill the run.

        Pre-fix: ``[float(x) for x in embedding]`` was unguarded inside the
        per-item loop. A single corrupted embedding (e.g. ``["abc", 0.1]``)
        from one server-side data-quality issue raised ValueError, killing
        the whole generator. Caller's iteration guard caught it as a
        generic Exception → entire migration tanked on one bad row.

        Now the bad row is skipped + logged; iteration continues.
        Mirrors the per-row try/except pattern in `list_local_entries`.
        """
        page = json.dumps({
            "entries": [
                {"entry_id": "01A", "thread_topic": "t",
                 "embedding": [0.1] * 1024, "group_id": "g"},
                {"entry_id": "01B", "thread_topic": "t",
                 "embedding": ["abc", 0.1, 0.2], "group_id": "g"},  # bad
                {"entry_id": "01C", "thread_topic": "t",
                 "embedding": [0.3] * 1024, "group_id": "g"},
            ],
            "next_cursor": "",
        })
        with patch.object(_remote, "call_remote_tool", return_value=page):
            out = list(_remote.list_remote_embeddings(
                MagicMock(),
                target_group_id="g",
            ))
        # 01B silently skipped (bad embedding); 01A and 01C come through.
        eids = [r.entry_id for r in out]
        assert eids == ["01A", "01C"], "Bad row must not abort iteration"

    def test_cursor_not_advancing_raises_to_avoid_infinite_loop(self) -> None:
        """Proactive: defensive against server-side pagination bugs.

        If the server returns the same cursor twice (broken pagination,
        replayed page, etc.), the client used to loop forever fetching
        the same page. Now it raises MigrationTransportError instead.
        """
        # Two pages, both ending with the SAME next_cursor — simulating a
        # buggy server that doesn't actually advance.
        page = json.dumps({
            "entries": [
                {"entry_id": "01A", "thread_topic": "t",
                 "embedding": [0.1] * 1024, "group_id": "g"},
            ],
            "next_cursor": "01A",  # not advancing
        })
        # call_remote_tool returns the same page on each call
        with patch.object(_remote, "call_remote_tool", return_value=page):
            collected = []
            with pytest.raises(_remote.MigrationTransportError, match="cursor did not advance"):
                for r in _remote.list_remote_embeddings(
                    MagicMock(),
                    target_group_id="g",
                ):
                    collected.append(r.entry_id)
                    if len(collected) > 100:
                        pytest.fail("Should have raised before collecting 100 entries")
        # First page yielded normally. Second iteration sends cursor="01A",
        # server returns next_cursor="01A" — detected BEFORE yielding the
        # duplicate row. So the caller sees 01A exactly once.
        assert collected == ["01A"]

    def test_transport_error_after_some_pages_yields_partial_then_raises(self) -> None:
        """Generator yields what it has, then raises when a later page errors.

        Pre-fix the user couldn't tell partial pull from end-of-stream.
        Now: yields rows from page 1, then raises on the page-2 error.
        Caller can count what it has plus increment errored.
        """
        page1 = json.dumps({
            "entries": [
                {"entry_id": "01A", "thread_topic": "t",
                 "embedding": [0.1] * 1024, "group_id": "g"},
            ],
            "next_cursor": "01A",
            "page_size": 1,
        })
        page2 = json.dumps({"error": "transient_failure"})

        with patch.object(_remote, "call_remote_tool", side_effect=[page1, page2]):
            collected = []
            with pytest.raises(_remote.MigrationTransportError, match="transient_failure"):
                for r in _remote.list_remote_embeddings(
                    MagicMock(),
                    target_group_id="g",
                ):
                    collected.append(r.entry_id)
            assert collected == ["01A"], "Page-1 yields visible to caller before raise"
