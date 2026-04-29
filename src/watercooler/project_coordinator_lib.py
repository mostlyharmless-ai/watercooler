"""Project Coordinator shared library — detection logic.

All five v1A detectors live here as stateless functions that the daemon calls
each tick.  ``_has_xref_decision`` performs a shallow graph read (Phase 3b-2)
but is guarded by the ``threads_dir`` optional so the rest of the module
remains testable without a real threads directory.

Type definitions (``EntryView``, ``BurstBaseline``, ``CoordinatorExtras``)
enforce typed boundaries between the daemon and these detectors.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict

from watercooler.pulse_stance_lib import AdvisoryAction, CoordinatorLead

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level threshold constants (promote to config fields in v1B only when
# a real user asks to tune them).
# ---------------------------------------------------------------------------

# stalled_open_loop
OPEN_LOOP_MIN_ENTRIES: int = 3
OPEN_LOOP_MIN_STALE_DAYS: int = 7

# stalled_dropout
DROPOUT_MIN_ENTRIES: int = 3
DROPOUT_CONTINUATION_GAP: int = 3

# aware_burst
BURST_BASELINE_WINDOW_DAYS: int = 7
BURST_MIN_THREAD_AGE_DAYS: int = 3
BURST_MULTIPLIER: float = 3.0
BURST_MIN_ENTRIES: int = 3

# aware_new_contributor
NEW_CONTRIBUTOR_REAPPEARANCE_DAYS: int = 30
NEW_CONTRIBUTOR_PRUNE_DAYS: int = 90

# aware_role_concentration
ROLE_CONCENTRATION_THRESHOLD: float = 0.8
ROLE_CONCENTRATION_MIN_ENTRIES: int = 5


# ---------------------------------------------------------------------------
# Typed structures
# ---------------------------------------------------------------------------


class EntryView(TypedDict):
    """Minimal typed view of a graph entry, validated at the daemon boundary."""

    entry_id: str
    agent: str
    role: str
    entry_type: str
    timestamp: str  # ISO 8601
    index: int


@dataclass(frozen=True)
class BurstBaseline:
    """Per-thread activity baseline for burst detection."""

    baseline_rate: float  # entries per day
    last_entry_count: int
    last_tick_time: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BurstBaseline:
        return cls(
            baseline_rate=float(d.get("baseline_rate", 0.0)),
            last_entry_count=int(d.get("last_entry_count", 0)),
            last_tick_time=float(d.get("last_tick_time", 0.0)),
        )


@dataclass
class ActiveSignalEntry:
    """Per-topic record of which detector categories are active.

    Used by stance modulation (v1B) to track the full steady-state of
    coordinator findings across ticks, not just the current tick's delta.
    """

    categories: set[str] = field(default_factory=set)
    last_evaluated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "categories": sorted(self.categories),
            "last_evaluated_at": self.last_evaluated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ActiveSignalEntry:
        return cls(
            categories=set(d.get("categories", [])),
            last_evaluated_at=float(d.get("last_evaluated_at", 0.0)),
        )


@dataclass
class CoordinatorExtras:
    """Typed wrapper around checkpoint extras for the project coordinator."""

    seen_contributors: dict[str, float] = field(default_factory=dict)
    burst_baselines: dict[str, BurstBaseline] = field(default_factory=dict)
    # v1B stance modulation state
    active_signals: dict[str, ActiveSignalEntry] = field(default_factory=dict)
    last_stance_signatures: dict[str, str] = field(default_factory=dict)
    # Stance fids explicitly cleared by tombstone emission — filtered from
    # _existing_keys after each dedup resync to allow re-escalation (todo 282)
    cleared_stance_fids: set[str] = field(default_factory=set)
    # Tick-scoped: corpus-level signal counts (e.g., aware_new_contributor)
    # populated during tick() and merged into stance coord_counts in
    # _emit_stance_advisories(). Cleared at tick start; not persisted.
    corpus_signal_inputs: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seen_contributors": dict(self.seen_contributors),
            "burst_baselines": {
                k: v.to_dict() for k, v in self.burst_baselines.items()
            },
            "active_signals": {k: v.to_dict() for k, v in self.active_signals.items()},
            "last_stance_signatures": dict(self.last_stance_signatures),
            "cleared_stance_fids": sorted(self.cleared_stance_fids),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CoordinatorExtras:
        raw_baselines = d.get("burst_baselines", {})
        raw_signals = d.get("active_signals", {})
        return cls(
            seen_contributors=dict(d.get("seen_contributors", {})),
            burst_baselines={
                k: BurstBaseline.from_dict(v) for k, v in raw_baselines.items()
            },
            active_signals={
                k: ActiveSignalEntry.from_dict(v) for k, v in raw_signals.items()
            },
            last_stance_signatures=dict(d.get("last_stance_signatures", {})),
            cleared_stance_fids=set(d.get("cleared_stance_fids", [])),
        )


@dataclass
class CoordinatorFinding:
    """Shared-lib detector result — materialized into Finding by the daemon."""

    category: str
    topic: str
    severity: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    dedup_signature: str = ""
    entry_id: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_entry_timestamp(entry: EntryView) -> float | None:
    """Parse ISO 8601 timestamp to unix epoch, or None if unparseable."""
    ts = entry.get("timestamp", "")  # type: ignore[arg-type]
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def entries_to_views(raw_entries: list[dict[str, Any]]) -> list[EntryView]:
    """Convert raw graph entry dicts to typed EntryView list.

    Handles missing fields with defaults at the boundary so detector
    functions don't need defensive .get() calls.
    """
    views: list[EntryView] = []
    for e in raw_entries:
        views.append(
            EntryView(
                entry_id=str(e.get("entry_id", "")),
                agent=str(e.get("agent", "")),
                role=str(e.get("role", "")),
                entry_type=str(e.get("entry_type", "Note")),
                timestamp=str(e.get("timestamp", "")),
                index=int(e.get("index", 0)),
            )
        )
    return views


def _suppression_details(
    thread_tags: set[str],
    suppression_tags: set[str],
) -> dict[str, str]:
    """Return a ``suppressed_by`` marker if a suppression tag matches.

    The tag-match primitive shared by ``stalled_*`` (which downgrades
    ``warning`` to ``info`` on match) and ``aware_*`` (which is already
    ``info`` and only annotates the finding).

    Returns an empty dict when no tag matches.
    """
    matched = thread_tags & suppression_tags
    if matched:
        tag = sorted(matched)[0]
        return {"suppressed_by": f"tag:{tag}"}
    return {}


def _check_suppression(
    thread_tags: set[str],
    suppression_tags: set[str],
) -> tuple[str, dict[str, str]]:
    """Severity downgrade helper for ``stalled_*`` detectors.

    Returns ``("info", {"suppressed_by": ...})`` if a suppression tag
    matches the thread's annotation tags, else ``("warning", {})``.

    ``aware_*`` detectors should call ``_suppression_details()`` directly
    so their base ``info`` severity is preserved (see Phase 3a-3).
    """
    details = _suppression_details(thread_tags, suppression_tags)
    if details:
        return "info", details
    return "warning", {}


# ---------------------------------------------------------------------------
# Detector 1: stalled_open_loop
# ---------------------------------------------------------------------------


# Defensive caps on xref fan-out. An annotation can legitimately reference a
# handful of entries; thousands is either runaway growth or adversarial input.
# Two independent ceilings keep traversal bounded without affecting any
# realistic use case:
#
# * ``_MAX_XREFS_PER_STATE`` — per-annotation-state truncation. Protects
#   against a single state carrying an unbounded xref list.
# * ``_MAX_XREFS_TOTAL`` — total unique xref fetches per ``_has_xref_decision``
#   call, summed across all annotation states on the source thread. Protects
#   against a thread with many annotation states each carrying xrefs near the
#   per-state cap, which the per-state cap alone cannot bound.
#
# Current production observation (2026-04-19, 413 threads): max xrefs per state
# = 7, p99 = 3, max distinct xrefs per thread ≈ 15. 50 per state / 500 total
# gives ~7×/30× headroom over observed peaks and bounds read-amplification at
# 500 cross-thread reads per affected tick — a defense-in-depth control.
_MAX_XREFS_PER_STATE = 50
_MAX_XREFS_TOTAL = 500


def _load_thread_annotation_states(
    threads_dir: Path, topic: str
) -> dict[str, Any] | None:
    """Load a thread's annotation states, or return ``None`` on any read error.

    Scoped to a single thread — cost grows with that thread's annotation file,
    not with the graph. Used by ``detect_stalled_open_loops`` to gate the
    reverse-index scan on actual xref presence before paying whole-repo cost.
    """
    try:
        from watercooler.baseline_graph.annotations import load_or_rebuild_state
        from watercooler.baseline_graph.storage import (
            get_graph_dir,
            get_thread_graph_dir,
        )

        graph_dir = get_graph_dir(threads_dir)
        thread_dir = get_thread_graph_dir(graph_dir, topic)
        return load_or_rebuild_state(thread_dir, read_only=True)
    except Exception as exc:
        # Fail-open per Phase 3b-2 contract: any read/parse/shape error here
        # (OSError from IO, JSONDecodeError from a truncated cache,
        # AttributeError/TypeError from a malformed cached state like
        # {"entry-1": 123} where from_dict() assumes a dict, etc.) must not
        # abort the tick. Returning None makes the xref traversal a no-op
        # for this thread and lets the detector fall through to its normal
        # stalled_open_loop path. exc_info preserves the trace for ops.
        logger.debug(
            "_load_thread_annotation_states: load failed for %s: %s",
            topic,
            exc,
            exc_info=True,
        )
        return None


def _states_have_any_xrefs(states: dict[str, Any]) -> bool:
    """True if any annotation state in *states* carries at least one xref."""
    for ann in states.values():
        if ann.xrefs:
            return True
    return False


def _has_xref_decision(
    threads_dir: Path,
    topic: str,
    entry_topic_index: dict[str, str],
    *,
    states: dict[str, Any] | None = None,
) -> str | None:
    """Return the resolving xref entry ID if any xref from *topic*'s annotations
    resolves to a cross-thread Decision entry, otherwise None.

    Fail-open: any read error silently returns None so a corrupt or missing
    annotation file never blocks the detector.  Only cross-thread targets are
    checked — same-thread Decision entries are already caught by the
    ``has_resolution`` guard inside ``detect_stalled_open_loops``.

    Args:
        threads_dir: Root threads directory passed through from the daemon tick.
        topic: The source thread's topic identifier.
        entry_topic_index: Pre-built ``entry_id → thread_topic`` mapping. The
            daemon builds this once per tick via
            ``watercooler.baseline_graph.writer.build_entry_topic_index`` and
            reuses it across all topics.
        states: Pre-loaded annotation states for *topic*. When omitted, the
            function loads them itself; callers that already gated on xref
            presence pass them in to avoid a redundant read.

    Returns:
        The target entry ID that resolved to a cross-thread Decision, or None
        if not found or on any read error (fail-open).
    """
    if states is None:
        loaded = _load_thread_annotation_states(threads_dir, topic)
        if loaded is None:
            return None
        states = loaded

    from watercooler.baseline_graph.writer import get_entry_node_from_graph

    # Per-traversal dedup — a specific entry never needs to be fetched twice
    # on the same call. Dedup MUST be by xref_entry_id (the actual fetch key
    # used by get_entry_node_from_graph), NOT by target_topic: two different
    # entries in the same target topic can legitimately differ in entry_type
    # (Note vs Decision), so topic-level dedup would mark the topic "checked"
    # after inspecting a Note and silently skip a subsequent Decision in the
    # same topic, producing a false-negative stalled_open_loop. Iteration
    # order over annotation states is nondeterministic (materialize_all_states
    # builds order from a set), so topic-dedup is a live intermittent bug,
    # not a theoretical one.
    #
    # ``_MAX_XREFS_TOTAL`` bounds the total number of *distinct* cross-thread
    # fetches across all states on this thread — the per-state cap alone
    # cannot bound a thread with many annotation states. We stop once
    # ``len(checked_entries)`` (which only grows on new entry IDs after the
    # dedup check) reaches the ceiling.
    checked_entries: set[str] = set()
    try:
        for ann in states.values():
            if len(checked_entries) >= _MAX_XREFS_TOTAL:
                break
            xrefs = (ann.xrefs or [])[:_MAX_XREFS_PER_STATE]
            for xref_entry_id in xrefs:
                if xref_entry_id in checked_entries:
                    continue
                if len(checked_entries) >= _MAX_XREFS_TOTAL:
                    break
                checked_entries.add(xref_entry_id)
                target_topic = entry_topic_index.get(xref_entry_id)
                if target_topic is None or target_topic == topic:
                    # Missing from index (genuinely absent) or same-thread xref
                    # (caught by has_resolution in detect_stalled_open_loops).
                    continue
                try:
                    target = get_entry_node_from_graph(
                        threads_dir, xref_entry_id, topic=target_topic
                    )
                except (OSError, KeyError, ValueError, json.JSONDecodeError):
                    continue
                if not target:
                    continue
                if target.get("thread_topic") == topic:
                    continue
                if target.get("entry_type") == "Decision":
                    return xref_entry_id
    except Exception as exc:
        logger.debug(
            "_has_xref_decision: traversal failed for %s: %s",
            topic,
            exc,
            exc_info=True,
        )
        return None
    return None


def detect_stalled_open_loops(
    entries: list[EntryView],
    thread_topic: str,
    thread_status: str,
    suppression_tags: set[str],
    thread_tags: set[str],
    tick_time: float = 0.0,
    *,
    threads_dir: Path | None = None,
    entry_topic_index: dict[str, str] | Callable[[], dict[str, str]] | None = None,
) -> CoordinatorFinding | None:
    """Detect threads with Plan entries but no Decision or Closure.

    A staleness gate ensures threads with recent activity (< OPEN_LOOP_MIN_STALE_DAYS)
    are not flagged — a Plan posted yesterday isn't "stalled."

    Args:
        entries: Entry views for the thread being evaluated.
        thread_topic: The thread's topic identifier.
        thread_status: Raw status string from the graph (e.g. "OPEN", "CLOSED").
        suppression_tags: Global suppression tag set from daemon config.
        thread_tags: Tags present on this specific thread.
        tick_time: Unix timestamp of the current daemon tick; 0 disables the
            staleness gate (backward-compatible default).
        threads_dir: When provided, performs a cross-thread xref traversal:
            if any annotation xref from this thread resolves to a Decision entry
            in another thread, ``xref_resolves_to`` is populated in the returned
            finding's details and the finding severity/shape are unchanged.
            Omit to skip the traversal (e.g. in unit tests that do not need a
            real threads dir).
        entry_topic_index: Pre-built ``entry_id → thread_topic`` mapping, or
            a zero-arg callable that returns one on demand. Daemons pass a
            memoized callable so the whole-repo scan only runs when a stalled
            thread actually needs the xref lookup — healthy ticks never pay
            the cost. If ``None`` and ``threads_dir`` is provided, the index
            is built locally (CLI/test convenience).

    Returns:
        A ``CoordinatorFinding`` when the thread matches the stalled-open-loop
        pattern (optionally annotated with ``xref_resolves_to`` if a
        cross-thread Decision was found), otherwise ``None``.
    """
    from watercooler.fs import is_closed

    if is_closed(thread_status):
        return None
    if len(entries) < OPEN_LOOP_MIN_ENTRIES:
        return None

    plan_count = sum(1 for e in entries if e["entry_type"] == "Plan")
    if plan_count == 0:
        return None

    has_resolution = any(e["entry_type"] in ("Decision", "Closure") for e in entries)
    if has_resolution:
        return None

    # Staleness gate: skip threads with recent activity.
    days_stale: float | None = None
    if tick_time > 0:
        newest_ts: float | None = None
        for e in entries:
            ts = parse_entry_timestamp(e)
            if ts is not None:
                if newest_ts is None or ts > newest_ts:
                    newest_ts = ts
        if newest_ts is not None:
            days_stale = (tick_time - newest_ts) / 86400.0
            if days_stale < OPEN_LOOP_MIN_STALE_DAYS:
                return None

    # Cross-thread xref suppression: if any xref from this thread resolves to a
    # Decision entry in another thread, the open loop is externally resolved.
    # Emits a coordinator_xref_suppression info finding instead of
    # stalled_open_loop for observability parity with tag suppression — agents
    # and operators can see that the thread would have been flagged.
    #
    # Cost gate: load *this* thread's annotation states first (scoped cost,
    # grows with thread-local state). Only when xrefs are actually present
    # do we resolve the whole-repo reverse index. A stale thread with no
    # xrefs never triggers a graph-wide scan.
    xref_resolver: str | None = None
    if threads_dir is not None:
        source_states = _load_thread_annotation_states(threads_dir, thread_topic)
        if source_states is not None and _states_have_any_xrefs(source_states):
            if entry_topic_index is None:
                from watercooler.baseline_graph.writer import build_entry_topic_index

                resolved_index = build_entry_topic_index(threads_dir)
            elif callable(entry_topic_index):
                resolved_index = entry_topic_index()
            else:
                resolved_index = entry_topic_index
            xref_resolver = _has_xref_decision(
                threads_dir, thread_topic, resolved_index, states=source_states
            )
        if xref_resolver is not None:
            return CoordinatorFinding(
                category="coordinator_xref_suppression",
                topic=thread_topic,
                severity="info",
                message=(
                    f"Thread '{thread_topic}' has {plan_count} Plan entries "
                    f"but is resolved by cross-thread Decision "
                    f"(xref {xref_resolver})"
                ),
                details={
                    "plan_count": plan_count,
                    "entry_count": len(entries),
                    "xref_resolves_to": xref_resolver,
                    "suppressed_by": f"xref:{xref_resolver}",
                },
                dedup_signature=f"coordinator_xref_suppression|{thread_topic}",
            )

    severity, suppression_details = _check_suppression(thread_tags, suppression_tags)
    suffix = ""
    if suppression_details:
        suffix = f" (suppressed by {suppression_details['suppressed_by']})"

    details: dict[str, Any] = {
        "plan_count": plan_count,
        "entry_count": len(entries),
        **suppression_details,
    }
    if days_stale is not None:
        details["days_stale"] = round(days_stale, 1)

    return CoordinatorFinding(
        category="stalled_open_loop",
        topic=thread_topic,
        severity=severity,
        message=(
            f"Thread '{thread_topic}' has {plan_count} Plan entries "
            f"but no Decision or Closure{suffix}"
        ),
        details=details,
        dedup_signature=f"stalled_open_loop|{thread_topic}",
    )


# ---------------------------------------------------------------------------
# Detector 2: stalled_dropout
# ---------------------------------------------------------------------------


def detect_stalled_dropout(
    entries: list[EntryView],
    thread_topic: str,
    thread_status: str,
    suppression_tags: set[str],
    thread_tags: set[str],
    normalize_agent_fn: Callable[[str], str] | None = None,
) -> list[CoordinatorFinding]:
    """Detect contributors who were active then stopped while the thread continued."""
    from watercooler.fs import is_closed

    if is_closed(thread_status):
        return []

    # Normalize contributor identities
    if normalize_agent_fn is None:
        from watercooler.analysis_lib import normalize_agent

        normalize_agent_fn = normalize_agent

    # Build per-contributor entry lists
    contributor_entries: dict[str, list[int]] = {}
    for entry in entries:
        agent = entry["agent"]
        if not agent:
            continue
        contributor = normalize_agent_fn(agent)
        contributor_entries.setdefault(contributor, []).append(entry["index"])

    # Need at least 2 unique contributors
    if len(contributor_entries) < 2:
        return []

    thread_last_index = max(e["index"] for e in entries)
    findings: list[CoordinatorFinding] = []

    for contributor, indices in contributor_entries.items():
        if len(indices) < DROPOUT_MIN_ENTRIES:
            continue

        last_idx = max(indices)
        gap = thread_last_index - last_idx

        if gap < DROPOUT_CONTINUATION_GAP:
            continue

        # Count entries from OTHER contributors after this one's last entry
        others_after = sum(
            1
            for e in entries
            if e["index"] > last_idx
            and normalize_agent_fn(e["agent"]) != contributor
            and e["agent"]
        )
        if others_after < DROPOUT_CONTINUATION_GAP:
            continue

        severity, suppression_details = _check_suppression(
            thread_tags, suppression_tags
        )
        details: dict[str, Any] = {
            "contributor": contributor,
            "contributor_entries": len(indices),
            "last_entry_index": last_idx,
            "thread_entry_count": len(entries),
            "gap_entries": gap,
            **suppression_details,
        }

        findings.append(
            CoordinatorFinding(
                category="stalled_dropout",
                topic=thread_topic,
                severity=severity,
                message=(
                    f"Contributor '{contributor}' stopped after {len(indices)} entries "
                    f"in '{thread_topic}' — {others_after} entries from others followed"
                ),
                details=details,
                dedup_signature=f"stalled_dropout|{thread_topic}|{contributor}",
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Detector 3: aware_burst
# ---------------------------------------------------------------------------


def detect_aware_burst(
    entries: list[EntryView],
    thread_topic: str,
    baseline: BurstBaseline | None,
    tick_time: float,
    *,
    suppression_tags: set[str] | frozenset[str] = frozenset(),
    thread_tags: set[str] | frozenset[str] = frozenset(),
) -> tuple[CoordinatorFinding | None, BurstBaseline]:
    """Detect sudden spikes in entry volume relative to thread baseline.

    When ``suppression_tags`` intersects ``thread_tags`` the finding is
    still emitted at base severity ``info`` with a ``suppressed_by``
    marker in ``details`` so downstream consumers can filter suppressed
    findings without losing observability (Phase 3a-3).
    """
    # Filter to entries with valid timestamps
    dated: list[tuple[EntryView, float]] = []
    for entry in entries:
        ts = parse_entry_timestamp(entry)
        if ts is not None:
            dated.append((entry, ts))

    if not dated:
        # No dated entries — preserve existing baseline or create empty
        return None, baseline or BurstBaseline(
            baseline_rate=0.0,
            last_entry_count=len(entries),
            last_tick_time=tick_time,
        )

    dated.sort(key=lambda x: x[1])
    earliest_ts = dated[0][1]

    # Thread age check
    thread_age_days = (tick_time - earliest_ts) / 86400.0
    if thread_age_days < BURST_MIN_THREAD_AGE_DAYS:
        return None, baseline or BurstBaseline(
            baseline_rate=0.0,
            last_entry_count=len(entries),
            last_tick_time=tick_time,
        )

    # Compute current baseline from entries within the window
    window_start = tick_time - (BURST_BASELINE_WINDOW_DAYS * 86400.0)
    entries_in_window = [ts for _, ts in dated if ts >= window_start]
    window_days = min(BURST_BASELINE_WINDOW_DAYS, thread_age_days)
    current_rate = len(entries_in_window) / max(window_days, 0.01)

    if baseline is None:
        # First observation — seed baseline, no finding
        return None, BurstBaseline(
            baseline_rate=current_rate,
            last_entry_count=len(entries),
            last_tick_time=tick_time,
        )

    # Compute new entries since last tick
    new_entries = len(entries) - baseline.last_entry_count
    days_since_last = (tick_time - baseline.last_tick_time) / 86400.0

    if new_entries < BURST_MIN_ENTRIES or days_since_last < 0.001:
        # Not enough new entries or ticks too close together
        return None, BurstBaseline(
            baseline_rate=current_rate,
            last_entry_count=len(entries),
            last_tick_time=tick_time,
        )

    updated_baseline = BurstBaseline(
        baseline_rate=current_rate,
        last_entry_count=len(entries),
        last_tick_time=tick_time,
    )

    # Compare window-based rates on the same timescale.  The prior baseline
    # rate was computed over the same BURST_BASELINE_WINDOW_DAYS window on
    # the previous tick, so ``current_rate`` (this tick's window rate) is the
    # correct comparand — NOT a delta-rate derived from ``new_entries /
    # days_since_last_tick`` which would be on a per-tick timescale and
    # produce systematic false positives at short tick intervals.
    if (
        baseline.baseline_rate > 0
        and current_rate >= baseline.baseline_rate * BURST_MULTIPLIER
    ):
        multiplier = current_rate / baseline.baseline_rate
        window_start_date = datetime.fromtimestamp(
            window_start, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        suppression_details = _suppression_details(
            set(thread_tags), set(suppression_tags)
        )
        details: dict[str, Any] = {
            "current_rate": round(current_rate, 2),
            "baseline_rate": round(baseline.baseline_rate, 2),
            "multiplier": round(multiplier, 2),
            "new_entries": new_entries,
            **suppression_details,
        }
        suffix = (
            f" (suppressed by {suppression_details['suppressed_by']})"
            if suppression_details
            else ""
        )
        return (
            CoordinatorFinding(
                category="aware_burst",
                topic=thread_topic,
                severity="info",
                message=(
                    f"Activity burst in '{thread_topic}': {current_rate:.1f} entries/day "
                    f"vs baseline {baseline.baseline_rate:.1f} entries/day "
                    f"({multiplier:.1f}x){suffix}"
                ),
                details=details,
                dedup_signature=f"aware_burst|{thread_topic}|{window_start_date}",
            ),
            updated_baseline,
        )

    return None, updated_baseline


# ---------------------------------------------------------------------------
# Detector 4: aware_new_contributor
# ---------------------------------------------------------------------------


def detect_new_contributors(
    all_contributors: dict[str, float],
    seen_set: dict[str, float],
    tick_time: float,
    contributor_threads: dict[str, list[str]] | None = None,
) -> tuple[list[CoordinatorFinding], dict[str, float]]:
    """Detect first-time or re-appearing contributors across the corpus.

    Args:
        all_contributors: Normalized contributor name → latest entry timestamp.
        seen_set: From CoordinatorExtras.seen_contributors.
        tick_time: Current unix timestamp.
        contributor_threads: Optional mapping of contributor → list of thread topics
            where they appear. Used for details["observed_threads"].

    Returns:
        (findings, updated_seen_set)
    """
    findings: list[CoordinatorFinding] = []
    updated = dict(seen_set)
    reappearance_threshold = NEW_CONTRIBUTOR_REAPPEARANCE_DAYS * 86400.0

    for contributor, latest_ts in all_contributors.items():
        last_seen = seen_set.get(contributor)
        threads = (contributor_threads or {}).get(contributor, [])

        if last_seen is None:
            # Genuinely new
            event_day = datetime.fromtimestamp(latest_ts, tz=timezone.utc).strftime(
                "%Y-%m-%d"
            )
            findings.append(
                CoordinatorFinding(
                    category="aware_new_contributor",
                    topic=f"contributor:{contributor}",
                    severity="info",
                    message=f"New contributor '{contributor}' appeared",
                    details={
                        "contributor": contributor,
                        "observed_threads": threads[:10],
                        "is_reappearance": False,
                        "days_absent": None,
                    },
                    dedup_signature=(
                        f"aware_new_contributor|{contributor}|new|{event_day}"
                    ),
                )
            )
            updated[contributor] = tick_time

        elif tick_time - last_seen > reappearance_threshold:
            # Re-appearance after long absence
            days_absent = int((tick_time - last_seen) / 86400.0)
            event_day = datetime.fromtimestamp(latest_ts, tz=timezone.utc).strftime(
                "%Y-%m-%d"
            )
            findings.append(
                CoordinatorFinding(
                    category="aware_new_contributor",
                    topic=f"contributor:{contributor}",
                    severity="info",
                    message=(
                        f"Contributor '{contributor}' reappeared after "
                        f"{days_absent} days"
                    ),
                    details={
                        "contributor": contributor,
                        "observed_threads": threads[:10],
                        "is_reappearance": True,
                        "days_absent": days_absent,
                    },
                    dedup_signature=(
                        f"aware_new_contributor|{contributor}|reappearance|{event_day}"
                    ),
                )
            )
            updated[contributor] = tick_time

        else:
            # Known, recently seen — just update timestamp
            updated[contributor] = max(last_seen, latest_ts)

    # Prune entries older than threshold
    prune_cutoff = tick_time - (NEW_CONTRIBUTOR_PRUNE_DAYS * 86400.0)
    updated = {k: v for k, v in updated.items() if v >= prune_cutoff}

    return findings, updated


# ---------------------------------------------------------------------------
# Detector 5: aware_role_concentration
# ---------------------------------------------------------------------------


def detect_role_concentration(
    entries: list[EntryView],
    thread_topic: str,
    thread_status: str,
    *,
    suppression_tags: set[str] | frozenset[str] = frozenset(),
    thread_tags: set[str] | frozenset[str] = frozenset(),
) -> CoordinatorFinding | None:
    """Detect threads dominated by a single role.

    When ``suppression_tags`` intersects ``thread_tags`` the finding is
    still emitted at base severity ``info`` with a ``suppressed_by``
    marker in ``details`` (Phase 3a-3).
    """
    from watercooler.fs import is_closed

    if is_closed(thread_status):
        return None
    if len(entries) < ROLE_CONCENTRATION_MIN_ENTRIES:
        return None

    # Count roles, excluding empty/unknown
    role_counter: Counter[str] = Counter()
    for entry in entries:
        role = entry["role"]
        if role:
            role_counter[role] += 1

    total_named = sum(role_counter.values())
    if total_named == 0:
        return None

    dominant_role, dominant_count = role_counter.most_common(1)[0]
    concentration = dominant_count / total_named

    if concentration < ROLE_CONCENTRATION_THRESHOLD:
        return None

    # Identify missing canonical roles
    present_roles = set(role_counter.keys())
    missing_roles = sorted(_CANONICAL_ROLES - present_roles)

    suppression_details = _suppression_details(set(thread_tags), set(suppression_tags))
    suffix = (
        f" (suppressed by {suppression_details['suppressed_by']})"
        if suppression_details
        else ""
    )

    return CoordinatorFinding(
        category="aware_role_concentration",
        topic=thread_topic,
        severity="info",
        message=(
            f"Thread '{thread_topic}' is {concentration:.0%} {dominant_role} "
            f"({dominant_count}/{total_named} entries){suffix}"
        ),
        details={
            "dominant_role": dominant_role,
            "concentration": round(concentration, 3),
            "role_distribution": dict(role_counter),
            "entry_count": len(entries),
            "missing_roles": missing_roles,
            **suppression_details,
        },
        dedup_signature=f"aware_role_concentration|{thread_topic}|{dominant_role}",
    )


# ---------------------------------------------------------------------------
# Coordinator leads (v1B follow-on) -- thread-specific investigation hints
# ---------------------------------------------------------------------------

_LEAD_TRIGGER_CATEGORIES: frozenset[str] = frozenset(
    {
        "stalled_open_loop",
        "aware_role_concentration",
        "stalled_dropout",
        "aware_burst",
        "connect_role_complement",
    }
)

_CANONICAL_ROLES: frozenset[str] = frozenset(
    {"planner", "critic", "implementer", "tester", "pm", "scribe"}
)


def _build_t2_context(
    thread_analysis: dict[str, Any],
    rule_flags: list[str] | None = None,
) -> dict[str, Any]:
    """Build a t2_context dict from a single window_threads entry.

    All keys are always present (no key omission).  ``bool`` fields use
    ``False`` as the type-safe default when absent — never ``None``.
    ``str``/``float``/``int`` fields use ``None`` for missing source data.

    ``recommendation_rule_ids`` strips empty strings and deduplicates in both
    the ``rule_flags`` argument and this helper, keeping the invariant
    self-contained on the public pure function so future callers in other
    daemons cannot regress it silently.

    Returns a dict with ``schema_version=2``.  v1 used ``stalled``; v2 renames
    it to ``analysis_stalled`` to make clear it reflects the analysis window's
    staleness verdict, not a global coordinator judgment.
    """
    wf = thread_analysis.get("workflow_shape") or {}
    return {
        "schema_version": 2,
        "analysis_stalled": bool(thread_analysis.get("stalled", False)),
        "days_since_last": thread_analysis.get("days_since_last"),
        "workflow_shape_id": wf.get("id"),
        "workflow_shape_name": wf.get("name"),
        "workflow_confidence": wf.get("confidence"),
        "has_decision": bool(thread_analysis.get("has_decision", False)),
        "has_closure": bool(thread_analysis.get("has_closure", False)),
        "entry_count_total": thread_analysis.get("entry_count_total"),
        "recommendation_rule_ids": sorted(
            {r for r in (rule_flags or []) if r}  # defensive: strip empty strings
        ),
    }


def _build_lead_for_finding(
    source_cf: CoordinatorFinding,
    thread_analysis: dict[str, Any] | None = None,
    rule_flags: list[str] | None = None,
) -> CoordinatorLead | None:
    """Construct a CoordinatorLead from a single v1A finding.

    Returns None if the category is not a lead trigger. All ``details`` access
    uses ``.get()`` with fallback to tolerate schema drift.
    """
    category = source_cf.category
    if category not in _LEAD_TRIGGER_CATEGORIES:
        return None

    topic = source_cf.topic
    d = source_cf.details

    if category == "stalled_open_loop":
        plan_count = d.get("plan_count", 0)
        days_stale = d.get("days_stale")
        stale_fragment = (
            f" ({days_stale:.0f} days stale)"
            if isinstance(days_stale, (int, float))
            else ""
        )
        summary = (
            f"Thread '{topic}' has {plan_count} Plan entries but no "
            f"Decision or Closure{stale_fragment}"
        )
        action = AdvisoryAction(
            phase="pre",
            tool="watercooler_read_thread",
            arguments={"topic": topic, "summary_only": True},
            reason="Get thread narrative before investigating coordination issue",
        )
        tags: tuple[str, ...] = ("implementer", "pm")

    elif category == "aware_role_concentration":
        dominant_role = d.get("dominant_role", "unknown")
        concentration = d.get("concentration", 0.0)
        missing_roles = d.get("missing_roles") or []
        entry_count = d.get("entry_count", 0)
        missing_str = ", ".join(missing_roles) if missing_roles else "none identified"
        summary = (
            f"Thread '{topic}' is {concentration:.0%} {dominant_role}; "
            f"missing: {missing_str} ({entry_count} entries)"
        )
        action = AdvisoryAction(
            phase="pre",
            tool="watercooler_list_thread_entries",
            arguments={"topic": topic, "format": "json"},
            reason="Inspect entry role distribution for this thread",
        )
        # Deterministic relevance_tags: sort missing_roles, pick first, dedup with "pm".
        # "pm" is always present, so the tuple is structurally non-empty.
        sorted_missing = sorted(missing_roles) if missing_roles else []
        first_missing = sorted_missing[0] if sorted_missing else None
        tags = tuple(dict.fromkeys(t for t in (first_missing, "pm") if t))

    elif category == "stalled_dropout":
        contributor = d.get("contributor", "unknown")
        contributor_entries = d.get("contributor_entries", 0)
        summary = (
            f"Contributor '{contributor}' dropped out of '{topic}' "
            f"after {contributor_entries} entries"
        )
        action = AdvisoryAction(
            phase="pre",
            tool="watercooler_search",
            arguments={"query": contributor, "thread_topic": topic, "limit": 20},
            reason="Retrieve dropped contributor's last known context within this thread",
        )
        tags = ("pm", "planner")

    elif category == "aware_burst":
        multiplier = d.get("multiplier", 0.0)
        new_entries = d.get("new_entries", 0)
        summary = (
            f"Thread '{topic}' saw {multiplier:.1f}x activity spike "
            f"({new_entries} new entries)"
        )
        action = AdvisoryAction(
            phase="pre",
            tool="watercooler_read_thread",
            arguments={"topic": topic, "summary_only": True},
            reason="Get thread narrative before investigating coordination issue",
        )
        tags = ("pm",)

    elif category == "connect_role_complement":
        missing_role = d.get("missing_role", "unknown")
        related_topic = d.get("related_thread_topic", "unknown")
        role_count = d.get("related_thread_role_entry_count", 0)
        summary = (
            f"Thread '{topic}' has no active {missing_role}; "
            f"related thread '{related_topic}' has {role_count} {missing_role} entries"
        )
        action = AdvisoryAction(
            phase="pre",
            tool="watercooler_read_thread",
            arguments={"topic": related_topic, "summary_only": True},
            reason=(
                f"Read related thread '{related_topic}' to understand "
                f"its active {missing_role} contribution"
            ),
        )
        tags = (missing_role, "pm") if missing_role != "pm" else ("pm",)

    else:  # pragma: no cover -- defensive; covered by _LEAD_TRIGGER_CATEGORIES filter
        return None

    t2_ctx = (
        _build_t2_context(thread_analysis, rule_flags)
        if thread_analysis is not None
        else None
    )

    return CoordinatorLead(
        schema_version=1,
        source_category=category,
        source_topic=topic,
        summary=summary,
        relevance_tags=tags,
        suggested_action=action,
        t2_context=t2_ctx,
    )


def generate_leads_for_thread(
    thread_findings: list[CoordinatorFinding],
    *,
    analysis_by_topic: dict[str, dict[str, Any]] | None = None,
    analysis_rule_flags: dict[str, list[str]] | None = None,
) -> list[CoordinatorFinding]:
    """Generate coordinator_lead findings from v1A per-thread detector results.

    One lead per triggering v1A finding. ``aware_new_contributor`` is excluded
    (informational only, corpus-scoped, not thread-specific).

    Each returned CoordinatorFinding has ``category="coordinator_lead"``,
    ``details={"lead": asdict(lead)}``, and ``dedup_signature`` derived from
    the source finding's own dedup signature so distinct source findings (e.g.
    two ``stalled_dropout`` findings for different contributors on the same
    thread) produce distinct leads.

    Suppression inheritance: the lead inherits ``severity`` and
    ``details["suppressed_by"]`` (if present) from the source finding so
    parked-thread leads stay quiet alongside v1A.

    Args:
        thread_findings: v1A CoordinatorFinding items for a single thread.
        analysis_by_topic: Optional mapping of topic → window_threads entry from
            AnalysisSnapshotDaemon.  When present, the matching entry is used to
            populate ``t2_context`` on each lead.  Missing topics yield ``None``.
        analysis_rule_flags: Optional mapping of topic → list of rule IDs that
            flagged that topic.  Passed into ``_build_t2_context()`` when the
            analysis entry is present.
    """
    leads: list[CoordinatorFinding] = []
    for source_cf in thread_findings:
        if source_cf.category not in _LEAD_TRIGGER_CATEGORIES:
            continue
        if not source_cf.dedup_signature:
            logger.warning(
                "skipping lead generation for %s on topic %r: empty dedup_signature",
                source_cf.category,
                source_cf.topic,
            )
            continue

        thread_analysis = (
            analysis_by_topic.get(source_cf.topic)
            if analysis_by_topic is not None
            else None
        )
        rule_flags = (
            analysis_rule_flags.get(source_cf.topic)
            if analysis_rule_flags is not None
            else None
        )
        lead = _build_lead_for_finding(source_cf, thread_analysis, rule_flags)
        if lead is None:
            continue

        details: dict[str, Any] = {"lead": asdict(lead)}
        # Propagate suppression metadata from the source finding.
        suppressed_by = source_cf.details.get("suppressed_by")
        if suppressed_by:
            details["suppressed_by"] = suppressed_by
        # Propagate relation_evidence for connect_role_complement reviewability.
        relation_evidence = source_cf.details.get("relation_evidence")
        if relation_evidence:
            details["relation_evidence"] = relation_evidence

        leads.append(
            CoordinatorFinding(
                category="coordinator_lead",
                topic=source_cf.topic,
                severity=source_cf.severity,
                message=lead.summary,
                details=details,
                dedup_signature=f"coordinator_lead|{source_cf.dedup_signature}",
                entry_id=source_cf.entry_id,
            )
        )
    return leads


# ---------------------------------------------------------------------------
# Detector 6: connect_role_complement (Phase 3d-1)
# ---------------------------------------------------------------------------


def _build_xref_topic_graph(
    threads_dir: Path,
    all_topics: list[str],
    entry_topic_index: dict[str, str],
) -> dict[frozenset[str], dict[str, Any]]:
    """Build bidirectional xref evidence map across *all_topics*.

    Returns a mapping from ``frozenset({topic_a, topic_b})`` to the first
    xref edge found between them::

        {
            "source_topic": str,     # topic whose annotation carries the xref
            "source_entry_id": str,  # annotation state key (entry in source_topic)
            "target_entry_id": str,  # xref target entry (in other topic)
        }

    Fail-open: annotation load failure for any topic silently skips it.
    Only topics present in *all_topics* are considered.

    Note: ``_MAX_XREFS_TOTAL`` applies per source-topic (``seen`` is reset
    each iteration), so the whole-graph cap is ``N_topics × _MAX_XREFS_TOTAL``.
    """
    pairs: dict[frozenset[str], dict[str, Any]] = {}
    topic_set = set(all_topics)

    for source_topic in all_topics:
        states = _load_thread_annotation_states(threads_dir, source_topic)
        if not states:
            continue
        seen: set[str] = set()
        try:
            for state_key, ann in states.items():
                if len(seen) >= _MAX_XREFS_TOTAL:
                    break
                xrefs = (ann.xrefs or [])[:_MAX_XREFS_PER_STATE]
                for target_entry_id in xrefs:
                    if target_entry_id in seen:
                        continue
                    if len(seen) >= _MAX_XREFS_TOTAL:
                        break
                    seen.add(target_entry_id)
                    target_topic = entry_topic_index.get(target_entry_id)
                    if not target_topic or target_topic == source_topic:
                        continue
                    if target_topic not in topic_set:
                        continue
                    pair_key: frozenset[str] = frozenset({source_topic, target_topic})
                    if pair_key not in pairs:
                        pairs[pair_key] = {
                            "source_topic": source_topic,
                            "source_entry_id": state_key,
                            "target_entry_id": target_entry_id,
                        }
        except Exception as exc:
            logger.debug(
                "_build_xref_topic_graph: traversal failed for %s: %s",
                source_topic,
                exc,
                exc_info=True,
            )

    return pairs


def _extract_shape_name(entry: Any) -> str | None:
    """Safely extract ``workflow_shape.name`` from an analysis entry.

    Tolerates schema drift at every level: non-dict entries, non-dict
    ``workflow_shape`` values, and non-string ``name`` values all yield
    ``None`` rather than raising.
    """
    if not isinstance(entry, dict):
        return None
    shape = entry.get("workflow_shape")
    if not isinstance(shape, dict):
        return None
    name = shape.get("name")
    return name if isinstance(name, str) and name else None


def _resolve_related_threads(
    thread_topic: str,
    all_active_tags: dict[str, set[str]],
    xref_pairs: dict[frozenset[str], dict[str, Any]],
    analysis_by_topic: dict[str, Any] | None,
    risk_clusters: list[tuple[str, str, frozenset[str]]] | None,
    pair_tag_prefix: str,
) -> dict[str, list[dict[str, Any]]]:
    """For thread A, find all related threads B with their relation evidence.

    Returns ``{related_topic: [evidence_items]}``.  Each evidence item has a
    ``tier`` key from ``{"xref", "pair_tag", "pulse_block+workflow_shape"}``.

    Combination rules (per addendum):
    - Tier 1 (xref) alone → related.
    - Tier 2 (pair_tag) alone → related.
    - Tier 3 (risk cluster) ∩ Tier 4 (shared workflow shape) → related.
    - Either Tier 3 or Tier 4 alone → not related.

    Tier 3+4 evidence requires a fresh pulse_block snapshot (``risk_clusters``
    is ``None`` when unavailable) AND analysis context (``analysis_by_topic``
    is ``None`` when the snapshot was not loaded).  Both ``None`` cases are
    valid, intentional no-ops — not bugs.
    """
    related: dict[str, list[dict[str, Any]]] = {}
    a_tags = all_active_tags.get(thread_topic, set())

    # Tier 1: explicit xref linkage
    for other_topic in all_active_tags:
        if other_topic == thread_topic:
            continue
        pair_key: frozenset[str] = frozenset({thread_topic, other_topic})
        edge = xref_pairs.get(pair_key)
        if edge is None:
            continue
        direction = "a_to_b" if edge["source_topic"] == thread_topic else "b_to_a"
        ev: dict[str, Any] = {
            "tier": "xref",
            "source_entry_id": edge["source_entry_id"],
            "target_entry_id": edge["target_entry_id"],
            "direction": direction,
        }
        related.setdefault(other_topic, []).append(ev)

    # Tier 2: shared pairing tag
    a_pair_tags = {t for t in a_tags if t.startswith(pair_tag_prefix)}
    if a_pair_tags:
        for other_topic, other_tags in all_active_tags.items():
            if other_topic == thread_topic:
                continue
            shared = a_pair_tags & other_tags
            if shared:
                related.setdefault(other_topic, []).append(
                    {"tier": "pair_tag", "tags": sorted(shared)}
                )

    # Tier 3 ∩ Tier 4: pulse_block co-affected + shared workflow shape (combined only)
    if risk_clusters is not None and analysis_by_topic is not None:
        a_shape_name = _extract_shape_name(analysis_by_topic.get(thread_topic))
        if a_shape_name:
            shape_peers = {
                t
                for t, ta in analysis_by_topic.items()
                if t != thread_topic
                and _extract_shape_name(ta) == a_shape_name
                and t in all_active_tags
            }
            for rule_id, risk_text, cluster_topics in risk_clusters:
                if thread_topic not in cluster_topics:
                    continue
                qualifying = (cluster_topics & shape_peers) - {thread_topic}
                for other_topic in qualifying:
                    related.setdefault(other_topic, []).append(
                        {
                            "tier": "pulse_block+workflow_shape",
                            "risk_rule_id": rule_id,
                            "risk_text": risk_text,
                            "workflow_shape_name": a_shape_name,
                        }
                    )

    return related


def detect_role_complement(
    all_active_entries: dict[str, list[EntryView]],
    all_active_tags: dict[str, set[str]],
    threads_dir: Path | None,
    entry_topic_index: dict[str, str],
    analysis_by_topic: dict[str, Any] | None,
    risk_clusters: list[tuple[str, str, frozenset[str]]] | None,
    *,
    monitored_roles: list[str],
    max_per_thread: int = 3,
    pair_tag_prefix: str = "pair:",
    min_role_entries_in_related: int = 2,
) -> list[CoordinatorFinding]:
    """Detect threads missing a role that is actively exercised in a related thread.

    Ships disabled by default — the call site guards on
    ``role_complement_enabled``.  This function is always safe to call; it
    returns an empty list if *monitored_roles* is empty or no qualifying
    pairs are found.

    Fail-open: any exception during xref graph building or relation resolution
    is caught and logged; the detector returns partial results rather than
    raising to the tick scheduler.
    """
    if not monitored_roles:
        return []

    all_topics = list(all_active_entries.keys())

    # Build xref pair graph once per call (fail-open to empty dict)
    xref_pairs: dict[frozenset[str], dict[str, Any]] = {}
    if threads_dir is not None and all_topics:
        try:
            xref_pairs = _build_xref_topic_graph(threads_dir, all_topics, entry_topic_index)
        except Exception as exc:
            logger.warning(
                "detect_role_complement: xref graph build failed — Tier 1 evidence disabled: %s",
                exc,
                exc_info=True,
            )

    # Pre-compute role counters for all active threads
    role_counts: dict[str, Counter[str]] = {}
    for topic, entries in all_active_entries.items():
        c: Counter[str] = Counter()
        for e in entries:
            r = e["role"]
            if r:
                c[r] += 1
        role_counts[topic] = c

    findings: list[CoordinatorFinding] = []

    for thread_topic, entries in all_active_entries.items():
        a_counts = role_counts[thread_topic]
        missing_roles = [r for r in monitored_roles if a_counts.get(r, 0) == 0]
        if not missing_roles:
            continue

        try:
            related = _resolve_related_threads(
                thread_topic,
                all_active_tags,
                xref_pairs,
                analysis_by_topic,
                risk_clusters,
                pair_tag_prefix,
            )
        except Exception as exc:
            logger.debug(
                "detect_role_complement: relation resolution failed for %s: %s",
                thread_topic,
                exc,
                exc_info=True,
            )
            continue

        if not related:
            continue

        # Collect all (role, thread) candidates globally before capping so that
        # weakest-first truncation is applied across roles, not per-role sequentially.
        # Tier rank: 0=xref (strongest), 1=pair_tag, 2=pulse_block+workflow_shape (weakest).
        all_candidates: list[tuple[int, str, str, list[dict[str, Any]], int]] = []
        for missing_role in missing_roles:
            for b_topic, evidence in related.items():
                count_in_b = role_counts.get(b_topic, Counter()).get(missing_role, 0)
                if count_in_b < min_role_entries_in_related:
                    continue
                if any(e["tier"] == "xref" for e in evidence):
                    tier_rank = 0
                elif any(e["tier"] == "pair_tag" for e in evidence):
                    tier_rank = 1
                else:
                    tier_rank = 2
                all_candidates.append((tier_rank, missing_role, b_topic, evidence, count_in_b))

        # Sort strongest-first; break ties by monitored_roles order then b_topic for determinism.
        role_order = {r: i for i, r in enumerate(missing_roles)}
        all_candidates.sort(key=lambda c: (c[0], role_order.get(c[1], 999), c[2]))

        truncated = len(all_candidates) > max_per_thread
        thread_findings: list[CoordinatorFinding] = []
        for _tier_rank, missing_role, b_topic, evidence, count_in_b in all_candidates[
            :max_per_thread
        ]:
            thread_findings.append(
                CoordinatorFinding(
                    category="connect_role_complement",
                    topic=thread_topic,
                    severity="info",
                    message=(
                        f"Thread '{thread_topic}' has no active {missing_role}; "
                        f"related thread '{b_topic}' has an active {missing_role}."
                    ),
                    details={
                        "missing_role": missing_role,
                        "thread_topic": thread_topic,
                        "related_thread_topic": b_topic,
                        "related_thread_role_entry_count": count_in_b,
                        "relation_evidence": evidence,
                    },
                    dedup_signature=f"{thread_topic}|{missing_role}|{b_topic}",
                )
            )

        if truncated and thread_findings:
            thread_findings[0].details["role_complement_truncated"] = True

        findings.extend(thread_findings)

    return findings
