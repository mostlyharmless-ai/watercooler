"""Plan v20 Phase 5: hybrid T2 hot-path + local handoff receipts.

Verifies that the ``_graphiti_sync_callback`` in ``local_hybrid`` routes T2
submissions through ``premium_client.call_tool_text`` and records a Stage-A
handoff receipt, without invoking the local Graphiti pipeline.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from watercooler.baseline_graph import sync as bg_sync
from watercooler_mcp import handoff_receipts
from watercooler_mcp import memory_sync


@pytest.fixture(autouse=True)
def _reset_hybrid_globals():
    """PR #654 in-PR review (test isolation): ensure the hybrid module
    globals mutated via ``memory_sync.set_runtime`` and
    ``set_hybrid_t2_handoff_active`` are restored even when a test crashes
    before its explicit teardown."""
    saved_runtime = memory_sync._runtime
    saved_t2 = bg_sync._HYBRID_T2_HANDOFF_ACTIVE
    try:
        yield
    finally:
        memory_sync._runtime = saved_runtime
        bg_sync._HYBRID_T2_HANDOFF_ACTIVE = saved_t2


@pytest.fixture(autouse=True)
def _stub_repo_slug(monkeypatch: pytest.MonkeyPatch):
    """Round 18: _submit_graphiti_to_hosted now fails closed when
    derive_repo_slug returns None. These tests run against throwaway
    tmp_path directories with no git remote, so we stub the slug to a
    canonical value. Individual tests that need to exercise the
    unresolved-slug path can override or undo this."""
    monkeypatch.setattr(
        "watercooler.path_resolver.derive_repo_slug",
        lambda **_kw: "mostlyharmless-ai/watercooler",
    )


@pytest.fixture
def receipts_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "handoff_receipts.jsonl"
    monkeypatch.setenv("WATERCOOLER_HANDOFF_RECEIPTS_FILE", str(path))
    return path


def _read_receipts(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


class _FakeRuntime:
    """Minimal ToolRuntime stand-in for hybrid routing."""

    def __init__(self, premium_client: Any) -> None:
        self.surface = "local_hybrid"
        self.premium_client = premium_client


class TestHybridRoutes:
    def test_submits_to_hosted_and_writes_receipt(
        self, tmp_path: Path, receipts_file: Path
    ) -> None:
        # Hosted stub returns a task_id on success.
        hosted_response = json.dumps(
            {
                "success": True,
                "status": "queued",
                "task_id": "hosted-abc-123",
                "remote_task_id": "hosted-abc-123",
                "group_id": "mostlyharmless_ai_watercooler_cloud",
            }
        )
        premium = MagicMock()
        premium.call_tool_text = AsyncMock(return_value=hosted_response)
        runtime = _FakeRuntime(premium_client=premium)

        memory_sync.set_runtime(runtime)
        try:
            ok = memory_sync._graphiti_sync_callback(
                threads_dir=tmp_path / "watercooler-cloud-threads",
                topic="hybrid-falkordb-state-vs-intent",
                entry_id="ENTRY1",
                entry_body="body",
                entry_title="title",
                timestamp="2026-04-22T00:00:00Z",
                agent="Claude",
                role="implementer",
                entry_type="Note",
                backend_config={},
                log=logging.getLogger("test"),
                dry_run=False,
                entry_summary="",
            )
        finally:
            memory_sync.set_runtime(None)

        assert ok is True
        assert premium.call_tool_text.await_count == 1
        name, args = premium.call_tool_text.await_args.args
        assert name == "watercooler_graphiti_add_episode"
        assert args["entry_id"] == "ENTRY1"
        assert args["content"] == "body"

        receipts = _read_receipts(receipts_file)
        assert len(receipts) == 1
        rec = receipts[0]
        assert rec["backend"] == "graphiti"
        assert rec["stage"] == "submitted"
        assert rec["entry_id"] == "ENTRY1"
        assert rec["remote_task_id"] == "hosted-abc-123"
        assert rec["submission_status"] == "queued"

    def test_rpc_failure_writes_submit_failed_receipt(
        self, tmp_path: Path, receipts_file: Path
    ) -> None:
        premium = MagicMock()
        premium.call_tool_text = AsyncMock(side_effect=RuntimeError("network"))
        runtime = _FakeRuntime(premium_client=premium)

        memory_sync.set_runtime(runtime)
        try:
            ok = memory_sync._graphiti_sync_callback(
                threads_dir=tmp_path / "wc-threads",
                topic="t",
                entry_id="ENTRY2",
                entry_body="body",
                entry_title="",
                timestamp=None,
                agent=None,
                role=None,
                entry_type=None,
                backend_config={},
                log=logging.getLogger("test"),
                dry_run=False,
                entry_summary="",
            )
        finally:
            memory_sync.set_runtime(None)

        assert ok is False
        receipts = _read_receipts(receipts_file)
        assert len(receipts) == 1
        rec = receipts[0]
        assert rec["stage"] == "submit_failed"
        assert "network" in rec["error"]

    def test_hosted_rejection_writes_submit_failed_receipt(
        self, tmp_path: Path, receipts_file: Path
    ) -> None:
        premium = MagicMock()
        premium.call_tool_text = AsyncMock(
            return_value=json.dumps({"success": False, "error": "invalid_group"})
        )
        runtime = _FakeRuntime(premium_client=premium)

        memory_sync.set_runtime(runtime)
        try:
            ok = memory_sync._graphiti_sync_callback(
                threads_dir=tmp_path / "wc-threads",
                topic="t",
                entry_id="ENTRY3",
                entry_body="x",
                entry_title=None,
                timestamp=None,
                agent=None,
                role=None,
                entry_type=None,
                backend_config={},
                log=logging.getLogger("test"),
                dry_run=False,
                entry_summary="",
            )
        finally:
            memory_sync.set_runtime(None)

        assert ok is False
        receipts = _read_receipts(receipts_file)
        assert len(receipts) == 1
        assert receipts[0]["stage"] == "submit_failed"
        assert "invalid_group" in receipts[0]["error"]


class TestHybridDoesNotExecuteLocally:
    def test_no_local_graphiti_backend_built_in_hybrid(self) -> None:
        """get_graphiti_backend must refuse in local_hybrid."""
        from watercooler_mcp import memory as mem

        # Ensure probe returns True so we reach the hybrid guard.
        runtime = _FakeRuntime(premium_client=MagicMock())
        memory_sync.set_runtime(runtime)

        with patch.object(mem, "_graphiti_importable", return_value=True):
            fake_config = MagicMock()
            try:
                result = mem.get_graphiti_backend(fake_config)
            finally:
                memory_sync.set_runtime(None)

        assert isinstance(result, dict)
        assert result.get("error") == "hybrid_refused"


class TestHandoffReceiptsModule:
    def test_summary_aggregates_counts(self, receipts_file: Path) -> None:
        handoff_receipts.append_handoff_receipt(
            backend="graphiti", stage="submitted", entry_id="A"
        )
        handoff_receipts.append_handoff_receipt(
            backend="graphiti", stage="submitted", entry_id="B"
        )
        handoff_receipts.append_handoff_receipt(
            backend="graphiti", stage="submit_failed", entry_id="C", error="boom"
        )

        s = handoff_receipts.summary()
        assert s["total"] == 3
        assert s["by_stage"]["submitted"] == 2
        assert s["by_stage"]["submit_failed"] == 1
        assert s["by_backend"]["graphiti"] == 3

    def test_recent_receipts_newest_first(self, receipts_file: Path) -> None:
        for i in range(5):
            handoff_receipts.append_handoff_receipt(
                backend="graphiti",
                stage="submitted",
                entry_id=f"E{i}",
            )
        recent = handoff_receipts.recent_receipts(limit=3)
        assert [r["entry_id"] for r in recent] == ["E4", "E3", "E2"]
