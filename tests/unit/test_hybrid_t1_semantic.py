"""Plan v20 Phase 8: hybrid T1 semantic routing tests.

Covers:
- resolve_search_capability: entries + semantic => semantic_similarity.
- baseline_graph.sync embedding write callbacks route to the remote handler
  and suppress local JSONL fallback.
- t1_hybrid.install_hybrid_callbacks registers the right callbacks based on
  runtime surface.
- hosted_semantic query formatting helpers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from watercooler.baseline_graph import sync as bg_sync
from watercooler_mcp import hosted_semantic, t1_hybrid
from watercooler_mcp import memory_sync
from watercooler_mcp.capabilities import resolve_search_capability


class _FakeQueryResult:
    """Mimics :class:`falkordb.QueryResult` enough for _rows()."""

    def __init__(self, rows: List[List[Any]]) -> None:
        self.result_set = rows


class _FakeGraph:
    """Stand-in for a FalkorDB ``Graph`` returned by the sync SDK.

    Records every ``(cypher, params)`` pair on ``calls`` and returns a
    configurable :class:`_FakeQueryResult`.
    """

    def __init__(self, rows: List[List[Any]] | None = None) -> None:
        self.calls: List[tuple[str, Dict[str, Any]]] = []
        self._rows = rows or []

    def query(self, cypher: str, params: Dict[str, Any] | None = None) -> Any:
        self.calls.append((cypher, dict(params or {})))
        return _FakeQueryResult(self._rows)


@pytest.fixture(autouse=True)
def _reset_hybrid_globals():
    """Snapshot-and-restore the module globals that hybrid routing mutates.

    PR #654 in-PR review (MEDIUM — test isolation): tests that assign to
    ``bg_sync._T1_REMOTE_UPSERT`` / ``_T1_REMOTE_DELETE`` /
    ``_HYBRID_T2_HANDOFF_ACTIVE`` or ``memory_sync._runtime`` must restore
    them even on failure, or a crashing test will poison every subsequent
    test in the same process. This autouse fixture is the belt to the
    per-test try/finally suspenders.
    """
    saved_upsert = bg_sync._T1_REMOTE_UPSERT
    saved_delete = bg_sync._T1_REMOTE_DELETE
    saved_t2 = bg_sync._HYBRID_T2_HANDOFF_ACTIVE
    saved_runtime = memory_sync._runtime
    saved_ensured = set(hosted_semantic._ENSURED_INDEXES)

    # Phase 8 post-merge fix: upsert_embedding now calls _ensure_entry_indexes
    # on first touch of each database. Pre-populate the cache with the
    # test DB names used throughout this file so existing assertions on
    # ``fake.calls[0]`` keep indexing the MERGE Entry query rather than
    # the newly-added vector-index CREATE. Tests that intentionally
    # exercise the ensure_index path clear the set locally.
    hosted_semantic._ENSURED_INDEXES.update({
        "x_t1",
        "mostlyharmless_ai_watercooler_cloud_t1",
    })
    try:
        yield
    finally:
        bg_sync._T1_REMOTE_UPSERT = saved_upsert
        bg_sync._T1_REMOTE_DELETE = saved_delete
        bg_sync._HYBRID_T2_HANDOFF_ACTIVE = saved_t2
        memory_sync._runtime = saved_runtime
        hosted_semantic._ENSURED_INDEXES.clear()
        hosted_semantic._ENSURED_INDEXES.update(saved_ensured)


class TestResolveSearchCapability:
    def test_entries_semantic_routes_remote_capability(self) -> None:
        assert resolve_search_capability(
            "entries", semantic=True
        ) == "semantic_similarity"

    def test_entries_non_semantic_remains_baseline(self) -> None:
        assert resolve_search_capability(
            "entries", semantic=False
        ) == "baseline_search"

    def test_facts_mode_still_memory_query(self) -> None:
        assert resolve_search_capability("facts") == "memory_query"


class TestHybridDefaultRoutes:
    def test_semantic_similarity_defaults_remote(self) -> None:
        # Codex review: semantic_similarity must default to "remote" so
        # hybrid's find_similar / semantic entry search lands on the hosted
        # T1 FalkorDB rather than silently using the local JSONL fallback.
        from watercooler_mcp.capabilities import HYBRID_DEFAULT_ROUTES

        assert HYBRID_DEFAULT_ROUTES["semantic_similarity"] == "remote"


class TestHostedSemanticSchemaMatchesLocal:
    """Codex review: hosted_semantic must match the local T1 FalkorDB schema."""

    def test_upsert_uses_entry_embedding_property(self) -> None:
        """Upsert writes n.embedding = vecf32(...) on :Entry, not :Entry_Embedding."""
        fake = _FakeGraph()
        with patch.object(hosted_semantic, "_select_graph", return_value=fake):
            hosted_semantic.upsert_embedding(
                database="mostlyharmless_ai_watercooler_cloud_t1",
                entry_id="E1",
                topic="t",
                embedding=[0.1, 0.2],
                group_id="mostlyharmless_ai_watercooler_cloud",
            )
        assert len(fake.calls) == 1
        cypher, params = fake.calls[0]
        # Schema must match falkordb_entries.store_embedding: MERGE on Entry
        # with embedding property, NOT Entry_Embedding.
        assert "MERGE (n:Entry {entry_id: $entry_id})" in cypher
        assert "n.embedding = vecf32($embedding)" in cypher
        assert "Entry_Embedding" not in cypher
        assert "HAS_EMBEDDING" not in cypher
        # Params go through the SDK as a dict — not a stringified --params.
        assert params["entry_id"] == "E1"
        assert params["embedding"] == [0.1, 0.2]

    def test_search_uses_entry_embedding_index(self) -> None:
        fake = _FakeGraph(rows=[])
        with patch.object(hosted_semantic, "_select_graph", return_value=fake):
            hosted_semantic.search_semantic_entries(
                database="x_t1",
                query_embedding=[0.1],
                group_id="x",
                limit=5,
            )
        cypher, _params = fake.calls[0]
        assert "db.idx.vector.queryNodes('Entry', 'embedding'" in cypher
        assert "node.group_id = $group_id" in cypher


class TestBaselineGraphHybridCallback:
    def test_upsert_callback_replaces_falkor_path(self, tmp_path: Path) -> None:
        calls: List[Any] = []

        def _remote(
            threads_dir: Path,
            entry_id: str,
            topic: str,
            embedding: List[float],
            role: str = "",
            entry_type: str = "",
            agent: str = "",
            timestamp: str = "",
        ) -> bool:
            calls.append(
                (
                    threads_dir,
                    entry_id,
                    topic,
                    list(embedding),
                    role,
                    entry_type,
                    agent,
                    timestamp,
                )
            )
            return True

        bg_sync.register_t1_remote_embedding_callbacks(upsert=_remote, delete=None)
        try:
            ok = bg_sync.store_entry_embedding_to_falkordb(
                tmp_path / "repo-threads",
                "E1",
                "topic-x",
                [0.1, 0.2],
                role="implementer",
                entry_type="Note",
                agent="Claude",
                timestamp="2026-04-24T06:00:00Z",
            )
        finally:
            bg_sync.register_t1_remote_embedding_callbacks(upsert=None, delete=None)

        assert ok is True
        assert calls == [(
            tmp_path / "repo-threads",
            "E1",
            "topic-x",
            [0.1, 0.2],
            "implementer",
            "Note",
            "Claude",
            "2026-04-24T06:00:00Z",
        )]

    def test_upsert_embedding_skips_jsonl_fallback_in_hybrid(
        self, tmp_path: Path
    ) -> None:
        bg_sync.register_t1_remote_embedding_callbacks(
            upsert=lambda *a, **kw: True, delete=None
        )
        try:
            with patch.object(
                bg_sync.storage, "upsert_search_index_entry"
            ) as mock_upsert_file:
                bg_sync.upsert_embedding(
                    threads_dir=tmp_path / "x-threads",
                    graph_dir=tmp_path / "graph",
                    entry_id="E2",
                    topic="t",
                    embedding=[0.0, 1.0],
                )
        finally:
            bg_sync.register_t1_remote_embedding_callbacks(upsert=None, delete=None)

        mock_upsert_file.assert_not_called()

    def test_upsert_embedding_writes_jsonl_when_no_callback(
        self, tmp_path: Path
    ) -> None:
        bg_sync.register_t1_remote_embedding_callbacks(upsert=None, delete=None)
        # Force the FalkorDB path to say "unavailable" by poisoning the module
        # cache — we only want to verify the fallback branch fires.
        with patch.object(bg_sync, "store_entry_embedding_to_falkordb", return_value=False), \
             patch.object(bg_sync.storage, "upsert_search_index_entry") as mock_file:
            bg_sync.upsert_embedding(
                threads_dir=tmp_path / "x-threads",
                graph_dir=tmp_path / "graph",
                entry_id="E3",
                topic="t",
                embedding=[0.2],
            )
        mock_file.assert_called_once()


class TestT1HybridInstall:
    def test_install_registers_callbacks_in_hybrid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WATERCOOLER_HANDOFF_RECEIPTS_FILE", str(tmp_path / "rh.jsonl"))
        runtime = MagicMock()
        runtime.surface = "local_hybrid"
        runtime.premium_client = MagicMock()
        runtime.premium_client.call_tool_text = AsyncMock(
            return_value=json.dumps({"success": True, "status": "upserted"})
        )

        t1_hybrid.install_hybrid_callbacks(runtime)
        try:
            ok = bg_sync.store_entry_embedding_to_falkordb(
                tmp_path / "repo-threads", "E4", "t", [0.3]
            )
        finally:
            t1_hybrid.install_hybrid_callbacks(None)
        assert ok is True
        runtime.premium_client.call_tool_text.assert_awaited_once()

    def test_install_clears_callbacks_in_non_hybrid(self, tmp_path: Path) -> None:
        runtime = MagicMock()
        runtime.surface = "local_full"
        t1_hybrid.install_hybrid_callbacks(runtime)
        # After install, callbacks must be None so upstream falls back normally.
        assert bg_sync._t1_remote_upsert_enabled() is False
        assert bg_sync._t1_remote_delete_enabled() is False


class TestHostedSemanticFormatters:
    def test_coerce_vector_accepts_floats(self) -> None:
        assert hosted_semantic._coerce_vector([1, 2.5, "3"]) == [1.0, 2.5, 3.0]

    def test_coerce_vector_rejects_invalid(self) -> None:
        assert hosted_semantic._coerce_vector("not-a-list") is None


class TestHostedSemanticParamsBinding:
    """PR #654 in-PR review (HIGH): the prior implementation used raw
    ``client.execute_command("GRAPH.QUERY", db, cypher, "--params", str)``
    against redis-py, which is not the FalkorDB wire format — the SDK
    combines the params with the query via its own marshaller. These tests
    pin the current implementation to the SDK form: parameters flow
    through ``Graph.query(cypher, params_dict)`` as a native dict, and
    string values with backslashes / quotes / newlines bind correctly
    because the SDK handles escaping."""

    def test_upsert_binds_params_as_dict(self) -> None:
        fake = _FakeGraph()
        with patch.object(hosted_semantic, "_select_graph", return_value=fake):
            hosted_semantic.upsert_embedding(
                database="x_t1",
                entry_id="E-bind-1",
                topic="t",
                embedding=[0.1],
                group_id="x",
                role="it's-messy",
                entry_type="Note\\backslash",
                agent="a\nb",
                timestamp="2026-04-24T09:00:00Z",
            )
        _cypher, params = fake.calls[0]
        # No stringification: the SDK gets the raw python strings.
        assert params["role"] == "it's-messy"
        assert params["entry_type"] == "Note\\backslash"
        assert params["agent"] == "a\nb"

    def test_no_params_string_helper_leaks(self) -> None:
        # If someone tries to re-introduce a --params string builder, this
        # test fails loudly.
        assert not hasattr(hosted_semantic, "_params_to_string")
        assert not hasattr(hosted_semantic, "_format_value")


class TestLocalFalkorSyncWrapperPassesMetadata:
    """Codex re-review round 4 (01KPZ5ZH65WQM503HBB06EWEVA): regression
    guard. The stdio/local T1 path calls FalkorDBEntryStoreSync via
    get_falkordb_entry_store(); if the sync wrapper's store_embedding
    signature doesn't accept the metadata kwargs, store_entry_embedding_to_falkordb
    raises TypeError and silently falls back to JSONL. This test exercises
    the real sync→async chain with stubbed _run_async so we catch any
    future signature drift before it lands on main."""

    def test_sync_store_embedding_forwards_metadata_kwargs(self) -> None:
        from watercooler.baseline_graph import falkordb_entries as fe

        captured: dict[str, Any] = {}

        class _FakeAsyncStore:
            async def store_embedding(
                self,
                entry_id,
                thread_topic,
                embedding,
                *,
                role="",
                entry_type="",
                agent="",
                timestamp="",
            ):
                captured["entry_id"] = entry_id
                captured["thread_topic"] = thread_topic
                captured["embedding"] = list(embedding)
                captured["role"] = role
                captured["entry_type"] = entry_type
                captured["agent"] = agent
                captured["timestamp"] = timestamp

        def _fake_run_async(coro):
            import asyncio

            # Use a fresh loop — prior tests in the suite may have left a
            # closed or running loop in the thread-local default slot.
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        sync_store = fe.FalkorDBEntryStoreSync.__new__(fe.FalkorDBEntryStoreSync)
        sync_store._async_store = _FakeAsyncStore()

        with patch.object(fe, "_run_async", _fake_run_async):
            sync_store.store_embedding(
                "E-local-1",
                "hybrid-falkordb-state-vs-intent",
                [0.1, 0.2, 0.3],
                role="implementer",
                entry_type="Note",
                agent="Claude",
                timestamp="2026-04-24T07:00:00Z",
            )

        assert captured["entry_id"] == "E-local-1"
        assert captured["role"] == "implementer"
        assert captured["entry_type"] == "Note"
        assert captured["agent"] == "Claude"
        assert captured["timestamp"] == "2026-04-24T07:00:00Z"

    def test_store_entry_embedding_to_falkordb_forwards_metadata_to_store(
        self, tmp_path: Path
    ) -> None:
        """End-to-end guard: the sync.py layer calls store.store_embedding
        with the metadata kwargs. If the sync wrapper signature regresses
        to (entry_id, topic, embedding) only, this test fails because the
        TypeError propagates before the except-branch catches it."""
        from watercooler.baseline_graph import sync as bg_sync

        captured: dict[str, Any] = {}

        class _FakeStore:
            def store_embedding(
                self,
                entry_id,
                thread_topic,
                embedding,
                *,
                role="",
                entry_type="",
                agent="",
                timestamp="",
            ):
                captured["entry_id"] = entry_id
                captured["role"] = role
                captured["entry_type"] = entry_type
                captured["agent"] = agent
                captured["timestamp"] = timestamp

        # Ensure no remote callback intercepts.
        bg_sync.register_t1_remote_embedding_callbacks(upsert=None, delete=None)

        # Module-level cache state from earlier tests may linger; force re-probe.
        bg_sync._falkordb_checked = False
        bg_sync._falkordb_available = False

        with patch(
            "watercooler.baseline_graph.falkordb_entries.get_falkordb_entry_store",
            return_value=_FakeStore(),
        ):
            ok = bg_sync.store_entry_embedding_to_falkordb(
                tmp_path / "repo-threads",
                "E-local-2",
                "topic-y",
                [0.5],
                role="planner",
                entry_type="Plan",
                agent="Codex",
                timestamp="2026-04-24T07:10:00Z",
            )

        assert ok is True
        assert captured["role"] == "planner"
        assert captured["entry_type"] == "Plan"
        assert captured["agent"] == "Codex"
        assert captured["timestamp"] == "2026-04-24T07:10:00Z"


class TestT1MaterializesMetadata:
    """Codex re-review §1: hosted semantic filters query fields that T1
    never stored. The T1 write surface now materialises role / entry_type
    / agent / timestamp on the Entry node so filters match real data."""

    def test_hosted_upsert_writes_metadata_properties(self) -> None:
        fake = _FakeGraph()
        with patch.object(hosted_semantic, "_select_graph", return_value=fake):
            hosted_semantic.upsert_embedding(
                database="x_t1",
                entry_id="E-meta-1",
                topic="t",
                embedding=[0.1, 0.2],
                group_id="x",
                role="implementer",
                entry_type="Note",
                agent="Claude",
                timestamp="2026-04-24T06:00:00Z",
            )
        cypher, _params = fake.calls[0]
        assert "n.role = $role" in cypher
        assert "n.entry_type = $entry_type" in cypher
        assert "n.agent = $agent" in cypher
        assert "n.timestamp = $timestamp" in cypher

    def test_semantic_tool_forwards_metadata_to_hosted_upsert(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # tools/semantic imports ``upsert_embedding`` at module level, so
        # patch the bound reference on that module rather than on
        # hosted_semantic. Either works at runtime; this form is the
        # portable way.
        from watercooler_mcp.tools import semantic as semantic_tool_module

        calls: list[dict] = []

        def _capture(**kwargs):
            calls.append(kwargs)
            return {"success": True, "status": "upserted", "entry_id": kwargs["entry_id"]}

        monkeypatch.setattr(semantic_tool_module, "upsert_embedding", _capture)

        from fastmcp import FastMCP

        from watercooler_mcp.tools.semantic import register_semantic_tools

        mcp = FastMCP(name="test")
        register_semantic_tools(mcp)

        import asyncio

        async def _run():
            tool = await mcp.get_tool("watercooler_semantic")
            return await tool.run({
                "action": "upsert",
                "entry_id": "E-meta-2",
                "topic": "t",
                "group_id": "mostlyharmless_ai_watercooler_cloud",
                "embedding": [0.1],
                "role": "implementer",
                "entry_type": "Note",
                "agent": "Claude",
                "timestamp": "2026-04-24T06:00:00Z",
            })

        asyncio.run(_run())
        assert calls
        first = calls[0]
        assert first["role"] == "implementer"
        assert first["entry_type"] == "Note"
        assert first["agent"] == "Claude"
        assert first["timestamp"] == "2026-04-24T06:00:00Z"


class TestHostedSemanticUpsert:
    def test_missing_entry_id_is_client_error(self) -> None:
        result = hosted_semantic.upsert_embedding(
            database="x_t1", entry_id="", topic="t", embedding=[0.0]
        )
        assert result == {"success": False, "error": "missing_entry_id"}

    def test_missing_database_is_client_error(self) -> None:
        result = hosted_semantic.upsert_embedding(
            database="", entry_id="E", topic="t", embedding=[0.0]
        )
        assert result == {"success": False, "error": "missing_database"}

    def test_executes_graph_query_on_success(self) -> None:
        fake = _FakeGraph()
        with patch.object(hosted_semantic, "_select_graph", return_value=fake):
            result = hosted_semantic.upsert_embedding(
                database="mostlyharmless_ai_watercooler_cloud_t1",
                entry_id="E5",
                topic="t",
                embedding=[0.1, 0.2, 0.3],
                group_id="mostlyharmless_ai_watercooler_cloud",
            )
        assert result["success"] is True
        assert result["status"] == "upserted"
        assert len(fake.calls) == 1
        assert "MERGE (n:Entry" in fake.calls[0][0]


class TestHybridT2HandoffFlag:
    """Codex review: hybrid T2 handoff flag must bypass local queue."""

    def test_set_runtime_activates_flag_in_hybrid(self) -> None:
        from watercooler.baseline_graph.sync import is_hybrid_t2_handoff_active
        from watercooler_mcp import memory_sync

        runtime = MagicMock()
        runtime.surface = "local_hybrid"
        runtime.premium_client = MagicMock()

        memory_sync.set_runtime(runtime)
        try:
            assert is_hybrid_t2_handoff_active() is True
        finally:
            memory_sync.set_runtime(None)
        assert is_hybrid_t2_handoff_active() is False

    def test_set_runtime_stays_off_in_stdio(self) -> None:
        from watercooler.baseline_graph.sync import is_hybrid_t2_handoff_active
        from watercooler_mcp import memory_sync

        runtime = MagicMock()
        runtime.surface = "local_full"
        runtime.premium_client = None

        memory_sync.set_runtime(runtime)
        try:
            assert is_hybrid_t2_handoff_active() is False
        finally:
            memory_sync.set_runtime(None)


class TestResolveHostedT1Target:
    """Codex re-review (01KPZ367CBHGCZZ6JWWM36KFE6 §1): target resolution
    must work against the hosted ThreadContext attributes that actually
    exist — ``code_repo`` — rather than the ``repo_slug`` /
    ``project_group_id`` attributes that hosted never sets."""

    def test_derives_from_code_repo_when_repo_slug_absent(self) -> None:
        from watercooler_mcp.tools.graph import _resolve_hosted_t1_target

        ctx = MagicMock()
        ctx.code_repo = "mostlyharmless-ai/watercooler"
        ctx.repo_slug = None
        t1_db, group_id = _resolve_hosted_t1_target(ctx)
        assert t1_db == "mostlyharmless_ai_watercooler_cloud_t1"
        assert group_id == "mostlyharmless_ai_watercooler_cloud"

    def test_strips_threads_suffix_from_hosted_slug(self) -> None:
        from watercooler_mcp.tools.graph import _resolve_hosted_t1_target

        # X-Repo often carries the threads-repo form (e.g.
        # "org/repo-threads"); the canonical T1 target is the CODE repo's
        # <org>_<repo>_t1, not <org>_<repo>_threads_t1.
        ctx = MagicMock()
        ctx.code_repo = "mostlyharmless-ai/watercooler-threads"
        ctx.repo_slug = None
        t1_db, group_id = _resolve_hosted_t1_target(ctx)
        assert t1_db == "mostlyharmless_ai_watercooler_cloud_t1"
        assert group_id == "mostlyharmless_ai_watercooler_cloud"

    def test_returns_empty_when_no_slug_available(self) -> None:
        from watercooler_mcp.tools.graph import _resolve_hosted_t1_target

        ctx = MagicMock()
        ctx.code_repo = ""
        ctx.repo_slug = ""
        t1_db, group_id = _resolve_hosted_t1_target(ctx)
        assert t1_db == ""
        assert group_id == ""


class TestHostedSemanticFilterParity:
    """Codex re-review §2: hosted semantic search must support the same
    metadata/time filters as the hosted keyword path (role, entry_type,
    agent, start_time, end_time)."""

    def test_search_applies_role_filter_case_insensitive_exact(self) -> None:
        """Codex re-review §2: role filter must be case-insensitive EXACT,
        matching the hosted keyword path (hosted_ops.py:3094-3096)."""
        fake = _FakeGraph(rows=[])
        with patch.object(hosted_semantic, "_select_graph", return_value=fake):
            hosted_semantic.search_semantic_entries(
                database="x_t1",
                query_embedding=[0.1],
                group_id="x",
                limit=5,
                role="implementer",
            )
        cypher, _params = fake.calls[0]
        assert "toLower(node.role) = toLower($role)" in cypher
        # Must NOT use raw equality (the pre-fix form).
        assert "node.role = $role" not in cypher

    def test_search_applies_entry_type_filter_case_insensitive_exact(self) -> None:
        fake = _FakeGraph(rows=[])
        with patch.object(hosted_semantic, "_select_graph", return_value=fake):
            hosted_semantic.search_semantic_entries(
                database="x_t1",
                query_embedding=[0.1],
                group_id="x",
                limit=5,
                entry_type="Note",
            )
        cypher, _params = fake.calls[0]
        assert "toLower(node.entry_type) = toLower($entry_type)" in cypher

    def test_search_applies_agent_filter_case_insensitive_substring(self) -> None:
        """Agent filter must be case-insensitive SUBSTRING match, matching
        the hosted keyword path (hosted_ops.py:3100-3102)."""
        fake = _FakeGraph(rows=[])
        with patch.object(hosted_semantic, "_select_graph", return_value=fake):
            hosted_semantic.search_semantic_entries(
                database="x_t1",
                query_embedding=[0.1],
                group_id="x",
                limit=5,
                agent="claude",
            )
        cypher, _params = fake.calls[0]
        assert "toLower(node.agent) CONTAINS toLower($agent)" in cypher

    def test_search_applies_time_range_filters(self) -> None:
        fake = _FakeGraph(rows=[])
        with patch.object(hosted_semantic, "_select_graph", return_value=fake):
            hosted_semantic.search_semantic_entries(
                database="x_t1",
                query_embedding=[0.1],
                group_id="x",
                limit=5,
                start_time="2026-01-01T00:00:00Z",
                end_time="2026-04-30T00:00:00Z",
            )
        cypher, _params = fake.calls[0]
        assert "node.timestamp >= $start_time" in cypher
        assert "node.timestamp <= $end_time" in cypher

    def test_search_returns_extended_fields(self) -> None:
        rows = [
            ["E-1", "topic-a", "implementer", "Note", "Claude",
             "2026-04-01T00:00:00Z", 0.2],
        ]
        fake = _FakeGraph(rows=rows)
        with patch.object(hosted_semantic, "_select_graph", return_value=fake):
            result = hosted_semantic.search_semantic_entries(
                database="x_t1",
                query_embedding=[0.1],
                group_id="x",
                limit=5,
            )
        assert result["results"][0]["role"] == "implementer"
        assert result["results"][0]["entry_type"] == "Note"
        assert result["results"][0]["agent"] == "Claude"
        assert result["results"][0]["timestamp"] == "2026-04-01T00:00:00Z"


class TestHostedSemanticTenantIsolation:
    """PR #654 in-PR review round 4 (MEDIUM §1): the hosted semantic tools
    must NOT trust a caller-supplied group_id. The authoritative value is
    derived from the hosted request context (X-Repo header)."""

    def test_upsert_tool_overrides_group_id_from_http_ctx(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from watercooler_mcp.tools import semantic as semantic_tool_module

        captured: dict = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return {"success": True, "status": "upserted", "entry_id": kwargs["entry_id"]}

        monkeypatch.setattr(semantic_tool_module, "upsert_embedding", _capture)

        class _Ctx:
            repo = "mostlyharmless-ai/watercooler"
            user_id = "u-real-tenant"

        monkeypatch.setattr(
            semantic_tool_module,
            "_scope_group_id_to_http_ctx",
            lambda caller: ("mostlyharmless_ai_watercooler_cloud", None),
        )

        from fastmcp import FastMCP
        from watercooler_mcp.tools.semantic import register_semantic_tools

        mcp = FastMCP(name="iso-test")
        register_semantic_tools(mcp)

        import asyncio

        async def _run():
            tool = await mcp.get_tool("watercooler_semantic")
            return await tool.run({
                "action": "upsert",
                "entry_id": "E-iso-1",
                "topic": "t",
                # Caller attempts to target a DIFFERENT tenant:
                "group_id": "attacker_tenant",
                "embedding": [0.1],
            })

        asyncio.run(_run())
        # The scoped (http_ctx-derived) value wins, NOT the caller's.
        assert captured["group_id"] == "mostlyharmless_ai_watercooler_cloud"

    def test_scope_helper_prefers_http_ctx_over_caller(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from watercooler_mcp.tools import semantic as semantic_tool_module

        # Move 1 (plan v5.1): the resolver requires user_id + repo on the
        # auth context. Both are always set by the hosted middleware in
        # production; the mock now matches that contract.
        #
        # The atomic single-lookup helper reads ``get_http_context``
        # and ``get_worker_context`` directly (not ``get_effective_context``)
        # so the source field can be attributed correctly without a
        # racy second lookup. The mock therefore patches both
        # accessors consistently — HTTP context returns the test ctx,
        # worker context returns None.
        class _Ctx:
            repo = "mostlyharmless-ai/watercooler-threads"
            user_id = "u-real-tenant"

        import watercooler_mcp.context as ctx_module

        monkeypatch.setattr(ctx_module, "get_http_context", lambda: _Ctx())
        monkeypatch.setattr(ctx_module, "get_worker_context", lambda: None)

        scoped, err = semantic_tool_module._scope_group_id_to_http_ctx(
            "attacker_tenant"
        )
        assert err is None
        # -threads stripped; canonical form derived from the real header.
        assert scoped == "mostlyharmless_ai_watercooler_cloud"

    def test_scope_helper_accepts_caller_when_no_http_ctx(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Off-hosted (stdio / dev) use-case: no http_ctx, caller value passes."""
        from watercooler_mcp.tools import semantic as semantic_tool_module
        import watercooler_mcp.context as ctx_module

        # Atomic helper reads both accessors directly.
        monkeypatch.setattr(ctx_module, "get_http_context", lambda: None)
        monkeypatch.setattr(ctx_module, "get_worker_context", lambda: None)

        scoped, err = semantic_tool_module._scope_group_id_to_http_ctx(
            "watercooler_cloud"
        )
        assert err is None
        assert scoped == "watercooler_cloud"

    def test_scope_helper_errors_when_http_ctx_has_empty_repo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PR #654 in-PR review round 9 (MEDIUM): a hosted request with
        a present-but-empty X-Repo must NOT fall through to the caller's
        group_id. Otherwise an authenticated request can supply any
        ``group_id`` and defeat the cross-tenant guard.

        Move 1 (plan v5.1) tightens this further: the resolver also
        requires a non-empty ``user_id``. Either missing field
        produces a scope_resolution_failed error.
        """
        from watercooler_mcp.tools import semantic as semantic_tool_module
        import watercooler_mcp.context as ctx_module

        class _Ctx:
            repo = ""  # authenticated but no X-Repo header
            user_id = "u-tenant"  # populated; only repo is missing

        # Atomic helper reads both accessors directly.
        monkeypatch.setattr(ctx_module, "get_http_context", lambda: _Ctx())
        monkeypatch.setattr(ctx_module, "get_worker_context", lambda: None)

        scoped, err = semantic_tool_module._scope_group_id_to_http_ctx(
            "attacker_tenant"
        )
        assert err is not None
        assert "scope_resolution_failed" in err.get("error", "")
        # Returned scope is empty, not the caller's attempted value.
        assert scoped == ""


class TestHostedSemanticTimestampCanonicalization:
    """PR #654 in-PR review round 4 (LOW §2): timestamps are compared as
    raw strings — mixed input formats (Z vs +00:00 vs naive) would break
    ordering. _canonicalize_timestamp normalises every write + query to
    YYYY-MM-DDTHH:MM:SSZ UTC."""

    def test_canonicalizes_offset_form_to_utc_z(self) -> None:
        assert (
            hosted_semantic._canonicalize_timestamp("2026-04-24T08:00:00+00:00")
            == "2026-04-24T08:00:00Z"
        )

    def test_canonicalizes_z_form_unchanged(self) -> None:
        assert (
            hosted_semantic._canonicalize_timestamp("2026-04-24T08:00:00Z")
            == "2026-04-24T08:00:00Z"
        )

    def test_treats_naive_as_utc(self) -> None:
        assert (
            hosted_semantic._canonicalize_timestamp("2026-04-24T08:00:00")
            == "2026-04-24T08:00:00Z"
        )

    def test_shifts_non_utc_offsets(self) -> None:
        # 10:00 +02:00 -> 08:00Z
        assert (
            hosted_semantic._canonicalize_timestamp("2026-04-24T10:00:00+02:00")
            == "2026-04-24T08:00:00Z"
        )

    def test_empty_input_stays_empty(self) -> None:
        assert hosted_semantic._canonicalize_timestamp("") == ""
        assert hosted_semantic._canonicalize_timestamp(None) == ""

    def test_garbage_input_becomes_empty(self) -> None:
        assert hosted_semantic._canonicalize_timestamp("not a timestamp") == ""

    def test_upsert_writes_canonical_form(self) -> None:
        fake = _FakeGraph()
        with patch.object(hosted_semantic, "_select_graph", return_value=fake):
            hosted_semantic.upsert_embedding(
                database="x_t1",
                entry_id="E-ts-1",
                topic="t",
                embedding=[0.1],
                group_id="x",
                timestamp="2026-04-24T10:00:00+02:00",
            )
        _cypher, params = fake.calls[0]
        assert params["timestamp"] == "2026-04-24T08:00:00Z"

    def test_search_canonicalizes_range_bounds(self) -> None:
        fake = _FakeGraph(rows=[])
        with patch.object(hosted_semantic, "_select_graph", return_value=fake):
            hosted_semantic.search_semantic_entries(
                database="x_t1",
                query_embedding=[0.1],
                group_id="x",
                limit=5,
                start_time="2026-04-24T10:00:00+02:00",
                end_time="2026-04-24T12:00:00+02:00",
            )
        _cypher, params = fake.calls[0]
        assert params["start_time"] == "2026-04-24T08:00:00Z"
        assert params["end_time"] == "2026-04-24T10:00:00Z"

    def test_unparseable_start_time_drops_clause(self) -> None:
        """PR #654 in-PR review round 6 (LOW): an unparseable start_time
        must NOT produce ``node.timestamp >= ""`` (silently matches
        everything). Drop the clause and log instead."""
        fake = _FakeGraph(rows=[])
        with patch.object(hosted_semantic, "_select_graph", return_value=fake):
            hosted_semantic.search_semantic_entries(
                database="x_t1",
                query_embedding=[0.1],
                group_id="x",
                limit=5,
                start_time="not a timestamp",
            )
        cypher, params = fake.calls[0]
        assert "node.timestamp >= $start_time" not in cypher
        assert "start_time" not in params

    def test_unparseable_end_time_drops_clause(self) -> None:
        """An unparseable end_time must NOT produce
        ``node.timestamp <= ""`` (silently matches nothing)."""
        fake = _FakeGraph(rows=[])
        with patch.object(hosted_semantic, "_select_graph", return_value=fake):
            hosted_semantic.search_semantic_entries(
                database="x_t1",
                query_embedding=[0.1],
                group_id="x",
                limit=5,
                end_time="also garbage",
            )
        cypher, params = fake.calls[0]
        assert "node.timestamp <= $end_time" not in cypher
        assert "end_time" not in params


class TestDeriveRepoSlug:
    """Codex review: repo_slug derivation from git remote."""

    def test_returns_org_repo_from_https_remote(
        self, tmp_path: Path
    ) -> None:
        from watercooler.path_resolver import derive_repo_slug

        from unittest.mock import patch

        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = "https://github.com/mostlyharmless-ai/watercooler.git\n"
        with patch("subprocess.run", return_value=completed):
            slug = derive_repo_slug(code_path=tmp_path)
        assert slug == "mostlyharmless-ai/watercooler"

    def test_returns_org_repo_from_ssh_remote(
        self, tmp_path: Path
    ) -> None:
        from watercooler.path_resolver import derive_repo_slug

        from unittest.mock import patch

        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = "git@github.com:mostlyharmless-ai/watercooler.git\n"
        with patch("subprocess.run", return_value=completed):
            slug = derive_repo_slug(code_path=tmp_path)
        assert slug == "mostlyharmless-ai/watercooler"

    def test_returns_none_when_no_remote(
        self, tmp_path: Path
    ) -> None:
        from watercooler.path_resolver import derive_repo_slug

        from unittest.mock import patch

        completed = MagicMock()
        completed.returncode = 1
        completed.stdout = ""
        with patch("subprocess.run", return_value=completed):
            slug = derive_repo_slug(code_path=tmp_path)
        assert slug is None

    def test_strips_dotless_hostname_from_https_remote(
        self, tmp_path: Path
    ) -> None:
        """PR #654 in-PR review round 5 (LOW §5): an intranet hostname
        without a dot (e.g. ``http://gitserver/org/repo``) used to slip
        past the host-strip heuristic, producing ``gitserver/org``."""
        from watercooler.path_resolver import derive_repo_slug

        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = "http://gitserver/org/repo.git\n"
        with patch("subprocess.run", return_value=completed):
            slug = derive_repo_slug(code_path=tmp_path)
        assert slug == "org/repo"

    def test_truncates_gitlab_subgroup_paths_to_two_segments(
        self, tmp_path: Path
    ) -> None:
        """GitLab subgroup paths like ``https://gitlab.com/grp/subgrp/repo``
        must still produce an ``<org>/<repo>`` pair — fall back to the
        first two path segments rather than failing."""
        from watercooler.path_resolver import derive_repo_slug

        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = "https://gitlab.com/grp/subgrp/repo.git\n"
        with patch("subprocess.run", return_value=completed):
            slug = derive_repo_slug(code_path=tmp_path)
        assert slug == "grp/subgrp"

    def test_returns_none_for_ambiguous_schemeless_dotless_url(
        self, tmp_path: Path
    ) -> None:
        """PR #654 in-PR review round 9 (LOW): a schemeless remote like
        ``gitserver/org/repo`` (no scheme, no colon, dotless hostname)
        is ambiguous — we can't tell ``gitserver`` from an ``org``. Prior
        heuristic kept ``gitserver`` as the first segment and produced
        the wrong slug ``gitserver/org``. Now: refuse and return None so
        the caller falls back to repo-only identity with a visible
        warning (logged by _derive_group_id)."""
        from watercooler.path_resolver import derive_repo_slug

        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = "gitserver/org/repo.git\n"
        with patch("subprocess.run", return_value=completed):
            slug = derive_repo_slug(code_path=tmp_path)
        assert slug is None


class TestBulkIndexHostedStripsThreadsSuffix:
    """PR #654 in-PR review round 7 (MEDIUM): _bulk_index_hosted_impl
    must normalise the ``-threads`` suffix on ``http_ctx.repo`` the same
    way _resolve_hosted_t1_target and _scope_group_id_to_http_ctx do.
    Otherwise bulk-indexed T2 episodes would land under
    ``org_repo_threads`` while semantic search / find_similar look in
    ``org_repo`` — entries permanently invisible to semantic queries."""

    def test_threads_suffix_stripped_when_deriving_group_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import watercooler_mcp.tools.memory as memory_module

        # Stub list_threads_hosted so we don't need a real GitHub client.
        class _Thread:
            def __init__(self, topic):
                self.topic = topic

        monkeypatch.setattr(
            "watercooler_mcp.hosted_ops.list_threads_hosted",
            lambda: (None, []),
            raising=False,
        )

        # Stub effective context with -threads-suffixed repo.
        class _HttpCtx:
            repo = "mostlyharmless-ai/watercooler-threads"

        import watercooler_mcp.context as ctx_module
        monkeypatch.setattr(ctx_module, "get_effective_context", lambda: _HttpCtx())

        captured: dict = {}

        def _capture_enqueue(**kwargs):
            captured.update(kwargs)
            return "task-bulk-1"

        monkeypatch.setattr(
            "watercooler_mcp.memory_queue.enqueue_memory_task",
            _capture_enqueue,
        )
        monkeypatch.setattr(
            "watercooler_mcp.hosted_ops.load_entries_hosted",
            lambda topic: (None, [
                {"entry_id": "E1", "body": "b", "title": "t", "timestamp": ""},
            ]),
            raising=False,
        )

        # Need at least one topic to drive the enqueue path.
        monkeypatch.setattr(
            "watercooler_mcp.hosted_ops.list_threads_hosted",
            lambda: (None, [_Thread("topic-a")]),
            raising=False,
        )

        import asyncio

        ctx = MagicMock()
        ctx.log = MagicMock()

        # Use a fake queue so the hosted impl thinks the queue is running.
        class _FakeQueue:
            max_depth = 100

            def depth(self) -> int:
                return 0

        asyncio.run(
            memory_module._bulk_index_hosted_impl(
                ctx=ctx,
                code_path="",
                backend="graphiti",
                threads="",
                max_entries=1,
                queue=_FakeQueue(),
            )
        )
        # If the -threads suffix stripping is correct, group_id is the
        # canonical ``mostlyharmless_ai_watercooler_cloud_t2`` — NOT
        # ``mostlyharmless_ai_watercooler_cloud_threads_t2`` (suffix not
        # stripped) or ``mostlyharmless_ai_watercooler_cloud`` (Plan v20
        # defect #34: must include ``_t2`` for hosted writes to land in
        # the canonical T2 graph).
        assert captured.get("group_id") == "mostlyharmless_ai_watercooler_cloud_t2"


class TestHostedSemanticClientIsSingleton:
    """PR #654 in-PR review round 7 (HIGH): every call previously created
    a new FalkorDB client, leaking a redis-py ConnectionPool per request.
    The process-wide singleton in _get_falkor_client avoids this."""

    def test_reuses_same_client_across_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Drop any cached singleton from prior tests.
        hosted_semantic._reset_falkor_client_for_tests()

        construction_count = {"n": 0}

        class _FakeFalkorClass:
            def __init__(self, **kwargs):
                construction_count["n"] += 1

            def select_graph(self, database):
                return _FakeGraph()

        # Make the ``from falkordb import FalkorDB`` inside _get_falkor_client
        # resolve to our fake.
        import sys
        import types

        fake_module = types.ModuleType("falkordb")
        fake_module.FalkorDB = _FakeFalkorClass
        monkeypatch.setitem(sys.modules, "falkordb", fake_module)

        try:
            # Six operations — should still result in one FalkorDB ctor call.
            for _ in range(6):
                hosted_semantic._get_falkor_client()
        finally:
            hosted_semantic._reset_falkor_client_for_tests()

        assert construction_count["n"] == 1, (
            "FalkorDB client must be constructed once and reused; "
            "prior form leaked a ConnectionPool per call."
        )

    def test_credential_rotation_builds_new_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hosted_semantic._reset_falkor_client_for_tests()

        construction_count = {"n": 0}

        class _FakeFalkorClass:
            def __init__(self, **kwargs):
                construction_count["n"] += 1

            def select_graph(self, database):
                return _FakeGraph()

        import sys
        import types

        fake_module = types.ModuleType("falkordb")
        fake_module.FalkorDB = _FakeFalkorClass
        monkeypatch.setitem(sys.modules, "falkordb", fake_module)

        # Strip canonical FALKORDB_HOST so this test exercises the legacy
        # FALKORDB_URL parsing path (which carries the credentials being
        # rotated). The new _resolve_falkor_kwargs() prefers FALKORDB_HOST,
        # so a CI environment that sets it would otherwise mask the URL.
        monkeypatch.delenv("FALKORDB_HOST", raising=False)
        monkeypatch.delenv("FALKORDB_PORT", raising=False)
        monkeypatch.delenv("FALKORDB_USERNAME", raising=False)
        monkeypatch.delenv("FALKORDB_PASSWORD", raising=False)

        try:
            monkeypatch.setenv("FALKORDB_URL", "redis://a:pw1@host:6379")
            hosted_semantic._get_falkor_client()
            monkeypatch.setenv("FALKORDB_URL", "redis://a:pw2@host:6379")
            hosted_semantic._get_falkor_client()
        finally:
            hosted_semantic._reset_falkor_client_for_tests()

        assert construction_count["n"] == 2


class TestHostedSemanticDeleteActuallyRemovesNode:
    """PR #654 in-PR review round 5 (HIGH §2): the prior delete set
    ``n.embedding = null`` which left the node in place, accumulating
    ghost Entry nodes forever. Match the local path: DELETE the node."""

    def test_delete_issues_detach_delete(self) -> None:
        fake = _FakeGraph()
        with patch.object(hosted_semantic, "_select_graph", return_value=fake):
            hosted_semantic.delete_embedding(
                database="x_t1",
                entry_id="E-del-1",
                group_id="x",
                topic="t",
            )
        cypher, _params = fake.calls[0]
        assert "DETACH DELETE n" in cypher
        # Must NOT nullify — that was the ghost-node bug.
        assert "SET n.embedding = null" not in cypher


class TestHybridUpsertFailureIsLogged:
    """PR #654 in-PR review round 5 (MEDIUM §3): a failed hybrid T1 remote
    upsert previously returned silently from upsert_embedding. Now it must
    emit a WARNING so the gap is auditable."""

    def test_warning_logged_when_hybrid_remote_fails(
        self, tmp_path: Path
    ) -> None:
        """Capture the sync module's own logger directly so the test is
        robust across pytest log-propagation quirks."""
        from watercooler.baseline_graph import sync as bg_sync

        # Register a remote callback that reports failure.
        bg_sync.register_t1_remote_embedding_callbacks(
            upsert=lambda *a, **kw: False, delete=None
        )

        recorded: list[str] = []
        orig_warning = bg_sync.logger.warning

        def _capture_warning(msg, *args, **kwargs):
            try:
                recorded.append(msg % args if args else msg)
            except Exception:
                recorded.append(str(msg))
            return orig_warning(msg, *args, **kwargs)

        try:
            with patch.object(bg_sync.storage, "upsert_search_index_entry"), \
                 patch.object(bg_sync.logger, "warning", _capture_warning):
                bg_sync.upsert_embedding(
                    threads_dir=tmp_path / "x-threads",
                    graph_dir=tmp_path / "graph",
                    entry_id="E-fail-1",
                    topic="t",
                    embedding=[0.1],
                )
        finally:
            bg_sync.register_t1_remote_embedding_callbacks(upsert=None, delete=None)

        assert any("T1 hybrid submit reported failure" in m for m in recorded)


class TestPhaseIndicatorTracksRuntime:
    """PR #654 in-PR review round 9 (LOW): phase_indicator was hardcoded
    to ``"phase_1_to_5_pre_split"`` regardless of runtime state. It now
    tracks the observable Phase 5 / Phase 8 signals."""

    def test_phase_1_when_no_handoff_active(self) -> None:
        from watercooler_mcp.tools.memory import _add_canonical_identity_fields
        from watercooler.baseline_graph import sync as bg_sync

        bg_sync.register_t1_remote_embedding_callbacks(upsert=None, delete=None)
        bg_sync.set_hybrid_t2_handoff_active(False)
        try:
            diagnostics: dict = {}
            _add_canonical_identity_fields(diagnostics)
        finally:
            bg_sync.set_hybrid_t2_handoff_active(False)
        assert diagnostics["phase_indicator"] == "phase_1_local_only"

    def test_phase_5_when_only_t2_handoff_active(self) -> None:
        from watercooler_mcp.tools.memory import _add_canonical_identity_fields
        from watercooler.baseline_graph import sync as bg_sync

        bg_sync.register_t1_remote_embedding_callbacks(upsert=None, delete=None)
        bg_sync.set_hybrid_t2_handoff_active(True)
        try:
            diagnostics: dict = {}
            _add_canonical_identity_fields(diagnostics)
        finally:
            bg_sync.set_hybrid_t2_handoff_active(False)
        assert diagnostics["phase_indicator"] == "phase_5_hybrid_t2_handoff"

    def test_phase_8_when_both_active(self) -> None:
        from watercooler_mcp.tools.memory import _add_canonical_identity_fields
        from watercooler.baseline_graph import sync as bg_sync

        bg_sync.register_t1_remote_embedding_callbacks(
            upsert=lambda *a, **kw: True, delete=None,
        )
        bg_sync.set_hybrid_t2_handoff_active(True)
        try:
            diagnostics: dict = {}
            _add_canonical_identity_fields(diagnostics)
        finally:
            bg_sync.register_t1_remote_embedding_callbacks(upsert=None, delete=None)
            bg_sync.set_hybrid_t2_handoff_active(False)
        assert diagnostics["phase_indicator"] == "phase_8_hybrid_t1_hosted"


class TestAuthorityLabelsUsesRuntimeSurface:
    """PR #654 in-PR review round 5 (MEDIUM §4): _authority_labels
    should consult the live runtime surface (memory_sync._runtime.surface),
    not the static config.mcp.transport key, so a mis-initialized hybrid
    (premium_client failed at startup) reports local_daemons instead of
    local_daemons_hybrid_override."""

    def test_falls_back_to_plain_local_when_runtime_says_local_full(
        self,
    ) -> None:
        from watercooler_mcp import memory_sync
        from watercooler_mcp.tools.daemon import _authority_labels

        class _Runtime:
            surface = "local_full"
            premium_client = None

        class _FakeDaemonManager:
            pass

        memory_sync.set_runtime(_Runtime())
        try:
            labels = _authority_labels(_FakeDaemonManager())
        finally:
            memory_sync.set_runtime(None)

        # Even if static config said transport="hybrid", live surface
        # (local_full) wins.
        assert labels["authority_scope"] == "local_daemons"
        assert "note" not in labels


class TestHostedSemanticKnnOrdering:
    """PR #654 round 22 HIGH: reviewer claimed ORDER BY score ASC was wrong.

    FalkorDB's db.idx.vector.queryNodes returns cosine DISTANCE (0 =
    identical, 2 = opposite). Ascending distance → most-similar first. The
    Cypher is correct; the defensive Python sort sorts by similarity DESC
    as belt-and-braces. This test asserts the output contract: rows come
    back most-similar-first, regardless of SDK row-order quirks.
    """

    def test_results_sorted_by_similarity_descending(self) -> None:
        fake = _FakeGraph(
            rows=[
                ["E_mid", "t", "r", "Note", "a", "2026-01-01T00:00:00Z", 0.8],
                ["E_near", "t", "r", "Note", "a", "2026-01-01T00:00:00Z", 0.1],
                ["E_far", "t", "r", "Note", "a", "2026-01-01T00:00:00Z", 1.5],
            ]
        )
        with patch.object(hosted_semantic, "_select_graph", return_value=fake):
            result = hosted_semantic.search_semantic_entries(
                database="x_t1",
                query_embedding=[0.1, 0.2],
                group_id="x",
                limit=5,
            )

        ids = [r["entry_id"] for r in result["results"]]
        sims = [r["similarity"] for r in result["results"]]
        assert ids == ["E_near", "E_mid", "E_far"], (
            f"expected similarity-DESC ordering, got {ids}"
        )
        assert sims == sorted(sims, reverse=True), sims

    def test_cypher_uses_order_by_score_asc(self) -> None:
        """The Cypher string itself must keep ORDER BY score ASC. If a
        future contributor is tempted to 'fix' this to DESC, this test
        explains why not.
        """
        fake = _FakeGraph(rows=[])
        with patch.object(hosted_semantic, "_select_graph", return_value=fake):
            hosted_semantic.search_semantic_entries(
                database="x_t1",
                query_embedding=[0.1],
                group_id="x",
                limit=3,
            )
        cypher, _ = fake.calls[0]
        # score = cosine DISTANCE; ASC = most-similar first. DO NOT change.
        assert "ORDER BY score ASC" in cypher


class TestSearchEntriesHostedSemanticImportChain:
    """Plan v20 post-merge bug (2026-04-24): ``_search_entries_hosted_semantic``
    at ``tools/graph.py:1349`` imported ``watercooler.baseline_graph.embedding``
    which does not exist in the tree. Unit tests that mock
    ``hosted_semantic.search_semantic_entries`` didn't surface the import
    because the call never reached it — the ImportError short-circuited
    first. First integration call against a real Railway deploy returned
    ``embedding_client_missing``.

    Fix: use the existing ``watercooler.baseline_graph.sync.generate_embedding``
    free function that the rest of the codebase uses.

    This test forces the real import chain (no patch on the embedding
    module) and verifies the code either succeeds or fails with a
    *different* error — not ``embedding_client_missing`` / ``*_module_missing``.
    """

    def test_import_chain_does_not_raise_module_missing(self) -> None:
        from watercooler_mcp.tools.graph import _search_entries_hosted_semantic

        # Fake context that resolves a valid hosted T1 target.
        class _Ctx:
            code_repo = "mostlyharmless-ai/watercooler"
            repo_slug = "mostlyharmless-ai/watercooler"
            code_repo_name = "watercooler-cloud"

        # generate_embedding will try to contact an embedding service; patch
        # it so the test does not depend on external infra.
        with patch(
            "watercooler.baseline_graph.sync.generate_embedding",
            return_value=[0.1, 0.2, 0.3],
        ), patch(
            "watercooler_mcp.hosted_semantic.search_semantic_entries",
            return_value={"count": 0, "method": "hosted_t1_hnsw", "results": []},
        ):
            result = _search_entries_hosted_semantic(
                context=_Ctx(),
                query="hello world",
                thread_topic="",
                limit=5,
                similarity_threshold=0.5,
                role="",
                entry_type="",
                agent="",
                start_time="",
                end_time="",
            )

        # Assert no import-failure error codes surface in the result.
        parsed = json.loads(result)
        forbidden = {
            "embedding_client_missing",
            "embedding_module_missing",
            "hosted_semantic_module_missing",
        }
        assert parsed.get("error") not in forbidden, (
            f"import chain broken — got {parsed.get('error')}: "
            f"{parsed.get('message')}"
        )

class TestHostedEnsureEntryIndexes:
    """Plan v20 post-merge bug (2026-04-24 round 2): first semantic search
    against a fresh hosted T1 graph returned
    ``falkor_error: Invalid arguments for procedure 'db.idx.vector.queryNodes'``
    because ``upsert_embedding`` created :Entry nodes with an ``embedding``
    property but never created the vector index. The HNSW index must be
    bootstrapped; fix is an idempotent ``_ensure_entry_indexes`` called
    from ``upsert_embedding``.
    """

    def test_ensure_creates_vector_and_range_index(self) -> None:
        fake = _FakeGraph()
        hosted_semantic._ENSURED_INDEXES.discard("x_t1")
        hosted_semantic._ensure_entry_indexes(fake, "x_t1")
        cyphers = [c for c, _ in fake.calls]
        assert any(
            "CREATE VECTOR INDEX FOR (n:Entry) ON (n.embedding)" in c
            for c in cyphers
        ), cyphers
        assert any(
            "CREATE INDEX FOR (n:Entry) ON" in c
            and "entry_id" in c
            and "group_id" in c
            for c in cyphers
        ), cyphers
        assert "x_t1" in hosted_semantic._ENSURED_INDEXES

    def test_ensure_is_idempotent_across_calls(self) -> None:
        fake = _FakeGraph()
        hosted_semantic._ENSURED_INDEXES.discard("x_t1")
        hosted_semantic._ensure_entry_indexes(fake, "x_t1")
        count_after_first = len(fake.calls)
        hosted_semantic._ensure_entry_indexes(fake, "x_t1")
        assert len(fake.calls) == count_after_first, (
            "second call should short-circuit via _ENSURED_INDEXES cache"
        )

    def test_ensure_swallows_already_exists_errors(self) -> None:
        class _AlreadyIndexedGraph:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict]] = []

            def query(self, cypher: str, params: dict | None = None):
                self.calls.append((cypher, dict(params or {})))
                raise RuntimeError("Index already indexed on label :Entry")

        fake = _AlreadyIndexedGraph()
        hosted_semantic._ENSURED_INDEXES.discard("x_t1")
        # Should not raise.
        hosted_semantic._ensure_entry_indexes(fake, "x_t1")
        assert "x_t1" in hosted_semantic._ENSURED_INDEXES

    def test_upsert_bootstraps_indexes_on_first_call(self) -> None:
        """End-to-end: first upsert against a fresh DB issues index CREATEs."""
        fake = _FakeGraph()
        hosted_semantic._ENSURED_INDEXES.discard("fresh_t1")
        with patch.object(hosted_semantic, "_select_graph", return_value=fake):
            result = hosted_semantic.upsert_embedding(
                database="fresh_t1",
                entry_id="E1",
                topic="t",
                embedding=[0.1, 0.2],
                group_id="x",
            )
        assert result["success"] is True
        cyphers = [c for c, _ in fake.calls]
        # Expect: vector-index CREATE, range-index CREATE, MERGE Entry.
        assert sum(1 for c in cyphers if "CREATE VECTOR INDEX" in c) == 1
        assert sum(1 for c in cyphers if "CREATE INDEX FOR (n:Entry)" in c) == 1
        assert any("MERGE (n:Entry" in c for c in cyphers)
        # Cleanup cache so other tests don't see this DB name.
        hosted_semantic._ENSURED_INDEXES.discard("fresh_t1")

    def test_search_bootstraps_indexes_on_first_call(self) -> None:
        """PR #656 review: search-first on a fresh DB must bootstrap.

        Previously ``_ensure_entry_indexes`` was called only from
        ``upsert_embedding``. A read-first access would skip the index
        bootstrap and the FalkorDB query would fail with "Invalid
        arguments for procedure 'db.idx.vector.queryNodes'".
        """
        fake = _FakeGraph(rows=[])
        hosted_semantic._ENSURED_INDEXES.discard("readfirst_t1")
        with patch.object(hosted_semantic, "_select_graph", return_value=fake):
            hosted_semantic.search_semantic_entries(
                database="readfirst_t1",
                query_embedding=[0.1, 0.2],
                group_id="x",
                limit=3,
            )
        cyphers = [c for c, _ in fake.calls]
        assert any("CREATE VECTOR INDEX" in c for c in cyphers), cyphers
        assert any("CREATE INDEX FOR (n:Entry)" in c for c in cyphers), cyphers
        # Cleanup.
        hosted_semantic._ENSURED_INDEXES.discard("readfirst_t1")

    def test_find_similar_bootstraps_indexes_on_first_call(self) -> None:
        """Same coverage for find_similar_t1 (read-path-only access)."""
        fake = _FakeGraph(rows=[])
        hosted_semantic._ENSURED_INDEXES.discard("readfirst2_t1")
        with patch.object(hosted_semantic, "_select_graph", return_value=fake):
            hosted_semantic.find_similar_t1(
                database="readfirst2_t1",
                entry_id="E1",
                group_id="x",
                limit=3,
            )
        cyphers = [c for c, _ in fake.calls]
        assert any("CREATE VECTOR INDEX" in c for c in cyphers), cyphers
        # Cleanup.
        hosted_semantic._ENSURED_INDEXES.discard("readfirst2_t1")

    def test_ensured_indexes_lock_exists_and_is_used(self) -> None:
        """PR #656 review (MEDIUM): _ENSURED_INDEXES must be lock-protected
        to mirror _FALKOR_CLIENT_STATE under free-threaded CPython. We
        verify the lock object exists at the module level and the
        ensure path acquires it (lock count goes up while inside the
        function).
        """
        import threading as _threading

        assert isinstance(
            hosted_semantic._ENSURED_INDEXES_LOCK,
            type(_threading.Lock()),
        ), (
            "_ENSURED_INDEXES_LOCK is not a threading.Lock instance — "
            "free-threaded race protection is missing"
        )

        # Functional check: monkeypatch the lock with a tracking proxy
        # and confirm acquisition happens during _ensure_entry_indexes.
        real = hosted_semantic._ENSURED_INDEXES_LOCK
        acquire_count = [0]

        class _TrackingLock:
            def __enter__(self):
                acquire_count[0] += 1
                real.acquire()
                return self

            def __exit__(self, *a):
                real.release()

            def acquire(self, *a, **k):
                acquire_count[0] += 1
                return real.acquire(*a, **k)

            def release(self):
                return real.release()

        original_lock = hosted_semantic._ENSURED_INDEXES_LOCK
        hosted_semantic._ENSURED_INDEXES_LOCK = _TrackingLock()
        try:
            hosted_semantic._ENSURED_INDEXES.discard("locktest_t1")
            fake = _FakeGraph()
            hosted_semantic._ensure_entry_indexes(fake, "locktest_t1")
            assert acquire_count[0] >= 1, (
                "lock was never acquired — race protection bypassed"
            )
        finally:
            hosted_semantic._ENSURED_INDEXES_LOCK = original_lock
            hosted_semantic._ENSURED_INDEXES.discard("locktest_t1")


    def test_embedding_unavailable_when_generate_returns_none(self) -> None:
        """If the embedding service is unreachable (generate_embedding -> None),
        we want a clean 'embedding_unavailable' signal, not an import error.
        """
        from watercooler_mcp.tools.graph import _search_entries_hosted_semantic

        class _Ctx:
            code_repo = "mostlyharmless-ai/watercooler"
            repo_slug = "mostlyharmless-ai/watercooler"
            code_repo_name = "watercooler-cloud"

        with patch(
            "watercooler.baseline_graph.sync.generate_embedding",
            return_value=None,
        ):
            result = _search_entries_hosted_semantic(
                context=_Ctx(),
                query="hello world",
                thread_topic="",
                limit=5,
                similarity_threshold=0.5,
                role="",
                entry_type="",
                agent="",
                start_time="",
                end_time="",
            )

        parsed = json.loads(result)
        assert parsed.get("error") == "embedding_unavailable", parsed
