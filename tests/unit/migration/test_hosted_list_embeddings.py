"""Unit tests for hosted_semantic.list_embeddings_t1 + the MCP tool surface."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from watercooler_mcp import hosted_semantic


class _FakeRow:
    """Match graph.query().result_set rows: tuple-indexable."""

    def __init__(self, *vals):
        self.vals = list(vals)

    def __getitem__(self, idx):
        return self.vals[idx]


def _make_graph(rows):
    g = MagicMock()
    res = MagicMock()
    res.result_set = rows
    g.query.return_value = res
    return g


class TestListEmbeddingsT1:
    def test_returns_entries_with_embeddings(self) -> None:
        rows = [
            _FakeRow("01ABC", "topic-a", [0.1, 0.2, 0.3], "implementer", "Note", "agent-x", "ts1"),
            _FakeRow("01DEF", "topic-b", [0.4, 0.5], "planner", "Plan", "agent-y", "ts2"),
        ]
        with patch.object(hosted_semantic, "_select_graph", return_value=_make_graph(rows)):
            out = hosted_semantic.list_embeddings_t1(
                database="my_db_t1", group_id="my_group", limit=200,
            )
        assert "error" not in out
        assert out["entries_returned"] == 2
        assert len(out["entries"]) == 2
        assert out["entries"][0]["entry_id"] == "01ABC"
        assert out["entries"][0]["thread_topic"] == "topic-a"
        assert out["entries"][0]["embedding"] == [0.1, 0.2, 0.3]
        assert out["entries"][0]["role"] == "implementer"
        assert out["entries"][0]["group_id"] == "my_group"

    def test_next_cursor_when_full_page(self) -> None:
        # 3 rows, limit=3 → last entry_id is the cursor for the next page.
        rows = [
            _FakeRow("01A", "t", [0.1], "", "", "", ""),
            _FakeRow("01B", "t", [0.2], "", "", "", ""),
            _FakeRow("01C", "t", [0.3], "", "", "", ""),
        ]
        with patch.object(hosted_semantic, "_select_graph", return_value=_make_graph(rows)):
            out = hosted_semantic.list_embeddings_t1(
                database="d", group_id="g", limit=3,
            )
        assert out["next_cursor"] == "01C"

    def test_empty_cursor_when_partial_page(self) -> None:
        # 1 row, limit=10 → end of stream, no next cursor.
        rows = [_FakeRow("01A", "t", [0.1], "", "", "", "")]
        with patch.object(hosted_semantic, "_select_graph", return_value=_make_graph(rows)):
            out = hosted_semantic.list_embeddings_t1(
                database="d", group_id="g", limit=10,
            )
        assert out["next_cursor"] == ""

    def test_skips_rows_without_embedding(self) -> None:
        rows = [
            _FakeRow("01A", "t", None, "", "", "", ""),       # no embedding → skip
            _FakeRow("01B", "t", [0.2], "", "", "", ""),
            _FakeRow("", "t", [0.3], "", "", "", ""),         # no entry_id → skip
        ]
        with patch.object(hosted_semantic, "_select_graph", return_value=_make_graph(rows)):
            out = hosted_semantic.list_embeddings_t1(
                database="d", group_id="g", limit=10,
            )
        assert out["entries_returned"] == 1
        assert out["entries"][0]["entry_id"] == "01B"

    def test_missing_database_returns_error(self) -> None:
        out = hosted_semantic.list_embeddings_t1(
            database="", group_id="g", limit=10,
        )
        assert out["error"] == "missing_database"
        assert out["entries"] == []

    def test_missing_group_id_returns_error(self) -> None:
        out = hosted_semantic.list_embeddings_t1(
            database="d", group_id="", limit=10,
        )
        assert out["error"] == "missing_group_id"

    def test_limit_capped_to_1000(self) -> None:
        captured_params = {}

        def _fake_query(query, params):
            captured_params.update(params)
            res = MagicMock()
            res.result_set = []
            return res

        graph = MagicMock()
        graph.query.side_effect = _fake_query
        with patch.object(hosted_semantic, "_select_graph", return_value=graph):
            hosted_semantic.list_embeddings_t1(
                database="d", group_id="g", limit=10000,
            )
        assert captured_params["limit"] == 1000

    def test_limit_zero_defaults_to_200(self) -> None:
        captured_params = {}

        def _fake_query(query, params):
            captured_params.update(params)
            res = MagicMock()
            res.result_set = []
            return res

        graph = MagicMock()
        graph.query.side_effect = _fake_query
        with patch.object(hosted_semantic, "_select_graph", return_value=graph):
            hosted_semantic.list_embeddings_t1(
                database="d", group_id="g", limit=0,
            )
        assert captured_params["limit"] == 200

    def test_pagination_cursor_uses_raw_row_count_not_post_filter(self) -> None:
        """Round-2 review: filtered-out rows must NOT clear next_cursor.

        Pre-fix: 3 raw rows returned (limit=3), 1 filtered out as bad
        embedding → len(entries)=2 < limit → next_cursor cleared, even
        though FalkorDB had more data after this page. Result: silent
        truncation of every entry past the first filter gap.
        """
        rows = [
            _FakeRow("01A", "t", [0.1], "", "", "", ""),
            _FakeRow("01B", "t", None, "", "", "", ""),  # filtered out
            _FakeRow("01C", "t", [0.3], "", "", "", ""),
        ]
        with patch.object(hosted_semantic, "_select_graph", return_value=_make_graph(rows)):
            out = hosted_semantic.list_embeddings_t1(
                database="d", group_id="g", limit=3,
            )
        # 2 entries (the filtered row dropped), but cursor advances to the
        # last RAW entry_id ("01C") so the next page picks up correctly.
        assert out["entries_returned"] == 2
        assert out["next_cursor"] == "01C", (
            "Cursor must be the last raw entry_id, not the last filtered entry_id"
        )

    def test_pagination_cursor_empty_when_partial_raw_page(self) -> None:
        """If FalkorDB returned fewer than `limit` raw rows, we ARE at the end."""
        rows = [
            _FakeRow("01A", "t", [0.1], "", "", "", ""),
            _FakeRow("01B", "t", [0.2], "", "", "", ""),
        ]
        with patch.object(hosted_semantic, "_select_graph", return_value=_make_graph(rows)):
            out = hosted_semantic.list_embeddings_t1(
                database="d", group_id="g", limit=10,
            )
        assert out["entries_returned"] == 2
        assert out["next_cursor"] == "", "Partial raw page → end of stream"

    def test_uses_parameterised_cypher(self) -> None:
        """Defensive: confirm cursor + group_id come through as $params, not interpolated."""
        graph = MagicMock()
        res = MagicMock()
        res.result_set = []
        graph.query.return_value = res
        with patch.object(hosted_semantic, "_select_graph", return_value=graph):
            hosted_semantic.list_embeddings_t1(
                database="d", group_id="g; DROP DATABASE",  # injection attempt
                cursor="' OR '1'='1", limit=10,
            )
        cypher_query = graph.query.call_args.args[0]
        params = graph.query.call_args.args[1]
        assert "$cursor" in cypher_query
        assert "$group_id" in cypher_query
        assert "$limit" in cypher_query
        # The malicious values come through as bound params, not interpolated.
        assert params["group_id"] == "g; DROP DATABASE"
        assert params["cursor"] == "' OR '1'='1"
