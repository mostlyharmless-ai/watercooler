"""Focused unit tests for tier escalation scenarios.

Tests the escalation logic in the multi-tier memory query system:
- T1 → T2 escalation when T1 results are insufficient
- T2 → T3 escalation when T2 results are insufficient
- Confidence-based escalation decisions
- Force tier and max_tiers constraints
- Edge cases: empty results, timeouts, partial availability

These tests complement test_tier_strategy.py with more complex escalation scenarios.
"""

from __future__ import annotations

from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from watercooler_memory.tier_strategy import (
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_MIN_RESULTS,
    QueryIntent,
    Tier,
    TierConfig,
    TierEvidence,
    TierOrchestrator,
    TierResult,
    evaluate_sufficiency,
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_threads_dir(tmp_path: Path) -> Path:
    """Create a mock threads directory with baseline graph."""
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()

    # Create graph directory
    graph_dir = threads_dir / "graph" / "baseline"
    graph_dir.mkdir(parents=True)

    # Create minimal per-thread graph
    thread_dir = graph_dir / "threads" / "test-topic"
    thread_dir.mkdir(parents=True)

    # Create meta.json
    (thread_dir / "meta.json").write_text(
        '{"type": "thread", "topic": "test-topic", "title": "Test", "status": "OPEN"}'
    )

    # Create entries.jsonl
    (thread_dir / "entries.jsonl").write_text(
        '{"type": "entry", "entry_id": "01TEST001", "thread_topic": "test-topic", '
        '"title": "Test Entry", "body": "Test content", "summary": "Test"}\n'
    )

    return threads_dir


@pytest.fixture
def full_tier_config(mock_threads_dir: Path) -> TierConfig:
    """Configuration with all tiers enabled."""
    return TierConfig(
        t1_enabled=True,
        t2_enabled=True,
        t3_enabled=True,
        threads_dir=mock_threads_dir,
        code_path=mock_threads_dir.parent,
        min_results=3,
        min_confidence=0.5,
        max_tiers=3,
    )


def make_evidence(
    tier: Tier,
    count: int,
    base_score: float = 0.8,
    prefix: str = "",
) -> List[TierEvidence]:
    """Create sample evidence for a tier."""
    return [
        TierEvidence(
            tier=tier,
            id=f"{prefix}{tier.value.lower()}-{i}",
            content=f"Content from {tier.value} item {i}",
            score=base_score - (i * 0.05),
            name=f"Evidence {i}",
            provenance={"thread_topic": "test"},
        )
        for i in range(count)
    ]


# ============================================================================
# Basic Escalation Tests
# ============================================================================


class TestT1ToT2Escalation:
    """Tests for T1 → T2 escalation scenarios."""

    def test_escalates_when_t1_insufficient_count(
        self, full_tier_config: TierConfig, monkeypatch
    ) -> None:
        """Should escalate to T2 when T1 returns fewer than min_results."""
        orchestrator = TierOrchestrator(full_tier_config)
        orchestrator._available_tiers = [Tier.T1, Tier.T2]

        # T1 returns 2 results (below min_results=3)
        t1_evidence = make_evidence(Tier.T1, 2, base_score=0.9)
        # T2 returns 3 more results
        t2_evidence = make_evidence(Tier.T2, 3, base_score=0.85)

        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t1",
            lambda *args, **kwargs: t1_evidence,
        )
        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t2",
            lambda *args, **kwargs: t2_evidence,
        )

        result = orchestrator.query("test query")

        # Should have queried both tiers
        assert Tier.T1 in result.tiers_queried
        assert Tier.T2 in result.tiers_queried
        # Primary tier should be T2 (provided additional results)
        assert result.primary_tier == Tier.T2
        # Should have combined results
        assert result.result_count == 5

    def test_escalates_when_t1_low_confidence(
        self, full_tier_config: TierConfig, monkeypatch
    ) -> None:
        """Should escalate to T2 when T1 results have low confidence."""
        orchestrator = TierOrchestrator(full_tier_config)
        orchestrator._available_tiers = [Tier.T1, Tier.T2]

        # T1 returns 5 results but with low scores (avg < min_confidence=0.5)
        t1_evidence = make_evidence(Tier.T1, 5, base_score=0.3)
        # T2 returns high confidence results
        t2_evidence = make_evidence(Tier.T2, 3, base_score=0.9)

        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t1",
            lambda *args, **kwargs: t1_evidence,
        )
        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t2",
            lambda *args, **kwargs: t2_evidence,
        )

        result = orchestrator.query("test query")

        assert Tier.T2 in result.tiers_queried
        # Should be sufficient now with high-confidence T2 results
        assert result.sufficient is True

    def test_no_escalation_when_t1_sufficient(
        self, full_tier_config: TierConfig, monkeypatch
    ) -> None:
        """Should not escalate when T1 provides sufficient results."""
        orchestrator = TierOrchestrator(full_tier_config)
        orchestrator._available_tiers = [Tier.T1, Tier.T2]

        # T1 returns 5 high-confidence results
        t1_evidence = make_evidence(Tier.T1, 5, base_score=0.9)

        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t1",
            lambda *args, **kwargs: t1_evidence,
        )

        result = orchestrator.query("test query", intent=QueryIntent.LOOKUP)

        # Should only have queried T1
        assert result.tiers_queried == [Tier.T1]
        assert result.sufficient is True


class TestT2ToT3Escalation:
    """Tests for T2 → T3 escalation scenarios."""

    def test_escalates_to_t3_when_t2_insufficient(
        self, full_tier_config: TierConfig, monkeypatch
    ) -> None:
        """Should escalate to T3 when T1+T2 combined are insufficient."""
        orchestrator = TierOrchestrator(full_tier_config)
        orchestrator._available_tiers = [Tier.T1, Tier.T2, Tier.T3]

        # Both T1 and T2 return minimal results
        t1_evidence = make_evidence(Tier.T1, 1, base_score=0.4)
        t2_evidence = make_evidence(Tier.T2, 1, base_score=0.4)
        t3_evidence = make_evidence(Tier.T3, 3, base_score=0.9)

        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t1",
            lambda *args, **kwargs: t1_evidence,
        )
        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t2",
            lambda *args, **kwargs: t2_evidence,
        )
        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t3",
            lambda *args, **kwargs: t3_evidence,
        )

        result = orchestrator.query("complex multi-hop query")

        # Should have queried all three tiers
        assert Tier.T1 in result.tiers_queried
        assert Tier.T2 in result.tiers_queried
        assert Tier.T3 in result.tiers_queried
        # Total results from all tiers
        assert result.result_count == 5


class TestIntentBasedEscalation:
    """Tests for intent-based tier selection."""

    def test_temporal_query_starts_with_t2(
        self, full_tier_config: TierConfig, monkeypatch
    ) -> None:
        """Temporal queries should prefer T2 for temporal graph capabilities."""
        orchestrator = TierOrchestrator(full_tier_config)
        orchestrator._available_tiers = [Tier.T1, Tier.T2]

        t2_evidence = make_evidence(Tier.T2, 5, base_score=0.9)

        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t2",
            lambda *args, **kwargs: t2_evidence,
        )

        result = orchestrator.query(
            "when was OAuth implemented?",
            intent=QueryIntent.TEMPORAL,
        )

        # T2 should be first (preferred for temporal queries)
        assert result.tiers_queried[0] == Tier.T2

    def test_multi_hop_query_prefers_t3(
        self, full_tier_config: TierConfig, monkeypatch
    ) -> None:
        """Multi-hop reasoning queries should prefer T3."""
        orchestrator = TierOrchestrator(full_tier_config)
        orchestrator._available_tiers = [Tier.T1, Tier.T2, Tier.T3]

        # T1/T2 return insufficient results
        t1_evidence = make_evidence(Tier.T1, 1, base_score=0.3)
        t2_evidence = make_evidence(Tier.T2, 1, base_score=0.3)
        t3_evidence = make_evidence(Tier.T3, 5, base_score=0.9)

        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t1",
            lambda *args, **kwargs: t1_evidence,
        )
        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t2",
            lambda *args, **kwargs: t2_evidence,
        )
        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t3",
            lambda *args, **kwargs: t3_evidence,
        )

        result = orchestrator.query(
            "how did authentication lead to the security refactor?",
            intent=QueryIntent.MULTI_HOP,
        )

        # Should reach T3 for multi-hop reasoning
        assert Tier.T3 in result.tiers_queried


# ============================================================================
# Edge Cases
# ============================================================================


class TestEmptyResultsEscalation:
    """Tests for escalation when tiers return empty results."""

    def test_escalates_on_empty_t1_results(
        self, full_tier_config: TierConfig, monkeypatch
    ) -> None:
        """Should escalate when T1 returns no results."""
        orchestrator = TierOrchestrator(full_tier_config)
        orchestrator._available_tiers = [Tier.T1, Tier.T2]

        t2_evidence = make_evidence(Tier.T2, 3, base_score=0.85)

        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t1",
            lambda *args, **kwargs: [],
        )
        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t2",
            lambda *args, **kwargs: t2_evidence,
        )

        result = orchestrator.query("obscure topic")

        assert Tier.T2 in result.tiers_queried
        assert result.result_count == 3

    def test_all_tiers_empty_returns_empty_result(
        self, full_tier_config: TierConfig, monkeypatch
    ) -> None:
        """Should return empty result when all tiers are empty."""
        orchestrator = TierOrchestrator(full_tier_config)
        orchestrator._available_tiers = [Tier.T1, Tier.T2, Tier.T3]

        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t1",
            lambda *args, **kwargs: [],
        )
        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t2",
            lambda *args, **kwargs: [],
        )
        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t3",
            lambda *args, **kwargs: [],
        )

        result = orchestrator.query("nonexistent topic")

        assert result.result_count == 0
        assert result.sufficient is False
        # Should have tried all available tiers
        assert len(result.tiers_queried) == 3


class TestPartialTierAvailability:
    """Tests for escalation with partially available tiers."""

    def test_skips_unavailable_t2_escalates_to_t3(
        self, full_tier_config: TierConfig, monkeypatch
    ) -> None:
        """Should skip unavailable T2 and escalate directly to T3."""
        orchestrator = TierOrchestrator(full_tier_config)
        # T2 is not available
        orchestrator._available_tiers = [Tier.T1, Tier.T3]

        t1_evidence = make_evidence(Tier.T1, 1, base_score=0.3)
        t3_evidence = make_evidence(Tier.T3, 4, base_score=0.9)

        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t1",
            lambda *args, **kwargs: t1_evidence,
        )
        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t3",
            lambda *args, **kwargs: t3_evidence,
        )

        result = orchestrator.query("test query")

        # Should only query T1 and T3
        assert Tier.T1 in result.tiers_queried
        assert Tier.T2 not in result.tiers_queried
        assert Tier.T3 in result.tiers_queried

    def test_single_tier_available(
        self, full_tier_config: TierConfig, monkeypatch
    ) -> None:
        """Should work correctly with only one tier available."""
        orchestrator = TierOrchestrator(full_tier_config)
        orchestrator._available_tiers = [Tier.T1]

        t1_evidence = make_evidence(Tier.T1, 2, base_score=0.6)

        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t1",
            lambda *args, **kwargs: t1_evidence,
        )

        result = orchestrator.query("test query")

        # Can only query T1
        assert result.tiers_queried == [Tier.T1]
        # Insufficient but no escalation possible
        assert result.result_count == 2


class TestMaxTiersConstraint:
    """Tests for max_tiers limit on escalation."""

    def test_max_tiers_limits_escalation(
        self, full_tier_config: TierConfig, monkeypatch
    ) -> None:
        """Should not escalate beyond max_tiers."""
        full_tier_config.max_tiers = 1
        orchestrator = TierOrchestrator(full_tier_config)
        orchestrator._available_tiers = [Tier.T1, Tier.T2, Tier.T3]

        t1_evidence = make_evidence(Tier.T1, 1, base_score=0.3)

        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t1",
            lambda *args, **kwargs: t1_evidence,
        )

        result = orchestrator.query("test query")

        # Should only query 1 tier despite insufficient results
        assert len(result.tiers_queried) == 1
        assert result.tiers_queried == [Tier.T1]

    def test_max_tiers_two_allows_t1_t2_only(
        self, full_tier_config: TierConfig, monkeypatch
    ) -> None:
        """max_tiers=2 should allow T1→T2 but not T3."""
        full_tier_config.max_tiers = 2
        orchestrator = TierOrchestrator(full_tier_config)
        orchestrator._available_tiers = [Tier.T1, Tier.T2, Tier.T3]

        t1_evidence = make_evidence(Tier.T1, 1, base_score=0.2)
        t2_evidence = make_evidence(Tier.T2, 1, base_score=0.3)

        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t1",
            lambda *args, **kwargs: t1_evidence,
        )
        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t2",
            lambda *args, **kwargs: t2_evidence,
        )

        result = orchestrator.query("test query")

        # Should query T1 and T2 only
        assert len(result.tiers_queried) == 2
        assert Tier.T3 not in result.tiers_queried


class TestForceTierBehavior:
    """Tests for force_tier parameter."""

    def test_force_tier_disables_escalation(
        self, full_tier_config: TierConfig, monkeypatch
    ) -> None:
        """force_tier should disable escalation even if insufficient."""
        orchestrator = TierOrchestrator(full_tier_config)
        orchestrator._available_tiers = [Tier.T1, Tier.T2, Tier.T3]

        t1_evidence = make_evidence(Tier.T1, 1, base_score=0.2)

        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t1",
            lambda *args, **kwargs: t1_evidence,
        )

        result = orchestrator.query("test query", force_tier=Tier.T1)

        # Should only query forced tier
        assert result.tiers_queried == [Tier.T1]
        # Result may be insufficient but we respect force_tier
        assert result.result_count == 1

    def test_force_unavailable_tier_returns_error(
        self, full_tier_config: TierConfig
    ) -> None:
        """Forcing unavailable tier should fail gracefully."""
        orchestrator = TierOrchestrator(full_tier_config)
        orchestrator._available_tiers = [Tier.T1]  # Only T1 available

        result = orchestrator.query("test query", force_tier=Tier.T2)

        assert result.result_count == 0
        assert "not available" in result.message


class TestAllowEscalationParameter:
    """Tests for allow_escalation parameter."""

    def test_allow_escalation_false_stops_at_first_tier(
        self, full_tier_config: TierConfig, monkeypatch
    ) -> None:
        """allow_escalation=False should stop at first tier."""
        orchestrator = TierOrchestrator(full_tier_config)
        orchestrator._available_tiers = [Tier.T1, Tier.T2]

        t1_evidence = make_evidence(Tier.T1, 1, base_score=0.2)

        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t1",
            lambda *args, **kwargs: t1_evidence,
        )

        result = orchestrator.query("test query", allow_escalation=False)

        assert len(result.tiers_queried) == 1


# ============================================================================
# Sufficiency Evaluation Edge Cases
# ============================================================================


class TestSufficiencyEdgeCases:
    """Tests for edge cases in sufficiency evaluation."""

    def test_exactly_min_results_is_sufficient(self) -> None:
        """Exactly min_results with good confidence should be sufficient."""
        evidence = make_evidence(Tier.T1, 3, base_score=0.8)
        is_sufficient, reason = evaluate_sufficiency(evidence, min_results=3)
        assert is_sufficient is True

    def test_high_count_low_confidence_not_sufficient(self) -> None:
        """Many results with low confidence should not be sufficient."""
        evidence = make_evidence(Tier.T1, 10, base_score=0.2)
        is_sufficient, reason = evaluate_sufficiency(
            evidence, min_results=3, min_confidence=0.5
        )
        assert is_sufficient is False
        assert "confidence" in reason.lower()

    def test_single_high_score_not_sufficient(self) -> None:
        """Single perfect result is insufficient (need count)."""
        evidence = [TierEvidence(
            tier=Tier.T1,
            id="single",
            content="Perfect match",
            score=1.0,
        )]
        is_sufficient, reason = evaluate_sufficiency(evidence, min_results=3)
        assert is_sufficient is False

    def test_mixed_tier_evidence_counts_together(self) -> None:
        """Evidence from multiple tiers counts toward total."""
        t1_evidence = make_evidence(Tier.T1, 2, base_score=0.8)
        t2_evidence = make_evidence(Tier.T2, 2, base_score=0.9)
        combined = t1_evidence + t2_evidence

        is_sufficient, reason = evaluate_sufficiency(combined, min_results=3)
        assert is_sufficient is True


# ============================================================================
# Result Aggregation Tests
# ============================================================================


class TestResultAggregation:
    """Tests for aggregating results across tiers."""

    def test_results_combined_from_all_queried_tiers(
        self, full_tier_config: TierConfig, monkeypatch
    ) -> None:
        """Results from all queried tiers should be combined."""
        orchestrator = TierOrchestrator(full_tier_config)
        orchestrator._available_tiers = [Tier.T1, Tier.T2]

        t1_evidence = make_evidence(Tier.T1, 2, base_score=0.7, prefix="t1-")
        t2_evidence = make_evidence(Tier.T2, 3, base_score=0.8, prefix="t2-")

        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t1",
            lambda *args, **kwargs: t1_evidence,
        )
        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t2",
            lambda *args, **kwargs: t2_evidence,
        )

        result = orchestrator.query("test query")

        # Total should be combined
        assert result.result_count == 5

        # Can filter by tier
        t1_results = result.by_tier(Tier.T1)
        t2_results = result.by_tier(Tier.T2)
        assert len(t1_results) == 2
        assert len(t2_results) == 3

    def test_top_results_sorted_by_score(
        self, full_tier_config: TierConfig, monkeypatch
    ) -> None:
        """Top results should be sorted by score regardless of tier."""
        orchestrator = TierOrchestrator(full_tier_config)
        orchestrator._available_tiers = [Tier.T1, Tier.T2]

        # T1 has lower scores than T2
        t1_evidence = make_evidence(Tier.T1, 2, base_score=0.5)
        t2_evidence = make_evidence(Tier.T2, 2, base_score=0.9)

        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t1",
            lambda *args, **kwargs: t1_evidence,
        )
        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t2",
            lambda *args, **kwargs: t2_evidence,
        )

        result = orchestrator.query("test query")

        top = result.top_results(4)
        # Should be sorted by score descending
        scores = [e.score for e in top]
        assert scores == sorted(scores, reverse=True)
        # T2 results should be first (higher scores)
        assert top[0].tier == Tier.T2
