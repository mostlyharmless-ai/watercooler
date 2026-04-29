"""Unit tests for scripts/reconcile_entry_episode_index.py.

Pins the source_description parsing contract that was set in
``src/watercooler_mcp/daemons/t2_indexer.py:345`` (local marker) and 605
(hosted marker). If t2_indexer changes its source_description format,
these tests fail loudly so reconcile is updated in lockstep.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def reconcile_module():
    repo_root = Path(__file__).parent.parent.parent
    script_path = repo_root / "scripts" / "reconcile_entry_episode_index.py"
    spec = importlib.util.spec_from_file_location("reconcile_entry_episode_index", script_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reconcile_entry_episode_index"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestParseEntryIdFromSourceDescription:
    def test_local_daemon_format_no_entry_id(self, reconcile_module):
        # Format from t2_indexer.py:345 — local branch (no _hosted suffix).
        # Carries group_id + topic + source marker, but no explicit entry_id.
        s = "mostlyharmless_ai_watercooler_cloud_t2 | thread:hybrid-falkordb-state-vs-intent | t2_indexer_daemon"
        out = reconcile_module._parse_entry_id_from_source_description(s)
        assert out is not None
        entry_id, topic = out
        assert entry_id == ""
        assert topic == "hybrid-falkordb-state-vs-intent"

    def test_hosted_daemon_format_no_entry_id(self, reconcile_module):
        # Format from t2_indexer.py:605 — hosted branch.
        s = "mostlyharmless_ai_watercooler_cloud_t2 | thread:foo-bar | t2_indexer_daemon_hosted"
        out = reconcile_module._parse_entry_id_from_source_description(s)
        assert out is not None
        entry_id, topic = out
        assert entry_id == ""
        assert topic == "foo-bar"

    def test_with_explicit_entry_id_field(self, reconcile_module):
        # Future-proof: if t2_indexer is enriched to include entry_id directly.
        s = "group_x | thread:tx | entry_id:01ABCXYZ | t2_indexer_daemon"
        out = reconcile_module._parse_entry_id_from_source_description(s)
        assert out is not None
        entry_id, topic = out
        assert entry_id == "01ABCXYZ"
        assert topic == "tx"

    def test_topic_with_internal_dashes(self, reconcile_module):
        s = "group | thread:my-multi-dash-topic | t2_indexer_daemon"
        _, topic = reconcile_module._parse_entry_id_from_source_description(s)
        assert topic == "my-multi-dash-topic"

    def test_empty_source_returns_none(self, reconcile_module):
        assert reconcile_module._parse_entry_id_from_source_description("") is None

    def test_no_pipe_returns_none(self, reconcile_module):
        assert reconcile_module._parse_entry_id_from_source_description("just text") is None

    def test_no_thread_field_returns_none(self, reconcile_module):
        # Pipes present but no "thread:" segment — can't recover topic, skip.
        s = "group_x | t2_indexer_daemon"
        assert reconcile_module._parse_entry_id_from_source_description(s) is None

    def test_whitespace_tolerant(self, reconcile_module):
        s = "  group  |   thread:topic-a   |   t2_indexer_daemon_hosted  "
        out = reconcile_module._parse_entry_id_from_source_description(s)
        assert out is not None
        _, topic = out
        assert topic == "topic-a"


class TestQueryHostedT2Pagination:
    """Reviewer finding #1: ensure paginated query terminates correctly,
    doesn't request e.content (would OOM on large episodes), and uses
    parameterised SKIP/LIMIT bindings rather than f-string interpolation.
    """

    def _fake_client(self, pages: list[list[tuple]]):
        """Stub falkor_client.select_graph(...).query(...) returning successive pages."""
        from unittest.mock import MagicMock

        graph = MagicMock()
        results = []
        for page in pages:
            r = MagicMock()
            r.result_set = page
            results.append(r)
        graph.query.side_effect = results
        client = MagicMock()
        client.select_graph.return_value = graph
        return client, graph

    def test_terminates_on_partial_final_page(self, reconcile_module):
        client, graph = self._fake_client([
            [("u1", "group | thread:t1 | t2_indexer_daemon_hosted")] * 500,
            [("u2", "group | thread:t2 | t2_indexer_daemon_hosted")] * 500,
            [("u3", "group | thread:t3 | t2_indexer_daemon_hosted")] * 7,  # short page
        ])
        out = list(reconcile_module._query_hosted_t2("db_t2", client, page_size=500))
        assert len(out) == 1007
        # 3 calls expected (two full pages + one partial that terminates the loop).
        assert graph.query.call_count == 3

    def test_terminates_on_empty_first_page(self, reconcile_module):
        client, graph = self._fake_client([[]])
        out = list(reconcile_module._query_hosted_t2("db_t2", client, page_size=500))
        assert out == []
        assert graph.query.call_count == 1

    def test_uses_parameterised_skip_limit_not_fstring(self, reconcile_module):
        """Defensive: confirm SKIP/LIMIT come through as $params, not interpolated."""
        client, graph = self._fake_client([[]])
        list(reconcile_module._query_hosted_t2("db_t2", client, page_size=500))
        # Inspect the Cypher query string passed to graph.query.
        cypher_arg = graph.query.call_args.args[0]
        assert "$skip" in cypher_arg
        assert "$limit" in cypher_arg
        # And content is NOT projected (would OOM on large episodes).
        assert "e.content" not in cypher_arg
        # uuid + source_description ARE projected.
        assert "e.uuid" in cypher_arg
        assert "e.source_description" in cypher_arg

    def test_does_not_emit_content_field(self, reconcile_module):
        """Yielded dicts must not carry 'content' (was a memory hog field)."""
        client, _ = self._fake_client([
            [("u1", "group | thread:t1 | t2_indexer_daemon_hosted")],
        ])
        out = list(reconcile_module._query_hosted_t2("db_t2", client, page_size=500))
        assert out == [{"uuid": "u1", "source_description": "group | thread:t1 | t2_indexer_daemon_hosted"}]
        assert "content" not in out[0]
