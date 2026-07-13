"""Per-repo pool routing at the hybrid memory call sites.

Covers the PR 3 call-site wiring from incident
bug-hybrid-static-x-repo-cross-tenant-t2-scope: T1 upserts, direct T2
submissions, queued T2 handoffs, and the mounted-ingest conversion
(graphiti_add_episode as a mixed tool).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from watercooler_mcp import memory_sync, t1_hybrid


def _client(repo: str) -> MagicMock:
    client = MagicMock()
    client.resolved_repo = repo
    client.resolved_branch = "main"
    client.call_tool_text = AsyncMock(
        return_value=json.dumps({"success": True, "status": "queued"})
    )
    return client


def _pool_returning(client: MagicMock) -> MagicMock:
    pool = MagicMock()
    pool.client_for_repo = MagicMock(return_value=client)
    pool.default = client
    return pool


class TestT1UpsertPoolRouting:
    def test_upsert_uses_pool_client_for_entry_repo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "WATERCOOLER_HANDOFF_RECEIPTS_FILE", str(tmp_path / "r.jsonl")
        )
        default = _client("mostlyharmless-ai/watercooler")
        site_client = _client("mostlyharmless-ai/watercooler-site")
        pool = _pool_returning(site_client)

        with patch(
            "watercooler.path_resolver.derive_repo_slug",
            return_value="mostlyharmless-ai/watercooler-site",
        ):
            ok = t1_hybrid._submit_t1_upsert(
                premium=default,
                pool=pool,
                threads_dir=tmp_path / "site-threads",
                entry_id="E1",
                topic="t",
                embedding=[0.1],
            )
        assert ok is True
        site_client.call_tool_text.assert_awaited_once()
        default.call_tool_text.assert_not_awaited()
        pool.client_for_repo.assert_called_once()
        assert (
            pool.client_for_repo.call_args.args[0]
            == "mostlyharmless-ai/watercooler-site"
        )

    def test_upsert_without_pool_uses_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "WATERCOOLER_HANDOFF_RECEIPTS_FILE", str(tmp_path / "r.jsonl")
        )
        default = _client("mostlyharmless-ai/watercooler")
        ok = t1_hybrid._submit_t1_upsert(
            premium=default,
            threads_dir=tmp_path / "threads",
            entry_id="E1",
            topic="t",
            embedding=[0.1],
        )
        assert ok is True
        default.call_tool_text.assert_awaited_once()


class TestDirectT2SubmitPoolRouting:
    def test_submit_selects_pool_client_by_slug(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "WATERCOOLER_HANDOFF_RECEIPTS_FILE", str(tmp_path / "r.jsonl")
        )
        default = _client("mostlyharmless-ai/watercooler")
        site_client = _client("mostlyharmless-ai/watercooler-site")
        pool = _pool_returning(site_client)
        runtime = MagicMock()
        runtime.surface = "local_hybrid"
        runtime.premium_client = default
        runtime.premium_pool = pool

        with patch(
            "watercooler.path_resolver.derive_repo_slug",
            return_value="mostlyharmless-ai/watercooler-site",
        ):
            ok = memory_sync._submit_graphiti_to_hosted(
                threads_dir=tmp_path / "site-threads",
                topic="t",
                entry_id="E1",
                entry_body="body",
                entry_title="T",
                timestamp="",
                entry_summary="",
                runtime=runtime,
                log=memory_sync.logger,
            )
        assert ok is True
        site_client.call_tool_text.assert_awaited_once()
        default.call_tool_text.assert_not_awaited()


class TestQueuedHandoffPoolRouting:
    @pytest.mark.anyio
    async def test_legacy_task_foreign_group_dead_letters(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A queued task with no derivable slug and a group that does NOT
        match the boot scope must fail permanently — never submit a
        foreign group under the boot X-Repo."""
        from watercooler_mcp.memory_queue import PermanentTaskError
        from watercooler_mcp.memory_queue.task import MemoryTask

        monkeypatch.setenv(
            "WATERCOOLER_HANDOFF_RECEIPTS_FILE", str(tmp_path / "r.jsonl")
        )
        default = _client("mostlyharmless-ai/watercooler")
        pool = _pool_returning(default)
        runtime = MagicMock()
        runtime.surface = "local_hybrid"
        runtime.premium_client = default
        runtime.premium_pool = pool
        monkeypatch.setattr(memory_sync, "get_runtime", lambda: runtime)

        task = MemoryTask(
            task_id="legacy1",
            backend="graphiti",
            topic="t",
            entry_id="E1",
            timestamp="",
            title="T",
            content="c",
            group_id="mostlyharmless_ai_watercooler_site_t2",
            code_path="",  # legacy: no code_path
        )
        with pytest.raises(PermanentTaskError, match="foreign group"):
            await memory_sync._graphiti_remote_handoff(task)
        default.call_tool_text.assert_not_awaited()

    @pytest.mark.anyio
    async def test_legacy_task_boot_group_uses_default_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from watercooler_mcp.memory_queue.task import MemoryTask

        monkeypatch.setenv(
            "WATERCOOLER_HANDOFF_RECEIPTS_FILE", str(tmp_path / "r.jsonl")
        )
        default = _client("mostlyharmless-ai/watercooler")
        pool = _pool_returning(default)
        runtime = MagicMock()
        runtime.surface = "local_hybrid"
        runtime.premium_client = default
        runtime.premium_pool = pool
        monkeypatch.setattr(memory_sync, "get_runtime", lambda: runtime)

        task = MemoryTask(
            task_id="legacy2",
            backend="graphiti",
            topic="t",
            entry_id="E1",
            timestamp="",
            title="T",
            content="c",
            group_id="mostlyharmless_ai_watercooler_cloud_t2",
            code_path="",
        )
        result = await memory_sync._graphiti_remote_handoff(task)
        assert result is not None
        default.call_tool_text.assert_awaited_once()

    @pytest.mark.anyio
    async def test_scope_rejection_is_permanent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hosted scope_resolution_failed rejection dead-letters
        immediately instead of burning retries (deterministic refusal)."""
        from watercooler_mcp.memory_queue import PermanentTaskError
        from watercooler_mcp.memory_queue.task import MemoryTask

        monkeypatch.setenv(
            "WATERCOOLER_HANDOFF_RECEIPTS_FILE", str(tmp_path / "r.jsonl")
        )
        default = _client("mostlyharmless-ai/watercooler")
        default.call_tool_text = AsyncMock(
            return_value=json.dumps(
                {
                    "success": False,
                    "error": "scope_resolution_failed: strict_mode: ...",
                }
            )
        )
        runtime = MagicMock()
        runtime.surface = "local_hybrid"
        runtime.premium_client = default
        runtime.premium_pool = None
        monkeypatch.setattr(memory_sync, "get_runtime", lambda: runtime)

        task = MemoryTask(
            task_id="rej1",
            backend="graphiti",
            topic="t",
            entry_id="E1",
            timestamp="",
            title="T",
            content="c",
            group_id="mostlyharmless_ai_watercooler_cloud_t2",
            code_path="",
        )
        with pytest.raises(PermanentTaskError, match="scope_resolution_failed"):
            await memory_sync._graphiti_remote_handoff(task)


class TestMountedIngestConversion:
    def test_graphiti_add_episode_is_mixed_not_mounted(self) -> None:
        """Registration triple (review :5): the tool must remain exposed on
        the hybrid surface via a local wrapper, not the proxy mount."""
        from watercooler_mcp.capabilities import (
            HYBRID_REMOTE_MOUNT_TOOLS,
            MIXED_TOOL_NAMES,
            REMOTE_CAPABLE_MEMORY_TOOL_NAMES,
        )

        assert "watercooler_graphiti_add_episode" in MIXED_TOOL_NAMES
        assert "watercooler_graphiti_add_episode" in (
            REMOTE_CAPABLE_MEMORY_TOOL_NAMES
        )
        assert "watercooler_graphiti_add_episode" not in (
            HYBRID_REMOTE_MOUNT_TOOLS
        )

    def test_hybrid_surface_registers_tool_locally(self) -> None:
        from watercooler_mcp.capabilities import (
            HYBRID_DEFAULT_ROUTES,
            CapabilityProfile,
        )
        from watercooler_mcp.server_factory import (
            memory_tools_for_surface,
            mountable_remote_tools_for_hybrid,
        )
        from watercooler_mcp.tool_runtime import ToolRuntime

        runtime = ToolRuntime(
            surface="local_hybrid",
            capability_profile=CapabilityProfile(
                routes=dict(HYBRID_DEFAULT_ROUTES)
            ),
            premium_client=MagicMock(),
        )
        assert "watercooler_graphiti_add_episode" in memory_tools_for_surface(
            runtime
        )
        assert "watercooler_graphiti_add_episode" not in (
            mountable_remote_tools_for_hybrid(runtime)
        )

    @pytest.mark.anyio
    async def test_wrapper_routes_through_pool_by_code_path(self) -> None:
        from watercooler_mcp.capabilities import (
            HYBRID_DEFAULT_ROUTES,
            CapabilityProfile,
        )
        from watercooler_mcp.tools.memory import (
            _build_hybrid_graphiti_add_episode_wrapper,
        )

        default = _client("mostlyharmless-ai/watercooler")
        site_client = _client("mostlyharmless-ai/watercooler-site")
        pool = MagicMock()
        pool.default = default
        pool.client_for_path = MagicMock(return_value=site_client)

        runtime = MagicMock()
        runtime.surface = "local_hybrid"
        runtime.premium_client = default
        runtime.premium_pool = pool
        runtime.capability_profile = CapabilityProfile(
            routes=dict(HYBRID_DEFAULT_ROUTES)
        )

        wrapper = _build_hybrid_graphiti_add_episode_wrapper(runtime)
        await wrapper(
            MagicMock(),
            content="c",
            group_id="mostlyharmless_ai_watercooler_site_t2",
            code_path="/path/to/site",
        )
        site_client.call_tool_text.assert_awaited_once()
        default.call_tool_text.assert_not_awaited()
        pool.client_for_path.assert_called_once_with("/path/to/site")


class TestWriteFailClosed:
    """PR #1062 review P1: repo-scoped writes must not fall back to the
    boot client when per-call resolution fails — fail closed locally."""

    def test_t1_foreign_scope_pool_failure_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receipts = tmp_path / "r.jsonl"
        monkeypatch.setenv("WATERCOOLER_HANDOFF_RECEIPTS_FILE", str(receipts))
        default = _client("mostlyharmless-ai/watercooler")
        pool = MagicMock()
        pool.client_for_repo = MagicMock(side_effect=RuntimeError("no api key"))
        pool.is_boot_scope = MagicMock(return_value=False)

        with patch(
            "watercooler.path_resolver.derive_repo_slug",
            return_value="mostlyharmless-ai/watercooler-site",
        ):
            ok = t1_hybrid._submit_t1_upsert(
                premium=default,
                pool=pool,
                threads_dir=tmp_path / "site-threads",
                entry_id="E1",
                topic="t",
                embedding=[0.1],
            )
        assert ok is False
        default.call_tool_text.assert_not_awaited()
        record = json.loads(receipts.read_text().strip().splitlines()[-1])
        assert "pool_client_unavailable" in record["error"]

    def test_t1_boot_scope_pool_failure_uses_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "WATERCOOLER_HANDOFF_RECEIPTS_FILE", str(tmp_path / "r.jsonl")
        )
        default = _client("mostlyharmless-ai/watercooler")
        pool = MagicMock()
        pool.client_for_repo = MagicMock(side_effect=RuntimeError("hiccup"))
        pool.is_boot_scope = MagicMock(return_value=True)

        with patch(
            "watercooler.path_resolver.derive_repo_slug",
            return_value="mostlyharmless-ai/watercooler",
        ):
            ok = t1_hybrid._submit_t1_upsert(
                premium=default,
                pool=pool,
                threads_dir=tmp_path / "threads",
                entry_id="E1",
                topic="t",
                embedding=[0.1],
            )
        assert ok is True
        default.call_tool_text.assert_awaited_once()

    def test_t2_direct_foreign_scope_pool_failure_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receipts = tmp_path / "r.jsonl"
        monkeypatch.setenv("WATERCOOLER_HANDOFF_RECEIPTS_FILE", str(receipts))
        default = _client("mostlyharmless-ai/watercooler")
        pool = MagicMock()
        pool.client_for_repo = MagicMock(side_effect=RuntimeError("boom"))
        pool.is_boot_scope = MagicMock(return_value=False)
        runtime = MagicMock()
        runtime.surface = "local_hybrid"
        runtime.premium_client = default
        runtime.premium_pool = pool

        with patch(
            "watercooler.path_resolver.derive_repo_slug",
            return_value="mostlyharmless-ai/watercooler-site",
        ):
            ok = memory_sync._submit_graphiti_to_hosted(
                threads_dir=tmp_path / "site-threads",
                topic="t",
                entry_id="E1",
                entry_body="body",
                entry_title="T",
                timestamp="",
                entry_summary="",
                runtime=runtime,
                log=memory_sync.logger,
            )
        assert ok is False
        default.call_tool_text.assert_not_awaited()
        record = json.loads(receipts.read_text().strip().splitlines()[-1])
        assert "pool_client_unavailable" in record["error"]

    @pytest.mark.anyio
    async def test_ingest_wrapper_foreign_group_no_path_fails_closed(
        self,
    ) -> None:
        from watercooler_mcp.capabilities import (
            HYBRID_DEFAULT_ROUTES,
            CapabilityProfile,
        )
        from watercooler_mcp.tools.memory import (
            _build_hybrid_graphiti_add_episode_wrapper,
        )

        default = _client("mostlyharmless-ai/watercooler")
        pool = MagicMock()
        pool.default = default
        pool.client_for_path = MagicMock(side_effect=ValueError("no slug"))
        runtime = MagicMock()
        runtime.surface = "local_hybrid"
        runtime.premium_client = default
        runtime.premium_pool = pool
        runtime.capability_profile = CapabilityProfile(
            routes=dict(HYBRID_DEFAULT_ROUTES)
        )

        wrapper = _build_hybrid_graphiti_add_episode_wrapper(runtime)
        result = await wrapper(
            MagicMock(),
            content="c",
            group_id="mostlyharmless_ai_watercooler_site_t2",
            code_path="/not/a/repo",
        )
        payload = json.loads(result.content[0].text)
        assert payload["success"] is False
        assert "scope_resolution_failed" in payload["error"]
        default.call_tool_text.assert_not_awaited()

    @pytest.mark.anyio
    async def test_ingest_wrapper_boot_group_no_path_uses_default(
        self,
    ) -> None:
        from watercooler_mcp.capabilities import (
            HYBRID_DEFAULT_ROUTES,
            CapabilityProfile,
        )
        from watercooler_mcp.tools.memory import (
            _build_hybrid_graphiti_add_episode_wrapper,
        )

        default = _client("mostlyharmless-ai/watercooler")
        pool = MagicMock()
        pool.default = default
        pool.client_for_path = MagicMock(side_effect=ValueError("no slug"))
        runtime = MagicMock()
        runtime.surface = "local_hybrid"
        runtime.premium_client = default
        runtime.premium_pool = pool
        runtime.capability_profile = CapabilityProfile(
            routes=dict(HYBRID_DEFAULT_ROUTES)
        )

        wrapper = _build_hybrid_graphiti_add_episode_wrapper(runtime)
        with patch(
            "watercooler.path_resolver.derive_t2_database_name",
            return_value="mostlyharmless_ai_watercooler_cloud_t2",
        ):
            await wrapper(
                MagicMock(),
                content="c",
                group_id="mostlyharmless_ai_watercooler_cloud_t2",
            )
        default.call_tool_text.assert_awaited_once()


class TestBulkIndexWriteFailClosed:
    """PR #1062 re-review P1: default bulk_index mode is memory_ingest —
    a WRITE — so its remote branch must not use the read-only boot
    fallback."""

    def _wrapper_runtime(self, pool):
        from watercooler_mcp.capabilities import (
            HYBRID_DEFAULT_ROUTES,
            CapabilityProfile,
        )

        runtime = MagicMock()
        runtime.surface = "local_hybrid"
        runtime.premium_client = pool.default
        runtime.premium_pool = pool
        runtime.capability_profile = CapabilityProfile(
            routes=dict(HYBRID_DEFAULT_ROUTES)
        )
        return runtime

    @pytest.mark.anyio
    async def test_underivable_code_path_fails_closed(self) -> None:
        from watercooler_mcp.tools.memory import (
            _build_hybrid_bulk_index_wrapper,
        )

        default = _client("mostlyharmless-ai/watercooler")
        pool = MagicMock()
        pool.default = default
        pool.client_for_path = MagicMock(side_effect=ValueError("no slug"))
        wrapper = _build_hybrid_bulk_index_wrapper(
            self._wrapper_runtime(pool)
        )

        result = await wrapper(MagicMock(), code_path="/not/a/repo")
        payload = json.loads(result.content[0].text)
        assert payload["success"] is False
        assert "scope_resolution_failed" in payload["error"]
        default.call_tool_text.assert_not_awaited()

    @pytest.mark.anyio
    async def test_absent_code_path_targets_boot_scope(self) -> None:
        from watercooler_mcp.tools.memory import (
            _build_hybrid_bulk_index_wrapper,
        )

        default = _client("mostlyharmless-ai/watercooler")
        pool = MagicMock()
        pool.default = default
        pool.client_for_path = MagicMock(
            side_effect=AssertionError("must not be called")
        )
        wrapper = _build_hybrid_bulk_index_wrapper(
            self._wrapper_runtime(pool)
        )

        await wrapper(MagicMock())
        default.call_tool_text.assert_awaited_once()

    @pytest.mark.anyio
    async def test_derivable_code_path_uses_per_repo_client(self) -> None:
        from watercooler_mcp.tools.memory import (
            _build_hybrid_bulk_index_wrapper,
        )

        default = _client("mostlyharmless-ai/watercooler")
        site_client = _client("mostlyharmless-ai/watercooler-site")
        pool = MagicMock()
        pool.default = default
        pool.client_for_path = MagicMock(return_value=site_client)
        wrapper = _build_hybrid_bulk_index_wrapper(
            self._wrapper_runtime(pool)
        )

        await wrapper(MagicMock(), code_path="/path/to/site")
        site_client.call_tool_text.assert_awaited_once()
        default.call_tool_text.assert_not_awaited()


class TestPooledReadConversion:
    """R3 (#1063 / audit-transport-modes-hosted-db-2026-07 plan v3): the
    seven formerly proxy-mounted read tools route through the per-repo
    premium client selected by the call's ``code_path``. Reads use
    ``select_pool_client``'s boot fallback (a mis-scoped read writes
    nothing), unlike the fail-closed WRITE wrappers above."""

    def _wrapper_runtime(self, pool):
        from watercooler_mcp.capabilities import (
            HYBRID_DEFAULT_ROUTES,
            CapabilityProfile,
        )

        runtime = MagicMock()
        runtime.surface = "local_hybrid"
        runtime.premium_client = pool.default
        runtime.premium_pool = pool
        runtime.capability_profile = CapabilityProfile(
            routes=dict(HYBRID_DEFAULT_ROUTES)
        )
        return runtime

    def _pool(self, default, per_repo=None):
        pool = MagicMock()
        pool.default = default
        if per_repo is None:
            pool.client_for_path = MagicMock(
                side_effect=AssertionError("must not be called")
            )
        else:
            pool.client_for_path = MagicMock(return_value=per_repo)
        return pool

    @pytest.mark.anyio
    async def test_smart_query_code_path_uses_per_repo_client(self) -> None:
        from watercooler_mcp.tools.memory import (
            _build_hybrid_pooled_read_wrapper,
            _smart_query_impl,
        )

        default = _client("mostlyharmless-ai/watercooler")
        site_client = _client("mostlyharmless-ai/watercooler-site")
        pool = self._pool(default, per_repo=site_client)
        wrapper = _build_hybrid_pooled_read_wrapper(
            self._wrapper_runtime(pool),
            "watercooler_smart_query",
            _smart_query_impl,
        )

        await wrapper(MagicMock(), query="q", code_path="/path/to/site")
        site_client.call_tool_text.assert_awaited_once()
        default.call_tool_text.assert_not_awaited()
        assert pool.client_for_path.call_args.args[0] == "/path/to/site"
        forwarded = site_client.call_tool_text.call_args.args
        assert forwarded[0] == "watercooler_smart_query"
        assert forwarded[1]["code_path"] == "/path/to/site"

    @pytest.mark.anyio
    async def test_smart_query_absent_code_path_uses_boot_client(self) -> None:
        from watercooler_mcp.tools.memory import (
            _build_hybrid_pooled_read_wrapper,
            _smart_query_impl,
        )

        default = _client("mostlyharmless-ai/watercooler")
        pool = self._pool(default)
        wrapper = _build_hybrid_pooled_read_wrapper(
            self._wrapper_runtime(pool),
            "watercooler_smart_query",
            _smart_query_impl,
        )

        await wrapper(MagicMock(), query="q")
        default.call_tool_text.assert_awaited_once()

    @pytest.mark.anyio
    async def test_underivable_code_path_falls_back_to_boot(self) -> None:
        """READ fallback semantics: an underivable code_path degrades to
        the boot client instead of failing closed (contrast the ingest
        wrapper's scope_resolution_failed above)."""
        from watercooler_mcp.tools.memory import (
            _build_hybrid_pooled_read_wrapper,
            _graph_trace_impl,
        )

        default = _client("mostlyharmless-ai/watercooler")
        pool = self._pool(default)
        pool.client_for_path = MagicMock(side_effect=ValueError("no slug"))
        wrapper = _build_hybrid_pooled_read_wrapper(
            self._wrapper_runtime(pool),
            "watercooler_graph_trace",
            _graph_trace_impl,
        )

        await wrapper(MagicMock(), entry_id="E1", code_path="/not/a/repo")
        default.call_tool_text.assert_awaited_once()

    @pytest.mark.anyio
    async def test_memory_task_status_forwards_on_boot_client(self) -> None:
        """memory_task_status has no code_path parameter — it always
        forwards on the boot client (scope table documents this)."""
        from watercooler_mcp.tools.memory import (
            _build_hybrid_pooled_read_wrapper,
            _memory_task_status_impl,
        )

        default = _client("mostlyharmless-ai/watercooler")
        pool = self._pool(default)
        wrapper = _build_hybrid_pooled_read_wrapper(
            self._wrapper_runtime(pool),
            "watercooler_memory_task_status",
            _memory_task_status_impl,
        )

        await wrapper(MagicMock(), task_id="t1")
        default.call_tool_text.assert_awaited_once()
        assert (
            default.call_tool_text.call_args.args[0]
            == "watercooler_memory_task_status"
        )

    @pytest.mark.anyio
    async def test_remote_unavailable_without_premium_client(self) -> None:
        from watercooler_mcp.tools.memory import (
            _build_hybrid_pooled_read_wrapper,
            _smart_query_impl,
        )
        from watercooler_mcp.capabilities import (
            HYBRID_DEFAULT_ROUTES,
            CapabilityProfile,
        )

        runtime = MagicMock()
        runtime.surface = "local_hybrid"
        runtime.premium_client = None
        runtime.premium_pool = None
        runtime.capability_profile = CapabilityProfile(
            routes=dict(HYBRID_DEFAULT_ROUTES)
        )
        wrapper = _build_hybrid_pooled_read_wrapper(
            runtime, "watercooler_smart_query", _smart_query_impl
        )

        result = await wrapper(MagicMock(), query="q")
        payload = json.loads(result.content[0].text)
        assert payload["error"] in {"remote_unavailable", "capability_disabled"}

    @pytest.mark.anyio
    async def test_local_route_calls_local_impl(self) -> None:
        """A user-pinned local route bypasses the pool entirely and runs
        the local implementation."""
        from watercooler_mcp.tools.memory import (
            _build_hybrid_pooled_read_wrapper,
        )

        default = _client("mostlyharmless-ai/watercooler")
        pool = self._pool(default)
        runtime = self._wrapper_runtime(pool)

        class _LocalProfile:
            def resolve_execution_target(self, cap, **_kw):
                return "local"

        runtime.capability_profile = _LocalProfile()

        called = {}

        async def _local_impl(ctx, **kwargs):
            called.update(kwargs)
            return "local-result"

        wrapper = _build_hybrid_pooled_read_wrapper(
            runtime, "watercooler_smart_query", _local_impl
        )
        result = await wrapper(MagicMock(), query="q")
        assert result == "local-result"
        assert called == {"query": "q"}
        default.call_tool_text.assert_not_awaited()

    @pytest.mark.anyio
    async def test_daemon_findings_code_path_uses_per_repo_client(self) -> None:
        from watercooler_mcp.tools.daemon import (
            _build_hybrid_pooled_daemon_wrapper,
            _daemon_findings_impl,
        )

        default = _client("mostlyharmless-ai/watercooler")
        site_client = _client("mostlyharmless-ai/watercooler-site")
        pool = self._pool(default, per_repo=site_client)
        wrapper = _build_hybrid_pooled_daemon_wrapper(
            self._wrapper_runtime(pool),
            "watercooler_daemon_findings",
            _daemon_findings_impl,
        )

        await wrapper(MagicMock(), code_path="/path/to/site")
        site_client.call_tool_text.assert_awaited_once()
        default.call_tool_text.assert_not_awaited()
        assert (
            site_client.call_tool_text.call_args.args[0]
            == "watercooler_daemon_findings"
        )

    @pytest.mark.anyio
    async def test_daemon_status_forwards_on_boot_client(self) -> None:
        """daemon_status has no code_path parameter — boot-client forward,
        documented in the scope table."""
        from watercooler_mcp.tools.daemon import (
            _build_hybrid_pooled_daemon_wrapper,
            _daemon_status_impl,
        )

        default = _client("mostlyharmless-ai/watercooler")
        pool = self._pool(default)
        wrapper = _build_hybrid_pooled_daemon_wrapper(
            self._wrapper_runtime(pool),
            "watercooler_daemon_status",
            _daemon_status_impl,
        )

        await wrapper(MagicMock(), daemon="t2_indexer")
        default.call_tool_text.assert_awaited_once()
        forwarded = default.call_tool_text.call_args.args
        assert forwarded[0] == "watercooler_daemon_status"
        assert forwarded[1]["daemon"] == "t2_indexer"

    def _wrapper_runtime_with_routes(self, pool, overrides):
        from watercooler_mcp.capabilities import (
            HYBRID_DEFAULT_ROUTES,
            CapabilityProfile,
        )

        runtime = MagicMock()
        runtime.surface = "local_hybrid"
        runtime.premium_client = pool.default
        runtime.premium_pool = pool
        routes = dict(HYBRID_DEFAULT_ROUTES)
        routes.update(overrides)
        runtime.capability_profile = CapabilityProfile(routes=routes)
        return runtime

    @pytest.mark.anyio
    async def test_acknowledge_honors_disabled_daemon_control(self) -> None:
        """PR #1081 review (blocking): daemon_findings(action="acknowledge")
        resolves to daemon_control (L3), not daemon_observe. With
        daemon_control disabled while daemon_observe stays remote, the
        acknowledge write must be refused per call — not forwarded to the
        hosted endpoint on the registration-time observe route."""
        from watercooler_mcp.tools.daemon import (
            _build_hybrid_pooled_daemon_wrapper,
            _daemon_findings_impl,
        )

        default = _client("mostlyharmless-ai/watercooler")
        pool = self._pool(default)
        runtime = self._wrapper_runtime_with_routes(
            pool, {"daemon_control": "disabled"}
        )
        wrapper = _build_hybrid_pooled_daemon_wrapper(
            runtime, "watercooler_daemon_findings", _daemon_findings_impl
        )

        result = await wrapper(
            MagicMock(), action="acknowledge", finding_id="F1"
        )
        payload = json.loads(result)
        assert payload["error"] == "capability_disabled"
        assert payload["capability"] == "daemon_control"
        default.call_tool_text.assert_not_awaited()

    @pytest.mark.anyio
    async def test_acknowledge_honors_local_daemon_control(self) -> None:
        """daemon_control routed local runs the local implementation —
        the observe-remote registration path must not hijack the write."""
        from watercooler_mcp.tools.daemon import (
            _build_hybrid_pooled_daemon_wrapper,
        )

        default = _client("mostlyharmless-ai/watercooler")
        pool = self._pool(default)
        runtime = self._wrapper_runtime_with_routes(
            pool, {"daemon_control": "local"}
        )
        called = {}

        def _local_impl(ctx, **kwargs):
            called.update(kwargs)
            return "local-ack"

        wrapper = _build_hybrid_pooled_daemon_wrapper(
            runtime, "watercooler_daemon_findings", _local_impl
        )
        result = await wrapper(
            MagicMock(), action="acknowledge", finding_id="F1"
        )
        assert result == "local-ack"
        assert called["action"] == "acknowledge"
        default.call_tool_text.assert_not_awaited()

    @pytest.mark.anyio
    async def test_acknowledge_forwards_remote_on_default_routes(self) -> None:
        """Default hybrid routes daemon_control remote — acknowledge still
        forwards, now via the per-call pooled client."""
        from watercooler_mcp.tools.daemon import (
            _build_hybrid_pooled_daemon_wrapper,
            _daemon_findings_impl,
        )

        default = _client("mostlyharmless-ai/watercooler")
        site_client = _client("mostlyharmless-ai/watercooler-site")
        pool = self._pool(default, per_repo=site_client)
        wrapper = _build_hybrid_pooled_daemon_wrapper(
            self._wrapper_runtime(pool),
            "watercooler_daemon_findings",
            _daemon_findings_impl,
        )

        await wrapper(
            MagicMock(),
            action="acknowledge",
            finding_id="F1",
            code_path="/path/to/site",
        )
        site_client.call_tool_text.assert_awaited_once()
        default.call_tool_text.assert_not_awaited()

    @pytest.mark.anyio
    async def test_daemon_observe_disabled_refuses_listing(self) -> None:
        """The plain listing leg re-resolves too: daemon_observe disabled
        refuses instead of forwarding."""
        from watercooler_mcp.tools.daemon import (
            _build_hybrid_pooled_daemon_wrapper,
            _daemon_status_impl,
        )

        default = _client("mostlyharmless-ai/watercooler")
        pool = self._pool(default)
        runtime = self._wrapper_runtime_with_routes(
            pool, {"daemon_observe": "disabled"}
        )
        wrapper = _build_hybrid_pooled_daemon_wrapper(
            runtime, "watercooler_daemon_status", _daemon_status_impl
        )

        result = await wrapper(MagicMock())
        payload = json.loads(result)
        assert payload["error"] == "capability_disabled"
        default.call_tool_text.assert_not_awaited()
