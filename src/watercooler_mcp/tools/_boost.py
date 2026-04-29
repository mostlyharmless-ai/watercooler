"""Shared post-rank boost helper for Decision entries.

Both ``watercooler_search`` (``graph.py``) and ``watercooler_smart_query``
(``memory.py``) multiply scores for ``entry_type == "Decision"`` items and
re-sort. The two call sites differ only in where ``entry_type`` lives on
each item (``item["entry"]["entry_type"]`` vs
``item["metadata"]["entry_type"]``), so they share this helper via
``type_path``.

Only T1 items carry ``entry_type`` reliably; T2 facts/entities and T3
summaries do not, and are left untouched by this helper.
"""

from __future__ import annotations

import math
from typing import Any

# Hard cap on amplification. A caller that supplies a multiplier larger
# than this almost certainly means to disable lower-ranked items entirely,
# which silently overrides the backend's ranking signals. 100x covers
# every legitimate boost-by-type use case.
_BOOST_CEILING = 100.0


def sanitize_boost(boost: Any) -> float:
    """Coerce *boost* into a safe multiplier.

    Returns 1.0 (a no-op) for any value that would otherwise produce
    undefined ranking behaviour:

    - Non-numeric / non-coercible input.
    - ``NaN`` — breaks TimSort's total-ordering contract; sort result is
      undefined on mixed NaN/float comparisons.
    - ``inf`` — collapses all Decision scores to infinity and erases the
      backend's ranking signal.
    - Negative / zero — flips or zeroes scores while the payload still
      claims ``decisions_prioritized=True``.

    Positive finite values above :data:`_BOOST_CEILING` are clamped to
    the ceiling to keep the amplification bounded.
    """
    try:
        value = float(boost)
    except (TypeError, ValueError):
        return 1.0
    if math.isnan(value) or math.isinf(value) or value <= 0:
        return 1.0
    return min(value, _BOOST_CEILING)


def _get_by_path(item: Any, path: tuple[str, ...]) -> Any:
    cur: Any = item
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def boost_decision_items(
    items: Any,
    boost: float,
    *,
    score_key: str = "score",
    type_path: tuple[str, ...] = ("metadata", "entry_type"),
    decision_type: str = "Decision",
) -> int:
    """Multiply the score of Decision items in *items* and re-sort in place.

    Args:
        items: List of result/evidence dicts. Non-list input is a no-op.
        boost: Multiplier. Sanitized via :func:`sanitize_boost` so NaN,
            inf, negative, zero, or non-numeric values collapse to 1.0
            (no-op) instead of corrupting ranking.
        score_key: Key on each item holding the numeric score.
        type_path: Nested key path whose final value is the entry type.
        decision_type: The entry type value that triggers boosting.

    Returns:
        Number of items that were boosted. ``0`` when input is malformed,
        sanitized boost is 1.0, or no Decision items were found.
    """
    boost = sanitize_boost(boost)
    if boost == 1.0 or not isinstance(items, list):
        return 0

    boosted_count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        entry_type = _get_by_path(item, type_path)
        if entry_type != decision_type:
            continue
        score = item.get(score_key)
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            continue
        item[score_key] = float(score) * boost
        boosted_count += 1

    if boosted_count:
        items.sort(
            key=lambda it: (
                it.get(score_key, 0)
                if isinstance(it, dict) and isinstance(it.get(score_key), (int, float))
                and not isinstance(it.get(score_key), bool)
                else 0
            ),
            reverse=True,
        )

    return boosted_count
