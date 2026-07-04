"""Unit tests for promotion_metrics_lib — honest metric states (computation deferred).

These exercise every substrate condition for the two promotion-gate instrumentation
metrics. The hazard VALUE is deferred (producer phase); this phase locks the STATES
and the output-shape contract so a value is never fabricated and "unmeasurable" is
never coerced to "measured zero".
"""

from watercooler.promotion_metrics_lib import (
    REASON_COMPUTATION_DEFERRED,
    REASON_INSUFFICIENT_T2_COVERAGE,
    REASON_NO_PROMOTED_POPULATION,
    REASON_NO_RETRIEVAL_PROVENANCE,
    REASON_T2_UNAVAILABLE,
    STATE_MEASURED,
    STATE_NOT_YET_MEASURABLE,
    STATE_UNKNOWN,
    EndogenousReinforcementResult,
    compute_early_supersession_hazard,
    compute_endogenous_reinforcement_rate,
)


def _record(state: str, reason: str = "") -> dict:
    return {"supersession": {"state": state, "reason": reason}}


class TestEarlySupersessionHazardStates:
    """compute_early_supersession_hazard returns the correct state per substrate."""

    def test_no_t2_is_whole_metric_unknown_never_zero(self):
        # Open-core / T1-only: the whole metric degrades to unknown. Never 0.0.
        result = compute_early_supersession_hazard(
            promoted_records=[_record("in_force"), _record("superseded")],
            t2_available=False,
        )
        assert result.state == STATE_UNKNOWN
        assert result.reason == REASON_T2_UNAVAILABLE
        assert result.value is None
        # The contrast trap: an unmeasurable metric must not look like measured-zero.
        assert result.value != 0.0
        assert result.coverage is None

    def test_t2_present_no_promoted_population_is_not_yet_measurable(self):
        result = compute_early_supersession_hazard(
            promoted_records=[],
            t2_available=True,
        )
        assert result.state == STATE_NOT_YET_MEASURABLE
        assert result.reason == REASON_NO_PROMOTED_POPULATION
        assert result.value is None
        assert result.promoted_total == 0

    def test_all_coverage_holes_is_unknown_not_zero(self):
        # T2 present, population present, but every record is a per-record coverage
        # hole (no derived edges / no episode mapping) -> unknown, never 0.0.
        result = compute_early_supersession_hazard(
            promoted_records=[
                _record("unknown", "no_derived_edges"),
                _record("unknown", "no_episode_mapping"),
                _record("unknown", "no_derived_edges"),
            ],
            t2_available=True,
        )
        assert result.state == STATE_UNKNOWN
        assert result.reason == REASON_INSUFFICIENT_T2_COVERAGE
        assert result.value is None
        assert result.resolvable_denominator == 0
        assert result.coverage == 0.0
        assert result.unknown_breakdown == {"no_derived_edges": 2, "no_episode_mapping": 1}

    def test_resolvable_population_defers_value_with_coverage(self):
        # 2 resolvable of 4 promoted -> coverage 0.5, but the VALUE is deferred.
        result = compute_early_supersession_hazard(
            promoted_records=[
                _record("in_force"),
                _record("superseded"),
                _record("unknown", "no_derived_edges"),
                _record("unknown", "lookup_error"),
            ],
            t2_available=True,
        )
        assert result.state == STATE_NOT_YET_MEASURABLE
        assert result.reason == REASON_COMPUTATION_DEFERRED
        assert result.value is None  # never fabricated
        assert result.numerator is None
        assert result.resolvable_denominator == 2
        assert result.promoted_total == 4
        assert result.coverage == 0.5
        assert result.unknown_breakdown == {"no_derived_edges": 1, "lookup_error": 1}

    def test_partially_superseded_counts_as_resolvable(self):
        # partially_superseded means T2 answered (it is in the resolvable set); the
        # full/fractional COUNTING rule is a deferred value-phase decision, not a
        # coverage question.
        result = compute_early_supersession_hazard(
            promoted_records=[_record("partially_superseded")],
            t2_available=True,
        )
        assert result.state == STATE_NOT_YET_MEASURABLE
        assert result.reason == REASON_COMPUTATION_DEFERRED
        assert result.resolvable_denominator == 1
        assert result.coverage == 1.0

    def test_record_missing_supersession_key_is_coverage_loss(self):
        result = compute_early_supersession_hazard(
            promoted_records=[{}],  # no supersession summary at all
            t2_available=True,
        )
        assert result.state == STATE_UNKNOWN
        assert result.unknown_breakdown == {"missing_supersession": 1}

    def test_full_coverage_resolvable(self):
        result = compute_early_supersession_hazard(
            promoted_records=[_record("in_force"), _record("superseded")],
            t2_available=True,
        )
        assert result.coverage == 1.0
        assert result.resolvable_denominator == 2
        assert result.unknown_breakdown == {}

    def test_never_returns_measured_this_phase(self):
        # The value path is deferred, so no input can yield STATE_MEASURED yet.
        for records, t2 in [
            ([], True),
            ([_record("in_force")], True),
            ([_record("superseded")], False),
            ([_record("unknown", "no_derived_edges")], True),
        ]:
            result = compute_early_supersession_hazard(
                promoted_records=records, t2_available=t2
            )
            assert result.state != STATE_MEASURED
            assert result.value is None


class TestHazardResultShape:
    """The output shape is aggregate-only (fail-fresh preserved) and serializable."""

    def test_result_carries_only_aggregates_no_per_decision_state(self):
        result = compute_early_supersession_hazard(
            promoted_records=[_record("in_force")], t2_available=True
        )
        d = result.to_dict()
        # Aggregate fields only — no per-Decision tether, entry_id, or as_of leaks.
        assert set(d) == {
            "state", "value", "numerator", "resolvable_denominator",
            "promoted_total", "coverage", "censored", "unknown_breakdown", "reason",
        }
        assert "as_of" not in d
        assert "entry_id" not in d
        assert "tether" not in d

    def test_result_is_frozen(self):
        import dataclasses
        result = compute_early_supersession_hazard(promoted_records=[], t2_available=True)
        try:
            result.state = STATE_MEASURED  # type: ignore[misc]
            assert False, "result should be immutable"
        except dataclasses.FrozenInstanceError:
            pass


class TestEndogenousReinforcementRate:
    """compute_endogenous_reinforcement_rate is always not_yet_measurable."""

    def test_always_not_yet_measurable_never_numeric(self):
        result = compute_endogenous_reinforcement_rate()
        assert isinstance(result, EndogenousReinforcementResult)
        assert result.state == STATE_NOT_YET_MEASURABLE
        assert result.reason == REASON_NO_RETRIEVAL_PROVENANCE
        assert result.value is None
        # Never a number — there is no threshold to cross, so it must never feed an alert.
        assert not isinstance(result.value, (int, float))

    def test_distinct_from_backend_unknown(self):
        # The endogenous "not yet measurable" status is categorically different from
        # the hazard's backend-gap "unknown": the substrate itself does not exist.
        endo = compute_endogenous_reinforcement_rate()
        hazard_no_t2 = compute_early_supersession_hazard(
            promoted_records=[], t2_available=False
        )
        assert endo.state == STATE_NOT_YET_MEASURABLE
        assert hazard_no_t2.state == STATE_UNKNOWN
        assert endo.state != hazard_no_t2.state
