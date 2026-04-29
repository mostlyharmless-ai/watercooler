"""Regression tests for hosted-mode smart_query orchestrator routing.

Covers the contract that ``_smart_query_impl`` must:

- call ``orchestrator.query`` with the correct kwargs (``group_ids``,
  ``force_tier``) when running in hosted mode (HOSTED_MODE_SENTINEL),
- return Graphiti T2/T3 evidence directly instead of silently falling
  back to the GitHub keyword endpoint,
- only fall back to ``search_entries_hosted`` when the orchestrator
  returns nothing AND the caller did not pin to T2/T3,
- skip the orchestrator entirely when ``force_tier="T1"`` (since the
  hosted MCP has no local T1 graph and routes T1 to GitHub).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


def _make_orchestrator(available, query_return):
    orch = MagicMock()
    orch.available_tiers = available
    orch.query.return_value = query_return
    return orch


def _make_result(evidence=None, tiers_queried=None, primary_tier=None,
                 sufficient=True, message="ok"):
    from watercooler_memory.tier_strategy import Tier, TierResult
    return TierResult(
        query="q",
        evidence=evidence or [],
        tiers_queried=tiers_queried or [Tier.T2],
        primary_tier=primary_tier or Tier.T2,
        sufficient=sufficient,
        message=message,
    )


def _make_evidence(tier, content="hit", score=0.9):
    from watercooler_memory.tier_strategy import TierEvidence
    return TierEvidence(
        tier=tier,
        id="ev-1",
        content=content,
        score=score,
        provenance={"group_id": "grp-1"},
        metadata={"node_type": "fact"},
    )


@pytest.mark.anyio
async def test_hosted_smart_query_returns_t2_evidence_without_falling_back():
    """Hosted mode: when T2 returns evidence, do NOT call GitHub fallback."""
    from watercooler_memory.tier_strategy import Tier
    from watercooler_mcp.tools.memory import _smart_query_impl
    from watercooler_mcp import validation

    ev = _make_evidence(Tier.T2)
    orch = _make_orchestrator([Tier.T2], _make_result(evidence=[ev]))
    cfg = MagicMock(); cfg.max_tiers = 2

    fake_github = MagicMock(return_value=("never_called", None))

    with (
        patch("watercooler_memory.tier_strategy.load_tier_config", return_value=cfg),
        patch("watercooler_memory.tier_strategy.TierOrchestrator", return_value=orch),
        patch("watercooler_mcp.hosted_ops.search_entries_hosted", fake_github),
        patch.object(validation, "_require_context",
                     return_value=(None, MagicMock(threads_dir=validation.HOSTED_MODE_SENTINEL,
                                                   code_root=None))),
    ):
        result = await _smart_query_impl(query="q", ctx=MagicMock())

    payload = json.loads(result.content[0].text)
    assert payload["result_count"] == 1
    assert payload["primary_tier"] == "T2"
    fake_github.assert_not_called()
    # Verify orchestrator.query received the right kwargs (regression for the
    # original bug: max_tiers= and resolve_provenance= caused TypeError).
    orch.query.assert_called_once()
    _, kwargs = orch.query.call_args
    assert "max_tiers" not in kwargs
    assert "resolve_provenance" not in kwargs
    assert kwargs.get("force_tier") is None
    assert kwargs.get("group_ids") is None


@pytest.mark.anyio
async def test_hosted_smart_query_falls_back_to_github_when_t2_empty():
    """Hosted: T2 returns no evidence, no force_tier → GitHub T1 fallback runs."""
    from watercooler_memory.tier_strategy import Tier
    from watercooler_mcp.tools.memory import _smart_query_impl
    from watercooler_mcp import validation

    orch = _make_orchestrator([Tier.T2], _make_result(evidence=[]))
    cfg = MagicMock(); cfg.max_tiers = 2

    github_payload = {"results": [{
        "entry_id": "e1", "title": "T", "summary": "S",
        "thread_topic": "topic",
    }]}
    fake_github = MagicMock(return_value=(None, github_payload))

    with (
        patch("watercooler_memory.tier_strategy.load_tier_config", return_value=cfg),
        patch("watercooler_memory.tier_strategy.TierOrchestrator", return_value=orch),
        patch("watercooler_mcp.hosted_ops.search_entries_hosted", fake_github),
        patch.object(validation, "_require_context",
                     return_value=(None, MagicMock(threads_dir=validation.HOSTED_MODE_SENTINEL,
                                                   code_root=None))),
    ):
        result = await _smart_query_impl(query="q", ctx=MagicMock())

    payload = json.loads(result.content[0].text)
    assert payload["source"] == "hosted_github_api"
    assert payload["result_count"] == 1
    fake_github.assert_called_once()


@pytest.mark.anyio
async def test_hosted_smart_query_force_t2_does_not_fall_back():
    """Hosted: force_tier=T2 with empty evidence → empty result, no GitHub fallback."""
    from watercooler_memory.tier_strategy import Tier
    from watercooler_mcp.tools.memory import _smart_query_impl
    from watercooler_mcp import validation

    orch = _make_orchestrator(
        [Tier.T2],
        _make_result(evidence=[], primary_tier=Tier.T2, sufficient=False,
                     message="0 results"),
    )
    cfg = MagicMock(); cfg.max_tiers = 2
    fake_github = MagicMock(return_value=("never_called", None))

    with (
        patch("watercooler_memory.tier_strategy.load_tier_config", return_value=cfg),
        patch("watercooler_memory.tier_strategy.TierOrchestrator", return_value=orch),
        patch("watercooler_mcp.hosted_ops.search_entries_hosted", fake_github),
        patch.object(validation, "_require_context",
                     return_value=(None, MagicMock(threads_dir=validation.HOSTED_MODE_SENTINEL,
                                                   code_root=None))),
    ):
        result = await _smart_query_impl(
            query="q", ctx=MagicMock(), force_tier="T2",
        )

    payload = json.loads(result.content[0].text)
    fake_github.assert_not_called()
    assert payload["result_count"] == 0
    # Verify force_tier was actually passed through to the orchestrator.
    _, kwargs = orch.query.call_args
    assert kwargs["force_tier"] == Tier.T2


@pytest.mark.anyio
async def test_hosted_smart_query_force_t1_skips_orchestrator():
    """Hosted: force_tier=T1 → orchestrator never built, GitHub T1 runs directly."""
    from watercooler_mcp.tools.memory import _smart_query_impl
    from watercooler_mcp import validation

    fake_github = MagicMock(return_value=(None, {"results": []}))
    fake_orch_ctor = MagicMock()

    with (
        patch("watercooler_memory.tier_strategy.TierOrchestrator", fake_orch_ctor),
        patch("watercooler_mcp.hosted_ops.search_entries_hosted", fake_github),
        patch.object(validation, "_require_context",
                     return_value=(None, MagicMock(threads_dir=validation.HOSTED_MODE_SENTINEL,
                                                   code_root=None))),
    ):
        await _smart_query_impl(
            query="q", ctx=MagicMock(), force_tier="T1",
        )

    fake_orch_ctor.assert_not_called()
    fake_github.assert_called_once()


@pytest.mark.anyio
async def test_hosted_smart_query_passes_group_ids():
    """Hosted: caller-supplied group_ids reaches orchestrator.query()."""
    from watercooler_memory.tier_strategy import Tier
    from watercooler_mcp.tools.memory import _smart_query_impl
    from watercooler_mcp import validation

    ev = _make_evidence(Tier.T2)
    orch = _make_orchestrator([Tier.T2], _make_result(evidence=[ev]))
    cfg = MagicMock(); cfg.max_tiers = 2

    with (
        patch("watercooler_memory.tier_strategy.load_tier_config", return_value=cfg),
        patch("watercooler_memory.tier_strategy.TierOrchestrator", return_value=orch),
        patch("watercooler_mcp.hosted_ops.search_entries_hosted",
              MagicMock(return_value=(None, {"results": []}))),
        patch.object(validation, "_require_context",
                     return_value=(None, MagicMock(threads_dir=validation.HOSTED_MODE_SENTINEL,
                                                   code_root=None))),
    ):
        await _smart_query_impl(
            query="q", ctx=MagicMock(),
            group_ids=["mostlyharmless_ai_watercooler_cloud"],
        )

    _, kwargs = orch.query.call_args
    assert kwargs["group_ids"] == ["mostlyharmless_ai_watercooler_cloud"]


@pytest.mark.anyio
async def test_hosted_smart_query_force_unavailable_tier_returns_error():
    """Hosted: force_tier=T3 when only T2 available → 'tier not available' error."""
    from watercooler_memory.tier_strategy import Tier
    from watercooler_mcp.tools.memory import _smart_query_impl
    from watercooler_mcp import validation

    orch = _make_orchestrator([Tier.T2], _make_result(evidence=[]))
    cfg = MagicMock(); cfg.max_tiers = 2

    fake_github = MagicMock(return_value=("never_called", None))

    with (
        patch("watercooler_memory.tier_strategy.load_tier_config", return_value=cfg),
        patch("watercooler_memory.tier_strategy.TierOrchestrator", return_value=orch),
        patch("watercooler_mcp.hosted_ops.search_entries_hosted", fake_github),
        patch.object(validation, "_require_context",
                     return_value=(None, MagicMock(threads_dir=validation.HOSTED_MODE_SENTINEL,
                                                   code_root=None))),
    ):
        result = await _smart_query_impl(
            query="q", ctx=MagicMock(), force_tier="T3",
        )

    payload = json.loads(result.content[0].text)
    assert "not available" in payload["error"]
    fake_github.assert_not_called()
    orch.query.assert_not_called()
