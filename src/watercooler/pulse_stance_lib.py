"""Pulse stance modulation library — per-role behavioral advisories.

Stdlib-only, fully testable.  Converts project health signals (from both
PulseSnapshotDaemon and ProjectCoordinatorDaemon v1A findings) into
structured per-role advisories that agents consume via daemon findings.

Public API
----------
- ``StanceVector`` — per-dimension behavioral pressure (0.0–1.0)
- ``AdvisoryAction`` — suggested read/query tool call
- ``StanceSignals`` — unified input signals from pulse + coordinator
- ``StanceAdvisory`` — complete advisory for one role
- ``extract_stance_signals(snapshot, ...)`` — build StanceSignals
- ``pulse_to_stance(role, signals)`` — compute advisory for one role
- ``build_stance_advisories(snapshot, ...)`` — compute all three roles
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Read-only tool allowlist — advisory actions must never suggest writes
# ---------------------------------------------------------------------------

_READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "watercooler_smart_query",
    "watercooler_read_thread",
    "watercooler_list_thread_entries",
    "watercooler_daemon_findings",
    "watercooler_search",
    "watercooler_find_similar",
})


# ---------------------------------------------------------------------------
# Signal thresholds (module-level constants)
# ---------------------------------------------------------------------------

_VOLATILITY_SOFT = 0.50
_VOLATILITY_HARD = 0.70
_STALLED_SOFT = 2
_STALLED_HARD = 4
_OPEN_LOOP_SOFT = 3
_OPEN_LOOP_HARD = 6
_RISK_TAG_SOFT = 1
_RISK_TAG_HARD = 3
_COORD_STALLED_SOFT = 2
_COORD_STALLED_HARD = 5
_COORD_ROLE_CONC_SOFT = 1
_COORD_ROLE_CONC_HARD = 3
_COORD_DROPOUT_SOFT = 1
_COORD_BURST_SOFT = 1
_COORD_NEW_CONTRIB_SOFT = 1  # SOFT-only: presence alone is noteworthy


# ---------------------------------------------------------------------------
# Threshold bucket names (for coarsened advisory_signature)
# ---------------------------------------------------------------------------

def _bucket(value: float, soft: float, hard: float) -> str:
    """Classify a value into NONE / SOFT / HARD bucket."""
    if value >= hard:
        return "HARD"
    if value >= soft:
        return "SOFT"
    return "NONE"





# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StanceVector:
    """Per-dimension behavioral pressure (0.0–1.0)."""

    retrieval_pressure: float = 0.0
    decision_caution: float = 0.0
    critique_intensity: float = 0.0
    provenance_requirement: float = 0.0
    handoff_bias: float = 0.0
    closure_pressure: float = 0.0


@dataclass(frozen=True)
class AdvisoryAction:
    """Suggested read/query tool call for an agent to execute.

    All tools must be in ``_READ_ONLY_TOOLS`` — no write actions permitted.
    """

    phase: str          # "pre" or "post"
    tool: str           # MCP tool name
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def __post_init__(self) -> None:
        if self.tool not in _READ_ONLY_TOOLS:
            raise ValueError(
                f"AdvisoryAction.tool {self.tool!r} not in _READ_ONLY_TOOLS. "
                f"Allowed: {sorted(_READ_ONLY_TOOLS)}"
            )


@dataclass(frozen=True)
class StanceSignals:
    """Unified input signals from pulse snapshot + coordinator findings."""

    # --- From pulse snapshot (via compute_state_signals) ---
    pulse_available: bool = False
    stalled_thread_count: int = 0
    volatility_ratio: float = 0.0
    open_loop_count: int = 0
    risk_tag_count: int = 0
    analysis_report_available: bool = False
    analysis_is_fresh: bool = False
    # staged: not yet thresholded — reserved for coordinator signal expansion
    sessions_in_window: int = 0
    # staged: not yet thresholded — reserved for coordinator signal expansion
    focus_area_overlap_count: int = 0
    # --- From coordinator v1A findings (this tick) ---
    coordinator_stalled_open_loop_count: int = 0
    coordinator_role_concentration_count: int = 0
    coordinator_dropout_count: int = 0
    coordinator_burst_count: int = 0
    # Coordinator-derived (corpus-level): not in _PULSE_SIGNAL_NAMES
    coordinator_new_contributor_count: int = 0
    # --- Phase 2: from TrendSnapshotDaemon ---
    # staged: not yet thresholded — reserved for coordinator signal expansion
    trend_supersession_rate: float | None = None


@dataclass(frozen=True)
class StanceAdvisory:
    """Complete advisory for one role."""

    schema_version: int
    role: str
    level: int              # 0-2
    summary: str
    triggered_signals: list[str]
    missing_inputs: list[str]
    threshold_crossings: list[str]
    advisory_signature: str
    signal_values: StanceSignals
    stance: StanceVector
    actions: list[AdvisoryAction]


# ---------------------------------------------------------------------------
# Signal extraction
# ---------------------------------------------------------------------------

# Pulse signal names that are unavailable in degraded mode
_PULSE_SIGNAL_NAMES: tuple[str, ...] = (
    "volatility_ratio",
    "stalled_thread_count",
    "open_loop_count",
    "risk_tag_count",
    "analysis_report_available",
    "analysis_is_fresh",
    # staged: not yet thresholded — reserved for coordinator signal expansion
    "sessions_in_window",
    # staged: not yet thresholded — reserved for coordinator signal expansion
    "focus_area_overlap_count",
)


def extract_stance_signals(
    snapshot: dict[str, Any] | None = None,
    *,
    coordinator_findings: list[dict[str, Any]] | None = None,
    trend_supersession_rate: float | None = None,
) -> StanceSignals:
    """Build StanceSignals from pulse snapshot and coordinator findings.

    Args:
        snapshot: Pulse snapshot dict, or None/empty for degraded mode.
        coordinator_findings: List of coordinator finding dicts with
            ``category`` keys (counts extracted per category).
        trend_supersession_rate: Phase 2 input (unused in Phase 1).

    Returns:
        StanceSignals with all available fields populated.
    """
    # Normalize: both None and {} → unavailable
    snapshot = snapshot or None
    coordinator_findings = coordinator_findings or []

    # Count coordinator findings by category
    coord_counts: dict[str, int] = {}
    for f in coordinator_findings:
        cat = f.get("category", "")
        coord_counts[cat] = coord_counts.get(cat, 0) + 1

    if snapshot is not None:
        # Full mode: compute pulse-derived signals
        # Deferred: pulse_snapshot_lib has non-stdlib deps; module is stdlib-only at top level
        from watercooler.pulse_snapshot_lib import compute_state_signals

        state = compute_state_signals(snapshot)
        per_contributor = state.get("per_contributor", {})
        repo_level = state.get("repo_level", {})

        # Aggregate volatility: mean of non-null ratios
        ratios = [
            c["volatility_ratio"]
            for c in per_contributor.values()
            if c.get("volatility_ratio") is not None
        ]
        volatility_ratio = round(sum(ratios) / len(ratios), 2) if ratios else 0.0

        # Aggregate open loop count — contributor-weighted exposure, not distinct threads.
        # Each contributor's open_loop_count is the number of loops they participate in.
        # A 2-person team sharing 3 open-loop threads yields open_loop_count=6 (3+3).
        # Thresholds _OPEN_LOOP_SOFT and _OPEN_LOOP_HARD are calibrated to this metric.
        open_loop_count = sum(
            c.get("open_loop_count", 0) for c in per_contributor.values()
        )

        # Analysis freshness
        analysis = snapshot.get("analysis", {})
        analysis_report_available = bool(analysis.get("latest_report_path"))
        analysis_is_fresh = bool(analysis.get("is_fresh", False))

        return StanceSignals(
            pulse_available=True,
            stalled_thread_count=repo_level.get("stalled_thread_count", 0),
            volatility_ratio=volatility_ratio,
            open_loop_count=open_loop_count,
            risk_tag_count=len(snapshot.get("risk_surface_tags", [])),
            analysis_report_available=analysis_report_available,
            analysis_is_fresh=analysis_is_fresh,
            sessions_in_window=repo_level.get("sessions_in_window", 0),
            focus_area_overlap_count=len(
                repo_level.get("focus_area_overlap", [])
            ),
            coordinator_stalled_open_loop_count=coord_counts.get(
                "stalled_open_loop", 0
            ),
            coordinator_role_concentration_count=coord_counts.get(
                "aware_role_concentration", 0
            ),
            coordinator_dropout_count=coord_counts.get("stalled_dropout", 0),
            coordinator_burst_count=coord_counts.get("aware_burst", 0),
            coordinator_new_contributor_count=coord_counts.get(
                "aware_new_contributor", 0
            ),
            trend_supersession_rate=trend_supersession_rate,
        )

    # Degraded mode: coordinator-only
    return StanceSignals(
        pulse_available=False,
        coordinator_stalled_open_loop_count=coord_counts.get(
            "stalled_open_loop", 0
        ),
        coordinator_role_concentration_count=coord_counts.get(
            "aware_role_concentration", 0
        ),
        coordinator_dropout_count=coord_counts.get("stalled_dropout", 0),
        coordinator_burst_count=coord_counts.get("aware_burst", 0),
        coordinator_new_contributor_count=coord_counts.get(
            "aware_new_contributor", 0
        ),
        trend_supersession_rate=trend_supersession_rate,
    )


# ---------------------------------------------------------------------------
# Per-role weight tables
# ---------------------------------------------------------------------------

def _planner_stance(signals: StanceSignals) -> StanceAdvisory:
    """Compute planner advisory from signals."""
    v = signals.volatility_ratio
    sol = signals.coordinator_stalled_open_loop_count
    rc = signals.coordinator_role_concentration_count

    triggered: set[str] = set()
    crossings: list[str] = []
    rows: list[StanceVector] = []
    level = 0

    # Row checks — multiple rows can match; take max level and max per-dimension
    if v >= _VOLATILITY_HARD:
        level = max(level, 2)
        rows.append(StanceVector(
            retrieval_pressure=0.9, decision_caution=0.8, closure_pressure=0.3,
        ))
        triggered.add("volatility_ratio")
        crossings.append(
            f"volatility_ratio={v:.2f} >= HARD({_VOLATILITY_HARD})"
        )
    elif v >= _VOLATILITY_SOFT:
        if sol >= _COORD_STALLED_SOFT:
            level = max(level, 2)
            rows.append(StanceVector(
                retrieval_pressure=0.8, decision_caution=0.7, closure_pressure=0.5,
            ))
            triggered.update({"volatility_ratio", "coordinator_stalled_open_loop_count"})
            crossings.append(
                f"volatility_ratio={v:.2f} >= SOFT({_VOLATILITY_SOFT})"
            )
            crossings.append(
                f"coordinator_stalled_open_loop_count={sol} >= SOFT({_COORD_STALLED_SOFT})"
            )
        else:
            level = max(level, 1)
            rows.append(StanceVector(
                retrieval_pressure=0.6, decision_caution=0.5, closure_pressure=0.0,
            ))
            triggered.add("volatility_ratio")
            crossings.append(
                f"volatility_ratio={v:.2f} >= SOFT({_VOLATILITY_SOFT})"
            )

    if sol >= _COORD_STALLED_HARD:
        level = max(level, 2)
        rows.append(StanceVector(
            retrieval_pressure=0.5, decision_caution=0.3, closure_pressure=0.9,
        ))
        triggered.add("coordinator_stalled_open_loop_count")
        crossings.append(
            f"coordinator_stalled_open_loop_count={sol} >= HARD({_COORD_STALLED_HARD})"
        )
    elif sol >= _COORD_STALLED_SOFT and v < _VOLATILITY_SOFT:
        level = max(level, 1)
        rows.append(StanceVector(
            retrieval_pressure=0.3, decision_caution=0.0, closure_pressure=0.6,
        ))
        triggered.add("coordinator_stalled_open_loop_count")
        crossings.append(
            f"coordinator_stalled_open_loop_count={sol} >= SOFT({_COORD_STALLED_SOFT})"
        )

    if rc >= _COORD_ROLE_CONC_HARD:
        level = max(level, 2)
        rows.append(StanceVector(
            retrieval_pressure=0.7, decision_caution=0.6, closure_pressure=0.4,
        ))
        triggered.add("coordinator_role_concentration_count")
        crossings.append(
            f"coordinator_role_concentration_count={rc} >= HARD({_COORD_ROLE_CONC_HARD})"
        )
    elif rc >= _COORD_ROLE_CONC_SOFT:
        level = max(level, 1)
        rows.append(StanceVector(
            retrieval_pressure=0.4, decision_caution=0.3, closure_pressure=0.2,
        ))
        triggered.add("coordinator_role_concentration_count")
        crossings.append(
            f"coordinator_role_concentration_count={rc} >= SOFT({_COORD_ROLE_CONC_SOFT})"
        )

    # P2.1: new contributor detected — raise retrieval pressure so the planner
    # re-reads context before making decisions that may assume prior context.
    nc = signals.coordinator_new_contributor_count
    if nc >= _COORD_NEW_CONTRIB_SOFT:
        level = max(level, 1)
        rows.append(StanceVector(retrieval_pressure=0.5))
        triggered.add("coordinator_new_contributor_count")
        crossings.append(
            f"coordinator_new_contributor_count={nc} >= SOFT({_COORD_NEW_CONTRIB_SOFT})"
        )

    # Merge rows: max per dimension
    stance = _merge_vectors(rows) if rows else StanceVector()

    # Build actions
    # code_path is intentionally omitted from action arguments: advisory actions
    # are repo-agnostic read queries; callers should inject code_path from their
    # session context if multi-repo scope isolation is needed.
    actions: list[AdvisoryAction] = []
    if level >= 1:
        actions.append(AdvisoryAction(
            phase="pre",
            tool="watercooler_smart_query",
            arguments={"query": "recent decisions and open questions"},
            reason="High volatility or stalled loops — review prior decisions before planning",
        ))
    if "coordinator_new_contributor_count" in triggered:
        actions.append(AdvisoryAction(
            phase="pre",
            tool="watercooler_daemon_findings",
            arguments={
                "daemon": "project_coordinator",
                "category": "aware_new_contributor",
            },
            reason="New contributor detected — review their context before planning",
        ))
    if level >= 2:
        actions.append(AdvisoryAction(
            phase="pre",
            tool="watercooler_daemon_findings",
            arguments={
                "daemon": "project_coordinator",
                "category": "stalled_open_loop",
            },
            reason="Multiple stalled loops — check which threads need closure",
        ))

    missing = _missing_inputs(signals)
    summary = _build_summary("planner", level, triggered)
    signature = _compute_signature(
        role="planner", level=level, signals=signals,
        triggered=triggered,
    )

    return StanceAdvisory(
        schema_version=1,
        role="planner",
        level=level,
        summary=summary,
        triggered_signals=sorted(triggered),
        missing_inputs=missing,
        threshold_crossings=crossings,
        advisory_signature=signature,
        signal_values=signals,
        stance=stance,
        actions=actions,
    )


def _critic_stance(signals: StanceSignals) -> StanceAdvisory:
    """Compute critic advisory from signals."""
    rt = signals.risk_tag_count
    cd = signals.coordinator_dropout_count
    ol = signals.open_loop_count

    triggered: set[str] = set()
    crossings: list[str] = []
    rows: list[StanceVector] = []
    level = 0

    if rt >= _RISK_TAG_HARD:
        level = max(level, 2)
        rows.append(StanceVector(
            critique_intensity=0.8, provenance_requirement=0.8,
            retrieval_pressure=0.5,
        ))
        triggered.add("risk_tag_count")
        crossings.append(
            f"risk_tag_count={rt} >= HARD({_RISK_TAG_HARD})"
        )
    elif rt >= _RISK_TAG_SOFT:
        if cd >= _COORD_DROPOUT_SOFT:
            level = max(level, 2)
            rows.append(StanceVector(
                critique_intensity=0.7, provenance_requirement=0.7,
                retrieval_pressure=0.6,
            ))
            triggered.update({"risk_tag_count", "coordinator_dropout_count"})
            crossings.append(
                f"risk_tag_count={rt} >= SOFT({_RISK_TAG_SOFT})"
            )
            crossings.append(
                f"coordinator_dropout_count={cd} >= SOFT({_COORD_DROPOUT_SOFT})"
            )
        else:
            level = max(level, 1)
            rows.append(StanceVector(
                critique_intensity=0.5, provenance_requirement=0.4,
                retrieval_pressure=0.3,
            ))
            triggered.add("risk_tag_count")
            crossings.append(
                f"risk_tag_count={rt} >= SOFT({_RISK_TAG_SOFT})"
            )

    if cd >= _COORD_DROPOUT_SOFT and rt < _RISK_TAG_SOFT:
        level = max(level, 1)
        rows.append(StanceVector(
            critique_intensity=0.3, provenance_requirement=0.6,
            retrieval_pressure=0.5,
        ))
        triggered.add("coordinator_dropout_count")
        crossings.append(
            f"coordinator_dropout_count={cd} >= SOFT({_COORD_DROPOUT_SOFT})"
        )

    if ol >= _OPEN_LOOP_HARD:
        level = max(level, 2)
        rows.append(StanceVector(
            critique_intensity=0.6, provenance_requirement=0.5,
            retrieval_pressure=0.8,
        ))
        triggered.add("open_loop_count")
        crossings.append(
            f"open_loop_count={ol} >= HARD({_OPEN_LOOP_HARD})"
        )
    elif ol >= _OPEN_LOOP_SOFT:
        level = max(level, 1)
        rows.append(StanceVector(
            critique_intensity=0.3, provenance_requirement=0.3, retrieval_pressure=0.4,
        ))
        triggered.add("open_loop_count")
        crossings.append(
            f"open_loop_count={ol} >= SOFT({_OPEN_LOOP_SOFT})"
        )

    stance = _merge_vectors(rows) if rows else StanceVector()

    actions: list[AdvisoryAction] = []
    if level >= 1:
        actions.append(AdvisoryAction(
            phase="pre",
            tool="watercooler_search",
            arguments={"query": "risk problem unresolved", "mode": "entries"},
            reason="Risk signals detected — search for unresolved issues before critiquing",
        ))

    missing = _missing_inputs(signals)
    summary = _build_summary("critic", level, triggered)
    signature = _compute_signature(
        role="critic", level=level, signals=signals,
        triggered=triggered,
    )

    return StanceAdvisory(
        schema_version=1,
        role="critic",
        level=level,
        summary=summary,
        triggered_signals=sorted(triggered),
        missing_inputs=missing,
        threshold_crossings=crossings,
        advisory_signature=signature,
        signal_values=signals,
        stance=stance,
        actions=actions,
    )


def _tester_stance(signals: StanceSignals) -> StanceAdvisory:
    """Compute tester advisory from signals."""
    st = signals.stalled_thread_count
    analysis_stale = (
        signals.analysis_report_available and not signals.analysis_is_fresh
    )
    cb = signals.coordinator_burst_count

    triggered: set[str] = set()
    crossings: list[str] = []
    rows: list[StanceVector] = []
    level = 0

    if st >= _STALLED_HARD:
        level = max(level, 2)
        rows.append(StanceVector(
            retrieval_pressure=0.7, provenance_requirement=0.5,
            handoff_bias=0.6,
        ))
        triggered.add("stalled_thread_count")
        crossings.append(
            f"stalled_thread_count={st} >= HARD({_STALLED_HARD})"
        )
    elif st >= _STALLED_SOFT:
        if analysis_stale:
            level = max(level, 2)
            rows.append(StanceVector(
                retrieval_pressure=0.6, provenance_requirement=0.8,
                handoff_bias=0.4,
            ))
            triggered.update({"stalled_thread_count", "analysis_stale"})
            crossings.append(
                f"stalled_thread_count={st} >= SOFT({_STALLED_SOFT})"
            )
            crossings.append("analysis_stale=True")
        else:
            level = max(level, 1)
            rows.append(StanceVector(
                retrieval_pressure=0.5, provenance_requirement=0.3,
                handoff_bias=0.0,
            ))
            triggered.add("stalled_thread_count")
            crossings.append(
                f"stalled_thread_count={st} >= SOFT({_STALLED_SOFT})"
            )

    if analysis_stale and "analysis_stale" not in triggered:
        # Standalone analysis_stale signal — only fires when not already handled
        # by the joint STALLED_SOFT + analysis_stale branch above (which sets
        # level=2 and appends the crossing itself to avoid a duplicate entry).
        level = max(level, 1)
        rows.append(StanceVector(
            retrieval_pressure=0.3, provenance_requirement=0.7,
            handoff_bias=0.0,
        ))
        triggered.add("analysis_stale")
        crossings.append("analysis_stale=True")

    if cb >= _COORD_BURST_SOFT:
        level = max(level, 1)
        rows.append(StanceVector(
            retrieval_pressure=0.4, provenance_requirement=0.3,
            handoff_bias=0.5,
        ))
        triggered.add("coordinator_burst_count")
        crossings.append(
            f"coordinator_burst_count={cb} >= SOFT({_COORD_BURST_SOFT})"
        )

    stance = _merge_vectors(rows) if rows else StanceVector()

    # P2.2: route the action to the signal that actually elevated the tester.
    # Static `stalled_open_loop` always-on was wrong when burst was the only trigger.
    actions: list[AdvisoryAction] = []
    if level >= 1:
        burst_active = signals.coordinator_burst_count >= _COORD_BURST_SOFT
        stalled_active = (
            signals.stalled_thread_count >= _STALLED_SOFT or analysis_stale
        )
        if burst_active:
            actions.append(AdvisoryAction(
                phase="pre",
                tool="watercooler_daemon_findings",
                arguments={"daemon": "project_coordinator", "category": "aware_burst"},
                reason="Burst activity detected — check which thread is spiking before testing",
            ))
        if stalled_active:
            actions.append(AdvisoryAction(
                phase="pre",
                tool="watercooler_daemon_findings",
                arguments={"daemon": "project_coordinator", "category": "stalled_open_loop"},
                reason="Stalled signals — review open loop findings before testing",
            ))

    missing = _missing_inputs(signals)
    summary = _build_summary("tester", level, triggered)
    signature = _compute_signature(
        role="tester", level=level, signals=signals,
        triggered=triggered,
    )

    return StanceAdvisory(
        schema_version=1,
        role="tester",
        level=level,
        summary=summary,
        triggered_signals=sorted(triggered),
        missing_inputs=missing,
        threshold_crossings=crossings,
        advisory_signature=signature,
        signal_values=signals,
        stance=stance,
        actions=actions,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

_ROLE_FNS = {
    "planner": _planner_stance,
    "critic": _critic_stance,
    "tester": _tester_stance,
}


def pulse_to_stance(role: str, signals: StanceSignals) -> StanceAdvisory:
    """Compute advisory for a single role.

    Args:
        role: One of "planner", "critic", "tester".
        signals: Unified input signals.

    Returns:
        StanceAdvisory for the given role.

    Raises:
        ValueError: If role is not recognized.
    """
    fn = _ROLE_FNS.get(role)
    if fn is None:
        raise ValueError(f"Unknown stance role: {role!r}")
    return fn(signals)


def build_stance_advisories(
    snapshot: dict[str, Any] | None = None,
    *,
    coordinator_findings: list[dict[str, Any]] | None = None,
    trend_supersession_rate: float | None = None,
) -> list[StanceAdvisory]:
    """Compute advisories for all Phase 1 roles.

    Args:
        snapshot: Pulse snapshot dict, or None for degraded mode.
        coordinator_findings: List of coordinator finding dicts with
            ``category`` keys.
        trend_supersession_rate: Phase 2 input (unused in Phase 1).

    Returns:
        List of three StanceAdvisory objects (planner, critic, tester).
    """
    signals = extract_stance_signals(
        snapshot,
        coordinator_findings=coordinator_findings,
        trend_supersession_rate=trend_supersession_rate,
    )
    return [pulse_to_stance(role, signals) for role in ("planner", "critic", "tester")]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _merge_vectors(rows: list[StanceVector]) -> StanceVector:
    """Merge multiple StanceVector rows — max per dimension."""
    return StanceVector(
        retrieval_pressure=max(r.retrieval_pressure for r in rows),
        decision_caution=max(r.decision_caution for r in rows),
        critique_intensity=max(r.critique_intensity for r in rows),
        provenance_requirement=max(r.provenance_requirement for r in rows),
        handoff_bias=max(r.handoff_bias for r in rows),
        closure_pressure=max(r.closure_pressure for r in rows),
    )


def _missing_inputs(signals: StanceSignals) -> list[str]:
    """Return list of pulse signal names unavailable in degraded mode."""
    if signals.pulse_available:
        return []
    return list(_PULSE_SIGNAL_NAMES)


def _compute_signature(
    *,
    role: str,
    level: int,
    signals: StanceSignals,
    triggered: set[str],
) -> str:
    """Compute deterministic advisory signature for replace-on-change dedup.

    Inputs to the signature:
    - level (0, 1, 2)
    - pulse_available (bool)
    - sorted triggered_signals
    - coarsened threshold_crossings (bucket names, not raw values)

    Only signals in `triggered` contribute buckets. This prevents cross-role
    signal contamination: a critic signal crossing its threshold must not change
    the planner signature when planner logic is unaffected.
    """
    buckets = _coarsen_crossings(signals, signal_filter=triggered)
    parts = [
        role,
        str(level),
        str(signals.pulse_available),
        "|".join(sorted(triggered)),
        "|".join(sorted(buckets)),
    ]
    raw = "::".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _coarsen_crossings(
    signals: StanceSignals,
    signal_filter: set[str] | None = None,
) -> list[str]:
    """Produce coarsened bucket labels for signal dimensions.

    If signal_filter is provided, only include buckets for signals whose
    canonical name appears in the filter. Use this to restrict coarsening to
    role-relevant signals and avoid cross-role signature contamination.
    """
    def _include(name: str) -> bool:
        return signal_filter is None or name in signal_filter

    buckets: list[str] = []

    if _include("volatility_ratio"):
        vb = _bucket(signals.volatility_ratio, _VOLATILITY_SOFT, _VOLATILITY_HARD)
        if vb != "NONE":
            buckets.append(f"volatility_ratio:{vb}")

    if _include("stalled_thread_count"):
        stb = _bucket(signals.stalled_thread_count, _STALLED_SOFT, _STALLED_HARD)
        if stb != "NONE":
            buckets.append(f"stalled_thread_count:{stb}")

    if _include("open_loop_count"):
        olb = _bucket(signals.open_loop_count, _OPEN_LOOP_SOFT, _OPEN_LOOP_HARD)
        if olb != "NONE":
            buckets.append(f"open_loop_count:{olb}")

    if _include("risk_tag_count"):
        rtb = _bucket(signals.risk_tag_count, _RISK_TAG_SOFT, _RISK_TAG_HARD)
        if rtb != "NONE":
            buckets.append(f"risk_tag_count:{rtb}")

    if _include("coordinator_stalled_open_loop_count"):
        csb = _bucket(
            signals.coordinator_stalled_open_loop_count,
            _COORD_STALLED_SOFT, _COORD_STALLED_HARD,
        )
        if csb != "NONE":
            buckets.append(f"coord_stalled:{csb}")

    if _include("coordinator_role_concentration_count"):
        crb = _bucket(
            signals.coordinator_role_concentration_count,
            _COORD_ROLE_CONC_SOFT, _COORD_ROLE_CONC_HARD,
        )
        if crb != "NONE":
            buckets.append(f"coord_role_conc:{crb}")

    if _include("coordinator_dropout_count"):
        if signals.coordinator_dropout_count >= _COORD_DROPOUT_SOFT:
            buckets.append("coord_dropout:SOFT")

    if _include("coordinator_burst_count"):
        if signals.coordinator_burst_count >= _COORD_BURST_SOFT:
            buckets.append("coord_burst:SOFT")

    if _include("coordinator_new_contributor_count"):
        if signals.coordinator_new_contributor_count >= _COORD_NEW_CONTRIB_SOFT:
            buckets.append("coord_new_contributor:SOFT")

    if _include("analysis_stale"):
        if signals.analysis_report_available and not signals.analysis_is_fresh:
            buckets.append("analysis_stale:True")

    return buckets


_SUMMARY_PHRASES: dict[str, tuple[str, str]] = {
    "planner": ("moderate pressure — review prior context", "elevated pressure — caution advised"),
    "critic":  ("moderate scrutiny — verify provenance", "elevated scrutiny — thorough review needed"),
    "tester":  ("moderate caution — check coverage", "elevated caution — validate thoroughly"),
}


def _build_summary(role: str, level: int, triggered: set[str]) -> str:
    if level == 0:
        return f"{role.title()} stance clear — no signals above thresholds"
    mod, elev = _SUMMARY_PHRASES[role]
    signals_str = ", ".join(sorted(triggered))
    phrase = mod if level == 1 else elev
    return f"{role.title()}: {phrase} ({signals_str})"
