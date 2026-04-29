"""Tests for the open-core decision-pipeline stance signals.

Covers:
- ``extract_decision_stance_signals`` (helper used by ``DecisionStanceDaemon``)
- New ``StanceSignals`` decision fields and their per-role mapping rows
- Signature backward-compat: legacy fixtures unchanged byte-for-byte
- ``_coarsen_crossings`` bucket entries gated by ``signal_filter``

The signal field names live in ``StanceSignals``; the threshold constants and
the rejection ratio helper live alongside the existing pulse machinery in
``watercooler.pulse_stance_lib``.
"""

from __future__ import annotations

from watercooler.pulse_stance_lib import (
    StanceSignals,
    _DEC_CANDIDATE_BACKLOG_HARD,
    _DEC_CANDIDATE_BACKLOG_SOFT,
    _DEC_RATE_LIMITED_SOFT,
    _DEC_REJECTION_RATIO_HARD,
    _DEC_REJECTION_RATIO_SOFT,
    _coarsen_crossings,
    _decision_rejection_ratio,
    extract_decision_stance_signals,
    extract_stance_signals,
    pulse_to_stance,
)

# ---------------------------------------------------------------------------
# extract_decision_stance_signals
# ---------------------------------------------------------------------------


def test_extract_decision_signals_empty() -> None:
    """No findings, no decisions → all decision counts at 0."""
    signals = extract_decision_stance_signals()
    assert signals.pulse_available is False
    assert signals.decision_candidate_high_count == 0
    assert signals.decision_extraction_success_count == 0
    assert signals.decision_extraction_rejected_count == 0
    assert signals.decision_extraction_rate_limited_count == 0
    assert signals.decisions_recorded_recent_count == 0


def test_extract_decision_signals_counts_by_category() -> None:
    """Counts are bucketed by extractor finding category."""
    extractor_findings = [
        {"category": "extraction_success"},
        {"category": "extraction_success"},
        {"category": "extraction_rejected"},
        {"category": "extraction_failed"},
        {"category": "extraction_parse_failure"},
        {"category": "extraction_rate_limited"},
        {"category": "extraction_cap_reached"},
        {"category": "extraction_push_failed"},  # unknown — ignored
    ]
    detector_findings = [
        {"category": "decision_candidate", "details": {"tier": "High"}},
        {"category": "decision_candidate", "details": {"tier": "High"}},
        {"category": "decision_candidate", "details": {"tier": "Medium"}},
        {"category": "decision_candidate", "details": {"tier": "Low"}},
        {"category": "decision_candidate", "details": {}},  # missing tier
    ]

    signals = extract_decision_stance_signals(
        detector_findings=detector_findings,
        extractor_findings=extractor_findings,
        recent_decisions_count=4,
    )
    assert signals.decision_candidate_high_count == 2
    assert signals.decision_extraction_success_count == 2
    assert signals.decision_extraction_rejected_count == 3  # rejected + failed + parse
    assert signals.decision_extraction_rate_limited_count == 2  # rate_limited + cap
    assert signals.decisions_recorded_recent_count == 4


def test_extract_decision_signals_negative_recent_clamped() -> None:
    """Bad input for ``recent_decisions_count`` is clamped to 0."""
    s = extract_decision_stance_signals(recent_decisions_count=-3)
    assert s.decisions_recorded_recent_count == 0


# ---------------------------------------------------------------------------
# Rejection ratio helper
# ---------------------------------------------------------------------------


def test_rejection_ratio_zero_when_no_traffic() -> None:
    assert _decision_rejection_ratio(StanceSignals()) == 0.0


def test_rejection_ratio_arithmetic() -> None:
    s = StanceSignals(
        decision_extraction_success_count=1,
        decision_extraction_rejected_count=4,
    )
    assert _decision_rejection_ratio(s) == 0.8


# ---------------------------------------------------------------------------
# Per-role mapping — planner
# ---------------------------------------------------------------------------


def test_planner_clear_when_no_decision_signals() -> None:
    s = StanceSignals(
        pulse_available=False,
        decision_extraction_success_count=10,
        decision_extraction_rejected_count=0,
    )
    a = pulse_to_stance("planner", s)
    assert a.level == 0
    assert "decision_extraction_rejected_count" not in a.triggered_signals
    assert "decision_extraction_rate_limited_count" not in a.triggered_signals


def test_planner_hard_rejection_ratio_triggers_l1() -> None:
    s = StanceSignals(
        pulse_available=False,
        decision_extraction_success_count=1,
        decision_extraction_rejected_count=9,  # ratio = 0.9
    )
    a = pulse_to_stance("planner", s)
    assert a.level == 1
    assert "decision_extraction_rejected_count" in a.triggered_signals
    assert a.stance.decision_caution >= 0.7
    assert a.stance.retrieval_pressure >= 0.5


def test_planner_below_hard_does_not_trigger_planner_row() -> None:
    """Planner only fires on HARD ratio (>=0.8). SOFT alone shouldn't trigger planner."""
    s = StanceSignals(
        pulse_available=False,
        decision_extraction_success_count=4,
        decision_extraction_rejected_count=4,  # ratio = 0.5 == SOFT but < HARD
    )
    a = pulse_to_stance("planner", s)
    assert a.level == 0
    assert "decision_extraction_rejected_count" not in a.triggered_signals


def test_planner_rate_limited_triggers_l1() -> None:
    s = StanceSignals(
        pulse_available=False,
        decision_extraction_rate_limited_count=2,
    )
    a = pulse_to_stance("planner", s)
    assert a.level == 1
    assert "decision_extraction_rate_limited_count" in a.triggered_signals
    assert a.stance.decision_caution >= 0.4


def test_planner_rejection_action_added_when_triggered() -> None:
    s = StanceSignals(
        pulse_available=False,
        decision_extraction_success_count=1,
        decision_extraction_rejected_count=9,
    )
    a = pulse_to_stance("planner", s)
    rejection_actions = [
        x
        for x in a.actions
        if x.tool == "watercooler_daemon_findings"
        and x.arguments.get("category") == "extraction_rejected"
    ]
    assert len(rejection_actions) == 1


# ---------------------------------------------------------------------------
# Per-role mapping — critic
# ---------------------------------------------------------------------------


def test_critic_backlog_hard_triggers_l2() -> None:
    s = StanceSignals(
        pulse_available=False,
        decision_candidate_high_count=_DEC_CANDIDATE_BACKLOG_HARD,
    )
    a = pulse_to_stance("critic", s)
    assert a.level == 2
    assert "decision_candidate_high_count" in a.triggered_signals
    assert a.stance.critique_intensity >= 0.7
    assert a.stance.provenance_requirement >= 0.6


def test_critic_backlog_soft_triggers_l1() -> None:
    s = StanceSignals(
        pulse_available=False,
        decision_candidate_high_count=_DEC_CANDIDATE_BACKLOG_SOFT,
    )
    a = pulse_to_stance("critic", s)
    assert a.level == 1
    assert "decision_candidate_high_count" in a.triggered_signals
    assert a.stance.critique_intensity >= 0.4


def test_critic_rejection_ratio_soft_triggers_l1() -> None:
    s = StanceSignals(
        pulse_available=False,
        decision_extraction_success_count=1,
        decision_extraction_rejected_count=1,  # 0.5 == SOFT
    )
    a = pulse_to_stance("critic", s)
    assert a.level == 1
    assert "decision_extraction_rejected_count" in a.triggered_signals
    assert a.stance.provenance_requirement >= 0.5


def test_critic_action_added_for_decision_signals() -> None:
    s = StanceSignals(
        pulse_available=False,
        decision_candidate_high_count=_DEC_CANDIDATE_BACKLOG_SOFT,
    )
    a = pulse_to_stance("critic", s)
    decision_actions = [
        x
        for x in a.actions
        if x.tool == "watercooler_daemon_findings"
        and x.arguments.get("daemon") == "decision_extractor"
    ]
    assert len(decision_actions) == 1


# ---------------------------------------------------------------------------
# Per-role mapping — tester
# ---------------------------------------------------------------------------


def test_tester_drought_with_backlog_triggers_l1() -> None:
    s = StanceSignals(
        pulse_available=False,
        decision_candidate_high_count=_DEC_CANDIDATE_BACKLOG_SOFT,
        decisions_recorded_recent_count=0,
    )
    a = pulse_to_stance("tester", s)
    assert a.level == 1
    assert "decisions_recorded_recent_count" in a.triggered_signals
    assert "decision_candidate_high_count" in a.triggered_signals
    assert a.stance.provenance_requirement >= 0.5
    assert a.stance.handoff_bias >= 0.4


def test_tester_drought_without_backlog_quiet() -> None:
    """Drought alone (no backlog) must not fire — would create noise on idle repos."""
    s = StanceSignals(
        pulse_available=False,
        decisions_recorded_recent_count=0,
        decision_candidate_high_count=_DEC_CANDIDATE_BACKLOG_SOFT - 1,
    )
    a = pulse_to_stance("tester", s)
    assert a.level == 0
    assert "decisions_recorded_recent_count" not in a.triggered_signals


def test_tester_backlog_with_recorded_decisions_quiet() -> None:
    """Backlog with active recording is healthy — drought row should not fire."""
    s = StanceSignals(
        pulse_available=False,
        decision_candidate_high_count=_DEC_CANDIDATE_BACKLOG_SOFT,
        decisions_recorded_recent_count=4,
    )
    a = pulse_to_stance("tester", s)
    assert a.level == 0
    assert "decisions_recorded_recent_count" not in a.triggered_signals


def test_tester_drought_action_uses_list_decisions() -> None:
    s = StanceSignals(
        pulse_available=False,
        decision_candidate_high_count=_DEC_CANDIDATE_BACKLOG_SOFT,
        decisions_recorded_recent_count=0,
    )
    a = pulse_to_stance("tester", s)
    list_decisions_actions = [
        x for x in a.actions if x.tool == "watercooler_list_decisions"
    ]
    assert len(list_decisions_actions) == 1
    args = list_decisions_actions[0].arguments
    assert args.get("only_extracted") is True


# ---------------------------------------------------------------------------
# Signature backward compatibility
# ---------------------------------------------------------------------------


def test_signature_unchanged_when_decision_fields_default() -> None:
    """Coordinator-only signals must produce identical signatures regardless
    of whether the new decision fields exist on ``StanceSignals``.

    Construct two equivalent ``StanceSignals`` — one omitting the new fields,
    one explicitly setting them to defaults — and confirm both produce the
    same advisory signature for each role.
    """
    legacy = StanceSignals(
        pulse_available=False,
        coordinator_stalled_open_loop_count=2,
    )
    explicit = StanceSignals(
        pulse_available=False,
        coordinator_stalled_open_loop_count=2,
        decision_candidate_high_count=0,
        decision_extraction_success_count=0,
        decision_extraction_rejected_count=0,
        decision_extraction_rate_limited_count=0,
        decisions_recorded_recent_count=0,
    )
    for role in ("planner", "critic", "tester"):
        a1 = pulse_to_stance(role, legacy)
        a2 = pulse_to_stance(role, explicit)
        assert a1.advisory_signature == a2.advisory_signature, role


def test_signature_changes_only_for_role_whose_row_fired() -> None:
    """A decision signal that crosses planner's threshold must change planner's
    signature but leave critic/tester signatures stable.
    """
    base = StanceSignals(pulse_available=False)
    planner_only = StanceSignals(
        pulse_available=False,
        decision_extraction_rate_limited_count=2,  # planner-only signal
    )

    for role in ("planner", "critic", "tester"):
        a_base = pulse_to_stance(role, base)
        a_changed = pulse_to_stance(role, planner_only)
        if role == "planner":
            assert a_base.advisory_signature != a_changed.advisory_signature
        else:
            assert a_base.advisory_signature == a_changed.advisory_signature


def test_extract_stance_signals_unchanged_for_coordinator_path() -> None:
    """Existing pulse / coordinator caller produces a StanceSignals with new
    decision fields at default 0 — signature equivalence with legacy callers.
    """
    coord_findings = [
        {"category": "stalled_open_loop"},
        {"category": "stalled_open_loop"},
    ]
    s = extract_stance_signals(
        snapshot=None,
        coordinator_findings=coord_findings,
    )
    assert s.decision_candidate_high_count == 0
    assert s.decision_extraction_success_count == 0
    assert s.decisions_recorded_recent_count == 0


# ---------------------------------------------------------------------------
# _coarsen_crossings filter behaviour
# ---------------------------------------------------------------------------


def test_coarsen_excludes_decision_buckets_when_filter_omits_them() -> None:
    s = StanceSignals(
        decision_candidate_high_count=_DEC_CANDIDATE_BACKLOG_HARD,
        decision_extraction_rate_limited_count=2,
        decision_extraction_success_count=1,
        decision_extraction_rejected_count=9,
    )
    # Filter excludes all decision-pipeline names — none of the new buckets
    # should appear (preserves coordinator-only signature stability).
    buckets = _coarsen_crossings(
        s, signal_filter={"coordinator_stalled_open_loop_count"}
    )
    for bucket in buckets:
        assert not bucket.startswith("decision_")


def test_coarsen_includes_decision_buckets_when_filter_admits_them() -> None:
    s = StanceSignals(
        decision_candidate_high_count=_DEC_CANDIDATE_BACKLOG_HARD,
    )
    buckets = _coarsen_crossings(s, signal_filter={"decision_candidate_high_count"})
    assert any(b.startswith("decision_candidate_backlog:HARD") for b in buckets)


def test_coarsen_drought_bucket_only_when_field_in_filter() -> None:
    s = StanceSignals(decisions_recorded_recent_count=0)
    # Drought bucket emits when count == 0 AND the field is in the filter
    with_drought = _coarsen_crossings(
        s, signal_filter={"decisions_recorded_recent_count"}
    )
    assert any(b == "decision_drought:True" for b in with_drought)
    without = _coarsen_crossings(s, signal_filter={"volatility_ratio"})
    assert not any(b == "decision_drought:True" for b in without)


# ---------------------------------------------------------------------------
# Constants + read-only allowlist
# ---------------------------------------------------------------------------


def test_thresholds_present_and_ordered() -> None:
    assert 0 < _DEC_CANDIDATE_BACKLOG_SOFT < _DEC_CANDIDATE_BACKLOG_HARD
    assert 0 < _DEC_REJECTION_RATIO_SOFT < _DEC_REJECTION_RATIO_HARD <= 1.0
    assert _DEC_RATE_LIMITED_SOFT >= 1


def test_list_decisions_in_read_only_allowlist() -> None:
    """The new tester drought action must use a tool from the allowlist."""
    from watercooler.pulse_stance_lib import _READ_ONLY_TOOLS

    assert "watercooler_list_decisions" in _READ_ONLY_TOOLS
