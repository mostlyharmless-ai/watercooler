"""Continuous dimension scoring for Project Pulse.

Replaces pulse_ternary_lib.py with a two-window (level + baseline) continuous model.
Each of the four pulse dimensions produces a DimensionState with:
  - level_score:    0.0–1.0 short-window estimate
  - baseline_score: 0.0–1.0 long-window structural average
  - trend_delta:    level_score - baseline_score

stdlib-only — no fastmcp, no graphiti imports. Ships via Copybara.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Calibration constants — edit to recalibrate without touching logic
# ---------------------------------------------------------------------------

PRESSURE_HIGH_RATE: float = 2.0
EVIDENCE_STRONG_RATE: float = 1.5
MIN_GROUNDING_ABS: int = 4
TREND_FALLING_THRESHOLD: float = -0.15
TREND_RISING_THRESHOLD: float = 0.15

# Confidence model weights
_SAMPLE_CONF_WEIGHT: float = 0.5
_CONTRIB_CONF_WEIGHT: float = 0.2
_SIGNAL_CONF_WEIGHT: float = 0.3

# Sample size targets for confidence
_SAMPLE_OBS_FULL_SHORT: int = 8  # Ws observations for full sample_conf
_SAMPLE_OBS_FULL_LONG: int = 25  # Wl observations for full sample_conf
_CONTRIB_CONF_FULL: int = 2  # unique contributors for full contributor_conf

# Watch flag thresholds
_WATCH_ABS_DELTA: float = 0.05
_WATCH_REL_DELTA: float = 0.30
_WATCH_BASELINE_FLOOR: float = 0.10
_WATCH_BOUNDARY_MARGIN: float = 0.05

# Signal confidence
_SIGNAL_CONF_FULL: float = 1.0
_SIGNAL_CONF_DEGRADED: float = 0.8  # D4 cold-start floor (retained from Phase 1)
_SIGNAL_CONF_MISSING: float = 0.6

# D4 execution momentum — Phase 2 constants
_D4_SIGNAL_FULL = (
    5  # D4-specific events needed for full signal confidence in each window
)

# Hygiene debt penalty constants (P2.4)
HYGIENE_DEBT_PER_FINDING: float = 0.02  # 2% absolute drop per unacknowledged finding
HYGIENE_DEBT_MAX_PENALTY: float = (
    0.20  # cap: 20% absolute drop regardless of finding count
)
# closure intentionally excluded — thread lifecycle is not delivery evidence.
# A session with only closure observations still triggers cold-start (correct).
_D4_KINDS: tuple[str, ...] = (
    "pr_merged",
    "resolved_loop",
    "closed_loops",
    "opened_loops",
)

# Prose translation tables
_DIM_LEVEL_PROSE: dict[str, dict[str, str]] = {
    "goal_clarity": {"low": "diffuse", "mixed": "forming", "high": "crisp"},
    "constraint_pressure": {"low": "loose", "mixed": "moderate", "high": "tight"},
    "evidence_quality": {"low": "weak", "mixed": "mixed", "high": "strong"},
    "execution_momentum": {"low": "stalled", "mixed": "probing", "high": "driving"},
}

# ---------------------------------------------------------------------------
# DimensionState
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DimensionState:
    """Continuous state for one pulse dimension."""

    level_score: float  # 0.0–1.0, short-window
    level_label: str  # "low" | "mixed" | "high"
    baseline_score: float  # 0.0–1.0, long-window
    trend_delta: float  # level_score - baseline_score
    trend_label: str  # "falling" | "stable" | "rising"
    confidence: float  # 0.0–1.0
    watch: bool  # watch flag
    notes: list[str]  # caveat strings

    def prose(self, dim: str) -> tuple[str, str]:
        """Return (level_prose, trend_prose) for rendered output."""
        lp = _DIM_LEVEL_PROSE.get(dim, {}).get(self.level_label, self.level_label)
        return lp, self.trend_label


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _clamp01(x: float) -> float:
    return min(max(x, 0.0), 1.0)


def _level_label(score: float) -> str:
    if score < 0.33:
        return "low"
    if score < 0.66:
        return "mixed"
    return "high"


def _trend_label(delta: float) -> str:
    if delta <= TREND_FALLING_THRESHOLD:
        return "falling"
    if delta >= TREND_RISING_THRESHOLD:
        return "rising"
    return "stable"


def _sum_obs(contributors: dict[str, Any], kind: str) -> int:
    """Sum observation_counts[kind] across all contributors."""
    total = 0
    for c in contributors.values():
        obs = c.get("observation_counts") or {}
        if isinstance(obs, dict):
            total += int(obs.get(kind, 0))
    return total


def _total_obs(contributors: dict[str, Any]) -> int:
    """Sum all observation_counts values across all contributors."""
    total = 0
    for c in contributors.values():
        obs = c.get("observation_counts") or {}
        if isinstance(obs, dict):
            total += sum(int(v) for v in obs.values() if isinstance(v, (int, float)))
    return total


def _unique_contributors(contributors: dict[str, Any]) -> int:
    """Count contributors with at least one observation."""
    count = 0
    for c in contributors.values():
        obs = c.get("observation_counts") or {}
        if isinstance(obs, dict) and any(
            int(v) > 0 for v in obs.values() if isinstance(v, (int, float))
        ):
            count += 1
    return count


def _open_loop_count(contributors: dict[str, Any]) -> int:
    """Sum open_loops list lengths across contributors."""
    total = 0
    for c in contributors.values():
        loops = c.get("open_loops") or []
        total += len(loops)
    return total


def _watch_flag(level: float, baseline: float, delta: float, confidence: float) -> bool:
    """Return True if any watch condition fires."""
    abs_delta = abs(delta)
    rel_delta = abs_delta / max(baseline, _WATCH_BASELINE_FLOOR)
    if abs_delta >= _WATCH_ABS_DELTA and rel_delta >= _WATCH_REL_DELTA:
        return True
    for boundary in (0.33, 0.66):
        if abs(level - boundary) <= _WATCH_BOUNDARY_MARGIN:
            return True
    if confidence < 0.50:
        return True
    return False


def _confidence(
    short_contributors: dict[str, Any],
    long_contributors: dict[str, Any],
    signal_conf: float,
) -> float:
    """Compute confidence score from sample size, contributor count, and signal availability."""
    total_obs_short = _total_obs(short_contributors)
    total_obs_long = _total_obs(long_contributors)
    sample_conf = 0.5 * _clamp01(
        total_obs_short / _SAMPLE_OBS_FULL_SHORT
    ) + 0.5 * _clamp01(total_obs_long / _SAMPLE_OBS_FULL_LONG)
    contrib_conf = _clamp01(
        _unique_contributors(short_contributors) / _CONTRIB_CONF_FULL
    )
    return (
        _SAMPLE_CONF_WEIGHT * sample_conf
        + _CONTRIB_CONF_WEIGHT * contrib_conf
        + _SIGNAL_CONF_WEIGHT * signal_conf
    )


# ---------------------------------------------------------------------------
# Per-dimension score functions (private)
# ---------------------------------------------------------------------------


def _score_goal_clarity(
    short_contribs: dict[str, Any],
    short_corpus: dict[str, Any],
    long_contribs: dict[str, Any],
    long_corpus: dict[str, Any],
) -> tuple[float, float]:
    """Returns (level_score, baseline_score)."""

    def _score_w(contribs: dict[str, Any], corpus: dict[str, Any]) -> float:
        decisions = _sum_obs(contribs, "decision")
        loops = _open_loop_count(contribs)
        sessions = max(corpus.get("sessions_in_window", 1), 1)
        dr = decisions / sessions
        ar = loops / sessions
        return _clamp01(dr / max(dr + ar, 1e-6))

    return _score_w(short_contribs, short_corpus), _score_w(long_contribs, long_corpus)


def _score_constraint_pressure(
    short_contribs: dict[str, Any],
    short_corpus: dict[str, Any],
    long_contribs: dict[str, Any],
    long_corpus: dict[str, Any],
    *,
    short_risk_tags: list[str],
    long_risk_tags: list[str],
) -> tuple[float, float]:
    """Returns (level_score, baseline_score)."""

    def _score_w(
        contribs: dict[str, Any], corpus: dict[str, Any], risk_tags: list[str]
    ) -> float:
        pressure = _sum_obs(contribs, "risk") + _sum_obs(contribs, "stopgap")
        sessions = max(corpus.get("sessions_in_window", 1), 1)
        tag_adj = min(len(risk_tags) / 5.0, 0.20)
        return _clamp01((pressure / sessions) / PRESSURE_HIGH_RATE + tag_adj)

    return (
        _score_w(short_contribs, short_corpus, short_risk_tags),
        _score_w(long_contribs, long_corpus, long_risk_tags),
    )


def _score_evidence_quality(
    short_contribs: dict[str, Any],
    short_corpus: dict[str, Any],
    long_contribs: dict[str, Any],
    long_corpus: dict[str, Any],
    *,
    analysis_age_days: float | None,
    supersession_rate: float | None,
    hygiene_evidence_debt: int = 0,
) -> tuple[float, float, list[str]]:
    """Returns (level_score, baseline_score, notes)."""

    def _base(contribs: dict[str, Any], corpus: dict[str, Any]) -> tuple[float, int]:
        grounding = (
            _sum_obs(contribs, "insight")
            + _sum_obs(contribs, "lesson")
            + _sum_obs(contribs, "reasoning")
        )
        sessions = max(corpus.get("sessions_in_window", 1), 1)
        base = _clamp01((grounding / sessions) / EVIDENCE_STRONG_RATE)
        # Grounding ratio cap: small bonus (max 0.05) when well-grounded relative to decisions.
        # Clamped inside _base so caller adjustments (freshness_adj, hazard_adj) are not masked.
        ratio_cap = 0.0
        decisions = _sum_obs(contribs, "decision")
        if decisions > 0 and grounding >= MIN_GROUNDING_ABS:
            ratio = grounding / decisions
            ratio_cap = 0.05 if ratio >= 1.5 else 0.0
        return _clamp01(base + ratio_cap), grounding

    base_s, grounding_s = _base(short_contribs, short_corpus)
    base_l, _ = _base(long_contribs, long_corpus)

    # Freshness adjustment (applies to level only — current state)
    if analysis_age_days is None:
        freshness_adj = -0.10
    elif analysis_age_days <= 7:
        freshness_adj = +0.10
    elif analysis_age_days <= 21:
        freshness_adj = 0.0
    else:
        freshness_adj = -0.10

    # Supersession hazard (T2 enrichment — applies to level only)
    hazard_adj = 0.0
    notes: list[str] = []
    if supersession_rate is not None:
        hazard_adj = -min(supersession_rate, 0.25)

    hygiene_penalty = -min(
        hygiene_evidence_debt * HYGIENE_DEBT_PER_FINDING,
        HYGIENE_DEBT_MAX_PENALTY,
    )
    if hygiene_penalty < 0:
        notes.append(f"hygiene_debt:{hygiene_evidence_debt}")

    if grounding_s < MIN_GROUNDING_ABS:
        notes.append("Evidence trend based on sparse grounding events")

    level = _clamp01(base_s + freshness_adj + hazard_adj + hygiene_penalty)
    baseline = _clamp01(base_l)
    return level, baseline, notes


def _score_execution_momentum(
    short_contribs: dict[str, Any],
    short_corpus: dict[str, Any],
    long_contribs: dict[str, Any],
    long_corpus: dict[str, Any],
    *,
    hygiene_execution_debt: int = 0,
) -> tuple[float, float, list[str]]:
    """Returns (level_score, baseline_score, notes). Phase 2 landed/created formula."""

    def _score_w(contribs: dict[str, Any]) -> float:
        landed = (
            _sum_obs(contribs, "pr_merged")
            + _sum_obs(contribs, "resolved_loop")
            + _sum_obs(contribs, "closed_loops")
        )
        created = _sum_obs(contribs, "opened_loops")
        if landed == 0 and created == 0:
            return 0.5  # epistemic absence: no D4-specific evidence yet, not negative momentum
        return _clamp01(landed / max(landed + created, 1))

    # Cold-start note when no D4-specific signals in either window
    short_d4 = sum(_sum_obs(short_contribs, k) for k in _D4_KINDS)
    long_d4 = sum(_sum_obs(long_contribs, k) for k in _D4_KINDS)
    notes: list[str] = []
    if short_d4 == 0 and long_d4 == 0:
        notes.append("Momentum cold-start: no D4-specific signals yet")

    hygiene_penalty = -min(
        hygiene_execution_debt * HYGIENE_DEBT_PER_FINDING,
        HYGIENE_DEBT_MAX_PENALTY,
    )
    if hygiene_penalty < 0:
        notes.append(f"hygiene_debt:{hygiene_execution_debt}")

    level = _clamp01(_score_w(short_contribs) + hygiene_penalty)
    baseline = _score_w(long_contribs)
    return level, baseline, notes


def _compute_d4_signal_conf(
    short_contribs: dict[str, Any],
    long_contribs: dict[str, Any],
) -> float:
    """D4 signal confidence, bottlenecked by the weaker window.

    Both windows must accumulate D4-specific evidence before signal confidence
    reaches _SIGNAL_CONF_FULL. Stays near _SIGNAL_CONF_DEGRADED when either window
    has no D4 signals.
    """
    short_signals = sum(_sum_obs(short_contribs, k) for k in _D4_KINDS)
    long_signals = sum(_sum_obs(long_contribs, k) for k in _D4_KINDS)
    ramp = min(
        _clamp01(short_signals / _D4_SIGNAL_FULL),
        _clamp01(long_signals / _D4_SIGNAL_FULL),
    )
    return _SIGNAL_CONF_DEGRADED + ramp * (_SIGNAL_CONF_FULL - _SIGNAL_CONF_DEGRADED)


# ---------------------------------------------------------------------------
# Public encoder
# ---------------------------------------------------------------------------


def encode_dimension_scores(
    short_snapshot: dict[str, Any],
    long_snapshot: dict[str, Any],
    supersession_rate: float | None = None,
    *,
    hygiene_evidence_debt: int = 0,
    hygiene_execution_debt: int = 0,
) -> dict[str, DimensionState]:
    """Encode short + long window snapshots into four DimensionState objects.

    Args:
      short_snapshot: PulseSnapshot v1.0 dict built with short_window_days lookback.
      long_snapshot:  PulseSnapshot v1.0 dict built with long_window_days lookback.
      supersession_rate: From TrendSnapshot checkpoint. None = not available.
                         0.0 = valid signal (zero superseded facts).
      hygiene_evidence_debt: Unacknowledged thread_auditor findings in evidence
                             categories. 0 = auditor disabled or no findings (no penalty).
      hygiene_execution_debt: Unacknowledged thread_auditor findings in execution
                              categories. 0 = auditor disabled or no findings (no penalty).

    Returns:
      Dict with keys: goal_clarity, constraint_pressure, evidence_quality,
      execution_momentum. Each value is a DimensionState.
    """
    sc = short_snapshot.get("contributors", {})
    lc = long_snapshot.get("contributors", {})
    s_corpus = short_snapshot.get("corpus", {})
    l_corpus = long_snapshot.get("corpus", {})
    s_tags = short_snapshot.get("risk_surface_tags", [])
    l_tags = long_snapshot.get("risk_surface_tags", [])
    analysis = short_snapshot.get("analysis", {})
    age_days = analysis.get("latest_report_age_days")

    def _make(
        dim: str,
        level: float,
        baseline: float,
        signal_conf: float = _SIGNAL_CONF_FULL,
        notes: list[str] | None = None,
    ) -> DimensionState:
        delta = level - baseline
        conf = _confidence(sc, lc, signal_conf)
        watch = _watch_flag(level, baseline, delta, conf)
        return DimensionState(
            level_score=round(level, 4),
            level_label=_level_label(level),
            baseline_score=round(baseline, 4),
            trend_delta=round(delta, 4),
            trend_label=_trend_label(delta),
            confidence=round(conf, 4),
            watch=watch,
            notes=notes or [],
        )

    d1_l, d1_b = _score_goal_clarity(sc, s_corpus, lc, l_corpus)
    d2_l, d2_b = _score_constraint_pressure(
        sc,
        s_corpus,
        lc,
        l_corpus,
        short_risk_tags=s_tags,
        long_risk_tags=l_tags,
    )
    d3_l, d3_b, d3_notes = _score_evidence_quality(
        sc,
        s_corpus,
        lc,
        l_corpus,
        analysis_age_days=age_days,
        supersession_rate=supersession_rate,
        hygiene_evidence_debt=hygiene_evidence_debt,
    )
    d4_l, d4_b, d4_notes = _score_execution_momentum(
        sc,
        s_corpus,
        lc,
        l_corpus,
        hygiene_execution_debt=hygiene_execution_debt,
    )
    d4_signal_conf = _compute_d4_signal_conf(sc, lc)

    return {
        "goal_clarity": _make("goal_clarity", d1_l, d1_b),
        "constraint_pressure": _make("constraint_pressure", d2_l, d2_b),
        "evidence_quality": _make("evidence_quality", d3_l, d3_b, notes=d3_notes),
        "execution_momentum": _make(
            "execution_momentum",
            d4_l,
            d4_b,
            signal_conf=d4_signal_conf,
            notes=d4_notes,
        ),
    }
