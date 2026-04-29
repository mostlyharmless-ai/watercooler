"""Unit tests for migration/_local.py helpers.

Most _local helpers (connect, list, upsert) require a live FalkorDB
client to test meaningfully. This file covers the input-validation
guards that are pure-Python and important for safety.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from watercooler.migration._local import close_local_falkor, ensure_local_indexes


class TestCloseLocalFalkor:
    def test_none_is_noop(self) -> None:
        # Must not raise.
        close_local_falkor(None)

    def test_calls_close_when_present(self) -> None:
        client = MagicMock()
        client.close = MagicMock()
        close_local_falkor(client)
        client.close.assert_called_once()

    def test_falls_through_to_pool_disconnect_when_no_close(self) -> None:
        client = MagicMock(spec=["connection_pool"])
        client.connection_pool = MagicMock()
        client.connection_pool.disconnect = MagicMock()
        close_local_falkor(client)
        client.connection_pool.disconnect.assert_called_once()

    def test_silently_swallows_close_failure(self) -> None:
        client = MagicMock()
        client.close = MagicMock(side_effect=RuntimeError("simulated"))
        # Must not raise — close is best-effort.
        close_local_falkor(client)


class TestEnsureLocalIndexesDimGuard:
    """Pin the Cypher-injection guard added in PR #678 review round 2."""

    def test_valid_int_dim_runs_queries(self) -> None:
        client = MagicMock()
        graph = MagicMock()
        graph.query.return_value = None
        client.select_graph.return_value = graph
        ensure_local_indexes(client, graph_name="g", dim=1024)
        # Two queries: vector index + range index.
        assert graph.query.call_count == 2
        vector_q = graph.query.call_args_list[0].args[0]
        assert "dimension: 1024" in vector_q

    def test_string_dim_int_form_accepted(self) -> None:
        client = MagicMock()
        graph = MagicMock()
        graph.query.return_value = None
        client.select_graph.return_value = graph
        ensure_local_indexes(client, graph_name="g", dim="384")
        vector_q = graph.query.call_args_list[0].args[0]
        assert "dimension: 384" in vector_q

    def test_non_integer_dim_rejected(self) -> None:
        client = MagicMock()
        with pytest.raises(ValueError, match="positive integer"):
            ensure_local_indexes(client, graph_name="g", dim="abc")

    def test_negative_dim_rejected(self) -> None:
        client = MagicMock()
        with pytest.raises(ValueError, match=r"\(0, 100000\]"):
            ensure_local_indexes(client, graph_name="g", dim=-1)

    def test_zero_dim_rejected(self) -> None:
        client = MagicMock()
        with pytest.raises(ValueError, match=r"\(0, 100000\]"):
            ensure_local_indexes(client, graph_name="g", dim=0)

    def test_absurdly_large_dim_rejected(self) -> None:
        client = MagicMock()
        with pytest.raises(ValueError, match=r"\(0, 100000\]"):
            ensure_local_indexes(client, graph_name="g", dim=999999)

    def test_cypher_injection_via_dim_string_blocked(self) -> None:
        """Defense in depth: even if a future caller passes a non-validated string."""
        client = MagicMock()
        with pytest.raises(ValueError, match="positive integer"):
            ensure_local_indexes(
                client,
                graph_name="g",
                dim="1024, similarityFunction: 'cosine'} } DROP DATABASE; //",
            )
