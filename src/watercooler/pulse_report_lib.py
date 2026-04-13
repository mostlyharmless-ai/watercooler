"""Shared library for Project Pulse report assembly.

Pure Python — no MCP, no subprocess calls.
Importable by the project-pulse skill, daemons, or any other caller.

Data acquisition stays in the caller. This module accepts normalised data
structs (``PulseReportInputs``) and renders a ``PulseReport``.

Public API
----------
Data types:
- ``ContributorSummary``, ``CorpusSummary``        — Signal 1 inputs
- ``PulseBlock``, ``AnalysisFeed``                 — Signal 2 inputs
- ``DecisionPipelineStatus``                        — Signal 2 inputs
- ``TrendSignals``                                  — Signal 3 inputs
- ``SignalStatus``, ``RunStatus``                   — metadata
- ``PulseReportInputs``, ``PulseReport``            — top-level I/O

File providers (acquire data from disk; return normalised types):
- ``load_analysis_feed_from_file(json_path, ...)`` — parse pulse_block JSON
- ``load_decision_pipeline_status(reports_dir, ...)`` — scan decision reports

Report assembly:
- ``assemble_report(inputs, ...)`` → ``PulseReport``

Optional LLM synthesis (report-time):
- ``synthesize_executive_summary(inputs, llm_client, ...)`` → ``str | None``
    See PulseReportDaemon module docstring for report-time vs. snapshot-time
    distinction.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class LLMClient(Protocol):
    """Duck-typed interface for optional LLM enrichment.

    Implementations must provide a ``complete`` method. No ``watercooler_mcp``
    import is required — daemons and skills supply their own concrete clients.
    """

    def complete(self, prompt: str, system: str, max_tokens: int) -> str | None: ...


# ---------------------------------------------------------------------------
# Render limits
# ---------------------------------------------------------------------------

# Maximum stalled threads shown in T1 and T2 body sections.  The executive
# summary still reports the *total* count — only the per-item listings are
# capped to keep reports scannable.
_MAX_STALLED_SHOWN = 10

# Dimension name → display label (used in _render_signal3_section).
# Internal Python field names must never appear in rendered output.
_DIM_DISPLAY: dict[str, str] = {
    "goal_clarity": "goal clarity",
    "constraint_pressure": "constraint pressure",
    "evidence_quality": "evidence quality",
    "execution_momentum": "execution momentum",
}

# Dimension → level label → prose translation (renderer-only presentation layer).
_DIM_LEVEL_PROSE: dict[str, dict[str, str]] = {
    "goal_clarity": {"low": "diffuse", "mixed": "forming", "high": "crisp"},
    "constraint_pressure": {"low": "loose", "mixed": "moderate", "high": "tight"},
    "evidence_quality": {"low": "weak", "mixed": "mixed", "high": "strong"},
    "execution_momentum": {"low": "stalled", "mixed": "probing", "high": "driving"},
}

# Suppress-band label map: strong/extreme labels → hedged equivalents.
# Applied when confidence < 0.40 (suppress band) to avoid overclaiming precision.
_SUPPRESS_LABEL: dict[str, str] = {
    "high": "mixed",
    "crisp": "appears crisp",
    "tight": "seems tight",
    "strong": "appears strong",
    "driving": "may be driving",
}

# Ordered dimension list for rendering — mirrors _DIM_LEVEL_PROSE key order.
_DIMS_ORDERED = [
    "goal_clarity",
    "constraint_pressure",
    "evidence_quality",
    "execution_momentum",
]


# ---------------------------------------------------------------------------
# Signal 1 data types
# ---------------------------------------------------------------------------


@dataclass
class ContributorSummary:
    """Per-contributor session activity summary (Signal 1)."""

    name: str
    session_count: int
    last_active: str  # ISO 8601
    focus_areas: list[str]
    recent_observations: list[dict[str, Any]]  # [{kind, text, session_timestamp}]
    open_loops: list[str]


@dataclass
class CorpusSummary:
    """Aggregate corpus statistics (Signal 1)."""

    session_context_threads: int
    total_entries_scanned: int
    sessions_in_window: int


# ---------------------------------------------------------------------------
# Signal 2 data types
# ---------------------------------------------------------------------------


@dataclass
class CoordinationRisk:
    """Single coordination risk from pulse_block."""

    rule_id: str
    text: str
    confidence: float
    affected_threads: list[str]


@dataclass
class StalledThreadInfo:
    """Thread with no recent activity."""

    topic: str
    days_since_last: int
    last_entry_timestamp: str | None


def stalled_thread_from_snapshot(record: dict[str, Any]) -> "StalledThreadInfo":
    """Adapt a pulse_snapshot_lib stalled-thread record to StalledThreadInfo.

    ``pulse_snapshot_lib.compute_stalled_threads()`` emits ``days_stale`` and
    ``last_entry_at``. ``StalledThreadInfo`` uses ``days_since_last`` and
    ``last_entry_timestamp``. Both key variants are accepted so callers do not
    need to pre-normalize snapshot output.
    """
    return StalledThreadInfo(
        topic=record["topic"],
        days_since_last=record.get("days_since_last", record.get("days_stale", 0)),
        last_entry_timestamp=record.get(
            "last_entry_timestamp", record.get("last_entry_at")
        ),
    )


@dataclass
class RecommendedPairing:
    """Recommended contributor pairing from pulse_block."""

    contributor: str
    recommended_partner: str | None
    reason: str
    rule_id: str


@dataclass
class TopAction:
    """High-priority action from pulse_block."""

    rule_id: str
    text: str
    confidence: float
    priority: str


@dataclass
class PulseBlock:
    """Structured analysis contract (v1.x) from watercooler-analysis."""

    pulse_block_version: str
    coordination_risks: list[CoordinationRisk]
    stalled_threads: list[StalledThreadInfo]
    recommended_pairings: list[RecommendedPairing]
    top_actions: list[TopAction]
    workflow_shape_distribution: dict[str, dict[str, Any]]


@dataclass
class AnalysisFeed:
    """Normalised Signal 2 analysis input."""

    pulse_block: PulseBlock | None
    report_path: str | None  # source file path or checkpoint key
    report_age_days: float | None
    is_fresh: bool
    degraded: bool
    degraded_reason: str = ""  # non-empty when degraded=True


@dataclass
class DecisionPipelineStatus:
    """Decision pipeline health summary."""

    recent_decision_count: int
    recent_decision_titles: list[str]  # up to 3 most recent
    detection_report_path: str | None
    detection_report_age_days: float | None
    is_detection_fresh: bool
    # Daemon-sourced fields (populated when decision_detector daemon is running)
    daemon_is_running: bool = False
    daemon_findings_count: int = 0
    daemon_findings_sample: list[str] = field(default_factory=list)  # top 3 messages


# ---------------------------------------------------------------------------
# Signal 3 data types
# ---------------------------------------------------------------------------


@dataclass
class TrendSignals:
    """Signal 3 trend signals — graph volatility metrics (experimental, sample-based)."""

    supersession_rate: float  # 0.0–1.0
    active_fact_count: int
    superseded_fact_count: int
    sample_size: int
    top_volatile_topics: list[str]
    top_stable_topics: list[str]
    trend_direction: str  # "improving" | "degrading" | "stable"


# ---------------------------------------------------------------------------
# Status / metadata types
# ---------------------------------------------------------------------------


class SignalStatus(str, Enum):
    OK = "ok"
    STALE = "stale"
    NO_DATA = "no data"
    UNAVAILABLE = "unavailable"


@dataclass
class RunStatus:
    """Per-signal-block run outcomes."""

    signal1: SignalStatus
    signal2: SignalStatus
    signal3: SignalStatus
    window_days: int
    branch: str
    generated_at: str  # ISO 8601


# ---------------------------------------------------------------------------
# Top-level I/O types
# ---------------------------------------------------------------------------


@dataclass
class PulseReportInputs:
    """All inputs needed to assemble a pulse report.

    Data acquisition (MCP queries, subprocess calls) is the caller's
    responsibility. This struct accepts only normalised values.
    """

    # Signal 1
    contributors: dict[str, ContributorSummary]
    corpus: CorpusSummary | None
    queue_pending: int
    window_days: int
    branch: str
    stalled_threads: list[StalledThreadInfo]
    # Used by Signal 1 rendering always. When Signal 2 pulse_block is available and non-degraded,
    # Signal 2 uses pulse_block.stalled_threads; this field serves as Signal 2 fallback only.
    # Signal 2
    analysis_feed: AnalysisFeed | None
    decision_pipeline: DecisionPipelineStatus | None
    # Signal 3
    trend_signals: TrendSignals | None
    # Metadata
    report_date: str  # YYYY-MM-DD
    generated_at: str  # ISO 8601
    # Dimension scores (from PulseSnapshot checkpoint)
    # Kept separate from TrendSignals — different checkpoint, different data source.
    dimension_scores: dict[str, Any] | None = None
    # Snapshot-time LLM enrichment (from PulseSnapshot checkpoint).
    # Daemon layer guarantees this is either a valid, fresh enrichment sub-dict or None —
    # the lib performs no freshness/validity checks (see PulseReportDaemon
    # ._extract_validated_enrichment()). Expected keys: situation_trajectory (str),
    # tension_signals (list[str]), coordination_risks (list[str]), recommended_focus (str).
    snapshot_enrichment: dict[str, Any] | None = None


@dataclass
class PulseReport:
    """Assembled pulse report output."""

    markdown: str
    run_status: RunStatus
    report_date: str
    generated_at: str


# ---------------------------------------------------------------------------
# File providers
# ---------------------------------------------------------------------------


def load_analysis_feed_from_file(
    json_path: Path,
    *,
    freshness_days: int = 7,
    now: datetime | None = None,
    report_age_days: float | None = None,
) -> AnalysisFeed:
    """Parse a pulse_block from a watercooler-analysis JSON output file.

    The JSON is the output of ``parse_analysis.py --output <path>``. It must
    contain a top-level ``pulse_block`` key with ``pulse_block_version``
    starting with ``"1."``.

    Returns a degraded ``AnalysisFeed`` when: file missing, JSON invalid,
    ``pulse_block`` absent, or version incompatible.

    Args:
        json_path: Path to the analysis JSON file.
        freshness_days: Days after which the feed is considered stale.
        now: Override for current time (UTC); defaults to ``datetime.now(UTC)``.
        report_age_days: Override for freshness computation. When provided, this
            value is used instead of the JSON file's mtime. Pass the age of the
            persisted ``.md`` analysis artifact so that ``is_fresh`` reflects the
            underlying data age, not the age of a freshly written temp file.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if not json_path.exists():
        return AnalysisFeed(
            pulse_block=None,
            report_path=str(json_path),
            report_age_days=None,
            is_fresh=False,
            degraded=True,
            degraded_reason=f"analysis JSON not found: {json_path}",
        )

    # Compute age: caller-supplied override takes precedence over mtime
    if report_age_days is not None:
        age_days: float | None = report_age_days
    else:
        try:
            mtime = datetime.fromtimestamp(json_path.stat().st_mtime, tz=timezone.utc)
            age_days = (now - mtime).total_seconds() / 86400
        except OSError:
            age_days = None
    is_fresh = age_days is not None and age_days <= freshness_days

    # Parse JSON
    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return AnalysisFeed(
            pulse_block=None,
            report_path=str(json_path),
            report_age_days=age_days,
            is_fresh=is_fresh,
            degraded=True,
            degraded_reason=f"failed to parse analysis JSON: {exc}",
        )

    pb_raw = raw.get("pulse_block")
    if not pb_raw:
        return AnalysisFeed(
            pulse_block=None,
            report_path=str(json_path),
            report_age_days=age_days,
            is_fresh=is_fresh,
            degraded=True,
            degraded_reason="pulse_block key absent in analysis JSON (schema < 1.3)",
        )

    version = pb_raw.get("pulse_block_version", "")
    if not version.startswith("1."):
        return AnalysisFeed(
            pulse_block=None,
            report_path=str(json_path),
            report_age_days=age_days,
            is_fresh=is_fresh,
            degraded=True,
            degraded_reason=f"incompatible pulse_block_version: {version!r}",
        )

    try:
        pulse_block = _parse_pulse_block(pb_raw)
    except (ValueError, TypeError, AttributeError) as exc:
        return AnalysisFeed(
            pulse_block=None,
            report_path=str(json_path),
            report_age_days=age_days,
            is_fresh=is_fresh,
            degraded=True,
            degraded_reason=f"failed to parse pulse_block fields: {exc}",
        )
    return AnalysisFeed(
        pulse_block=pulse_block,
        report_path=str(json_path),
        report_age_days=age_days,
        is_fresh=is_fresh,
        degraded=False,
        degraded_reason="",
    )


def load_analysis_feed_from_dict(
    data: dict[str, Any],
    *,
    freshness_days: int = 7,
    now: datetime | None = None,
) -> AnalysisFeed:
    """Build AnalysisFeed from an in-memory analysis result dict.

    Parallel to load_analysis_feed_from_file() but reads from a dict
    (e.g., from daemon checkpoint) instead of a JSON file. Uses
    data["generated_at"] for freshness instead of file mtime.

    Args:
        data: Analysis result dict (output of run_analysis()).
        freshness_days: Days after which the feed is considered stale.
        now: Override for current time (UTC); defaults to ``datetime.now(UTC)``.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Compute age from generated_at timestamp
    age_days: float | None = None
    generated_at = data.get("generated_at")
    if generated_at:
        try:
            gen_raw = str(generated_at).replace("Z", "+00:00")
            gen_dt = datetime.fromisoformat(gen_raw)
            if gen_dt.tzinfo is None:
                gen_dt = gen_dt.replace(tzinfo=timezone.utc)
            age_days = (now - gen_dt).total_seconds() / 86400
        except (ValueError, TypeError):
            pass
    is_fresh = age_days is not None and age_days <= freshness_days

    pb_raw = data.get("pulse_block")
    if not pb_raw:
        return AnalysisFeed(
            pulse_block=None,
            report_path="daemon:analysis_snapshot",
            report_age_days=age_days,
            is_fresh=is_fresh,
            degraded=True,
            degraded_reason="pulse_block key absent in analysis result (schema < 1.3)",
        )

    version = pb_raw.get("pulse_block_version", "")
    if not version.startswith("1."):
        return AnalysisFeed(
            pulse_block=None,
            report_path="daemon:analysis_snapshot",
            report_age_days=age_days,
            is_fresh=is_fresh,
            degraded=True,
            degraded_reason=f"incompatible pulse_block_version: {version!r}",
        )

    try:
        pulse_block = _parse_pulse_block(pb_raw)
    except (ValueError, TypeError, AttributeError) as exc:
        return AnalysisFeed(
            pulse_block=None,
            report_path="daemon:analysis_snapshot",
            report_age_days=age_days,
            is_fresh=is_fresh,
            degraded=True,
            degraded_reason=f"failed to parse pulse_block fields: {exc}",
        )
    return AnalysisFeed(
        pulse_block=pulse_block,
        report_path="daemon:analysis_snapshot",
        report_age_days=age_days,
        is_fresh=is_fresh,
        degraded=False,
        degraded_reason="",
    )


def _parse_pulse_block(raw: dict[str, Any]) -> PulseBlock:
    """Parse a pulse_block dict into typed dataclasses."""
    risks = [
        CoordinationRisk(
            rule_id=r.get("rule_id", ""),
            text=r.get("text", ""),
            confidence=float(r.get("confidence") or 0.0),
            affected_threads=list(r.get("affected_threads", [])),
        )
        for r in raw.get("coordination_risks", [])
    ]
    stalled = [
        StalledThreadInfo(
            topic=s.get("topic", ""),
            days_since_last=int(s.get("days_since_last") or 0),
            last_entry_timestamp=s.get("last_entry_timestamp"),
        )
        for s in raw.get("stalled_threads", [])
    ]
    pairings = [
        RecommendedPairing(
            contributor=p.get("contributor", ""),
            recommended_partner=p.get("recommended_partner"),
            reason=p.get("reason", ""),
            rule_id=p.get("rule_id", ""),
        )
        for p in raw.get("recommended_pairings", [])
    ]
    actions = [
        TopAction(
            rule_id=a.get("rule_id", ""),
            text=a.get("text", ""),
            confidence=float(a.get("confidence") or 0.0),
            priority=a.get("priority", ""),
        )
        for a in raw.get("top_actions", [])
    ]
    return PulseBlock(
        pulse_block_version=raw.get("pulse_block_version", ""),
        coordination_risks=risks,
        stalled_threads=stalled,
        recommended_pairings=pairings,
        top_actions=actions,
        workflow_shape_distribution=dict(raw.get("workflow_shape_distribution", {})),
    )


def load_decision_pipeline_status(
    reports_dir: Path,
    *,
    freshness_days: int = 7,
    now: datetime | None = None,
    recent_decisions: list[dict[str, Any]] | None = None,
    window_days: int = 7,
    daemon_findings: list[dict[str, Any]] | None = None,
    daemon_is_running: bool = False,
) -> DecisionPipelineStatus:
    """Build decision pipeline status from disk and optional MCP-fetched data.

    Scans ``reports_dir`` for ``*-decision-candidates.md`` files.
    ``recent_decisions`` is a list of Decision-type thread entries fetched by
    the caller via ``watercooler_search`` — this module never calls MCP directly.

    When the ``decision_detector`` daemon is running, pass its findings via
    ``daemon_findings`` and set ``daemon_is_running=True``. The rendered report
    will show live daemon status instead of a stale on-disk file warning.

    Args:
        reports_dir: Directory to scan for detection report files.
        freshness_days: Days after which a detection report is considered stale.
        now: Override for current time (UTC).
        recent_decisions: Decision entries from ``watercooler_search`` (optional).
        window_days: Look-back window used when counting recent decisions.
        daemon_findings: Findings from ``watercooler_daemon_findings`` (optional).
        daemon_is_running: True when the ``decision_detector`` daemon is active.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Find most recent detection report
    detection_path: str | None = None
    detection_age: float | None = None
    is_fresh = False
    if reports_dir.exists():
        candidates = sorted(
            reports_dir.glob("*-decision-candidates.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            report = candidates[0]
            detection_path = str(report)
            try:
                mtime = datetime.fromtimestamp(report.stat().st_mtime, tz=timezone.utc)
                detection_age = (now - mtime).total_seconds() / 86400
                is_fresh = detection_age <= freshness_days
            except OSError:
                pass

    # Count and title recent Decision entries (most-recent-first)
    count = 0
    titles: list[str] = []
    if recent_decisions:
        cutoff = now.timestamp() - window_days * 86400
        # Parse timestamps first so we can sort by recency
        dated: list[tuple[float, str]] = []
        for entry in recent_decisions:
            ts_str = entry.get("timestamp") or entry.get("created_at", "")
            try:
                if ts_str.endswith("Z"):
                    ts_str = ts_str[:-1] + "+00:00"
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts.timestamp() >= cutoff:
                    dated.append((ts.timestamp(), entry.get("title", "")))
            except (ValueError, AttributeError):
                pass
        dated.sort(key=lambda x: x[0], reverse=True)
        count = len(dated)
        titles = [t for _, t in dated[:3]]

    # Populate daemon fields
    d_count = 0
    d_sample: list[str] = []
    if daemon_findings:
        d_count = len(daemon_findings)
        d_sample = []
        for f in daemon_findings[:3]:
            msg = f.get("message", "")
            d_sample.append(msg[:97] + "…" if len(msg) > 100 else msg)

    return DecisionPipelineStatus(
        recent_decision_count=count,
        recent_decision_titles=titles,
        detection_report_path=detection_path,
        detection_report_age_days=detection_age,
        is_detection_fresh=is_fresh,
        daemon_is_running=daemon_is_running,
        daemon_findings_count=d_count,
        daemon_findings_sample=d_sample,
    )


# ---------------------------------------------------------------------------
# Report assembly — public entry point
# ---------------------------------------------------------------------------


def assemble_report(
    inputs: PulseReportInputs,
    *,
    llm_executive_summary: str | None = None,
) -> PulseReport:
    """Assemble a full pulse report from normalised inputs.

    Args:
        inputs: All signal-block data, already acquired by the caller.
        llm_executive_summary: Optional pre-generated LLM executive summary
            string. When provided, replaces the deterministic executive summary.
            Generate with ``synthesize_executive_summary()`` before calling here.

    Returns:
        ``PulseReport`` with ``markdown`` and ``run_status``.
    """
    run_status = _derive_run_status(inputs)

    parts: list[str] = [
        f"# Project Pulse — {inputs.report_date}",
        "",
        _render_executive_summary(inputs, llm_executive_summary, run_status),
        "",
    ]

    if inputs.snapshot_enrichment is not None:
        parts.append(_build_enrichment_section(inputs.snapshot_enrichment))
        parts.append("")

    parts += [
        _render_signal1_section(
            inputs.contributors,
            inputs.corpus,
            inputs.window_days,
            inputs.stalled_threads,
            run_status.signal1,
            inputs.queue_pending,
        ),
        "",
        _render_signal2_section(
            inputs.analysis_feed,
            inputs.decision_pipeline,
            inputs.stalled_threads,
            run_status.signal2,
        ),
        "",
        _render_signal3_section(
            inputs.trend_signals,
            run_status.signal3,
            dimension_scores=inputs.dimension_scores,
        ),
        "",
        "---",
        _render_run_footer(run_status),
    ]

    return PulseReport(
        markdown="\n".join(parts),
        run_status=run_status,
        report_date=inputs.report_date,
        generated_at=inputs.generated_at,
    )


# ---------------------------------------------------------------------------
# Status derivation
# ---------------------------------------------------------------------------


def _derive_run_status(inputs: PulseReportInputs) -> RunStatus:
    """Derive per-signal-block status from inputs."""
    # Signal 1
    if not inputs.contributors:
        signal1 = SignalStatus.NO_DATA
    else:
        signal1 = SignalStatus.OK

    # Signal 2
    if inputs.analysis_feed is None and inputs.decision_pipeline is None:
        signal2 = SignalStatus.NO_DATA
    elif inputs.analysis_feed is None and inputs.decision_pipeline is not None:
        signal2 = SignalStatus.STALE  # decision data present but coordination feed absent
    elif inputs.analysis_feed is not None and (
        inputs.analysis_feed.degraded or inputs.analysis_feed.pulse_block is None
    ):
        signal2 = SignalStatus.UNAVAILABLE
    elif inputs.analysis_feed is not None and not inputs.analysis_feed.is_fresh:
        signal2 = SignalStatus.STALE
    else:
        signal2 = SignalStatus.OK

    # Signal 3
    # None = not attempted / daemon not running → NO_DATA
    # sample_size=0 = query succeeded but returned no matching facts → UNAVAILABLE
    if inputs.trend_signals is None:
        signal3 = SignalStatus.NO_DATA
    elif inputs.trend_signals.sample_size == 0:
        signal3 = SignalStatus.UNAVAILABLE
    else:
        signal3 = SignalStatus.OK

    return RunStatus(
        signal1=signal1,
        signal2=signal2,
        signal3=signal3,
        window_days=inputs.window_days,
        branch=inputs.branch,
        generated_at=inputs.generated_at,
    )


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


# Matches leading markdown structural characters that would let LLM output
# smuggle fake headings (``#``), blockquotes (``>``), list breakers (``---``),
# tables (``|``), or new list items (``-``, ``*``, ``+``) into the report.
# Applied after internal whitespace is collapsed so these cannot be hidden
# behind leading indentation or embedded newlines.
_ENRICHMENT_LEADING_MD_RE = re.compile(r"^[#>\-*+|]+\s*")
_ENRICHMENT_WHITESPACE_RE = re.compile(r"\s+")
# Inline markdown constructs that LLM output could use to embed clickable
# links, images, or emphasis into the published report body. Neutralized by
# backslash-escaping the lead-in character so the surrounding text survives
# but the construct is no longer parsed as markdown.
_ENRICHMENT_INLINE_LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\(([^)]*)\)")


def _sanitize_enrichment_text(value: Any) -> str:
    """Flatten LLM-generated text to a single line safe for markdown splicing.

    Collapses all internal whitespace (including newlines) to single spaces,
    strips leading markdown-structural characters, and neutralizes inline
    markdown link/image constructs. This defuses four injection classes that
    arise when LLM output — potentially influenced by attacker-controlled
    thread content — is concatenated into the pulse report markdown:

    1. Heading smuggling: ``## Fake section`` becomes ``Fake section``.
    2. List/rule breakers: ``---`` or ``- item\\n- item`` collapse to a single
       bullet; the horizontal-rule form is stripped as leading ``-``.
    3. Multi-line bullets: embedded newlines that would break the ``- `` list
       structure become spaces.
    4. Inline links/images: ``[text](url)`` and ``![alt](url)`` are escaped so
       they render as literal text and cannot smuggle clickable URLs into the
       published report body.

    Non-string inputs are coerced via ``str()`` so the daemon gate's type
    contract is enforced defensively at the rendering boundary.
    """
    if value is None:
        return ""
    flat = _ENRICHMENT_WHITESPACE_RE.sub(" ", str(value)).strip()
    # Strip leading markdown-structural characters iteratively so ``> ## X``
    # (quote + heading) is fully defused, not just the outermost layer.
    prev = None
    while prev != flat:
        prev = flat
        flat = _ENRICHMENT_LEADING_MD_RE.sub("", flat).strip()
    # Neutralize inline link/image constructs by escaping the leading bracket
    # (and the leading ``!`` for images). The escape only affects the
    # structural characters, so the visible text and URL both remain readable.
    flat = _ENRICHMENT_INLINE_LINK_RE.sub(
        lambda m: (
            ("\\!" if m.group(1) else "") + "\\[" + m.group(2) + "](" + m.group(3) + ")"
        ),
        flat,
    )
    return flat


def _build_enrichment_section(enrichment: dict[str, Any]) -> str:
    """Render the Snapshot analysis section from validated PulseSnapshot enrichment.

    Surfaces four additive fields produced by snapshot-time LLM enrichment:
    ``situation_trajectory`` (str), ``tension_signals`` (list[str]),
    ``coordination_risks`` (list[str]), ``recommended_focus`` (str).

    The daemon layer guarantees that this function is only invoked with a validated,
    fresh enrichment dict; missing fields within the dict are tolerated silently
    (individual bullets are omitted when their source field is empty). If none of
    the four fields produce content, the entire section is collapsed to a single
    "no analytical content available" line.

    All field values pass through ``_sanitize_enrichment_text()`` to prevent
    LLM-generated content from smuggling markdown structure (headings, list
    breakers, multi-line bullets) into the report.
    """
    lines = ["## Snapshot analysis"]

    trajectory = _sanitize_enrichment_text(enrichment.get("situation_trajectory"))
    focus = _sanitize_enrichment_text(enrichment.get("recommended_focus"))

    tensions_raw = enrichment.get("tension_signals") or []
    tensions = [s for s in (_sanitize_enrichment_text(t) for t in tensions_raw) if s]

    risks_raw = enrichment.get("coordination_risks") or []
    risks = [s for s in (_sanitize_enrichment_text(r) for r in risks_raw) if s]

    rendered_any = False

    if trajectory:
        lines.append("")
        lines.append("**Situation trajectory:**")
        lines.append(trajectory)
        rendered_any = True

    if tensions:
        lines.append("")
        lines.append("**Tension signals:**")
        for t in tensions:
            lines.append(f"- {t}")
        rendered_any = True

    if risks:
        lines.append("")
        lines.append("**Coordination risks:**")
        for r in risks:
            lines.append(f"- {r}")
        rendered_any = True

    if focus:
        lines.append("")
        lines.append("**Recommended focus:**")
        lines.append(focus)
        rendered_any = True

    if not rendered_any:
        lines.append("")
        lines.append("_No analytical content available in snapshot enrichment._")

    return "\n".join(lines)


def _render_executive_summary(
    inputs: PulseReportInputs,
    llm_summary: str | None,
    run_status: RunStatus,
) -> str:
    """Render the executive summary section."""
    lines = ["## Executive Summary"]

    if llm_summary:
        lines.append("")
        lines.append(llm_summary)
        return "\n".join(lines)

    # Deterministic fallback
    bullets: list[str] = []

    # Top focus areas across all contributors
    focus_counts: dict[str, int] = {}
    for cs in inputs.contributors.values():
        for fa in cs.focus_areas:
            focus_counts[fa] = focus_counts.get(fa, 0) + 1
    top_focus = sorted(focus_counts, key=lambda k: -focus_counts[k])[:3]
    if top_focus:
        bullets.append(f"Active focus areas: {', '.join(top_focus)}")

    # Stalled threads
    stalled = inputs.stalled_threads
    if stalled:
        topics = ", ".join(s.topic for s in stalled[:3])
        more = f" (+{len(stalled) - 3} more)" if len(stalled) > 3 else ""
        bullets.append(f"{len(stalled)} stalled thread(s): {topics}{more}")

    # T2 freshness
    if inputs.analysis_feed is not None:
        if inputs.analysis_feed.degraded:
            bullets.append(
                f"Signal 2 analysis degraded: {inputs.analysis_feed.degraded_reason}"
            )
        elif (
            not inputs.analysis_feed.is_fresh
            and inputs.analysis_feed.report_age_days is not None
        ):
            age = round(inputs.analysis_feed.report_age_days, 1)
            bullets.append(
                f"Signal 2 analysis stale ({age} days old) — re-run analysis to update"
            )

    # T3 supersession
    if inputs.trend_signals is not None:
        pct = round(inputs.trend_signals.supersession_rate * 100)
        bullets.append(
            f"Knowledge supersession rate: {pct}% ({inputs.trend_signals.trend_direction})"
        )

    # Queue warning
    if inputs.queue_pending > 0:
        bullets.append(
            f"⚠ {inputs.queue_pending} session theme(s) queued for deposit "
            "(queue drain required)"
        )

    if not bullets:
        bullets.append("No significant signals detected in this window.")

    lines.append("")
    for b in bullets:
        lines.append(f"- {b}")

    return "\n".join(lines)


def _render_signal1_section(
    contributors: dict[str, ContributorSummary],
    corpus: CorpusSummary | None,
    window_days: int,
    stalled_threads: list[StalledThreadInfo],
    status: SignalStatus,
    queue_pending: int,
) -> str:
    """Render the Signal 1 Session Activity section."""
    lines = ["## Session Activity (Signal 1)"]

    if status == SignalStatus.NO_DATA:
        lines.append("")
        if corpus is not None and corpus.session_context_threads > 0:
            lines.append(
                f"_No session activity in the last {window_days} days "
                f"across {corpus.session_context_threads} contributor thread(s)._"
            )
        else:
            lines.append(
                "_No session-context threads found. Run `watercooler setup-pulse-hook` "
                "to configure the PostCompactHook capture layer._"
            )
        return "\n".join(lines)

    if corpus is not None:
        lines.append("")
        lines.append(
            f"_{corpus.sessions_in_window} session(s) across "
            f"{corpus.session_context_threads} contributor thread(s) · "
            f"last {window_days} days_"
        )

    for name, summary in sorted(contributors.items()):
        lines.append("")
        lines.extend(_render_contributor_block(name, summary, window_days))

    # Stalled threads in Signal 1 source (capped for readability)
    if stalled_threads:
        shown = stalled_threads[:_MAX_STALLED_SHOWN]
        remaining = len(stalled_threads) - len(shown)
        lines.append("")
        lines.append("### Stalled Threads")
        for s in shown:
            age = f"{s.days_since_last}d"
            lines.append(f"- `{s.topic}` — inactive {age}")
        if remaining > 0:
            lines.append(f"_... and {remaining} more_")

    if queue_pending > 0:
        lines.append("")
        lines.append(
            f"> **Queue**: {queue_pending} theme(s) pending deposit "
            "(queue drain required)"
        )

    return "\n".join(lines)


def _render_contributor_block(
    name: str,
    summary: ContributorSummary,
    window_days: int,
) -> list[str]:
    """Render a single contributor subsection."""
    lines = [f"### {name}"]
    lines.append(
        f"- **Sessions**: {summary.session_count} in last {window_days} days "
        f"(last active: {summary.last_active})"
    )
    if summary.focus_areas:
        lines.append(f"- **Focus areas**: {', '.join(summary.focus_areas)}")

    if summary.recent_observations:
        lines.append("- **Key observations**:")
        for obs in summary.recent_observations:
            kind = obs.get("kind", "note")
            text = obs.get("text", "")
            lines.append(f"  - [{kind}] {text}")

    if summary.open_loops:
        lines.append("- **Open loops**:")
        for loop in summary.open_loops:
            lines.append(f"  - {loop}")

    return lines


def _render_signal2_section(
    analysis_feed: AnalysisFeed | None,
    decision_pipeline: DecisionPipelineStatus | None,
    stalled_threads: list[StalledThreadInfo],
    status: SignalStatus,
) -> str:
    """Render the Signal 2 Project Health section."""
    lines = ["## Project Health (Signal 2)"]

    if status == SignalStatus.NO_DATA:
        lines.append("")
        lines.append(
            "_No Signal 2 data available. Run `/watercooler-analysis` to generate "
            "the analysis feed._"
        )
        return "\n".join(lines)

    # Decision pipeline
    if decision_pipeline is not None:
        lines.append("")
        lines.append("### Decision Pipeline")
        count = decision_pipeline.recent_decision_count
        if count:
            lines.append(f"- Recent decisions: {count}")
            for title in decision_pipeline.recent_decision_titles:
                if title:
                    lines.append(f"  - {title}")
        else:
            lines.append("- No recent Decision entries found")

        if decision_pipeline.daemon_is_running:
            n = decision_pipeline.daemon_findings_count
            lines.append(f"- Detector daemon: running — {n} finding(s)")
            for msg in decision_pipeline.daemon_findings_sample:
                if msg:
                    lines.append(f"  - {msg}")
        elif decision_pipeline.detection_report_path:
            age = (
                f"{round(decision_pipeline.detection_report_age_days, 1)}d old"
                if decision_pipeline.detection_report_age_days is not None
                else "age unknown"
            )
            freshness = "" if decision_pipeline.is_detection_fresh else " ⚠ stale"
            lines.append(
                f"- Detection report: {Path(decision_pipeline.detection_report_path).name} "
                f"({age}{freshness})"
            )
        else:
            lines.append("- No detection report found and detector daemon not running")

    # Coordination section
    lines.append("")
    lines.append("### Coordination")

    if analysis_feed is None:
        lines.append(
            "_No analysis feed — run `/watercooler-analysis` for coordination data._"
        )
        return "\n".join(lines)

    if analysis_feed.degraded:
        lines.append(
            f"> ⚠ **Degraded mode**: {analysis_feed.degraded_reason}. "
            "Coordination risks and recommended pairings unavailable."
        )
        # Fall back to stalled threads from Signal 1 (capped)
        if stalled_threads:
            shown = stalled_threads[:_MAX_STALLED_SHOWN]
            remaining = len(stalled_threads) - len(shown)
            lines.append("")
            lines.append("**Stalled threads** (from Signal 1 data):")
            for s in shown:
                lines.append(f"- `{s.topic}` — {s.days_since_last}d inactive")
            if remaining > 0:
                lines.append(f"_... and {remaining} more_")
        return "\n".join(lines)

    if not analysis_feed.is_fresh and analysis_feed.report_age_days is not None:
        age = round(analysis_feed.report_age_days, 1)
        lines.append(
            f"> ⚠ Analysis data is {age} days old — re-run analysis to update."
        )

    pb = analysis_feed.pulse_block
    if pb is None:
        lines.append("_pulse_block not available in analysis feed._")
        return "\n".join(lines)

    # Risks
    if pb.coordination_risks:
        lines.append("")
        lines.append("**Coordination risks:**")
        for r in pb.coordination_risks:
            pct = round(r.confidence * 100)
            lines.append(f"- [{r.rule_id}] {r.text} _(confidence: {pct}%)_")
    else:
        lines.append("")
        lines.append("**Coordination risks:** none detected")

    # Stalled threads (prefer T2 pulse_block; T1 used when degraded)
    t2_stalled = pb.stalled_threads or stalled_threads
    if t2_stalled:
        shown = t2_stalled[:_MAX_STALLED_SHOWN]
        remaining = len(t2_stalled) - len(shown)
        lines.append("")
        lines.append("**Stalled threads:**")
        for s in shown:
            lines.append(f"- `{s.topic}` — {s.days_since_last}d inactive")
        if remaining > 0:
            lines.append(f"_... and {remaining} more_")
    else:
        lines.append("")
        lines.append("**Stalled threads:** none")

    # Top actions
    if pb.top_actions:
        lines.append("")
        lines.append("**Top actions:**")
        for a in pb.top_actions:
            pct = round(a.confidence * 100)
            lines.append(
                f"- [{a.rule_id}] {a.text} _(priority: {a.priority}, confidence: {pct}%)_"
            )

    # Recommended pairings
    if pb.recommended_pairings:
        lines.append("")
        lines.append("**Recommended pairings:**")
        for p in pb.recommended_pairings:
            partner = p.recommended_partner or "no specific partner identified"
            lines.append(f"- {p.contributor} ↔ {partner}: {p.reason} _{p.rule_id}_")

    return "\n".join(lines)


def _render_signal3_section(
    trend_signals: TrendSignals | None,
    status: SignalStatus,
    dimension_scores: dict[str, Any] | None = None,
) -> str:
    """Render the Signal 3 Trend Signals section.

    Dimension scores (project configuration vector) are rendered unconditionally when
    present — derived from session-layer signals with no Graphiti dependency, so they
    surface even when TrendSnapshot is disabled or unavailable.
    """
    lines = ["## Trend Signals (Signal 3) [experimental]"]

    if status == SignalStatus.NO_DATA:
        lines.append("")
        lines.append(
            "_Signal 3 trend signals require T2 graph availability "
            "_(TrendSnapshotDaemon)._"
        )
    elif status == SignalStatus.UNAVAILABLE:
        lines.append("")
        lines.append(
            "_No matching facts found in knowledge graph — "
            "trend signals unavailable._"
        )
    else:
        pct = round(trend_signals.supersession_rate * 100)
        lines.append("")
        lines.append(
            f"- Supersession rate: **{pct}%** "
            f"(sample: {trend_signals.sample_size} facts · "
            f"{trend_signals.active_fact_count} active, "
            f"{trend_signals.superseded_fact_count} superseded)"
        )
        lines.append(f"- Trend direction: **{trend_signals.trend_direction}**")

        # "topics" field name is legacy; values are edge relation types (e.g. "uses", "decided"),
        # not semantic domains. Rendered as "relations" to reflect actual grouping.
        if trend_signals.top_volatile_topics:
            lines.append(
                f"- Volatile relations: {', '.join(trend_signals.top_volatile_topics)}"
            )
        if trend_signals.top_stable_topics:
            lines.append(
                f"- Stable relations: {', '.join(trend_signals.top_stable_topics)}"
            )

        lines.append("")
        lines.append(
            f"_Precision note: metrics are directional indicators computed on a sample of up to "
            f"{trend_signals.sample_size} facts, not exhaustive counts._"
        )

    # Dimension scores prose — rendered regardless of T3 status.
    # Derived from T1 signals; no Graphiti dependency.
    # Internal Python field names (goal_clarity, etc.) must NOT appear in rendered output.
    if dimension_scores is not None:
        lines.append("")
        lines.append("**Project configuration:**")
        for dim in _DIMS_ORDERED:
            ds = dimension_scores.get(dim)
            if ds is None:
                continue
            display = _DIM_DISPLAY.get(dim, dim)
            conf = ds.get("confidence", 0.0)
            level_label = ds.get("level_label", "")
            level_prose = _DIM_LEVEL_PROSE.get(dim, {}).get(level_label, level_label)
            trend_prose = ds.get("trend_label", "stable")

            # Confidence-band presentation policy:
            #   >= 0.50  → normal: all labels rendered as-is
            #   0.40–0.49 → watch band: normal labels + caveat note + watch marker
            #   < 0.40   → suppress band: strong labels hedged, caveat note + watch marker
            if conf < 0.40:
                level_prose = _SUPPRESS_LABEL.get(level_prose, level_prose)
                trend_prose = _SUPPRESS_LABEL.get(trend_prose, trend_prose)

            watch_marker = " ⚠" if ds.get("watch") else ""
            conf_str = f"{conf:.0%}"
            lines.append(
                f"- **{display.capitalize()}** — **{level_prose}**, {trend_prose}"
                f" _(conf: {conf_str}){watch_marker}_"
            )
            for note in ds.get("notes", []):
                lines.append(f"  - ↳ _{note}_")
            # Confidence caveat for watch and suppress bands
            if conf < 0.50:
                lines.append("  - ↳ _Low confidence — interpret with caution_")

    return "\n".join(lines)


def _render_run_footer(run_status: RunStatus) -> str:
    """Render the run stats footer line."""
    return (
        f"Run stats: Signal 1 {run_status.signal1.value} · "
        f"Signal 2 {run_status.signal2.value} · "
        f"Signal 3 {run_status.signal3.value}\n"
        f"Window: last {run_status.window_days} days · "
        f"Branch: {run_status.branch} · "
        f"Generated: {run_status.generated_at}"
    )


# ---------------------------------------------------------------------------
# Optional LLM synthesis
# ---------------------------------------------------------------------------


def synthesize_executive_summary(
    inputs: PulseReportInputs,
    llm_client: LLMClient,
    *,
    max_tokens: int = 400,
) -> str | None:
    """Generate an LLM-enhanced executive summary.

    The ``llm_client`` is duck-typed — it must expose:
        ``complete(prompt: str, system: str, max_tokens: int) -> str | None``

    Returns ``None`` on any failure. Never raises. The caller should pass the
    result to ``assemble_report(inputs, llm_executive_summary=...)``.

    Args:
        inputs: Normalised pulse report inputs.
        llm_client: Duck-typed LLM client (no watercooler_mcp import needed).
        max_tokens: Max tokens for the LLM response.
    """
    try:
        system = (
            "You are a technical project monitor. Produce a concise executive summary "
            "for a software project pulse report. Return 3-5 bullet points in markdown "
            "(leading dash), no headers, no preamble."
        )
        # Build a compact context string
        contributor_names = list(inputs.contributors.keys())
        focus: list[str] = []
        for cs in inputs.contributors.values():
            focus.extend(cs.focus_areas[:2])
        stalled_topics = [s.topic for s in inputs.stalled_threads[:3]]
        t2_status = (
            "available"
            if inputs.analysis_feed and not inputs.analysis_feed.degraded
            else "degraded or unavailable"
        )
        t3_rate = (
            f"{round(inputs.trend_signals.supersession_rate * 100)}% supersession"
            if inputs.trend_signals
            else "unavailable"
        )

        prompt = (
            f"Project pulse window: last {inputs.window_days} days, branch: {inputs.branch}\n"
            f"Active contributors: {', '.join(contributor_names) or 'none'}\n"
            f"Top focus areas: {', '.join(dict.fromkeys(focus))[:200] or 'none'}\n"
            f"Stalled threads: {', '.join(stalled_topics) or 'none'}\n"
            f"Signal 2 (analysis): {t2_status}\n"
            f"Signal 3 (trends): {t3_rate}\n"
            f"Queue pending: {inputs.queue_pending}\n"
            "Summarise the project health in 3-5 bullet points."
        )

        result = llm_client.complete(prompt, system, max_tokens)
        if result and isinstance(result, str):
            return result.strip()
        return None
    except (
        Exception
    ):  # noqa: BLE001 — never-raise contract; daemon callers must not crash
        logger.warning("synthesize_executive_summary failed", exc_info=True)
        return None
