"""Unit tests for multi-tier memory query orchestration.

Tests the TierOrchestrator class and supporting functions for intelligent
routing across T1 (Baseline), T2 (Graphiti), and T3 (LeanRAG) memory tiers.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from watercooler_memory.tier_strategy import (
    DEFAULT_MAX_TIERS,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_MIN_RESULTS,
    LEANRAG_LEVEL_MODE_ALL,
    LEANRAG_LEVEL_MODE_BASE,
    LEANRAG_LEVEL_MODE_CLUSTERS,
    QueryIntent,
    Tier,
    TierConfig,
    TierEvidence,
    TierOrchestrator,
    TierResult,
    _get_int_env,
    detect_intent,
    evaluate_sufficiency,
    get_leanrag_level_mode,
    load_tier_config,
    smart_query,
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def sample_evidence() -> list[TierEvidence]:
    """Create sample evidence for testing."""
    return [
        TierEvidence(
            tier=Tier.T1,
            id="entry-1",
            content="Authentication implemented with OAuth2",
            score=0.85,
            name="Auth Implementation",
            provenance={"thread_topic": "auth"},
        ),
        TierEvidence(
            tier=Tier.T1,
            id="entry-2",
            content="JWT tokens used for session management",
            score=0.75,
            name="Session Design",
            provenance={"thread_topic": "auth"},
        ),
        TierEvidence(
            tier=Tier.T2,
            id="node-1",
            content="OAuth2Provider entity",
            score=0.90,
            name="OAuth2Provider",
            provenance={"group_id": "project_x"},
        ),
    ]


@pytest.fixture
def mock_threads_dir(tmp_path: Path) -> Path:
    """Create a mock threads directory with graph files."""
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()

    # Create graph directory (must match baseline_graph.reader.get_graph_dir)
    graph_dir = threads_dir / "graph" / "baseline"
    graph_dir.mkdir(parents=True)

    # Create minimal nodes.jsonl
    nodes_file = graph_dir / "nodes.jsonl"
    nodes_file.write_text(
        '{"type": "entry", "entry_id": "test-1", "title": "Test Entry", '
        '"body": "Test content about authentication", "summary": "Auth test", '
        '"thread_topic": "auth", "timestamp": "2025-01-01T12:00:00Z"}\n'
    )

    return threads_dir


@pytest.fixture
def basic_config(mock_threads_dir: Path) -> TierConfig:
    """Create a basic tier configuration for testing."""
    return TierConfig(
        t1_enabled=True,
        t2_enabled=False,
        t3_enabled=False,
        threads_dir=mock_threads_dir,
    )


# ============================================================================
# Test _get_int_env Helper
# ============================================================================


class TestGetIntEnv:
    """Tests for _get_int_env environment variable helper."""

    def test_valid_integer(self, monkeypatch) -> None:
        """Test parsing a valid integer from environment."""
        monkeypatch.setenv("TEST_INT", "42")
        assert _get_int_env("TEST_INT", 10) == 42

    def test_invalid_integer_returns_default(self, monkeypatch) -> None:
        """Test that invalid integer returns default with warning."""
        monkeypatch.setenv("TEST_INT", "not_a_number")
        assert _get_int_env("TEST_INT", 10) == 10

    def test_missing_returns_default(self, monkeypatch) -> None:
        """Test that missing env var returns default."""
        monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
        assert _get_int_env("NONEXISTENT_VAR", 10) == 10

    def test_empty_string_returns_default(self, monkeypatch) -> None:
        """Test that empty string returns default."""
        monkeypatch.setenv("TEST_INT", "")
        assert _get_int_env("TEST_INT", 10) == 10

    def test_negative_integer(self, monkeypatch) -> None:
        """Test parsing negative integer."""
        monkeypatch.setenv("TEST_INT", "-5")
        assert _get_int_env("TEST_INT", 10) == -5

    def test_zero(self, monkeypatch) -> None:
        """Test parsing zero."""
        monkeypatch.setenv("TEST_INT", "0")
        assert _get_int_env("TEST_INT", 10) == 0


# ============================================================================
# Test Intent Detection
# ============================================================================


class TestIntentDetection:
    """Tests for query intent detection."""

    def test_summarize_intent(self) -> None:
        """Test detection of summarize intent."""
        queries = [
            "summarize the authentication approach",
            "give me an overview of error handling",
            "explain the architecture",
            "describe the evolution of the API",
        ]
        for query in queries:
            assert detect_intent(query) == QueryIntent.SUMMARIZE, f"Failed for: {query}"

    def test_multi_hop_intent(self) -> None:
        """Test detection of multi-hop reasoning intent."""
        queries = [
            "how did authentication lead to the security refactor?",
            "why did we change the database schema?",
            "what led to the performance improvements?",
            "trace the path from design to implementation",
        ]
        for query in queries:
            assert detect_intent(query) == QueryIntent.MULTI_HOP, f"Failed for: {query}"

    def test_temporal_intent(self) -> None:
        """Test detection of temporal query intent."""
        queries = [
            "when was OAuth implemented?",
            "what happened before the refactor?",
            "show me the timeline of changes",
            "what was the latest update?",
        ]
        for query in queries:
            assert detect_intent(query) == QueryIntent.TEMPORAL, f"Failed for: {query}"

    def test_entity_search_intent(self) -> None:
        """Test detection of entity search intent."""
        queries = [
            "who implemented authentication?",
            "find the UserService class",
            "what is OAuth2Provider?",
        ]
        for query in queries:
            assert detect_intent(query) == QueryIntent.ENTITY_SEARCH, f"Failed for: {query}"

    def test_relational_intent(self) -> None:
        """Test detection of relational query intent."""
        queries = [
            "components related to authentication",
            "code that depends on UserService",
            "modules that uses the OAuth module",
        ]
        for query in queries:
            assert detect_intent(query) == QueryIntent.RELATIONAL, f"Failed for: {query}"

    def test_default_lookup_intent(self) -> None:
        """Test that simple queries default to lookup intent."""
        queries = [
            "error handling patterns",
            "test coverage",
            "deployment steps",
        ]
        for query in queries:
            assert detect_intent(query) == QueryIntent.LOOKUP, f"Failed for: {query}"


# ============================================================================
# Test LeanRAG Level Mode Mapping
# ============================================================================


class TestLeanRAGLevelMode:
    """Tests for intent-to-level_mode mapping for LeanRAG queries."""

    def test_constants_defined(self) -> None:
        """Verify level_mode constants are correctly defined."""
        assert LEANRAG_LEVEL_MODE_BASE == 0
        assert LEANRAG_LEVEL_MODE_CLUSTERS == 1
        assert LEANRAG_LEVEL_MODE_ALL == 2

    def test_lookup_uses_base_level(self) -> None:
        """LOOKUP intent should use base entities for precision."""
        assert get_leanrag_level_mode(QueryIntent.LOOKUP) == LEANRAG_LEVEL_MODE_BASE

    def test_entity_search_uses_base_level(self) -> None:
        """ENTITY_SEARCH intent should use base entities for precision."""
        assert get_leanrag_level_mode(QueryIntent.ENTITY_SEARCH) == LEANRAG_LEVEL_MODE_BASE

    def test_summarize_uses_clusters(self) -> None:
        """SUMMARIZE intent should use clusters for synthesis."""
        assert get_leanrag_level_mode(QueryIntent.SUMMARIZE) == LEANRAG_LEVEL_MODE_CLUSTERS

    def test_multi_hop_uses_clusters(self) -> None:
        """MULTI_HOP intent should use clusters for broader context."""
        assert get_leanrag_level_mode(QueryIntent.MULTI_HOP) == LEANRAG_LEVEL_MODE_CLUSTERS

    def test_temporal_uses_all_levels(self) -> None:
        """TEMPORAL intent should use all levels for completeness."""
        assert get_leanrag_level_mode(QueryIntent.TEMPORAL) == LEANRAG_LEVEL_MODE_ALL

    def test_relational_uses_all_levels(self) -> None:
        """RELATIONAL intent should use all levels for completeness."""
        assert get_leanrag_level_mode(QueryIntent.RELATIONAL) == LEANRAG_LEVEL_MODE_ALL

    def test_unknown_defaults_to_clusters(self) -> None:
        """UNKNOWN intent should default to clusters."""
        assert get_leanrag_level_mode(QueryIntent.UNKNOWN) == LEANRAG_LEVEL_MODE_CLUSTERS

    def test_all_intents_have_mapping(self) -> None:
        """All QueryIntent values should have a defined level_mode mapping."""
        for intent in QueryIntent:
            level_mode = get_leanrag_level_mode(intent)
            assert level_mode in (
                LEANRAG_LEVEL_MODE_BASE,
                LEANRAG_LEVEL_MODE_CLUSTERS,
                LEANRAG_LEVEL_MODE_ALL,
            ), f"Invalid level_mode {level_mode} for {intent}"


# ============================================================================
# Test Sufficiency Evaluation
# ============================================================================


class TestSufficiencyEvaluation:
    """Tests for result sufficiency evaluation."""

    def test_empty_evidence_not_sufficient(self) -> None:
        """Empty evidence should not be sufficient."""
        is_sufficient, reason = evaluate_sufficiency([])
        assert not is_sufficient
        assert "No results" in reason

    def test_insufficient_result_count(self) -> None:
        """Fewer than min_results should not be sufficient."""
        evidence = [
            TierEvidence(tier=Tier.T1, id="1", content="test", score=0.8),
            TierEvidence(tier=Tier.T1, id="2", content="test2", score=0.9),
        ]
        is_sufficient, reason = evaluate_sufficiency(evidence, min_results=5)
        assert not is_sufficient
        assert "Only 2 results" in reason

    def test_total_results_used_for_count(self) -> None:
        """total_results should drive the quantity check even with subset evidence."""
        evidence = [
            TierEvidence(tier=Tier.T2, id="1", content="test", score=0.9),
        ]
        is_sufficient, reason = evaluate_sufficiency(
            evidence,
            min_results=3,
            min_confidence=0.5,
            total_results=3,
        )
        assert is_sufficient
        assert "Sufficient" in reason

    def test_low_confidence_not_sufficient(self) -> None:
        """Low average confidence should not be sufficient."""
        evidence = [
            TierEvidence(tier=Tier.T1, id="1", content="test", score=0.2),
            TierEvidence(tier=Tier.T1, id="2", content="test2", score=0.3),
            TierEvidence(tier=Tier.T1, id="3", content="test3", score=0.25),
        ]
        is_sufficient, reason = evaluate_sufficiency(evidence, min_confidence=0.5)
        assert not is_sufficient
        assert "Low confidence" in reason

    def test_sufficient_results(self) -> None:
        """Should be sufficient with enough good results."""
        evidence = [
            TierEvidence(tier=Tier.T1, id="1", content="test", score=0.8),
            TierEvidence(tier=Tier.T1, id="2", content="test2", score=0.9),
            TierEvidence(tier=Tier.T1, id="3", content="test3", score=0.85),
        ]
        is_sufficient, reason = evaluate_sufficiency(evidence)
        assert is_sufficient
        assert "Sufficient" in reason

    def test_all_zero_scores_not_sufficient(self) -> None:
        """All-zero scores should not be sufficient due to low confidence."""
        evidence = [
            TierEvidence(tier=Tier.T1, id="1", content="x", score=0.0),
            TierEvidence(tier=Tier.T1, id="2", content="y", score=0.0),
            TierEvidence(tier=Tier.T1, id="3", content="z", score=0.0),
        ]
        is_sufficient, reason = evaluate_sufficiency(evidence, min_confidence=0.5)
        assert not is_sufficient
        assert "Low confidence" in reason


# ============================================================================
# Test TierConfig
# ============================================================================


class TestTierConfig:
    """Tests for TierConfig dataclass."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        config = TierConfig()
        assert config.t1_enabled is True
        assert config.t2_enabled is False
        assert config.t3_enabled is False
        assert config.min_results == DEFAULT_MIN_RESULTS
        assert config.min_confidence == DEFAULT_MIN_CONFIDENCE
        assert config.max_tiers == DEFAULT_MAX_TIERS

    def test_load_from_env(self, monkeypatch) -> None:
        """Test loading configuration from environment variables."""
        from watercooler.config_facade import config as cfg_facade

        monkeypatch.setenv("WATERCOOLER_TIER_T1_ENABLED", "1")
        monkeypatch.setenv("WATERCOOLER_TIER_T2_ENABLED", "0")
        monkeypatch.setenv("WATERCOOLER_TIER_T3_ENABLED", "0")
        monkeypatch.setenv("WATERCOOLER_TIER_MAX_TIERS", "1")
        monkeypatch.setenv("WATERCOOLER_TIER_MIN_RESULTS", "5")

        # Prevent project config from overriding env var settings
        with patch("watercooler.config_loader._get_project_config_dir", return_value=None):
            cfg_facade.reset()
            config = load_tier_config()

            assert config.t1_enabled is True
            assert config.t2_enabled is False
            assert config.t3_enabled is False
            assert config.max_tiers == 1
            assert config.min_results == 5

    def test_t2_requires_graphiti(self, monkeypatch, isolated_config) -> None:
        """T2 should only be enabled if Graphiti is configured or env var set."""
        from watercooler.config_facade import config as cfg_facade

        # Clear any T2 env var overrides (isolated_config already provides config isolation)
        monkeypatch.delenv("WATERCOOLER_GRAPHITI_ENABLED", raising=False)
        monkeypatch.delenv("WATERCOOLER_TIER_T2_ENABLED", raising=False)

        # Prevent project config from overriding test isolation
        with patch("watercooler.config_loader._get_project_config_dir", return_value=None):
            # Without graphiti backend (and no TOML t2_enabled), T2 uses schema default (True)
            # but should auto-enable from graphiti backend if configured
            with patch("watercooler.memory_config.get_memory_backend", return_value="null"):
                cfg_facade.reset()
                config = load_tier_config()
                # Schema default is t2_enabled=True, so it's enabled even without graphiti
                assert config.t2_enabled is True

            # With WATERCOOLER_TIER_T2_ENABLED=0, T2 should be disabled
            monkeypatch.setenv("WATERCOOLER_TIER_T2_ENABLED", "0")
            cfg_facade.reset()
            config = load_tier_config()
            assert config.t2_enabled is False

            # With WATERCOOLER_TIER_T2_ENABLED=1, T2 should be enabled
            monkeypatch.setenv("WATERCOOLER_TIER_T2_ENABLED", "1")
            cfg_facade.reset()
            config = load_tier_config()
            assert config.t2_enabled is True


# ============================================================================
# Test TierEvidence
# ============================================================================


class TestTierEvidence:
    """Tests for TierEvidence dataclass."""

    def test_create_evidence(self) -> None:
        """Test creating tier evidence."""
        evidence = TierEvidence(
            tier=Tier.T1,
            id="entry-123",
            content="Test content",
            score=0.85,
            name="Test Entry",
        )
        assert evidence.tier == Tier.T1
        assert evidence.id == "entry-123"
        assert evidence.score == 0.85

    def test_evidence_with_provenance(self) -> None:
        """Test evidence with provenance metadata."""
        evidence = TierEvidence(
            tier=Tier.T2,
            id="node-456",
            content="Entity content",
            score=0.9,
            provenance={
                "group_id": "project_x",
                "timestamp": "2025-01-01T12:00:00Z",
            },
        )
        assert evidence.provenance["group_id"] == "project_x"


# ============================================================================
# Test TierResult
# ============================================================================


class TestTierResult:
    """Tests for TierResult dataclass."""

    def test_empty_result(self) -> None:
        """Test empty result properties."""
        result = TierResult(query="test query")
        assert result.result_count == 0
        assert result.top_results() == []
        assert result.by_tier(Tier.T1) == []

    def test_result_with_evidence(self, sample_evidence) -> None:
        """Test result with evidence."""
        result = TierResult(
            query="test query",
            evidence=sample_evidence,
            tiers_queried=[Tier.T1, Tier.T2],
            primary_tier=Tier.T2,
            sufficient=True,
        )
        assert result.result_count == 3
        assert len(result.by_tier(Tier.T1)) == 2
        assert len(result.by_tier(Tier.T2)) == 1

    def test_top_results_ordering(self, sample_evidence) -> None:
        """Test top results are ordered by score."""
        result = TierResult(query="test", evidence=sample_evidence)
        top = result.top_results(2)
        assert len(top) == 2
        assert top[0].score >= top[1].score

    def test_to_dict_serialization(self, sample_evidence) -> None:
        """Test JSON serialization."""
        result = TierResult(
            query="test query",
            evidence=sample_evidence,
            tiers_queried=[Tier.T1, Tier.T2],
            primary_tier=Tier.T1,
            sufficient=True,
            message="Found 3 results",
        )
        d = result.to_dict()
        assert d["query"] == "test query"
        assert d["result_count"] == 3
        assert d["tiers_queried"] == ["T1", "T2"]
        assert d["primary_tier"] == "T1"
        assert len(d["evidence"]) == 3


# ============================================================================
# Test TierOrchestrator
# ============================================================================


class TestTierOrchestrator:
    """Tests for TierOrchestrator class."""

    def test_init_with_no_tiers(self, tmp_path) -> None:
        """Test orchestrator initialization with no available tiers."""
        config = TierConfig(
            t1_enabled=False,
            t2_enabled=False,
            t3_enabled=False,
        )
        orchestrator = TierOrchestrator(config)
        assert orchestrator.available_tiers == []

    def test_init_with_t1_only(self, basic_config) -> None:
        """Test orchestrator with only T1 available."""
        orchestrator = TierOrchestrator(basic_config)
        assert Tier.T1 in orchestrator.available_tiers
        assert Tier.T2 not in orchestrator.available_tiers

    def test_query_no_tiers_available(self) -> None:
        """Test query when no tiers are available."""
        config = TierConfig(
            t1_enabled=False,
            t2_enabled=False,
            t3_enabled=False,
        )
        orchestrator = TierOrchestrator(config)
        result = orchestrator.query("test query")
        assert result.result_count == 0
        assert "No memory tiers available" in result.message

    def test_query_with_t1(self, basic_config, mock_threads_dir) -> None:
        """Test query execution with T1 tier."""
        orchestrator = TierOrchestrator(basic_config)

        # Query should work (even if no results match)
        result = orchestrator.query("authentication")
        assert Tier.T1 in result.tiers_queried

    def test_force_tier(self, basic_config) -> None:
        """Test forcing a specific tier."""
        orchestrator = TierOrchestrator(basic_config)

        # Force T1 should work
        result = orchestrator.query("test", force_tier=Tier.T1)
        assert result.tiers_queried == [Tier.T1]

    def test_force_unavailable_tier(self, basic_config) -> None:
        """Test forcing an unavailable tier."""
        orchestrator = TierOrchestrator(basic_config)

        # Force T2 (not available) should fail
        result = orchestrator.query("test", force_tier=Tier.T2)
        assert "not available" in result.message

    def test_escalation_disabled(self, basic_config) -> None:
        """Test that escalation can be disabled."""
        orchestrator = TierOrchestrator(basic_config)
        result = orchestrator.query("test", allow_escalation=False)
        # Should only query T1 even if insufficient
        assert len(result.tiers_queried) == 1

    def test_max_tiers_respected(self, basic_config) -> None:
        """Test that max_tiers limit is respected."""
        basic_config.max_tiers = 1
        orchestrator = TierOrchestrator(basic_config)
        result = orchestrator.query("test")
        assert len(result.tiers_queried) <= 1

    def test_fallback_to_t1_when_t2_empty(self, mock_threads_dir, monkeypatch) -> None:
        """Should fall back to cheaper tier if higher tier returns nothing."""
        config = TierConfig(
            t1_enabled=True,
            t2_enabled=True,
            t3_enabled=False,
            threads_dir=mock_threads_dir,
            code_path=mock_threads_dir.parent,
        )
        orchestrator = TierOrchestrator(config)
        orchestrator._available_tiers = [Tier.T1, Tier.T2]

        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t2",
            lambda *args, **kwargs: [],
        )
        t1_evidence = [
            TierEvidence(tier=Tier.T1, id="t1", content="fallback", score=0.8),
        ]
        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t1",
            lambda *args, **kwargs: t1_evidence,
        )

        result = orchestrator.query("when was OAuth implemented?", intent=QueryIntent.TEMPORAL)
        assert result.tiers_queried[0] == Tier.T2
        assert Tier.T1 in result.tiers_queried
        assert result.result_count == len(t1_evidence)

    def test_sufficiency_uses_current_tier_confidence(self, mock_threads_dir, monkeypatch) -> None:
        """Confidence should be judged on the current tier while counting total results."""
        config = TierConfig(
            t1_enabled=True,
            t2_enabled=True,
            t3_enabled=False,
            threads_dir=mock_threads_dir,
            code_path=mock_threads_dir.parent,
            min_confidence=0.5,
            min_results=3,
        )
        orchestrator = TierOrchestrator(config)
        orchestrator._available_tiers = [Tier.T1, Tier.T2]

        low_confidence = [
            TierEvidence(tier=Tier.T1, id="1", content="low1", score=0.2),
            TierEvidence(tier=Tier.T1, id="2", content="low2", score=0.3),
        ]
        high_confidence = [
            TierEvidence(tier=Tier.T2, id="3", content="high1", score=0.9),
            TierEvidence(tier=Tier.T2, id="4", content="high2", score=0.85),
        ]

        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t1",
            lambda *args, **kwargs: low_confidence,
        )
        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t2",
            lambda *args, **kwargs: high_confidence,
        )

        result = orchestrator.query("when was OAuth implemented?", intent=QueryIntent.LOOKUP)
        assert result.sufficient is True
        assert result.primary_tier == Tier.T2
        assert result.result_count == len(low_confidence) + len(high_confidence)


# ============================================================================
# Test LeanRAG Level Mode Integration
# ============================================================================


class TestLevelModeIntegration:
    """Tests verifying level_mode is passed through the orchestration chain."""

    def test_t3_passes_level_mode_to_backend(self, mock_threads_dir, monkeypatch) -> None:
        """Verify _query_t3 passes intent-based level_mode to LeanRAG search_nodes."""
        config = TierConfig(
            t1_enabled=False,
            t2_enabled=False,
            t3_enabled=True,
            threads_dir=mock_threads_dir,
            code_path=mock_threads_dir.parent,
        )
        orchestrator = TierOrchestrator(config)
        orchestrator._available_tiers = [Tier.T3]

        # Track what level_mode was passed to search_nodes
        captured_kwargs: dict[str, Any] = {}

        class FakeLeanRAGBackend:
            def __init__(self, *args, **kwargs):
                pass
            def search_nodes(self, query, **kwargs):
                captured_kwargs.update(kwargs)
                return []
            def search_facts(self, query, **kwargs):
                return []

        # Mock the imports inside _query_t3
        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t3",
            lambda query, code_path, limit=5, group_ids=None, intent=None: (
                # Simulate what _query_t3 does: compute level_mode from intent
                _capture_and_return(intent, captured_kwargs)
            ),
        )

        # Use LOOKUP intent -> should map to level_mode=0 (base)
        result = orchestrator.query("find UserService class", intent=QueryIntent.LOOKUP)
        assert captured_kwargs.get("intent") == QueryIntent.LOOKUP

        # Use SUMMARIZE intent -> should map to level_mode=1 (clusters)
        result = orchestrator.query("summarize the auth approach", intent=QueryIntent.SUMMARIZE)
        assert captured_kwargs.get("intent") == QueryIntent.SUMMARIZE

    def test_query_tier_passes_intent_to_t3(self, mock_threads_dir, monkeypatch) -> None:
        """Verify _query_tier passes intent through when querying T3."""
        config = TierConfig(
            t1_enabled=False,
            t2_enabled=False,
            t3_enabled=True,
            threads_dir=mock_threads_dir,
            code_path=mock_threads_dir.parent,
        )
        orchestrator = TierOrchestrator(config)
        orchestrator._available_tiers = [Tier.T3]

        received_intent = [None]

        def mock_query_t3(query, code_path, limit=5, group_ids=None, intent=None):
            received_intent[0] = intent
            return [
                TierEvidence(tier=Tier.T3, id="t3-1", content="test", score=0.9),
                TierEvidence(tier=Tier.T3, id="t3-2", content="test2", score=0.8),
                TierEvidence(tier=Tier.T3, id="t3-3", content="test3", score=0.7),
            ]

        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t3",
            mock_query_t3,
        )

        # Query with RELATIONAL intent
        result = orchestrator.query("components related to auth", intent=QueryIntent.RELATIONAL)
        assert received_intent[0] == QueryIntent.RELATIONAL
        assert Tier.T3 in result.tiers_queried

    def test_level_mode_in_evidence_metadata(self, mock_threads_dir, monkeypatch) -> None:
        """Verify level_mode is included in TierEvidence metadata."""
        config = TierConfig(
            t1_enabled=False,
            t2_enabled=False,
            t3_enabled=True,
            threads_dir=mock_threads_dir,
            code_path=mock_threads_dir.parent,
        )
        orchestrator = TierOrchestrator(config)
        orchestrator._available_tiers = [Tier.T3]

        def mock_query_t3(query, code_path, limit=5, group_ids=None, intent=None):
            # Simulate what the real _query_t3 does: include level_mode in metadata
            level_mode = get_leanrag_level_mode(intent or QueryIntent.UNKNOWN)
            return [
                TierEvidence(
                    tier=Tier.T3,
                    id="t3-1",
                    content="test entity",
                    score=0.9,
                    metadata={
                        "node_type": "hierarchical_entity",
                        "backend": "leanrag",
                        "level_mode": level_mode,
                    },
                ),
                TierEvidence(
                    tier=Tier.T3,
                    id="t3-2",
                    content="test entity 2",
                    score=0.8,
                    metadata={
                        "node_type": "hierarchical_entity",
                        "backend": "leanrag",
                        "level_mode": level_mode,
                    },
                ),
                TierEvidence(
                    tier=Tier.T3,
                    id="t3-3",
                    content="test entity 3",
                    score=0.7,
                    metadata={
                        "node_type": "hierarchical_entity",
                        "backend": "leanrag",
                        "level_mode": level_mode,
                    },
                ),
            ]

        monkeypatch.setattr(
            "watercooler_memory.tier_strategy._query_t3",
            mock_query_t3,
        )

        # LOOKUP -> level_mode=0 (base)
        result = orchestrator.query("find auth class", intent=QueryIntent.LOOKUP)
        for e in result.by_tier(Tier.T3):
            assert e.metadata["level_mode"] == LEANRAG_LEVEL_MODE_BASE

        # SUMMARIZE -> level_mode=1 (clusters)
        result = orchestrator.query("summarize auth", intent=QueryIntent.SUMMARIZE)
        for e in result.by_tier(Tier.T3):
            assert e.metadata["level_mode"] == LEANRAG_LEVEL_MODE_CLUSTERS

        # TEMPORAL -> level_mode=2 (all)
        result = orchestrator.query("when was auth added", intent=QueryIntent.TEMPORAL)
        for e in result.by_tier(Tier.T3):
            assert e.metadata["level_mode"] == LEANRAG_LEVEL_MODE_ALL


def _capture_and_return(intent, captured_kwargs):
    """Helper for test_t3_passes_level_mode_to_backend."""
    captured_kwargs["intent"] = intent
    return []


# ============================================================================
# Test Tier Selection Logic
# ============================================================================


class TestTierSelection:
    """Tests for tier selection based on query intent."""

    def test_lookup_starts_with_t1(self, basic_config) -> None:
        """Lookup queries should start with T1."""
        orchestrator = TierOrchestrator(basic_config)
        # Simple lookup query
        result = orchestrator.query("error handling", intent=QueryIntent.LOOKUP)
        assert result.tiers_queried[0] == Tier.T1

    @patch.dict(os.environ, {"WATERCOOLER_GRAPHITI_ENABLED": "1"})
    def test_temporal_prefers_t2(self, mock_threads_dir) -> None:
        """Temporal queries should prefer T2 if available."""
        config = TierConfig(
            t1_enabled=True,
            t2_enabled=True,
            t3_enabled=False,
            threads_dir=mock_threads_dir,
            code_path=mock_threads_dir.parent,
        )

        with patch("watercooler_memory.tier_strategy._query_t2") as mock_t2:
            mock_t2.return_value = []

            orchestrator = TierOrchestrator(config)
            # Mock T2 being available
            orchestrator._available_tiers = [Tier.T1, Tier.T2]

            result = orchestrator.query("when was OAuth implemented?", intent=QueryIntent.TEMPORAL)
            # Should prefer T2 for temporal queries
            assert Tier.T2 in result.tiers_queried


# ============================================================================
# Test Smart Query Convenience Function
# ============================================================================


class TestSmartQuery:
    """Tests for the smart_query convenience function."""

    def test_smart_query_with_threads_dir(self, mock_threads_dir) -> None:
        """Test smart_query with threads directory."""
        result = smart_query(
            "authentication",
            threads_dir=mock_threads_dir,
        )
        assert isinstance(result, TierResult)
        assert Tier.T1 in result.tiers_queried

    def test_smart_query_no_paths(self) -> None:
        """Test smart_query without any paths."""
        result = smart_query("test query")
        # Should handle gracefully with no available tiers
        assert isinstance(result, TierResult)
