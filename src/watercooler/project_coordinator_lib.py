"""Project Coordinator shared library — pure detection logic.

Stdlib-only, fully testable.  All five v1A detectors live here as
stateless functions that the daemon calls each tick.

Type definitions (``EntryView``, ``BurstBaseline``, ``CoordinatorExtras``)
enforce typed boundaries between the daemon and these detectors.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any, TypedDict

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
            "active_signals": {
                k: v.to_dict() for k, v in self.active_signals.items()
            },
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


def _check_suppression(
    thread_tags: set[str],
    suppression_tags: set[str],
) -> tuple[str, dict[str, str]]:
    """Check whether a thread is suppressed by annotation tags.

    Returns:
        (severity, extra_details) — severity is "info" if suppressed,
        "warning" otherwise.  extra_details contains "suppressed_by" key
        when suppressed, empty dict otherwise.
    """
    matched = thread_tags & suppression_tags
    if matched:
        tag = sorted(matched)[0]
        return "info", {"suppressed_by": f"tag:{tag}"}
    return "warning", {}


# ---------------------------------------------------------------------------
# Detector 1: stalled_open_loop
# ---------------------------------------------------------------------------


def detect_stalled_open_loops(
    entries: list[EntryView],
    thread_topic: str,
    thread_status: str,
    suppression_tags: set[str],
    thread_tags: set[str],
    tick_time: float = 0.0,
) -> CoordinatorFinding | None:
    """Detect threads with Plan entries but no Decision or Closure.

    A staleness gate ensures threads with recent activity (< OPEN_LOOP_MIN_STALE_DAYS)
    are not flagged — a Plan posted yesterday isn't "stalled."
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
) -> tuple[CoordinatorFinding | None, BurstBaseline]:
    """Detect sudden spikes in entry volume relative to thread baseline."""
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
        return (
            CoordinatorFinding(
                category="aware_burst",
                topic=thread_topic,
                severity="info",
                message=(
                    f"Activity burst in '{thread_topic}': {current_rate:.1f} entries/day "
                    f"vs baseline {baseline.baseline_rate:.1f} entries/day "
                    f"({multiplier:.1f}x)"
                ),
                details={
                    "current_rate": round(current_rate, 2),
                    "baseline_rate": round(baseline.baseline_rate, 2),
                    "multiplier": round(multiplier, 2),
                    "new_entries": new_entries,
                },
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
) -> CoordinatorFinding | None:
    """Detect threads dominated by a single role."""
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
    canonical_roles = {"planner", "critic", "implementer", "tester", "pm", "scribe"}
    present_roles = set(role_counter.keys())
    missing_roles = sorted(canonical_roles - present_roles)

    return CoordinatorFinding(
        category="aware_role_concentration",
        topic=thread_topic,
        severity="info",
        message=(
            f"Thread '{thread_topic}' is {concentration:.0%} {dominant_role} "
            f"({dominant_count}/{total_named} entries)"
        ),
        details={
            "dominant_role": dominant_role,
            "concentration": round(concentration, 3),
            "role_distribution": dict(role_counter),
            "entry_count": len(entries),
            "missing_roles": missing_roles,
        },
        dedup_signature=f"aware_role_concentration|{thread_topic}|{dominant_role}",
    )
