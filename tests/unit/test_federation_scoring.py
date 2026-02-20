"""Unit tests for federation scoring module."""

import math
from datetime import datetime, timedelta, timezone

import pytest

from watercooler.config_schema import FederationScoringConfig
from watercooler_mcp.federation.scoring import (
    KEYWORD_SCORE_MAX,
    KEYWORD_SCORE_MIN,
    compute_ranking_score,
    compute_recency_decay,
    normalize_keyword_score,
    resolve_namespace_weight,
)


class TestNormalizeKeywordScore:
    """Tests for normalize_keyword_score."""

    def test_min_score(self):
        assert normalize_keyword_score(KEYWORD_SCORE_MIN) == 0.0

    def test_max_score(self):
        assert normalize_keyword_score(KEYWORD_SCORE_MAX) == pytest.approx(1.0)

    def test_midpoint(self):
        mid = KEYWORD_SCORE_MIN + (KEYWORD_SCORE_MAX - KEYWORD_SCORE_MIN) / 2.0
        assert normalize_keyword_score(mid) == pytest.approx(0.5)

    def test_below_min_clamps(self):
        assert normalize_keyword_score(0.5) == 0.0

    def test_above_max_clamps(self):
        assert normalize_keyword_score(3.0) == 1.0

    def test_known_value(self):
        # 1.7 => (1.7 - 1.0) / 1.4 = 0.5
        assert normalize_keyword_score(1.7) == pytest.approx(0.5)


class TestResolveNamespaceWeight:
    """Tests for resolve_namespace_weight."""

    @pytest.fixture()
    def scoring_config(self):
        return FederationScoringConfig()

    def test_primary_returns_local_weight(self, scoring_config):
        w = resolve_namespace_weight("cloud", "cloud", frozenset(), scoring_config)
        assert w == 1.0

    def test_lens_returns_lens_weight(self, scoring_config):
        w = resolve_namespace_weight("site", "cloud", frozenset(["site"]), scoring_config)
        assert w == 0.7

    def test_wide_returns_wide_weight(self, scoring_config):
        w = resolve_namespace_weight("docs", "cloud", frozenset(["site"]), scoring_config)
        assert w == 0.55

    def test_custom_weights(self):
        cfg = FederationScoringConfig(local_weight=1.0, lens_weight=0.8, wide_weight=0.6)
        w = resolve_namespace_weight("docs", "cloud", frozenset(), cfg)
        assert w == 0.6


class TestComputeRecencyDecay:
    """Tests for compute_recency_decay."""

    def test_now_returns_one(self):
        now = datetime.now(timezone.utc)
        assert compute_recency_decay(now, now) == pytest.approx(1.0)

    def test_half_life_returns_midpoint(self):
        now = datetime.now(timezone.utc)
        past = now - timedelta(days=60)
        decay = compute_recency_decay(past, now, floor=0.7, half_life_days=60.0)
        # At half_life: floor + (1-floor) * 0.5 = 0.7 + 0.3*0.5 = 0.85
        assert decay == pytest.approx(0.85, abs=0.001)

    def test_very_old_approaches_floor(self):
        now = datetime.now(timezone.utc)
        past = now - timedelta(days=365)
        decay = compute_recency_decay(past, now, floor=0.7, half_life_days=60.0)
        assert decay == pytest.approx(0.7, abs=0.01)

    def test_future_entry_clamped_to_one(self):
        now = datetime.now(timezone.utc)
        future = now + timedelta(days=10)
        decay = compute_recency_decay(future, now)
        # age_days clamped to 0 => decay = 1.0
        assert decay == pytest.approx(1.0)

    def test_naive_datetimes_treated_as_utc(self):
        now = datetime(2026, 1, 1, 12, 0, 0)
        past = now - timedelta(days=60)
        decay = compute_recency_decay(past, now)
        assert 0.7 <= decay <= 1.0

    def test_custom_floor_and_halflife(self):
        now = datetime.now(timezone.utc)
        past = now - timedelta(days=30)
        decay = compute_recency_decay(past, now, floor=0.5, half_life_days=30.0)
        # At half_life: 0.5 + 0.5*0.5 = 0.75
        assert decay == pytest.approx(0.75, abs=0.001)


class TestComputeRankingScore:
    """Tests for compute_ranking_score."""

    def test_perfect_score(self):
        score = compute_ranking_score(
            raw_score=KEYWORD_SCORE_MAX,
            namespace_weight=1.0,
            recency_decay=1.0,
        )
        assert score == pytest.approx(1.0)

    def test_multiplicative_composition(self):
        score = compute_ranking_score(
            raw_score=1.7,  # normalized to 0.5
            namespace_weight=0.7,
            recency_decay=0.85,
        )
        expected = 0.5 * 0.7 * 0.85
        assert score == pytest.approx(expected)

    def test_zero_raw_score(self):
        score = compute_ranking_score(
            raw_score=0.0,
            namespace_weight=1.0,
            recency_decay=1.0,
        )
        assert score == 0.0


class TestRankingStability:
    """Verify multiplicative scoring guarantees rank independence."""

    def test_removing_namespace_preserves_relative_order(self):
        """Removing namespace C does not reorder A/B results."""
        # Simulate results from namespaces A and B
        score_a1 = compute_ranking_score(2.0, 1.0, 0.95)
        score_a2 = compute_ranking_score(1.5, 1.0, 0.90)
        score_b1 = compute_ranking_score(1.8, 0.7, 0.85)

        # Order with just A and B
        order_ab = sorted(
            [("a1", score_a1), ("a2", score_a2), ("b1", score_b1)],
            key=lambda x: x[1],
            reverse=True,
        )

        # Adding namespace C results should not change A/B relative order
        score_c1 = compute_ranking_score(2.4, 0.55, 1.0)
        order_abc = sorted(
            [("a1", score_a1), ("a2", score_a2), ("b1", score_b1), ("c1", score_c1)],
            key=lambda x: x[1],
            reverse=True,
        )

        # Extract A/B items from both orderings
        ab_only_from_ab = [x[0] for x in order_ab if x[0].startswith(("a", "b"))]
        ab_only_from_abc = [x[0] for x in order_abc if x[0].startswith(("a", "b"))]
        assert ab_only_from_ab == ab_only_from_abc
