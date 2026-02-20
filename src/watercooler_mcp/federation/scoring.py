"""Fixed-anchor normalization, namespace weight resolution, and recency decay.

Style: modern Python 3.10+ syntax (list[str], str | None) throughout.
Config schema additions use typing.List/Dict for consistency with config_schema.py.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from watercooler.config_schema import FederationScoringConfig

__all__ = [
    "KEYWORD_SCORE_MIN",
    "KEYWORD_SCORE_MAX",
    "normalize_keyword_score",
    "resolve_namespace_weight",
    "compute_recency_decay",
    "compute_ranking_score",
]

# Named constants — source: baseline_graph/search.py:620-629
# Max = 1.0 (base) + 0.5 (title) + 0.3 (body) + 6*0.1 (6 matched_fields) = 2.4
KEYWORD_SCORE_MIN = 1.0
KEYWORD_SCORE_MAX = 2.4
_SCORE_RANGE = KEYWORD_SCORE_MAX - KEYWORD_SCORE_MIN  # 1.4

_LN2 = math.log(2)  # ~0.6931 — used in RecencyDecay


def normalize_keyword_score(raw_score: float) -> float:
    """Fixed-anchor normalization for keyword search scores.

    Formula: clamp((score - KEYWORD_SCORE_MIN) / _SCORE_RANGE, 0.0, 1.0)
    Calibrated to actual keyword range [1.0, 2.4] from
    baseline_graph/search.py:620-629.

    NOTE: If searchable fields change in search.py, update KEYWORD_SCORE_MAX.
    """
    return max(0.0, min(1.0, (raw_score - KEYWORD_SCORE_MIN) / _SCORE_RANGE))


def resolve_namespace_weight(
    namespace: str,
    primary_namespace: str,
    scoring_config: FederationScoringConfig,
) -> float:
    """Resolve namespace weight tier for a namespace.

    Returns:
        local_weight (1.0) if namespace == primary
        wide_weight (0.55) otherwise
    """
    if namespace == primary_namespace:
        return scoring_config.local_weight
    return scoring_config.wide_weight


def compute_recency_decay(
    entry_timestamp: datetime,
    now: datetime,
    floor: float = 0.7,
    half_life_days: float = 60.0,
) -> float:
    """Exponential recency decay with configurable floor.

    Formula: floor + (1 - floor) * exp(-_LN2 * age_days / half_life)

    Args:
        entry_timestamp: Entry creation time (should be timezone-aware UTC).
        now: Query execution time. Compute ONCE per query, pass to all calls.
        floor: Minimum decay value (entries never decay below this).
        half_life_days: Number of days for decay to reach midpoint.

    Returns:
        Decay factor in [floor, 1.0].
    """
    # Handle naive datetimes by assuming UTC
    if entry_timestamp.tzinfo is None:
        entry_timestamp = entry_timestamp.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    age_seconds = max(0.0, (now - entry_timestamp).total_seconds())
    age_days = age_seconds / 86400.0

    decay = math.exp(-_LN2 * age_days / half_life_days)
    return floor + (1.0 - floor) * decay


def compute_ranking_score(
    raw_score: float,
    namespace_weight: float,
    recency_decay: float,
) -> float:
    """Multiplicative composition: normalize(raw_score) * NW * RecencyDecay."""
    return normalize_keyword_score(raw_score) * namespace_weight * recency_decay
