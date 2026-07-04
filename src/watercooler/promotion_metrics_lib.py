"""promotion_metrics_lib — pure-Python promotion-gate instrumentation metrics.

Two instrumentation metrics the epistemic-custody brainstorm mandates *before* any
promotion producer can claim safety (the coupled metric: a control is net-positive
only where inspectability rises at least as fast as authority):

- ``early_supersession_hazard`` — of promoted Decisions, the fraction superseded
  within an early window. Computable only with T2/Graphiti; degrades to ``unknown``
  in open-core (supersession is a pure T2 property).
- ``endogenous_reinforcement_rate`` — how much "use" was the system re-reading
  itself. No input substrate exists today (no retrieval->write provenance), so it is
  always ``not_yet_measurable``.

**Phase-2 scope (computation deferred).** This module locks the *honest states* and
the *output-shape contract*, not the hosted hazard *value*. The hard value-computation
edge cases (the early-window anchor, right-censoring, the ``partially_superseded``
counting rule, coverage math) have correct answers only relative to a launch-gate
consumer (the ``StabilizedBeliefCandidate`` producer) that does not yet exist, so the
hazard value is deferred to the producer phase. Until then this module returns the
correct *state* for every substrate condition and never fabricates a value.

A StabilizedBeliefCandidate *producer surface* now exists
(``watercooler.belief_candidate``, #897b), but it composes candidate fact-edges and
runs the composition/disagreement gates — it does **not** compute this hazard value or
define the launch-gate consumer contract. The value computation (S-anchor,
right-censoring, ``partially_superseded`` rule, coverage math — see
``dev_docs/plans/_artifacts/897a-deferred-verification.md``) therefore remains deferred;
do not read the existence of the producer as wiring this metric.

Pure and stdlib-only: it operates on already-fetched per-record supersession
summaries (the dicts produced by ``watercooler_memory.supersession.summarize_supersession``
/ the ``_unknown_supersession`` degradation path), so it is unit-testable without a
live T2 backend. The I/O (resolving promoted Decisions and their supersession state)
lives in the calling tool/daemon — which does not exist yet (surface deferred).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Metric states — a measured value, an unmeasurable backend gap, and a
# not-yet-built measurement are three DISTINCT states, never one sentinel float.
# ---------------------------------------------------------------------------
STATE_MEASURED = "measured"
STATE_UNKNOWN = "unknown"
STATE_NOT_YET_MEASURABLE = "not_yet_measurable"

# Reasons for the hazard's non-measured states.
REASON_T2_UNAVAILABLE = "t2_unavailable"
REASON_INSUFFICIENT_T2_COVERAGE = "insufficient_t2_coverage"
REASON_NO_PROMOTED_POPULATION = "no_promoted_population"
REASON_COMPUTATION_DEFERRED = "computation_deferred"
# Reserved for the producer phase once the right-censoring rule exists; never
# returned today (censoring computation is deferred).
REASON_ALL_CENSORED = "all_censored"

# Reason for the endogenous metric — distinct from a backend gap: there is no
# substrate at all (no retrieval->write provenance is recorded anywhere).
REASON_NO_RETRIEVAL_PROVENANCE = "no_retrieval_provenance"

# Per-record supersession states that mean "T2 answered" — the resolvable
# population the hazard would be denominated over.
_RESOLVABLE_STATES = frozenset({"in_force", "superseded", "partially_superseded"})


@dataclass(frozen=True)
class SupersessionHazardResult:
    """Aggregate-only result for ``compute_early_supersession_hazard``.

    Carries no per-Decision tether or ``as_of`` — only population aggregates — so the
    fail-fresh property (support is re-derived live, never persisted) is preserved even
    if a future daemon snapshots this result.
    """

    state: str
    value: float | None = None
    numerator: int | None = None
    resolvable_denominator: int | None = None
    promoted_total: int | None = None
    coverage: float | None = None
    censored: int | None = None
    unknown_breakdown: dict[str, int] = field(default_factory=dict)
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "value": self.value,
            "numerator": self.numerator,
            "resolvable_denominator": self.resolvable_denominator,
            "promoted_total": self.promoted_total,
            "coverage": self.coverage,
            "censored": self.censored,
            "unknown_breakdown": dict(self.unknown_breakdown),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class EndogenousReinforcementResult:
    """Result for ``compute_endogenous_reinforcement_rate`` — always not-yet-measurable."""

    state: str
    value: float | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "value": self.value, "reason": self.reason}


def compute_early_supersession_hazard(
    *,
    promoted_records: list[dict[str, Any]],
    t2_available: bool,
) -> SupersessionHazardResult:
    """Classify the early-supersession hazard's *state* (value computation deferred).

    Args:
        promoted_records: One dict per promoted Decision in the window, each carrying
            a ``supersession`` summary dict ``{state, reason, ...}`` (from
            ``summarize_supersession`` on hosted/T2, or an ``unknown`` degradation).
            Records without a ``supersession`` key are treated as coverage loss with
            reason ``"missing_supersession"``.
        t2_available: Whether T2/Graphiti could be consulted at all. ``False`` (e.g.
            open-core T1-only) short-circuits the *whole* metric to ``unknown`` — a
            backend absence is categorically different from per-record coverage loss.

    Returns:
        A :class:`SupersessionHazardResult`. The hazard ``value`` is never computed in
        this phase: when a resolvable population exists, the state is
        ``not_yet_measurable`` with reason ``computation_deferred``. ``unknown`` is
        never coerced to ``0.0`` — "we cannot measure" and "we measured zero" are
        distinct states.
    """
    # Whole-metric degradation: no T2 at all. Never a value, never 0.0.
    if not t2_available:
        return SupersessionHazardResult(
            state=STATE_UNKNOWN,
            reason=REASON_T2_UNAVAILABLE,
        )

    promoted_total = len(promoted_records)

    # T2 present but nothing to measure yet — distinct from a backend gap.
    if promoted_total == 0:
        return SupersessionHazardResult(
            state=STATE_NOT_YET_MEASURABLE,
            promoted_total=0,
            resolvable_denominator=0,
            coverage=None,
            reason=REASON_NO_PROMOTED_POPULATION,
        )

    # Partition into resolvable (T2 answered) vs coverage loss (per-record unknown).
    resolvable = 0
    unknown_breakdown: dict[str, int] = {}
    for record in promoted_records:
        summary = record.get("supersession") or {}
        state = summary.get("state")
        if state in _RESOLVABLE_STATES:
            resolvable += 1
        else:
            reason = summary.get("reason") or "missing_supersession"
            unknown_breakdown[reason] = unknown_breakdown.get(reason, 0) + 1

    coverage = resolvable / promoted_total

    # T2 present, population present, but every record was a coverage hole — the
    # metric cannot be denominated. Unknown (per-record coverage), never 0.0.
    if resolvable == 0:
        return SupersessionHazardResult(
            state=STATE_UNKNOWN,
            resolvable_denominator=0,
            promoted_total=promoted_total,
            coverage=0.0,
            unknown_breakdown=unknown_breakdown,
            reason=REASON_INSUFFICIENT_T2_COVERAGE,
        )

    # A resolvable population exists. The hazard VALUE (which requires the early-window
    # anchor, right-censoring, and the partially_superseded counting rule) is deferred
    # to the producer phase, where a real launch-gate consumer defines the contract.
    return SupersessionHazardResult(
        state=STATE_NOT_YET_MEASURABLE,
        value=None,
        numerator=None,
        resolvable_denominator=resolvable,
        promoted_total=promoted_total,
        coverage=coverage,
        censored=None,
        unknown_breakdown=unknown_breakdown,
        reason=REASON_COMPUTATION_DEFERRED,
    )


def compute_endogenous_reinforcement_rate() -> EndogenousReinforcementResult:
    """Return the endogenous-reinforcement rate — always ``not_yet_measurable``.

    There is no retrieval->write provenance recorded anywhere today (the access
    odometer is disabled dead code), so the system cannot distinguish independent
    support from retrieval echo / agent reassertion. The honest result is a status
    *distinct* from ``unknown`` (which would imply a merely-absent backend): the
    measurement substrate itself does not exist. The metric must never return a number
    and must never feed a threshold alert (there is no threshold to cross on a constant).
    """
    return EndogenousReinforcementResult(
        state=STATE_NOT_YET_MEASURABLE,
        value=None,
        reason=REASON_NO_RETRIEVAL_PROVENANCE,
    )
