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
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Read-only tool allowlist — advisory actions must never suggest writes
# ---------------------------------------------------------------------------

_READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "watercooler_smart_query",
        "watercooler_read_thread",
        "watercooler_get_thread_entry",
        "watercooler_list_thread_entries",
        "watercooler_daemon_findings",
        "watercooler_search",
        "watercooler_list_decisions",
    }
)


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

# Decision-pipeline signal thresholds (open-core stance producer).
# These feed ``DecisionStanceDaemon`` and only contribute to a signature when
# their corresponding signal name appears in ``triggered`` for a given role.
_DEC_CANDIDATE_BACKLOG_SOFT = 3
_DEC_CANDIDATE_BACKLOG_HARD = 8
_DEC_REJECTION_RATIO_SOFT = 0.5
_DEC_REJECTION_RATIO_HARD = 0.8
_DEC_RATE_LIMITED_SOFT = 1  # SOFT-only: any rate-limit / cap finding is noteworthy


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

    phase: str  # "pre" or "post"
    tool: str  # MCP tool name
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
    # --- Open-core: from decision pipeline (DecisionStanceDaemon) ---
    # 24h rolling counts. Default 0 so existing callers and signatures are
    # unaffected; only ``DecisionStanceDaemon`` populates these.
    decision_candidate_high_count: int = 0
    decision_extraction_success_count: int = 0
    decision_extraction_rejected_count: int = 0
    decision_extraction_rate_limited_count: int = 0
    decisions_recorded_recent_count: int = 0


@dataclass(frozen=True)
class StanceAdvisory:
    """Complete advisory for one role.

    ``source_lead_ids`` is populated by one of two mutually-exclusive
    producers (the conflict gate at ``daemons/__init__.py`` ensures only one
    is active per deployment):

    - the premium ``ProjectCoordinatorDaemon`` from active ``coordinator_lead``
      findings whose v1A source category maps back to one of
      ``triggered_signals`` via ``_STANCE_SIGNAL_TO_LEAD_CATEGORIES``; or
    - the open-core ``DecisionStanceDaemon`` from ``decision_detector``
      (``decision_candidate`` findings filtered to detector confidence
      ``details.tier == "High"``) and ``decision_extractor`` findings
      whose category matches one of ``triggered_signals`` via
      ``_STANCE_SIGNAL_TO_DECISION_SOURCES``. (``details.tier`` is the
      decision-detector's candidate-confidence taxonomy and is unrelated
      to stance ``level`` or to memory tiers.)

    Premium IDs point to richer ``coordinator_lead`` findings (with a
    pre-built ``suggested_action``); open-core IDs point to raw signal
    findings that the agent must interpret itself. It is advisory
    provenance, not a complete audit trail, and is capped at
    ``_SOURCE_LEAD_IDS_CAP``. Default ``()`` preserves backward compatibility
    for existing callers that do not wire lead provenance.
    """

    schema_version: int
    role: str
    level: int  # 0-2
    summary: str
    triggered_signals: list[str]
    missing_inputs: list[str]
    threshold_crossings: list[str]
    advisory_signature: str
    signal_values: StanceSignals
    stance: StanceVector
    actions: list[AdvisoryAction]
    source_lead_ids: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Stance signal → v1A lead category mapping (3c-2 provenance)
# ---------------------------------------------------------------------------

# Maps coordinator_*_count signals to the v1A finding categories whose
# generated ``coordinator_lead`` findings should appear in
# ``StanceAdvisory.source_lead_ids`` when that signal is triggered.
#
# Non-coordinator signals in ``StanceSignals`` (``volatility_ratio``,
# ``risk_tag_count``, pulse-derived counts) are intentionally absent — they
# do not map to a coordinator lead category.
_STANCE_SIGNAL_TO_LEAD_CATEGORIES: dict[str, frozenset[str]] = {
    "coordinator_stalled_open_loop_count": frozenset({"stalled_open_loop"}),
    "coordinator_role_concentration_count": frozenset({"aware_role_concentration"}),
    "coordinator_dropout_count": frozenset({"stalled_dropout"}),
    "coordinator_burst_count": frozenset({"aware_burst"}),
    "coordinator_new_contributor_count": frozenset({"aware_new_contributor"}),
}

# Upper bound on ``StanceAdvisory.source_lead_ids`` entries. Provenance,
# not an audit trail — agents should query daemon findings for the full set.
_SOURCE_LEAD_IDS_CAP: int = 10


@dataclass(frozen=True)
class CoordinatorLead:
    """Thread-specific investigation hint derived from a v1A coordinator finding.

    Leads tell agents *which thread deserves attention right now* and *what tool
    call gets them started*. The payload is stored under
    ``CoordinatorFinding.details["lead"]`` via ``dataclasses.asdict()``.

    Attributes:
        schema_version: Outer lead envelope version. Currently always ``1``.
            Distinct from ``t2_context["schema_version"]``, which tracks the
            nested analysis context schema independently (``2`` as of Phase 3b-1).
            Bump this field only when the top-level lead serialization format changes.
        source_category: v1A category that triggered this lead (e.g. ``stalled_open_loop``).
        source_topic: Thread topic slug. Duplicates outer ``CoordinatorFinding.topic``
            so the lead core is self-contained for downstream consumers (e.g.
            ``PulseReportDaemon``) that read serialized leads without the outer envelope.
        summary: Human-readable description of what is interesting and why.
        relevance_tags: Roles/focus areas most affected. Tuple so the dataclass stays frozen-safe.
        suggested_action: Read-only ``AdvisoryAction`` the agent can execute to
            investigate. ``None`` if reconstruction from a persisted dict failed.
        t2_context: Populated in Phase 2+ when ``AnalysisSnapshotDaemon`` data is available
            for the lead's ``source_topic``. ``None`` when analysis data is unavailable or
            the daemon has not yet run. Schema: see ``_build_t2_context()`` in
            ``project_coordinator_lib.py``.
    """

    schema_version: int
    source_category: str
    source_topic: str
    summary: str
    relevance_tags: tuple[str, ...]
    suggested_action: AdvisoryAction | None
    t2_context: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CoordinatorLead":
        """Reconstruct a CoordinatorLead from ``asdict()`` output.

        Top-level fields use ``.get()`` with safe defaults so a missing
        non-load-bearing field (e.g. future schema additions read by old code)
        does not raise ``KeyError``. The ``suggested_action`` sub-dict is handled
        with a stricter rule: if any of its required fields is missing, or the
        tool is not in ``_READ_ONLY_TOOLS``, reconstruction returns
        ``suggested_action=None`` rather than attempting partial instantiation.
        This avoids a ``ValueError`` from ``AdvisoryAction.__post_init__`` on
        malformed records.

        Note: Raw-passthrough consumers (e.g. CoordinatorRefinerDaemon) read
        ``suggested_action`` and ``t2_context`` directly from the source dict
        before calling ``from_dict()``, to preserve verbatim wire shape across
        schema migrations. Do not remove this note.
        """
        action: AdvisoryAction | None = None
        a = d.get("suggested_action")
        if isinstance(a, dict):
            required = ("phase", "tool", "arguments", "reason")
            if all(k in a for k in required):
                try:
                    action = AdvisoryAction(
                        phase=a["phase"],
                        tool=a["tool"],
                        arguments=a["arguments"],
                        reason=a["reason"],
                    )
                except ValueError:
                    action = None
        t2_context = d.get("t2_context")
        # v1 → v2 migration: rename "stalled" to "analysis_stalled" on read.
        # Discriminant is field presence, not schema_version — each migration block
        # must remain idempotent (safe to apply to already-migrated data).
        if (
            isinstance(t2_context, dict)
            and "stalled" in t2_context
            and "analysis_stalled" not in t2_context
        ):
            t2_context = dict(t2_context)
            t2_context["analysis_stalled"] = t2_context.pop("stalled")
            t2_context["schema_version"] = 2
        return cls(
            schema_version=d.get("schema_version", 1),
            source_category=d.get("source_category", ""),
            source_topic=d.get("source_topic", ""),
            summary=d.get("summary", ""),
            relevance_tags=tuple(d.get("relevance_tags") or ()),
            suggested_action=action,
            t2_context=t2_context,
        )


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
            focus_area_overlap_count=len(repo_level.get("focus_area_overlap", [])),
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
        coordinator_stalled_open_loop_count=coord_counts.get("stalled_open_loop", 0),
        coordinator_role_concentration_count=coord_counts.get(
            "aware_role_concentration", 0
        ),
        coordinator_dropout_count=coord_counts.get("stalled_dropout", 0),
        coordinator_burst_count=coord_counts.get("aware_burst", 0),
        coordinator_new_contributor_count=coord_counts.get("aware_new_contributor", 0),
        trend_supersession_rate=trend_supersession_rate,
    )


# Extractor finding categories that count as "rejected" for the rejection ratio.
# Sourced from ``decision_extractor.py`` finding emissions.
_DEC_REJECTION_CATEGORIES: frozenset[str] = frozenset(
    {
        "extraction_rejected",
        "extraction_failed",
        "extraction_parse_failure",
    }
)

# Extractor finding categories that count as "rate-limited" capacity exhaustion.
_DEC_RATE_LIMIT_CATEGORIES: frozenset[str] = frozenset(
    {
        "extraction_rate_limited",
        "extraction_cap_reached",
    }
)


# Open-core decision-pipeline analogue of ``_STANCE_SIGNAL_TO_LEAD_CATEGORIES``.
# Maps each decision-pipeline ``StanceSignals`` field name to a
# ``(daemon_name, set-of-categories)`` pair used to filter source-finding IDs
# that contributed to the elevation. ``DecisionStanceDaemon`` populates
# ``StanceAdvisory.source_lead_ids`` from these.
#
# Detector findings are additionally filtered to ``details.tier == "High"`` —
# only ``High`` decision-detector confidence candidates contribute to
# ``decision_candidate_high_count``. (``details.tier`` here is the detector's
# own High/Medium/Low candidate-confidence taxonomy, not stance ``level``.)
_STANCE_SIGNAL_TO_DECISION_SOURCES: dict[str, tuple[str, frozenset[str]]] = {
    "decision_candidate_high_count": (
        "decision_detector",
        frozenset({"decision_candidate"}),
    ),
    "decision_extraction_rejected_count": (
        "decision_extractor",
        _DEC_REJECTION_CATEGORIES,
    ),
    "decision_extraction_rate_limited_count": (
        "decision_extractor",
        _DEC_RATE_LIMIT_CATEGORIES,
    ),
    "decision_extraction_success_count": (
        "decision_extractor",
        frozenset({"extraction_success"}),
    ),
    # Phase 1 proxy — same source as success_count. The Phase 2 baseline-graph
    # scan will widen this to include hand-authored Decision entry IDs.
    "decisions_recorded_recent_count": (
        "decision_extractor",
        frozenset({"extraction_success"}),
    ),
}


def resolve_decision_source_ids(
    *,
    triggered_signals: Iterable[str],
    detector_findings: list[dict[str, Any]],
    extractor_findings: list[dict[str, Any]],
) -> tuple[tuple[str, ...], bool]:
    """Resolve ``source_lead_ids`` for a decision-pipeline-driven advisory.

    Walks ``triggered_signals`` and collects ``finding_id`` values from the
    matching findings, filtered by the categories in
    ``_STANCE_SIGNAL_TO_DECISION_SOURCES``. Detector findings are further
    filtered to ``details.tier == "High"`` — i.e., only the detector's
    high-confidence candidates contribute. Dedups across signals and caps
    at ``_SOURCE_LEAD_IDS_CAP``.

    Args:
        triggered_signals: Names of ``StanceSignals`` fields that crossed
            thresholds for one role (from ``StanceAdvisory.triggered_signals``).
        detector_findings: ``decision_candidate`` findings within the
            daemon's rolling window — must include ``finding_id``,
            ``category``, and ``details`` (with the detector's confidence
            ``tier``: ``"High"`` / ``"Medium"`` / ``"Low"``).
        extractor_findings: ``decision_extractor`` findings within the
            same window — must include ``finding_id`` and ``category``.

    Returns:
        ``(ids, truncated)`` where ``ids`` is a deduped, cap-bounded tuple
        of finding ULIDs in encounter order, and ``truncated`` is True iff
        the cap was hit (i.e. additional matching IDs were dropped).
    """
    collected: list[str] = []
    seen: set[str] = set()
    for sig in triggered_signals:
        spec = _STANCE_SIGNAL_TO_DECISION_SOURCES.get(sig)
        if spec is None:
            continue
        daemon, cats = spec
        pool = (
            detector_findings if daemon == "decision_detector" else extractor_findings
        )
        for f in pool:
            if f.get("category") not in cats:
                continue
            if daemon == "decision_detector":
                tier = (f.get("details") or {}).get("tier")
                if tier != "High":
                    continue
            fid = f.get("finding_id") or ""
            if fid and fid not in seen:
                seen.add(fid)
                collected.append(fid)
    truncated = len(collected) > _SOURCE_LEAD_IDS_CAP
    return tuple(collected[:_SOURCE_LEAD_IDS_CAP]), truncated


def extract_decision_stance_signals(
    *,
    detector_findings: list[dict[str, Any]] | None = None,
    extractor_findings: list[dict[str, Any]] | None = None,
    recent_decisions_count: int = 0,
) -> StanceSignals:
    """Build StanceSignals from decision-pipeline findings (open-core path).

    Pulse and coordinator signals stay at their defaults. ``pulse_available`` is
    ``False``, so the existing pulse-derived rows in role functions are inert.

    Args:
        detector_findings: ``decision_candidate`` findings (typically already
            time-filtered to the daemon's rolling window).
        extractor_findings: Extractor findings of any category from the same
            window. Categorized internally — callers do not pre-filter.
        recent_decisions_count: Count of extracted decisions recorded in the
            same rolling window (typically from ``list_decisions(only_extracted
            =True, since=now-window)``).

    Returns:
        StanceSignals with the five decision-pipeline fields populated. Pulse
        and coordinator fields stay at defaults.
    """
    detector_findings = detector_findings or []
    extractor_findings = extractor_findings or []

    high_tier_count = 0
    for f in detector_findings:
        details = f.get("details") or {}
        tier = details.get("tier")
        if tier == "High":
            high_tier_count += 1

    success_count = 0
    rejected_count = 0
    rate_limited_count = 0
    for f in extractor_findings:
        cat = f.get("category", "")
        if cat == "extraction_success":
            success_count += 1
        elif cat in _DEC_REJECTION_CATEGORIES:
            rejected_count += 1
        elif cat in _DEC_RATE_LIMIT_CATEGORIES:
            rate_limited_count += 1

    return StanceSignals(
        pulse_available=False,
        decision_candidate_high_count=high_tier_count,
        decision_extraction_success_count=success_count,
        decision_extraction_rejected_count=rejected_count,
        decision_extraction_rate_limited_count=rate_limited_count,
        decisions_recorded_recent_count=max(0, int(recent_decisions_count)),
    )


def _decision_rejection_ratio(signals: StanceSignals) -> float:
    """Rejected / (success + rejected). Returns 0.0 when neither has fired."""
    total = (
        signals.decision_extraction_success_count
        + signals.decision_extraction_rejected_count
    )
    if total == 0:
        return 0.0
    return signals.decision_extraction_rejected_count / total


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
        rows.append(
            StanceVector(
                retrieval_pressure=0.9,
                decision_caution=0.8,
                closure_pressure=0.3,
            )
        )
        triggered.add("volatility_ratio")
        crossings.append(f"volatility_ratio={v:.2f} >= HARD({_VOLATILITY_HARD})")
    elif v >= _VOLATILITY_SOFT:
        if sol >= _COORD_STALLED_SOFT:
            level = max(level, 2)
            rows.append(
                StanceVector(
                    retrieval_pressure=0.8,
                    decision_caution=0.7,
                    closure_pressure=0.5,
                )
            )
            triggered.update(
                {"volatility_ratio", "coordinator_stalled_open_loop_count"}
            )
            crossings.append(f"volatility_ratio={v:.2f} >= SOFT({_VOLATILITY_SOFT})")
            crossings.append(
                f"coordinator_stalled_open_loop_count={sol} >= SOFT({_COORD_STALLED_SOFT})"
            )
        else:
            level = max(level, 1)
            rows.append(
                StanceVector(
                    retrieval_pressure=0.6,
                    decision_caution=0.5,
                    closure_pressure=0.0,
                )
            )
            triggered.add("volatility_ratio")
            crossings.append(f"volatility_ratio={v:.2f} >= SOFT({_VOLATILITY_SOFT})")

    if sol >= _COORD_STALLED_HARD:
        level = max(level, 2)
        rows.append(
            StanceVector(
                retrieval_pressure=0.5,
                decision_caution=0.3,
                closure_pressure=0.9,
            )
        )
        triggered.add("coordinator_stalled_open_loop_count")
        crossings.append(
            f"coordinator_stalled_open_loop_count={sol} >= HARD({_COORD_STALLED_HARD})"
        )
    elif sol >= _COORD_STALLED_SOFT and v < _VOLATILITY_SOFT:
        level = max(level, 1)
        rows.append(
            StanceVector(
                retrieval_pressure=0.3,
                decision_caution=0.0,
                closure_pressure=0.6,
            )
        )
        triggered.add("coordinator_stalled_open_loop_count")
        crossings.append(
            f"coordinator_stalled_open_loop_count={sol} >= SOFT({_COORD_STALLED_SOFT})"
        )

    if rc >= _COORD_ROLE_CONC_HARD:
        level = max(level, 2)
        rows.append(
            StanceVector(
                retrieval_pressure=0.7,
                decision_caution=0.6,
                closure_pressure=0.4,
            )
        )
        triggered.add("coordinator_role_concentration_count")
        crossings.append(
            f"coordinator_role_concentration_count={rc} >= HARD({_COORD_ROLE_CONC_HARD})"
        )
    elif rc >= _COORD_ROLE_CONC_SOFT:
        level = max(level, 1)
        rows.append(
            StanceVector(
                retrieval_pressure=0.4,
                decision_caution=0.3,
                closure_pressure=0.2,
            )
        )
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

    # Decision-pipeline rows (open-core path). The hard-rejection-ratio row
    # signals that the extractor keeps failing validity gates — the planner
    # should ground proposals in prior decisions before adding new ones.
    rej_ratio = _decision_rejection_ratio(signals)
    if rej_ratio >= _DEC_REJECTION_RATIO_HARD:
        level = max(level, 1)
        rows.append(StanceVector(retrieval_pressure=0.5, decision_caution=0.7))
        triggered.add("decision_extraction_rejected_count")
        crossings.append(
            f"decision_rejection_ratio={rej_ratio:.2f} >= HARD({_DEC_REJECTION_RATIO_HARD})"
        )

    rl = signals.decision_extraction_rate_limited_count
    if rl >= _DEC_RATE_LIMITED_SOFT:
        level = max(level, 1)
        rows.append(StanceVector(decision_caution=0.4))
        triggered.add("decision_extraction_rate_limited_count")
        crossings.append(
            f"decision_extraction_rate_limited_count={rl} >= SOFT({_DEC_RATE_LIMITED_SOFT})"
        )

    # Merge rows: max per dimension
    stance = _merge_vectors(rows) if rows else StanceVector()

    # Build actions
    # code_path is intentionally omitted from action arguments: advisory actions
    # are repo-agnostic read queries; callers should inject code_path from their
    # session context if multi-repo scope isolation is needed.
    actions: list[AdvisoryAction] = []
    if level >= 1:
        actions.append(
            AdvisoryAction(
                phase="pre",
                tool="watercooler_smart_query",
                arguments={"query": "recent decisions and open questions"},
                reason="High volatility or stalled loops — review prior decisions before planning",
            )
        )
    if "coordinator_new_contributor_count" in triggered:
        actions.append(
            AdvisoryAction(
                phase="pre",
                tool="watercooler_daemon_findings",
                arguments={
                    "daemon": "project_coordinator",
                    "category": "aware_new_contributor",
                },
                reason="New contributor detected — review their context before planning",
            )
        )
    if level >= 2:
        actions.append(
            AdvisoryAction(
                phase="pre",
                tool="watercooler_daemon_findings",
                arguments={
                    "daemon": "project_coordinator",
                    "category": "stalled_open_loop",
                },
                reason="Multiple stalled loops — check which threads need closure",
            )
        )
    if "decision_extraction_rejected_count" in triggered:
        actions.append(
            AdvisoryAction(
                phase="pre",
                tool="watercooler_daemon_findings",
                arguments={
                    "daemon": "decision_extractor",
                    "category": "extraction_rejected",
                },
                reason="Extractor failing validity gates — review rejection reasons before proposing",
            )
        )

    missing = _missing_inputs(signals)
    summary = _build_summary("planner", level, triggered)
    signature = _compute_signature(
        role="planner",
        level=level,
        signals=signals,
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
        rows.append(
            StanceVector(
                critique_intensity=0.8,
                provenance_requirement=0.8,
                retrieval_pressure=0.5,
            )
        )
        triggered.add("risk_tag_count")
        crossings.append(f"risk_tag_count={rt} >= HARD({_RISK_TAG_HARD})")
    elif rt >= _RISK_TAG_SOFT:
        if cd >= _COORD_DROPOUT_SOFT:
            level = max(level, 2)
            rows.append(
                StanceVector(
                    critique_intensity=0.7,
                    provenance_requirement=0.7,
                    retrieval_pressure=0.6,
                )
            )
            triggered.update({"risk_tag_count", "coordinator_dropout_count"})
            crossings.append(f"risk_tag_count={rt} >= SOFT({_RISK_TAG_SOFT})")
            crossings.append(
                f"coordinator_dropout_count={cd} >= SOFT({_COORD_DROPOUT_SOFT})"
            )
        else:
            level = max(level, 1)
            rows.append(
                StanceVector(
                    critique_intensity=0.5,
                    provenance_requirement=0.4,
                    retrieval_pressure=0.3,
                )
            )
            triggered.add("risk_tag_count")
            crossings.append(f"risk_tag_count={rt} >= SOFT({_RISK_TAG_SOFT})")

    if cd >= _COORD_DROPOUT_SOFT and rt < _RISK_TAG_SOFT:
        level = max(level, 1)
        rows.append(
            StanceVector(
                critique_intensity=0.3,
                provenance_requirement=0.6,
                retrieval_pressure=0.5,
            )
        )
        triggered.add("coordinator_dropout_count")
        crossings.append(
            f"coordinator_dropout_count={cd} >= SOFT({_COORD_DROPOUT_SOFT})"
        )

    if ol >= _OPEN_LOOP_HARD:
        level = max(level, 2)
        rows.append(
            StanceVector(
                critique_intensity=0.6,
                provenance_requirement=0.5,
                retrieval_pressure=0.8,
            )
        )
        triggered.add("open_loop_count")
        crossings.append(f"open_loop_count={ol} >= HARD({_OPEN_LOOP_HARD})")
    elif ol >= _OPEN_LOOP_SOFT:
        level = max(level, 1)
        rows.append(
            StanceVector(
                critique_intensity=0.3,
                provenance_requirement=0.3,
                retrieval_pressure=0.4,
            )
        )
        triggered.add("open_loop_count")
        crossings.append(f"open_loop_count={ol} >= SOFT({_OPEN_LOOP_SOFT})")

    # Decision-pipeline rows (open-core path). High-confidence
    # decision-detector candidate backlog and validity-gate rejection ratios
    # both signal that an extra layer of scrutiny is warranted on what counts
    # as a real decision.
    dch = signals.decision_candidate_high_count
    if dch >= _DEC_CANDIDATE_BACKLOG_HARD:
        level = max(level, 2)
        rows.append(StanceVector(critique_intensity=0.7, provenance_requirement=0.6))
        triggered.add("decision_candidate_high_count")
        crossings.append(
            f"decision_candidate_high_count={dch} >= HARD({_DEC_CANDIDATE_BACKLOG_HARD})"
        )
    elif dch >= _DEC_CANDIDATE_BACKLOG_SOFT:
        level = max(level, 1)
        rows.append(StanceVector(critique_intensity=0.4, provenance_requirement=0.3))
        triggered.add("decision_candidate_high_count")
        crossings.append(
            f"decision_candidate_high_count={dch} >= SOFT({_DEC_CANDIDATE_BACKLOG_SOFT})"
        )

    rej_ratio = _decision_rejection_ratio(signals)
    if rej_ratio >= _DEC_REJECTION_RATIO_SOFT:
        level = max(level, 1)
        rows.append(StanceVector(provenance_requirement=0.5, critique_intensity=0.3))
        triggered.add("decision_extraction_rejected_count")
        crossings.append(
            f"decision_rejection_ratio={rej_ratio:.2f} >= SOFT({_DEC_REJECTION_RATIO_SOFT})"
        )

    stance = _merge_vectors(rows) if rows else StanceVector()

    actions: list[AdvisoryAction] = []
    if level >= 1:
        actions.append(
            AdvisoryAction(
                phase="pre",
                tool="watercooler_search",
                arguments={"query": "risk problem unresolved", "mode": "entries"},
                reason="Risk signals detected — search for unresolved issues before critiquing",
            )
        )
    if (
        "decision_candidate_high_count" in triggered
        or "decision_extraction_rejected_count" in triggered
    ):
        actions.append(
            AdvisoryAction(
                phase="pre",
                tool="watercooler_daemon_findings",
                arguments={
                    "daemon": "decision_extractor",
                    "category": "extraction_rejected",
                },
                reason="Decision backlog or extraction rejections — pressure-test which candidates are real",
            )
        )

    missing = _missing_inputs(signals)
    summary = _build_summary("critic", level, triggered)
    signature = _compute_signature(
        role="critic",
        level=level,
        signals=signals,
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
    analysis_stale = signals.analysis_report_available and not signals.analysis_is_fresh
    cb = signals.coordinator_burst_count

    triggered: set[str] = set()
    crossings: list[str] = []
    rows: list[StanceVector] = []
    level = 0

    if st >= _STALLED_HARD:
        level = max(level, 2)
        rows.append(
            StanceVector(
                retrieval_pressure=0.7,
                provenance_requirement=0.5,
                handoff_bias=0.6,
            )
        )
        triggered.add("stalled_thread_count")
        crossings.append(f"stalled_thread_count={st} >= HARD({_STALLED_HARD})")
    elif st >= _STALLED_SOFT:
        if analysis_stale:
            level = max(level, 2)
            rows.append(
                StanceVector(
                    retrieval_pressure=0.6,
                    provenance_requirement=0.8,
                    handoff_bias=0.4,
                )
            )
            triggered.update({"stalled_thread_count", "analysis_stale"})
            crossings.append(f"stalled_thread_count={st} >= SOFT({_STALLED_SOFT})")
            crossings.append("analysis_stale=True")
        else:
            level = max(level, 1)
            rows.append(
                StanceVector(
                    retrieval_pressure=0.5,
                    provenance_requirement=0.3,
                    handoff_bias=0.0,
                )
            )
            triggered.add("stalled_thread_count")
            crossings.append(f"stalled_thread_count={st} >= SOFT({_STALLED_SOFT})")

    if analysis_stale and "analysis_stale" not in triggered:
        # Standalone analysis_stale signal — only fires when not already handled
        # by the joint STALLED_SOFT + analysis_stale branch above (which sets
        # level=2 and appends the crossing itself to avoid a duplicate entry).
        level = max(level, 1)
        rows.append(
            StanceVector(
                retrieval_pressure=0.3,
                provenance_requirement=0.7,
                handoff_bias=0.0,
            )
        )
        triggered.add("analysis_stale")
        crossings.append("analysis_stale=True")

    if cb >= _COORD_BURST_SOFT:
        level = max(level, 1)
        rows.append(
            StanceVector(
                retrieval_pressure=0.4,
                provenance_requirement=0.3,
                handoff_bias=0.5,
            )
        )
        triggered.add("coordinator_burst_count")
        crossings.append(f"coordinator_burst_count={cb} >= SOFT({_COORD_BURST_SOFT})")

    # Decision-pipeline drought row (open-core path). When candidates are
    # piling up but nothing has been formally recorded, the tester should
    # treat verbal/implicit decisions skeptically and surface them for capture
    # before relying on them in test design.
    dch = signals.decision_candidate_high_count
    drr = signals.decisions_recorded_recent_count
    if drr == 0 and dch >= _DEC_CANDIDATE_BACKLOG_SOFT:
        level = max(level, 1)
        rows.append(StanceVector(provenance_requirement=0.5, handoff_bias=0.4))
        triggered.add("decisions_recorded_recent_count")
        triggered.add("decision_candidate_high_count")
        crossings.append(
            f"decisions_recorded_recent_count={drr} == 0 with "
            f"decision_candidate_high_count={dch} >= SOFT({_DEC_CANDIDATE_BACKLOG_SOFT})"
        )

    stance = _merge_vectors(rows) if rows else StanceVector()

    # P2.2: route the action to the signal that actually elevated the tester.
    # Static `stalled_open_loop` always-on was wrong when burst was the only trigger.
    actions: list[AdvisoryAction] = []
    if level >= 1:
        burst_active = signals.coordinator_burst_count >= _COORD_BURST_SOFT
        stalled_active = signals.stalled_thread_count >= _STALLED_SOFT or analysis_stale
        if burst_active:
            actions.append(
                AdvisoryAction(
                    phase="pre",
                    tool="watercooler_daemon_findings",
                    arguments={
                        "daemon": "project_coordinator",
                        "category": "aware_burst",
                    },
                    reason="Burst activity detected — check which thread is spiking before testing",
                )
            )
        if stalled_active:
            actions.append(
                AdvisoryAction(
                    phase="pre",
                    tool="watercooler_daemon_findings",
                    arguments={
                        "daemon": "project_coordinator",
                        "category": "stalled_open_loop",
                    },
                    reason="Stalled signals — review open loop findings before testing",
                )
            )
    if "decisions_recorded_recent_count" in triggered:
        actions.append(
            AdvisoryAction(
                phase="pre",
                tool="watercooler_list_decisions",
                arguments={"only_extracted": True, "confidence_min": 3},
                reason="Decision drought with active candidates — verify what's been ratified before testing",
            )
        )

    missing = _missing_inputs(signals)
    summary = _build_summary("tester", level, triggered)
    signature = _compute_signature(
        role="tester",
        level=level,
        signals=signals,
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

# Fixed protocol surface: stance modulation is emitted for these three
# canonical roles only. Custom roles in .watercooler/roles.toml are vocabulary
# for entry authoring, not stance participants. Keep ``STANCE_ROLES`` and
# ``_ROLE_FNS`` aligned — a drift test in tests/unit/test_decision_stance_daemon.py
# enforces this. See docs/DAEMONS.md#decision-stance-decision_stance.
STANCE_ROLES: tuple[str, ...] = ("planner", "critic", "tester")

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
            _COORD_STALLED_SOFT,
            _COORD_STALLED_HARD,
        )
        if csb != "NONE":
            buckets.append(f"coord_stalled:{csb}")

    if _include("coordinator_role_concentration_count"):
        crb = _bucket(
            signals.coordinator_role_concentration_count,
            _COORD_ROLE_CONC_SOFT,
            _COORD_ROLE_CONC_HARD,
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

    if _include("decision_candidate_high_count"):
        dcb = _bucket(
            signals.decision_candidate_high_count,
            _DEC_CANDIDATE_BACKLOG_SOFT,
            _DEC_CANDIDATE_BACKLOG_HARD,
        )
        if dcb != "NONE":
            buckets.append(f"decision_candidate_backlog:{dcb}")

    if _include("decision_extraction_rejected_count"):
        rrb = _bucket(
            _decision_rejection_ratio(signals),
            _DEC_REJECTION_RATIO_SOFT,
            _DEC_REJECTION_RATIO_HARD,
        )
        if rrb != "NONE":
            buckets.append(f"decision_rejection_ratio:{rrb}")

    if _include("decision_extraction_rate_limited_count"):
        if signals.decision_extraction_rate_limited_count >= _DEC_RATE_LIMITED_SOFT:
            buckets.append("decision_rate_limited:SOFT")

    if _include("decisions_recorded_recent_count"):
        if signals.decisions_recorded_recent_count == 0:
            buckets.append("decision_drought:True")

    return buckets


_SUMMARY_PHRASES: dict[str, tuple[str, str]] = {
    "planner": (
        "moderate pressure — review prior context",
        "elevated pressure — caution advised",
    ),
    "critic": (
        "moderate scrutiny — verify provenance",
        "elevated scrutiny — thorough review needed",
    ),
    "tester": (
        "moderate caution — check coverage",
        "elevated caution — validate thoroughly",
    ),
}


def _build_summary(role: str, level: int, triggered: set[str]) -> str:
    if level == 0:
        return f"{role.title()} stance clear — no signals above thresholds"
    mod, elev = _SUMMARY_PHRASES[role]
    signals_str = ", ".join(sorted(triggered))
    phrase = mod if level == 1 else elev
    return f"{role.title()}: {phrase} ({signals_str})"
