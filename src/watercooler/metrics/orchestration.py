"""Orchestration-turn metric (Phase 6 of the build-mode authority-ladder plan).

Quantifies the effect of the authority ladder (Phases 1-4) on agent-authored
authoritative writes, candidate Note emission rate, promotion volume, and
coordination-pattern entries.

The master plan committed to a "human orchestration turn" metric for Phase 0d;
this module is the tool that implements it. The metric answers: "did the
authority ladder change agent behaviour?" — and, more importantly, gives the
numbers to back the v0.5.5 release-notes claim.

Architecture:

- ``classify_actor``: parses ``agent_func`` (when present) and falls back to
  the ``agent`` field to classify each entry as ``human`` / ``agent`` /
  ``daemon`` / ``unknown``.
- ``is_coordination_pattern``: detects routing-only entries (``Spec: pm``,
  ``Ball:``/``Next:`` only, short bodies with no substantive content).
- ``compute_orchestration_metrics``: walks all entries in a window and
  computes the metric dict.
- ``format_markdown_report``: turns the metric dict into a release-notes
  fragment.

This module is pure given an ``entries_by_thread`` mapping; the I/O wrapper
loads from the baseline graph.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


_HUMAN_AGENT_NAMES = frozenset(
    {
        "caleb", "calebjacksonhoward", "jay", "jay-reynolds", "team", "human",
    }
)
_DAEMON_AGENT_PREFIXES = (
    "ExtractDecisionsDaemon",
    "DetectDecisionsDaemon",
    "DecisionStanceDaemon",
    "PulseSnapshotDaemon",
    "ProjectCoordinatorDaemon",
    "ContentRefinerDaemon",
    "ContentScoutDaemon",
)
_AGENT_PLATFORM_PREFIXES = (
    "Claude Code", "Claude", "Cursor", "Codex", "OpenCode", "Sonnet", "Opus",
    "GPT-",
)


_AGENT_FUNC_RE = re.compile(r"^(?P<platform>[^:]+):(?P<model>[^:]+):(?P<role>[^:]+)$")


@dataclass
class ActorClassification:
    """Classification of an entry's author."""

    actor_class: str  # "human" | "agent" | "daemon" | "unknown"
    platform: Optional[str] = None
    model: Optional[str] = None
    role: Optional[str] = None


_AGENT_USER_PAREN_RE = re.compile(r"^(?P<base>[^()]+?)\s*\((?P<user>[^()]+)\)\s*$")


def classify_actor(entry: dict[str, Any]) -> ActorClassification:
    """Classify an entry's author by ``agent`` field + ``agent_func``.

    Heuristics (in order):

    1. Parse ``agent_func`` if present: ``platform:model:role`` → agent.
    2. Match ``agent`` field against known daemon prefixes → daemon.
    3. Strip a trailing ``(user)`` parenthetical (the canonical agent
       form is ``"<Platform> (<user>)"``); if the user part matches a
       known human or the base matches an agent platform, classify
       accordingly. This is what real production entries look like —
       e.g. ``"Caleb Howard (slack)"``, ``"ChatGPT (caleb)"``,
       ``"Claude Code (caleb)"`` — and the bare allowlist alone never
       fires on them.
    4. Match ``agent`` field against known human names → human.
    5. Match ``agent`` field against agent platform prefixes → agent.
    6. Fall back to ``actor_class`` field if present (Phase 4a backfill).
    7. ``unknown``.
    """
    agent_func = entry.get("agent_func") or entry.get("agent_spec")
    if isinstance(agent_func, str):
        m = _AGENT_FUNC_RE.match(agent_func.strip())
        if m:
            return ActorClassification(
                actor_class="agent",
                platform=m.group("platform").strip(),
                model=m.group("model").strip(),
                role=m.group("role").strip(),
            )

    agent = str(entry.get("agent", "")).strip()
    if any(agent.startswith(p) for p in _DAEMON_AGENT_PREFIXES):
        return ActorClassification(actor_class="daemon")

    # Decompose ``<base> (<user>)`` — the canonical agent form. The base
    # carries the platform identity, the user is the human running it.
    # A bare-allowlist check on ``agent`` would never fire on these
    # because the parens are part of the string.
    paren_match = _AGENT_USER_PAREN_RE.match(agent)
    if paren_match is not None:
        base = paren_match.group("base").strip()
        user = paren_match.group("user").strip()
        # If the user part identifies a human (or the base IS a human
        # name like "Caleb Howard"), classify as human — they're the
        # authority behind the write, even if a platform routed it.
        if (
            user.lower() in _HUMAN_AGENT_NAMES
            or base.lower() in _HUMAN_AGENT_NAMES
        ):
            return ActorClassification(actor_class="human")
        # Otherwise it's an agent with a recognised platform prefix.
        if any(base.startswith(p) for p in _AGENT_PLATFORM_PREFIXES):
            return ActorClassification(
                actor_class="agent", platform=base, role=user
            )
        # Unknown base — fall through to other heuristics with the raw
        # ``agent`` so the explicit actor_class backfill can still fire.

    if agent.lower() in _HUMAN_AGENT_NAMES:
        return ActorClassification(actor_class="human")
    if any(agent.startswith(p) for p in _AGENT_PLATFORM_PREFIXES):
        # Platform-prefixed but no agent_func — likely an agent.
        return ActorClassification(actor_class="agent")

    # Phase 4a backfill: trust actor_class when set.
    explicit = entry.get("actor_class")
    if explicit in {"human", "agent", "daemon", "human_grandfathered"}:
        # grandfathered → human for metric purposes
        if explicit == "human_grandfathered":
            return ActorClassification(actor_class="human")
        return ActorClassification(actor_class=explicit)

    return ActorClassification(actor_class="unknown")


_COORDINATION_BODY_PATTERNS = (
    re.compile(r"^\s*Spec:\s*pm\b", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*Ball:\s*[^\n]+\.\s*Next:\s*[^\n]+\s*$", re.MULTILINE),
)


def is_coordination_pattern(entry: dict[str, Any]) -> bool:
    """Heuristic: short routing-only entries that don't carry substantive content.

    A coordination entry typically:
    - Has ``Spec: pm`` (the routing/handoff role).
    - Carries only ``Ball:`` / ``Next:`` advisory suffixes.
    - Has a body shorter than the substantive-content threshold below.

    Implementation: the body-length cutoff is ``> 800`` characters
    (anything larger is treated as substantive content even if it
    matches the pm-spec / ball-next patterns). 800 ≈ ~150 words —
    enough room for a routing message plus a brief explanation.
    """
    if entry.get("entry_type") != "Note":
        return False
    body = entry.get("body", "") or ""
    if len(body) > 800:
        return False
    if any(p.search(body) for p in _COORDINATION_BODY_PATTERNS):
        return True
    return False


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _within_window(
    entry: dict[str, Any],
    window_start: Optional[datetime],
    window_end: Optional[datetime],
) -> bool:
    ts = _parse_iso(entry.get("timestamp"))
    if ts is None:
        return False
    if window_start is not None and ts < window_start:
        return False
    if window_end is not None and ts > window_end:
        return False
    return True


# ---------------------------------------------------------------------------
# Core metric
# ---------------------------------------------------------------------------


@dataclass
class OrchestrationMetrics:
    """Snapshot of orchestration-turn metrics for one window."""

    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    total_entries: int = 0
    by_entry_type: Counter = field(default_factory=Counter)
    decisions_total: int = 0
    decisions_by_actor: Counter = field(default_factory=Counter)
    candidate_note_emissions: int = 0
    promotion_count: int = 0  # CandidateDisposition: promoted Notes
    coordination_pattern_count: int = 0
    agent_authored_decision_ratio: float = 0.0
    threads_covered: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_start": (
                self.window_start.isoformat() if self.window_start else None
            ),
            "window_end": (
                self.window_end.isoformat() if self.window_end else None
            ),
            "total_entries": self.total_entries,
            "by_entry_type": dict(self.by_entry_type),
            "decisions_total": self.decisions_total,
            "decisions_by_actor": dict(self.decisions_by_actor),
            "candidate_note_emissions": self.candidate_note_emissions,
            "promotion_count": self.promotion_count,
            "coordination_pattern_count": self.coordination_pattern_count,
            "agent_authored_decision_ratio": round(
                self.agent_authored_decision_ratio, 4
            ),
            "threads_covered": self.threads_covered,
        }


_CANDIDATE_NOTE_MARKER = re.compile(
    r"^Candidate-Status:\s*needs_human_confirmation", re.MULTILINE
)
_CANDIDATE_DISPOSITION_PROMOTED = re.compile(
    r"^CandidateDisposition:\s*promoted", re.MULTILINE
)


def _is_candidate_note(entry: dict[str, Any]) -> bool:
    if entry.get("entry_type") != "Note":
        return False
    body = entry.get("body", "") or ""
    return bool(_CANDIDATE_NOTE_MARKER.search(body))


def _is_promotion_disposition(entry: dict[str, Any]) -> bool:
    if entry.get("entry_type") != "Note":
        return False
    body = entry.get("body", "") or ""
    return bool(_CANDIDATE_DISPOSITION_PROMOTED.search(body))


def compute_orchestration_metrics(
    entries_by_thread: dict[str, list[dict[str, Any]]],
    *,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
) -> OrchestrationMetrics:
    """Compute the metric over the provided entries.

    Pure function. Callers should preload thread state and pass it in.

    Args:
        entries_by_thread: Map of topic → list of entry dicts.
        window_start: Optional inclusive start (UTC); entries with earlier
            timestamps are excluded.
        window_end: Optional inclusive end (UTC).
    """
    metrics = OrchestrationMetrics(
        window_start=window_start, window_end=window_end
    )

    for topic, entries in entries_by_thread.items():
        thread_touched = False
        for entry in entries:
            if not _within_window(entry, window_start, window_end):
                continue
            thread_touched = True
            metrics.total_entries += 1
            entry_type = entry.get("entry_type", "Note")
            metrics.by_entry_type[entry_type] += 1

            classification = classify_actor(entry)

            if entry_type == "Decision":
                metrics.decisions_total += 1
                metrics.decisions_by_actor[classification.actor_class] += 1
            if _is_candidate_note(entry):
                metrics.candidate_note_emissions += 1
            if _is_promotion_disposition(entry):
                metrics.promotion_count += 1
            if is_coordination_pattern(entry):
                metrics.coordination_pattern_count += 1
        if thread_touched:
            metrics.threads_covered += 1

    if metrics.decisions_total > 0:
        agent_count = (
            metrics.decisions_by_actor.get("agent", 0)
            + metrics.decisions_by_actor.get("daemon", 0)
        )
        metrics.agent_authored_decision_ratio = (
            agent_count / metrics.decisions_total
        )

    return metrics


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def format_markdown_report(
    current: OrchestrationMetrics,
    baseline: Optional[OrchestrationMetrics] = None,
) -> str:
    """Format the metric as a markdown report.

    With ``baseline``, the report includes a comparison column showing the
    delta from the baseline window to the current window.
    """
    lines: list[str] = ["# Orchestration-Turn Metric"]
    if current.window_start or current.window_end:
        lines.append(
            f"\nWindow: {current.window_start} → {current.window_end}"
        )

    def _row(label: str, current_value: Any, baseline_value: Any = None) -> str:
        if baseline is None:
            return f"| {label} | {current_value} |"
        return f"| {label} | {current_value} | {baseline_value} |"

    header = "| Metric | Current |" + (" Baseline |" if baseline else "")
    sep = "|---|---|" + ("---|" if baseline else "")
    lines.extend(["", header, sep])

    def _bf(field_name: str) -> Any:
        return getattr(baseline, field_name) if baseline else None

    lines.append(_row("Threads covered", current.threads_covered, _bf("threads_covered")))
    lines.append(_row("Total entries", current.total_entries, _bf("total_entries")))
    lines.append(_row("Decisions (total)", current.decisions_total, _bf("decisions_total")))
    lines.append(
        _row(
            "  Decisions by human",
            current.decisions_by_actor.get("human", 0),
            (baseline.decisions_by_actor.get("human", 0) if baseline else None),
        )
    )
    lines.append(
        _row(
            "  Decisions by agent",
            current.decisions_by_actor.get("agent", 0),
            (baseline.decisions_by_actor.get("agent", 0) if baseline else None),
        )
    )
    lines.append(
        _row(
            "  Decisions by daemon",
            current.decisions_by_actor.get("daemon", 0),
            (baseline.decisions_by_actor.get("daemon", 0) if baseline else None),
        )
    )
    lines.append(
        _row(
            "Agent-authored Decision ratio",
            f"{current.agent_authored_decision_ratio:.2%}",
            (
                f"{baseline.agent_authored_decision_ratio:.2%}"
                if baseline
                else None
            ),
        )
    )
    lines.append(
        _row("Candidate Note emissions", current.candidate_note_emissions, _bf("candidate_note_emissions"))
    )
    lines.append(_row("Promotions", current.promotion_count, _bf("promotion_count")))
    lines.append(
        _row("Coordination-pattern entries", current.coordination_pattern_count, _bf("coordination_pattern_count"))
    )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# I/O wrapper
# ---------------------------------------------------------------------------


def compute_from_baseline_graph(
    threads_dir: Path,
    *,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
) -> OrchestrationMetrics:
    """Load every thread's entries from the baseline graph and compute the metric.

    Wraps ``compute_orchestration_metrics`` with the appropriate baseline-graph
    I/O.
    """
    import logging as _logging

    from watercooler.baseline_graph import storage
    from watercooler.baseline_graph.writer import get_entries_for_thread

    log = _logging.getLogger(__name__)

    graph_dir = storage.get_graph_dir(threads_dir)
    topics = storage.list_thread_topics(graph_dir) if graph_dir.exists() else []

    entries_by_thread: dict[str, list[dict[str, Any]]] = {}
    for topic in topics:
        try:
            entries = list(get_entries_for_thread(threads_dir, topic))
        except (OSError, KeyError, ValueError) as exc:
            # Log per-thread load failures at WARNING so a silently
            # dropped thread shows up in operator logs as a metric
            # undercount (was silent before — the re-review noted that
            # corrupt or partially-written threads dropped without
            # signal).
            log.warning(
                "orchestration_metric: skipping thread %r — load failed: %s",
                topic,
                exc,
            )
            continue
        entries_by_thread[topic] = entries

    return compute_orchestration_metrics(
        entries_by_thread,
        window_start=window_start,
        window_end=window_end,
    )
