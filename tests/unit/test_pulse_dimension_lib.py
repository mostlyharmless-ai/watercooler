"""Tests for pulse_dimension_lib.py — DimensionState encoding and dimension score formulas."""

from __future__ import annotations

import pytest

from watercooler.pulse_dimension_lib import (
    HYGIENE_DEBT_MAX_PENALTY,
    HYGIENE_DEBT_PER_FINDING,
    DimensionState,
    _SIGNAL_CONF_DEGRADED,
    _SIGNAL_CONF_FULL,
    _confidence,
    _compute_d4_signal_conf,
    _level_label,
    _score_constraint_pressure,
    _score_evidence_quality,
    _score_execution_momentum,
    _score_goal_clarity,
    _trend_label,
    _watch_flag,
    encode_dimension_scores,
)

# ---------------------------------------------------------------------------
# Snapshot builder helper
# ---------------------------------------------------------------------------


def _contrib(
    *,
    name: str = "jay",
    decision: int = 0,
    insight: int = 0,
    lesson: int = 0,
    reasoning: int = 0,
    risk: int = 0,
    stopgap: int = 0,
    problem: int = 0,
    procedure: int = 0,
    exploration: int = 0,
    open_loops: int = 0,
    # D4 delivery signals (Phase 2)
    pr_merged: int = 0,
    resolved_loop: int = 0,
    closed_loops: int = 0,
    opened_loops: int = 0,
    closure: int = 0,
) -> dict:
    return {
        "name": name,
        "session_count": 1,
        "observation_counts": {
            "decision": decision,
            "insight": insight,
            "lesson": lesson,
            "reasoning": reasoning,
            "risk": risk,
            "stopgap": stopgap,
            "problem": problem,
            "procedure": procedure,
            "exploration": exploration,
            "pr_merged": pr_merged,
            "resolved_loop": resolved_loop,
            "closed_loops": closed_loops,
            "opened_loops": opened_loops,
            "closure": closure,
        },
        "open_loops": [f"loop_{i}" for i in range(open_loops)],
    }


def _snapshot(
    *,
    sessions_in_window: int = 5,
    contributors: dict | None = None,
    risk_surface_tags: list[str] | None = None,
    analysis_age_days: float | None = 3.0,
) -> dict:
    if contributors is None:
        contributors = {"jay": _contrib()}
    return {
        "contributors": contributors,
        "corpus": {
            "sessions_in_window": sessions_in_window,
            "session_context_threads": len(contributors),
            "total_entries_scanned": sessions_in_window,
        },
        "risk_surface_tags": risk_surface_tags or [],
        "analysis": {
            "latest_report_age_days": analysis_age_days,
        },
    }


def _empty_snapshot() -> dict:
    return _snapshot(sessions_in_window=0, contributors={})


# ---------------------------------------------------------------------------
# 1. DimensionState.prose()
# ---------------------------------------------------------------------------


def test_prose_goal_clarity_all_labels():
    for label, expected in [
        ("low", "diffuse"),
        ("mixed", "forming"),
        ("high", "crisp"),
    ]:
        ds = DimensionState(
            level_score=0.5,
            level_label=label,
            baseline_score=0.5,
            trend_delta=0.0,
            trend_label="stable",
            confidence=0.7,
            watch=False,
            notes=[],
        )
        lp, _ = ds.prose("goal_clarity")
        assert lp == expected


def test_prose_constraint_pressure_all_labels():
    for label, expected in [("low", "loose"), ("mixed", "moderate"), ("high", "tight")]:
        ds = DimensionState(
            level_score=0.5,
            level_label=label,
            baseline_score=0.5,
            trend_delta=0.0,
            trend_label="stable",
            confidence=0.7,
            watch=False,
            notes=[],
        )
        lp, _ = ds.prose("constraint_pressure")
        assert lp == expected


def test_prose_evidence_quality_all_labels():
    for label, expected in [("low", "weak"), ("mixed", "mixed"), ("high", "strong")]:
        ds = DimensionState(
            level_score=0.5,
            level_label=label,
            baseline_score=0.5,
            trend_delta=0.0,
            trend_label="stable",
            confidence=0.7,
            watch=False,
            notes=[],
        )
        lp, _ = ds.prose("evidence_quality")
        assert lp == expected


def test_prose_execution_momentum_all_labels():
    for label, expected in [
        ("low", "stalled"),
        ("mixed", "probing"),
        ("high", "driving"),
    ]:
        ds = DimensionState(
            level_score=0.5,
            level_label=label,
            baseline_score=0.5,
            trend_delta=0.0,
            trend_label="stable",
            confidence=0.7,
            watch=False,
            notes=[],
        )
        lp, _ = ds.prose("execution_momentum")
        assert lp == expected


def test_prose_trend_labels():
    for trend, expected in [
        ("falling", "falling"),
        ("stable", "stable"),
        ("rising", "rising"),
    ]:
        ds = DimensionState(
            level_score=0.5,
            level_label="mixed",
            baseline_score=0.5,
            trend_delta=0.0,
            trend_label=trend,
            confidence=0.7,
            watch=False,
            notes=[],
        )
        _, tp = ds.prose("goal_clarity")
        assert tp == expected


def test_prose_unknown_dim_falls_back_to_label():
    ds = DimensionState(
        level_score=0.5,
        level_label="high",
        baseline_score=0.5,
        trend_delta=0.0,
        trend_label="stable",
        confidence=0.7,
        watch=False,
        notes=[],
    )
    lp, _ = ds.prose("unknown_dim")
    assert lp == "high"  # falls back to raw label


# ---------------------------------------------------------------------------
# 2. _level_label() boundary values
# ---------------------------------------------------------------------------


def test_level_label_low():
    assert _level_label(0.0) == "low"
    assert _level_label(0.32) == "low"


def test_level_label_boundary_0_33():
    assert _level_label(0.33) == "mixed"  # 0.33 is NOT < 0.33


def test_level_label_mixed():
    assert _level_label(0.5) == "mixed"
    assert _level_label(0.65) == "mixed"


def test_level_label_boundary_0_66():
    assert _level_label(0.66) == "high"  # 0.66 is NOT < 0.66


def test_level_label_high():
    assert _level_label(1.0) == "high"


# ---------------------------------------------------------------------------
# 3. _trend_label() boundary values
# ---------------------------------------------------------------------------


def test_trend_label_falling():
    assert _trend_label(-0.20) == "falling"
    assert _trend_label(-0.15) == "falling"  # at threshold


def test_trend_label_stable():
    assert _trend_label(-0.14) == "stable"
    assert _trend_label(0.0) == "stable"
    assert _trend_label(0.14) == "stable"


def test_trend_label_rising():
    assert _trend_label(0.15) == "rising"  # at threshold
    assert _trend_label(0.30) == "rising"


# ---------------------------------------------------------------------------
# 4. _watch_flag() — each condition independently
# ---------------------------------------------------------------------------


def test_watch_flag_abs_and_rel_delta_trigger():
    # abs(0.1) >= 0.05 AND abs(0.1)/max(0.2, 0.1) = 0.5 >= 0.3 → watch
    assert _watch_flag(level=0.3, baseline=0.2, delta=0.1, confidence=0.8) is True


def test_watch_flag_abs_delta_only_no_trigger():
    # abs(0.06) >= 0.05 but rel = 0.06/max(0.8, 0.1) = 0.075 < 0.3 → no watch from delta
    assert _watch_flag(level=0.86, baseline=0.8, delta=0.06, confidence=0.8) is False


def test_watch_flag_boundary_proximity_low():
    # level = 0.30, distance to 0.33 = 0.03 <= 0.05 → watch
    assert _watch_flag(level=0.30, baseline=0.30, delta=0.0, confidence=0.8) is True


def test_watch_flag_boundary_proximity_high():
    # level = 0.69, distance to 0.66 = 0.03 <= 0.05 → watch
    assert _watch_flag(level=0.69, baseline=0.69, delta=0.0, confidence=0.8) is True


def test_watch_flag_low_confidence_trigger():
    assert _watch_flag(level=0.5, baseline=0.5, delta=0.0, confidence=0.49) is True


def test_watch_flag_no_false_positive():
    # confidence >= 0.5, not near boundary, small delta
    assert _watch_flag(level=0.5, baseline=0.5, delta=0.01, confidence=0.8) is False


# ---------------------------------------------------------------------------
# 5. D1 — _score_goal_clarity()
# ---------------------------------------------------------------------------


def test_d1_crisp_high_decisions_no_loops():
    contribs = {"jay": _contrib(decision=5)}
    corpus = {"sessions_in_window": 5}
    level, _ = _score_goal_clarity(contribs, corpus, contribs, corpus)
    assert level > 0.9  # 5 decisions, 0 loops → nearly all direction → high


def test_d1_diffuse_no_decisions_many_loops():
    contribs = {"jay": _contrib(open_loops=5)}
    corpus = {"sessions_in_window": 5}
    level, _ = _score_goal_clarity(contribs, corpus, contribs, corpus)
    assert level < 0.1  # 0 decisions, 5 loops → all ambiguity → low


def test_d1_moderate_mixed():
    contribs = {"jay": _contrib(decision=2, open_loops=2)}
    corpus = {"sessions_in_window": 5}
    level, _ = _score_goal_clarity(contribs, corpus, contribs, corpus)
    assert 0.3 < level < 0.7


def test_d1_zero_session_fallback():
    contribs = {"jay": _contrib(decision=3)}
    corpus = {"sessions_in_window": 0}  # zero sessions → max(1) fallback
    level, _ = _score_goal_clarity(contribs, corpus, contribs, corpus)
    assert 0.0 <= level <= 1.0  # no crash


# ---------------------------------------------------------------------------
# 6. D2 — _score_constraint_pressure()
# ---------------------------------------------------------------------------


def test_d2_loose_no_risk_no_tags():
    contribs = {"jay": _contrib()}
    corpus = {"sessions_in_window": 5}
    level, _ = _score_constraint_pressure(
        contribs,
        corpus,
        contribs,
        corpus,
        short_risk_tags=[],
        long_risk_tags=[],
    )
    assert level < 0.1


def test_d2_tight_high_risk_obs():
    contribs = {"jay": _contrib(risk=10)}
    corpus = {"sessions_in_window": 5}
    level, _ = _score_constraint_pressure(
        contribs,
        corpus,
        contribs,
        corpus,
        short_risk_tags=[],
        long_risk_tags=[],
    )
    # rate = 10/5 = 2.0; 2.0/PRESSURE_HIGH_RATE = 1.0 → clamped to 1.0
    assert level == pytest.approx(1.0)


def test_d2_tag_adj_capped_at_020():
    contribs = {"jay": _contrib()}
    corpus = {"sessions_in_window": 5}
    # 10 risk tags → min(10/5, 0.20) = 0.20
    level, _ = _score_constraint_pressure(
        contribs,
        corpus,
        contribs,
        corpus,
        short_risk_tags=["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"],
        long_risk_tags=[],
    )
    assert level == pytest.approx(0.20)  # only tag_adj, no obs


# ---------------------------------------------------------------------------
# 7. D3 — _score_evidence_quality()
# ---------------------------------------------------------------------------


def test_d3_strong_high_grounding_fresh_analysis():
    contribs = {"jay": _contrib(insight=5, lesson=3, reasoning=2, decision=3)}
    corpus = {"sessions_in_window": 5}
    level, _, notes = _score_evidence_quality(
        contribs,
        corpus,
        contribs,
        corpus,
        analysis_age_days=3.0,
        supersession_rate=None,
    )
    assert level > 0.5


def test_d3_weak_no_grounding_stale_analysis():
    contribs = {"jay": _contrib()}
    corpus = {"sessions_in_window": 5}
    level, _, notes = _score_evidence_quality(
        contribs,
        corpus,
        contribs,
        corpus,
        analysis_age_days=30.0,
        supersession_rate=None,
    )
    assert level < 0.1
    assert any("sparse grounding" in n for n in notes)


def test_d3_supersession_hazard_lowers_level():
    contribs = {"jay": _contrib(insight=3, lesson=2, reasoning=2)}
    corpus = {"sessions_in_window": 5}
    level_no_super, _, _ = _score_evidence_quality(
        contribs,
        corpus,
        contribs,
        corpus,
        analysis_age_days=5.0,
        supersession_rate=None,
    )
    level_super, _, _ = _score_evidence_quality(
        contribs,
        corpus,
        contribs,
        corpus,
        analysis_age_days=5.0,
        supersession_rate=0.25,
    )
    assert level_super < level_no_super


def test_d3_zero_division_guard_no_decision():
    contribs = {"jay": _contrib(insight=2)}  # insight but no decisions
    corpus = {"sessions_in_window": 5}
    # Should not raise ZeroDivisionError
    level, baseline, notes = _score_evidence_quality(
        contribs,
        corpus,
        contribs,
        corpus,
        analysis_age_days=5.0,
        supersession_rate=None,
    )
    assert 0.0 <= level <= 1.0


# ---------------------------------------------------------------------------
# 8. D4 — _score_execution_momentum() and _compute_d4_signal_conf()
# ---------------------------------------------------------------------------


def test_d4_all_landed():
    """Only pr_merged → level = 1.0, no cold-start note."""
    contribs = {"jay": _contrib(pr_merged=5)}
    corpus = {"sessions_in_window": 5}
    level, _, notes = _score_execution_momentum(contribs, corpus, contribs, corpus)
    assert level == pytest.approx(1.0)
    assert not any("cold-start" in n for n in notes)
    assert not any("degraded mode" in n for n in notes)


def test_d4_all_created():
    """Only opened_loops → level = 0.0, no cold-start note."""
    contribs = {"jay": _contrib(opened_loops=3)}
    corpus = {"sessions_in_window": 5}
    level, _, notes = _score_execution_momentum(contribs, corpus, contribs, corpus)
    assert level == pytest.approx(0.0)
    assert not any("cold-start" in n for n in notes)


def test_d4_mixed_3_landed_1_created():
    """3 landed, 1 created → 3/4 = 0.75."""
    contribs = {"jay": _contrib(pr_merged=2, resolved_loop=1, opened_loops=1)}
    corpus = {"sessions_in_window": 5}
    level, _, _ = _score_execution_momentum(contribs, corpus, contribs, corpus)
    assert level == pytest.approx(0.75)


def test_d4_cold_start_zero_signals():
    """Zero D4-specific signals in both windows → level=0.5, cold-start note."""
    contribs = {"jay": _contrib(decision=3, insight=2)}
    corpus = {"sessions_in_window": 5}
    level, baseline, notes = _score_execution_momentum(
        contribs, corpus, contribs, corpus
    )
    assert level == pytest.approx(0.5)
    assert baseline == pytest.approx(0.5)
    assert any("cold-start" in n for n in notes)
    assert not any("degraded mode" in n for n in notes)


def test_d4_closure_excluded_from_landed():
    """closure kind is excluded from landed_count — triggers cold-start."""
    contribs = {"jay": _contrib(closure=5)}
    corpus = {"sessions_in_window": 5}
    level, _, notes = _score_execution_momentum(contribs, corpus, contribs, corpus)
    # closure is not in _D4_KINDS, so cold-start triggers (both windows have no D4 signals)
    assert level == pytest.approx(0.5)
    assert any("cold-start" in n for n in notes)


def test_d4_asymmetric_windows():
    """Short window has D4 signals, long window empty → no cold-start note, signal near degraded floor."""
    short_contribs = {"jay": _contrib(pr_merged=3)}
    long_contribs = {"jay": _contrib(decision=5)}  # no D4 kinds
    corpus = {"sessions_in_window": 5}
    level, baseline, notes = _score_execution_momentum(
        short_contribs, corpus, long_contribs, corpus
    )
    # short has signals → level = 1.0 (3 landed / 3 total)
    assert level == pytest.approx(1.0)
    # long has no D4 signals → baseline = 0.5 (cold-start per window)
    assert baseline == pytest.approx(0.5)
    # Not both windows empty → no cold-start note
    assert not any("cold-start" in n for n in notes)
    # signal_conf bottlenecked by long window (empty)
    signal_conf = _compute_d4_signal_conf(short_contribs, long_contribs)
    assert signal_conf == pytest.approx(_SIGNAL_CONF_DEGRADED)


def test_d4_no_degraded_mode_note_ever():
    """Verify 'degraded mode' string never appears in notes for any input."""
    for contribs in [
        {"jay": _contrib(pr_merged=5)},
        {"jay": _contrib(opened_loops=3)},
        {"jay": _contrib()},
        {"jay": _contrib(procedure=5, decision=5)},
    ]:
        corpus = {"sessions_in_window": 5}
        _, _, notes = _score_execution_momentum(contribs, corpus, contribs, corpus)
        assert not any("degraded mode" in n for n in notes)


def test_compute_d4_signal_conf_both_empty():
    """Both windows empty → _SIGNAL_CONF_DEGRADED."""
    assert _compute_d4_signal_conf({}, {}) == pytest.approx(_SIGNAL_CONF_DEGRADED)


def test_compute_d4_signal_conf_one_window_rich():
    """One window rich, other empty → min() bottleneck → near _SIGNAL_CONF_DEGRADED."""
    rich = {"jay": _contrib(pr_merged=10)}
    empty = {}
    conf = _compute_d4_signal_conf(rich, empty)
    assert conf == pytest.approx(_SIGNAL_CONF_DEGRADED)


def test_compute_d4_signal_conf_both_rich():
    """Both windows ≥ _D4_SIGNAL_FULL → _SIGNAL_CONF_FULL."""
    rich = {"jay": _contrib(pr_merged=5)}
    conf = _compute_d4_signal_conf(rich, rich)
    assert conf == pytest.approx(_SIGNAL_CONF_FULL)


def test_compute_d4_signal_conf_boundary_exactly_full():
    """Exactly _D4_SIGNAL_FULL (5) signals in both windows → _SIGNAL_CONF_FULL."""
    contribs = {"jay": _contrib(pr_merged=3, resolved_loop=2)}  # 5 total D4 signals
    conf = _compute_d4_signal_conf(contribs, contribs)
    assert conf == pytest.approx(_SIGNAL_CONF_FULL)


# ---------------------------------------------------------------------------
# 9. Confidence model
# ---------------------------------------------------------------------------


def test_confidence_low_sample_gives_low_confidence():
    # Empty snapshot → low total_obs → low sample_conf
    empty = {}
    conf = _confidence(empty, empty, signal_conf=1.0)
    # Only signal_conf contributes: 0.5*0 + 0.2*0 + 0.3*1.0 = 0.3
    assert conf == pytest.approx(0.3)


def test_confidence_single_contributor_lower_than_two():
    one = {"jay": _contrib(decision=5, insight=3)}
    two = {
        "jay": _contrib(decision=5, insight=3),
        "kai": _contrib(decision=5, insight=3),
    }
    conf_one = _confidence(one, one, signal_conf=1.0)
    conf_two = _confidence(two, two, signal_conf=1.0)
    assert conf_one < conf_two


def test_confidence_single_contributor_sparse_obs_enters_watch_band():
    # One contributor, very sparse observations → total_obs < 5 → low confidence → < 0.50
    sparse = {"jay": _contrib(decision=1)}  # only 1 observation in each window
    conf = _confidence(sparse, sparse, signal_conf=0.8)
    assert conf < 0.50


# ---------------------------------------------------------------------------
# 10. encode_dimension_scores() round-trip
# ---------------------------------------------------------------------------


def test_encode_identical_snapshots_trend_delta_zero():
    # Use analysis_age_days=14 so freshness_adj=0.0 (no level-only adjustment).
    # D3 applies freshness_adj to level only, so identical snapshots with adj != 0
    # would produce a non-zero delta — this test specifically verifies the zero-adj case.
    s = _snapshot(
        sessions_in_window=5,
        contributors={"jay": _contrib(decision=3, insight=2)},
        analysis_age_days=14.0,
    )
    result = encode_dimension_scores(s, s)
    for dim, ds in result.items():
        assert ds.trend_delta == pytest.approx(
            0.0, abs=1e-6
        ), f"{dim}: expected trend_delta=0"


def test_encode_level_greater_than_baseline_trend_rising():
    short = _snapshot(
        sessions_in_window=5,
        contributors={"jay": _contrib(decision=10)},
    )
    long = _snapshot(
        sessions_in_window=5,
        contributors={"jay": _contrib(decision=5, open_loops=5)},
    )
    result = encode_dimension_scores(short, long)
    assert result["goal_clarity"].trend_label == "rising"


def test_encode_returns_four_dimensions():
    s = _snapshot()
    result = encode_dimension_scores(s, s)
    assert set(result.keys()) == {
        "goal_clarity",
        "constraint_pressure",
        "evidence_quality",
        "execution_momentum",
    }


def test_encode_empty_snapshots_low_confidence():
    s = _empty_snapshot()
    result = encode_dimension_scores(s, s)
    for dim, ds in result.items():
        assert (
            ds.confidence < 0.40
        ), f"{dim}: expected confidence < 0.40 on empty snapshot"


def test_encode_execution_momentum_no_stalled_thread_count():
    # Verify encode_dimension_scores does NOT use stalled_thread_count
    # Uses D4 delivery signals (Phase 2) so momentum is non-cold-start
    short = _snapshot(contributors={"jay": _contrib(pr_merged=5, resolved_loop=3)})
    long = _snapshot(contributors={"jay": _contrib(pr_merged=5, resolved_loop=3)})
    # Add stalled_thread_count to snapshot — should be ignored
    short["stalled_threads"] = [{"topic": "x"}, {"topic": "y"}, {"topic": "z"}]
    long["stalled_threads"] = [{"topic": "x"}, {"topic": "y"}, {"topic": "z"}]
    result = encode_dimension_scores(short, long)
    # With all landed D4 signals, momentum should be high regardless of stalled threads
    assert result["execution_momentum"].level_score > 0.5


def test_encode_supersession_rate_zero_is_valid_signal():
    s = _snapshot(contributors={"jay": _contrib(insight=3, lesson=2)})
    result_none = encode_dimension_scores(s, s, supersession_rate=None)
    result_zero = encode_dimension_scores(s, s, supersession_rate=0.0)
    # supersession_rate=0.0 means zero superseded facts (good) — should not penalize
    assert (
        result_zero["evidence_quality"].level_score
        >= result_none["evidence_quality"].level_score
    )


def test_encode_all_fields_present_and_in_range():
    s = _snapshot(contributors={"jay": _contrib(decision=3, insight=2, risk=1)})
    result = encode_dimension_scores(s, s)
    for dim, ds in result.items():
        assert 0.0 <= ds.level_score <= 1.0, f"{dim}.level_score out of range"
        assert 0.0 <= ds.baseline_score <= 1.0, f"{dim}.baseline_score out of range"
        assert 0.0 <= ds.confidence <= 1.0, f"{dim}.confidence out of range"
        assert ds.level_label in {"low", "mixed", "high"}, f"{dim}.level_label invalid"
        assert ds.trend_label in {
            "falling",
            "stable",
            "rising",
        }, f"{dim}.trend_label invalid"
        assert isinstance(ds.watch, bool), f"{dim}.watch must be bool"
        assert isinstance(ds.notes, list), f"{dim}.notes must be list"


# ---------------------------------------------------------------------------
# P2.4 — Hygiene debt penalty tests (2.4.d – 2.4.h)
# ---------------------------------------------------------------------------


def test_zero_debt_no_penalty():
    """2.4.g — Zero hygiene debt leaves dimension scores unchanged."""
    s = _snapshot(
        contributors={
            "jay": _contrib(insight=3, lesson=2, pr_merged=2, resolved_loop=1)
        }
    )
    result_no_debt = encode_dimension_scores(s, s)
    result_zero_debt = encode_dimension_scores(
        s, s, hygiene_evidence_debt=0, hygiene_execution_debt=0
    )
    assert (
        result_no_debt["evidence_quality"].level_score
        == result_zero_debt["evidence_quality"].level_score
    )
    assert (
        result_no_debt["execution_momentum"].level_score
        == result_zero_debt["execution_momentum"].level_score
    )


def test_evidence_quality_penalty_applied():
    """2.4.d — hygiene_evidence_debt=5 reduces evidence_quality by 5 * HYGIENE_DEBT_PER_FINDING."""
    s = _snapshot(contributors={"jay": _contrib(insight=3, lesson=2)})
    debt = 5
    baseline = encode_dimension_scores(s, s)
    penalised = encode_dimension_scores(s, s, hygiene_evidence_debt=debt)
    expected_penalty = debt * HYGIENE_DEBT_PER_FINDING
    diff = (
        baseline["evidence_quality"].level_score
        - penalised["evidence_quality"].level_score
    )
    assert (
        abs(diff - expected_penalty) < 1e-6
    ), f"Expected penalty {expected_penalty}, got {diff}"


def test_execution_momentum_penalty_applied():
    """2.4.e — hygiene_execution_debt=10 reduces execution_momentum by min(0.20, 10*0.02)==0.20."""
    s = _snapshot(contributors={"jay": _contrib(pr_merged=4, resolved_loop=2)})
    debt = 10
    baseline = encode_dimension_scores(s, s)
    penalised = encode_dimension_scores(s, s, hygiene_execution_debt=debt)
    expected_penalty = min(debt * HYGIENE_DEBT_PER_FINDING, HYGIENE_DEBT_MAX_PENALTY)
    assert expected_penalty == 0.20
    diff = (
        baseline["execution_momentum"].level_score
        - penalised["execution_momentum"].level_score
    )
    assert (
        abs(diff - expected_penalty) < 1e-6
    ), f"Expected penalty {expected_penalty}, got {diff}"


def test_penalty_clamped_at_max():
    """2.4.f — hygiene_evidence_debt=100 penalty is clamped at HYGIENE_DEBT_MAX_PENALTY."""
    s = _snapshot(contributors={"jay": _contrib(insight=5, lesson=5)})
    debt = 100
    baseline = encode_dimension_scores(s, s)
    penalised = encode_dimension_scores(s, s, hygiene_evidence_debt=debt)
    diff = (
        baseline["evidence_quality"].level_score
        - penalised["evidence_quality"].level_score
    )
    # Penalty is clamped so score cannot drop below max_penalty from baseline
    assert (
        diff <= HYGIENE_DEBT_MAX_PENALTY + 1e-6
    ), f"Penalty {diff} exceeded cap {HYGIENE_DEBT_MAX_PENALTY}"


def test_hygiene_note_appended_when_debt_nonzero():
    """hygiene_debt note is appended to dimension notes when debt > 0."""
    s = _snapshot(contributors={"jay": _contrib(insight=3, lesson=2)})
    result = encode_dimension_scores(
        s, s, hygiene_evidence_debt=3, hygiene_execution_debt=2
    )
    ev_notes = result["evidence_quality"].notes
    em_notes = result["execution_momentum"].notes
    assert any(
        "hygiene_debt:3" in n for n in ev_notes
    ), f"Evidence notes missing hygiene note: {ev_notes}"
    assert any(
        "hygiene_debt:2" in n for n in em_notes
    ), f"Execution notes missing hygiene note: {em_notes}"


def test_hygiene_note_absent_when_debt_zero():
    """hygiene_debt note is absent from notes when debt is 0."""
    s = _snapshot(contributors={"jay": _contrib(insight=3, lesson=2)})
    result = encode_dimension_scores(
        s, s, hygiene_evidence_debt=0, hygiene_execution_debt=0
    )
    for dim, ds in result.items():
        assert not any(
            "hygiene_debt" in n for n in ds.notes
        ), f"{dim} had unexpected hygiene note"


def test_layering_lib_has_no_mcp_imports():
    """2.4.h — pulse_dimension_lib must not import any watercooler_mcp symbols."""
    import sys

    # Ensure the module is loaded
    import watercooler.pulse_dimension_lib  # noqa: F401

    mod = sys.modules["watercooler.pulse_dimension_lib"]
    source_file = mod.__file__ or ""
    with open(source_file) as f:
        source = f.read()
    assert (
        "watercooler_mcp" not in source
    ), "pulse_dimension_lib.py must not import watercooler_mcp symbols"
