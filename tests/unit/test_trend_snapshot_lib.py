"""Unit tests for trend_snapshot_lib.compute_trend_metrics()."""

from typing import Any

from watercooler.trend_snapshot_lib import compute_trend_metrics


def _fact(name: str, invalid_at: str | None = None) -> dict[str, Any]:
    """Build a minimal fact dict."""
    return {
        "uuid": "test-uuid",
        "name": name,
        "fact": f"some fact about {name}",
        "invalid_at": invalid_at,
        "source_node_uuid": "src-uuid",
        "target_node_uuid": "tgt-uuid",
    }


# ---------------------------------------------------------------------------
# Basic counts
# ---------------------------------------------------------------------------

def test_compute_empty_facts():
    result = compute_trend_metrics([])
    assert result["sample_size"] == 0
    assert result["active_fact_count"] == 0
    assert result["superseded_fact_count"] == 0
    assert result["supersession_rate"] == 0.0
    assert result["trend_direction"] == "stable"
    assert result["top_volatile_topics"] == []
    assert result["top_stable_topics"] == []


def test_compute_all_active():
    facts = [_fact("uses") for _ in range(10)]
    result = compute_trend_metrics(facts)
    assert result["sample_size"] == 10
    assert result["active_fact_count"] == 10
    assert result["superseded_fact_count"] == 0
    assert result["supersession_rate"] == 0.0
    assert result["trend_direction"] == "stable"


def test_compute_all_superseded():
    facts = [_fact("decided", invalid_at="2026-01-01T00:00:00Z") for _ in range(10)]
    result = compute_trend_metrics(facts)
    assert result["sample_size"] == 10
    assert result["active_fact_count"] == 0
    assert result["superseded_fact_count"] == 10
    assert result["supersession_rate"] == 1.0
    assert result["trend_direction"] == "degrading"


def test_compute_mixed_improving():
    """7 active, 3 superseded → rate=0.3, direction=improving."""
    facts = (
        [_fact("uses") for _ in range(7)]
        + [_fact("depends_on", invalid_at="2026-01-01T00:00:00Z") for _ in range(3)]
    )
    result = compute_trend_metrics(facts)
    assert result["sample_size"] == 10
    assert result["active_fact_count"] == 7
    assert result["superseded_fact_count"] == 3
    assert abs(result["supersession_rate"] - 0.3) < 1e-9
    assert result["trend_direction"] == "improving"


def test_trend_direction_boundary_stable():
    """Exactly 0.2 rate → stable (< 0.2 is stable; 0.2 is improving)."""
    facts = (
        [_fact("uses") for _ in range(4)]
        + [_fact("uses", invalid_at="2026-01-01T00:00:00Z") for _ in range(1)]
    )
    result = compute_trend_metrics(facts)
    # rate = 0.2 exactly → NOT < 0.2 → improving
    assert result["trend_direction"] == "improving"


def test_trend_direction_boundary_degrading():
    """Exactly 0.5 rate → improving (> 0.5 is degrading; 0.5 is improving)."""
    facts = (
        [_fact("uses") for _ in range(5)]
        + [_fact("uses", invalid_at="2026-01-01T00:00:00Z") for _ in range(5)]
    )
    result = compute_trend_metrics(facts)
    # rate = 0.5 exactly → NOT > 0.5 → improving
    assert result["trend_direction"] == "improving"


# ---------------------------------------------------------------------------
# Topic grouping by edge name
# ---------------------------------------------------------------------------

def test_topic_grouping_by_edge_name():
    """Facts are grouped by edge `name` field, not by source/target UUIDs."""
    facts = [
        _fact("uses"),
        _fact("uses"),
        _fact("decided"),
        _fact("decided", invalid_at="2026-01-01T00:00:00Z"),
    ]
    result = compute_trend_metrics(facts)
    # "uses" has 2 active, 0 superseded → should appear in stable
    assert "uses" in result["top_stable_topics"]
    # "decided" has 1 active, 1 superseded → 50% superseded → NOT volatile (>50% required)
    assert "decided" not in result["top_volatile_topics"]


# ---------------------------------------------------------------------------
# Volatile topics
# ---------------------------------------------------------------------------

def test_volatile_topics_require_majority_superseded():
    """Volatile requires >50% of topic's facts superseded."""
    facts = [
        # "auth" — 1 active, 2 superseded → 67% superseded → volatile
        _fact("auth"),
        _fact("auth", invalid_at="2026-01-01T00:00:00Z"),
        _fact("auth", invalid_at="2026-01-02T00:00:00Z"),
        # "api" — 1 active, 1 superseded → 50% → NOT volatile
        _fact("api"),
        _fact("api", invalid_at="2026-01-01T00:00:00Z"),
    ]
    result = compute_trend_metrics(facts)
    assert "auth" in result["top_volatile_topics"]
    assert "api" not in result["top_volatile_topics"]


def test_volatile_topics_sorted_by_supersession_count_desc():
    """Volatile sorted by supersession count descending, then alpha."""
    facts = [
        # "zzz" — 1 superseded
        _fact("zzz", invalid_at="2026-01-01T00:00:00Z"),
        # "aaa" — 3 superseded (more, so ranks first even though alpha is later)
        _fact("aaa", invalid_at="2026-01-01T00:00:00Z"),
        _fact("aaa", invalid_at="2026-01-02T00:00:00Z"),
        _fact("aaa", invalid_at="2026-01-03T00:00:00Z"),
    ]
    result = compute_trend_metrics(facts)
    volatile = result["top_volatile_topics"]
    # Both are >50% superseded. "aaa" has count 3, "zzz" has count 1 → "aaa" first
    assert volatile.index("aaa") < volatile.index("zzz")


def test_volatile_topics_alpha_tiebreak():
    """Same supersession count → alphabetical."""
    facts = [
        _fact("zzz", invalid_at="2026-01-01T00:00:00Z"),
        _fact("zzz", invalid_at="2026-01-02T00:00:00Z"),
        _fact("aaa", invalid_at="2026-01-01T00:00:00Z"),
        _fact("aaa", invalid_at="2026-01-02T00:00:00Z"),
    ]
    result = compute_trend_metrics(facts)
    volatile = result["top_volatile_topics"]
    assert volatile.index("aaa") < volatile.index("zzz")


# ---------------------------------------------------------------------------
# Stable topics
# ---------------------------------------------------------------------------

def test_stable_topics_require_min_count():
    """Single-fact topics excluded from stable list."""
    facts = [
        _fact("lone_relation"),       # 1 active only → excluded
        _fact("pair_relation"),       # 2 active → included
        _fact("pair_relation"),
    ]
    result = compute_trend_metrics(facts)
    assert "lone_relation" not in result["top_stable_topics"]
    assert "pair_relation" in result["top_stable_topics"]


def test_stable_topics_require_all_active():
    """Topic with any superseded facts excluded from stable."""
    facts = [
        _fact("mixed"),
        _fact("mixed"),
        _fact("mixed", invalid_at="2026-01-01T00:00:00Z"),
        _fact("pure"),
        _fact("pure"),
    ]
    result = compute_trend_metrics(facts)
    assert "mixed" not in result["top_stable_topics"]
    assert "pure" in result["top_stable_topics"]


# ---------------------------------------------------------------------------
# Top-N cap
# ---------------------------------------------------------------------------

def test_top_n_capped_at_5_volatile():
    """At most 5 volatile topics returned."""
    facts = []
    for i in range(8):
        name = f"rel_{i:02d}"
        facts.append(_fact(name, invalid_at="2026-01-01T00:00:00Z"))
        facts.append(_fact(name, invalid_at="2026-01-02T00:00:00Z"))
    result = compute_trend_metrics(facts)
    assert len(result["top_volatile_topics"]) <= 5


def test_top_n_capped_at_5_stable():
    """At most 5 stable topics returned."""
    facts = []
    for i in range(8):
        name = f"rel_{i:02d}"
        facts.append(_fact(name))
        facts.append(_fact(name))
    result = compute_trend_metrics(facts)
    assert len(result["top_stable_topics"]) <= 5


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_missing_invalid_at_field_treated_as_active():
    """Facts without 'invalid_at' key (not just None) treated as active."""
    fact = {
        "uuid": "test-uuid",
        "name": "uses",
        "fact": "some fact",
        # invalid_at key is absent entirely
        "source_node_uuid": "src",
        "target_node_uuid": "tgt",
    }
    result = compute_trend_metrics([fact])
    assert result["active_fact_count"] == 1
    assert result["superseded_fact_count"] == 0
