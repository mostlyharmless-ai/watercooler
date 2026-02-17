"""Unit tests for dual-stream sufficiency evaluation.

Tests the evaluate_dual_stream_sufficiency() function that checks entity and
fact streams independently for T2/T3 tiers. This prevents high-quality entities
+ low-quality facts from incorrectly triggering T3 escalation (100x cost).

See: https://github.com/mostlyharmless-ai/watercooler-cloud/issues/140
"""

from __future__ import annotations

from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

from watercooler_memory.tier_strategy import (
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_MIN_RESULTS,
    QueryIntent,
    Tier,
    TierConfig,
    TierEvidence,
    TierOrchestrator,
    evaluate_dual_stream_sufficiency,
    evaluate_sufficiency,
)


# ============================================================================
# Helpers
# ============================================================================


def _make_evidence(node_type: str, score: float, count: int = 1) -> list[TierEvidence]:
    """Create evidence items with a given node_type and score."""
    return [
        TierEvidence(
            tier=Tier.T2,
            id=f"test-{node_type}-{i}",
            content=f"Test {node_type} {i}",
            score=score,
            metadata={"node_type": node_type},
        )
        for i in range(count)
    ]


# ============================================================================
# Core Dual-Stream Sufficiency Tests
# ============================================================================


class TestDualStreamSufficiency:
    """Tests for evaluate_dual_stream_sufficiency()."""

    def test_good_entities_poor_facts_sufficient(self) -> None:
        """High-quality entities + low-quality facts should be SUFFICIENT.

        This is the primary bug scenario from issue #140: entity stream alone
        should pass sufficiency without being dragged down by low-quality facts.
        """
        entities = _make_evidence("entity", score=0.9, count=5)
        facts = _make_evidence("fact", score=0.1, count=5)
        evidence = entities + facts

        is_sufficient, reason = evaluate_dual_stream_sufficiency(
            evidence,
            min_results=DEFAULT_MIN_RESULTS,
            min_confidence=DEFAULT_MIN_CONFIDENCE,
        )

        assert is_sufficient is True
        assert "Entity stream sufficient" in reason

    def test_poor_entities_good_facts_sufficient(self) -> None:
        """Low-quality entities + high-quality facts should be SUFFICIENT.

        The fact stream alone should pass sufficiency.
        """
        entities = _make_evidence("entity", score=0.1, count=5)
        facts = _make_evidence("fact", score=0.9, count=5)
        evidence = entities + facts

        is_sufficient, reason = evaluate_dual_stream_sufficiency(
            evidence,
            min_results=DEFAULT_MIN_RESULTS,
            min_confidence=DEFAULT_MIN_CONFIDENCE,
        )

        assert is_sufficient is True
        assert "Fact stream sufficient" in reason

    def test_both_streams_good_sufficient(self) -> None:
        """Both streams with good count and scores should be SUFFICIENT."""
        entities = _make_evidence("entity", score=0.85, count=5)
        facts = _make_evidence("fact", score=0.9, count=5)
        evidence = entities + facts

        is_sufficient, reason = evaluate_dual_stream_sufficiency(
            evidence,
            min_results=DEFAULT_MIN_RESULTS,
            min_confidence=DEFAULT_MIN_CONFIDENCE,
        )

        assert is_sufficient is True
        # Entity stream is checked first
        assert "Entity stream sufficient" in reason

    def test_both_streams_low_count_combined_fallback(self) -> None:
        """Each stream below min_results but combined count insufficient.

        When neither stream alone has enough items, falls back to combined check.
        """
        entities = _make_evidence("entity", score=0.8, count=1)
        facts = _make_evidence("fact", score=0.8, count=1)
        evidence = entities + facts

        # Combined count=2 is below default min_results=3
        is_sufficient, reason = evaluate_dual_stream_sufficiency(
            evidence,
            min_results=DEFAULT_MIN_RESULTS,
            min_confidence=DEFAULT_MIN_CONFIDENCE,
        )

        # Combined count (2) < min_results (3) so NOT sufficient
        assert is_sufficient is False
        assert "Insufficient" in reason

    def test_both_streams_low_confidence_insufficient(self) -> None:
        """Enough count but all scores below threshold should be NOT SUFFICIENT."""
        entities = _make_evidence("entity", score=0.1, count=5)
        facts = _make_evidence("fact", score=0.1, count=5)
        evidence = entities + facts

        is_sufficient, reason = evaluate_dual_stream_sufficiency(
            evidence,
            min_results=DEFAULT_MIN_RESULTS,
            min_confidence=DEFAULT_MIN_CONFIDENCE,
        )

        assert is_sufficient is False
        assert "Insufficient" in reason

    def test_only_entities_sufficient(self) -> None:
        """Only entity stream present (no facts) should be SUFFICIENT."""
        entities = _make_evidence("entity", score=0.9, count=5)

        is_sufficient, reason = evaluate_dual_stream_sufficiency(
            entities,
            min_results=DEFAULT_MIN_RESULTS,
            min_confidence=DEFAULT_MIN_CONFIDENCE,
        )

        assert is_sufficient is True
        assert "Entity stream sufficient" in reason

    def test_only_facts_sufficient(self) -> None:
        """Only fact stream present (no entities) should be SUFFICIENT."""
        facts = _make_evidence("fact", score=0.9, count=5)

        is_sufficient, reason = evaluate_dual_stream_sufficiency(
            facts,
            min_results=DEFAULT_MIN_RESULTS,
            min_confidence=DEFAULT_MIN_CONFIDENCE,
        )

        assert is_sufficient is True
        assert "Fact stream sufficient" in reason

    def test_empty_evidence_insufficient(self) -> None:
        """Empty evidence list should be NOT SUFFICIENT."""
        is_sufficient, reason = evaluate_dual_stream_sufficiency([])

        assert is_sufficient is False
        assert "No results found" in reason

    def test_combined_fallback_sufficient(self) -> None:
        """Neither stream alone sufficient, but combined passes fallback.

        Entity stream: 2 items (below min_results=3), high score
        Fact stream: 2 items (below min_results=3), high score
        Combined: 4 items with total_results=4, avg score high -> SUFFICIENT
        """
        entities = _make_evidence("entity", score=0.85, count=2)
        facts = _make_evidence("fact", score=0.9, count=2)
        evidence = entities + facts

        is_sufficient, reason = evaluate_dual_stream_sufficiency(
            evidence,
            min_results=3,
            min_confidence=0.5,
            total_results=4,
        )

        assert is_sufficient is True
        assert "Combined streams sufficient" in reason


# ============================================================================
# T3 Hierarchical Node Type Tests
# ============================================================================


class TestHierarchicalNodeTypes:
    """Tests verifying endswith() matching works for T3 node types."""

    def test_hierarchical_entity_matched(self) -> None:
        """T3 'hierarchical_entity' should be matched as entity stream."""
        entities = _make_evidence("hierarchical_entity", score=0.9, count=5)

        is_sufficient, reason = evaluate_dual_stream_sufficiency(
            entities,
            min_results=DEFAULT_MIN_RESULTS,
            min_confidence=DEFAULT_MIN_CONFIDENCE,
        )

        assert is_sufficient is True
        assert "Entity stream sufficient" in reason

    def test_hierarchical_fact_matched(self) -> None:
        """T3 'hierarchical_fact' should be matched as fact stream."""
        facts = _make_evidence("hierarchical_fact", score=0.9, count=5)

        is_sufficient, reason = evaluate_dual_stream_sufficiency(
            facts,
            min_results=DEFAULT_MIN_RESULTS,
            min_confidence=DEFAULT_MIN_CONFIDENCE,
        )

        assert is_sufficient is True
        assert "Fact stream sufficient" in reason

    def test_mixed_t2_t3_node_types(self) -> None:
        """Mixed T2 and T3 node types should be classified correctly."""
        t2_entities = _make_evidence("entity", score=0.9, count=2)
        t3_entities = _make_evidence("hierarchical_entity", score=0.85, count=3)
        low_facts = _make_evidence("fact", score=0.1, count=3)
        evidence = t2_entities + t3_entities + low_facts

        is_sufficient, reason = evaluate_dual_stream_sufficiency(
            evidence,
            min_results=DEFAULT_MIN_RESULTS,
            min_confidence=DEFAULT_MIN_CONFIDENCE,
        )

        # Combined entity count (2+3=5) with high avg score
        assert is_sufficient is True
        assert "Entity stream sufficient" in reason


# ============================================================================
# Regression: Old Behavior vs New Behavior
# ============================================================================


class TestRegressionCompare:
    """Compare old evaluate_sufficiency vs new dual-stream for the bug scenario."""

    def test_old_function_would_escalate_incorrectly(self) -> None:
        """Demonstrate the bug: old function averages across streams.

        5 entities (0.9) + 5 facts (0.05) = avg 0.475 < 0.5 -> NOT sufficient.
        New function checks entity stream alone: 5 items, avg 0.9 -> SUFFICIENT.
        """
        entities = _make_evidence("entity", score=0.9, count=5)
        facts = _make_evidence("fact", score=0.05, count=5)
        evidence = entities + facts

        # Old function: avg = (5*0.9 + 5*0.05) / 10 = 0.475 < 0.5 -> NOT sufficient
        old_sufficient, old_reason = evaluate_sufficiency(
            evidence,
            min_results=DEFAULT_MIN_RESULTS,
            min_confidence=DEFAULT_MIN_CONFIDENCE,
        )

        # New function: entity stream alone passes (5 items, avg 0.9 >= 0.5)
        new_sufficient, new_reason = evaluate_dual_stream_sufficiency(
            evidence,
            min_results=DEFAULT_MIN_RESULTS,
            min_confidence=DEFAULT_MIN_CONFIDENCE,
        )

        assert old_sufficient is False, "Old function should fail (blended avg too low)"
        assert new_sufficient is True, "New function should pass (entity stream alone sufficient)"


# ============================================================================
# Integration Test: Orchestrator Routing
# ============================================================================


class TestOrchestratorDualStream:
    """Integration test verifying the orchestrator routes T2 to dual-stream."""

    @pytest.fixture
    def mock_threads_dir(self, tmp_path: Path) -> Path:
        """Create mock threads directory with graph files."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        graph_dir = threads_dir / "graph" / "baseline"
        graph_dir.mkdir(parents=True)
        (graph_dir / "nodes.jsonl").write_text(
            '{"type": "entry", "entry_id": "test-1", "title": "Test"}\n'
        )
        return threads_dir

    def test_orchestrator_uses_dual_stream_for_t2(
        self, mock_threads_dir: Path, monkeypatch
    ) -> None:
        """T2 with good entities + poor facts should NOT escalate to T3.

        Before the fix, the blended average would trigger escalation.
        After the fix, the entity stream alone is sufficient.
        """
        config = TierConfig(
            t1_enabled=True,
            t2_enabled=True,
            t3_enabled=True,
            threads_dir=mock_threads_dir,
            code_path=mock_threads_dir.parent,
            min_results=3,
            min_confidence=0.5,
            max_tiers=3,
        )
        orchestrator = TierOrchestrator(config)
        orchestrator._available_tiers = [Tier.T1, Tier.T2, Tier.T3]

        # T1: insufficient
        t1_evidence = [
            TierEvidence(
                tier=Tier.T1, id="t1-0", content="low", score=0.3,
                metadata={"node_type": "entry"},
            ),
        ]

        # T2: high-quality entities + low-quality facts
        t2_entities = [
            TierEvidence(
                tier=Tier.T2,
                id=f"t2-entity-{i}",
                content=f"Entity {i}",
                score=0.9,
                metadata={"node_type": "entity", "backend": "graphiti"},
            )
            for i in range(5)
        ]
        t2_facts = [
            TierEvidence(
                tier=Tier.T2,
                id=f"t2-fact-{i}",
                content=f"Fact {i}",
                score=0.05,
                metadata={"node_type": "fact", "backend": "graphiti"},
            )
            for i in range(5)
        ]
        t2_evidence = t2_entities + t2_facts

        # T3 should NOT be called
        t3_called = False

        def mock_t3(*args, **kwargs):
            nonlocal t3_called
            t3_called = True
            return []

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
            mock_t3,
        )

        result = orchestrator.query("test query", intent=QueryIntent.LOOKUP)

        # T2 should be marked sufficient (entity stream passes)
        assert result.sufficient is True
        assert result.primary_tier == Tier.T2
        # T3 should NOT have been called
        assert t3_called is False, "T3 should not be queried when T2 entity stream is sufficient"
        assert Tier.T3 not in result.tiers_queried
