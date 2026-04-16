"""Project Coordinator Daemon — T1-backed coordination intelligence.

Scans the baseline graph for coordination signals: stalled open loops,
contributor dropout, activity bursts, new contributors, and role
concentration.  Zero T2/LLM dependency — all detectors are pure functions
in ``watercooler.project_coordinator_lib``.

This daemon produces Finding objects consumed via ``watercooler_daemon_findings``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from watercooler.baseline_graph import storage
from watercooler.baseline_graph.storage import get_graph_dir
from watercooler.baseline_graph.writer import (
    get_entries_for_thread,
    get_thread_from_graph,
)
from watercooler.config_schema import ProjectCoordinatorConfig
from watercooler.project_coordinator_lib import (
    ActiveSignalEntry,
    BurstBaseline,
    CoordinatorExtras,
    CoordinatorFinding,
    EntryView,
    detect_aware_burst,
    detect_new_contributors,
    detect_role_concentration,
    detect_stalled_dropout,
    detect_stalled_open_loops,
    entries_to_views,
    generate_leads_for_thread,
    parse_entry_timestamp,
)
from watercooler.pulse_stance_lib import build_stance_advisories

from .base import BaseDaemon
from .hosted_data import is_daemon_hosted_mode
from .state import Finding, build_finding_id, load_findings

logger = logging.getLogger(__name__)

# Re-sync dedup cache from disk every N ticks to evict acknowledged/compacted keys
_DEDUP_RESYNC_INTERVAL = 10

# Hard cap on findings loaded for dedup — prevents unbounded memory growth.
_DEDUP_LIMIT = 50_000

# v1B: Time-sensitive detector categories that need periodic re-evaluation
# even when thread content hasn't changed (wall-clock dependent).
_TIME_SENSITIVE_CATEGORIES = frozenset({"stalled_open_loop", "aware_burst"})

# Re-evaluate time-sensitive signals for unchanged threads older than this.
_ACTIVE_SIGNALS_RESCAN_AGE = 86400.0  # 24 hours


class ProjectCoordinatorDaemon(BaseDaemon):
    """Coordination intelligence scanner.

    Reads baseline graph entries and thread metadata to detect
    coordination signals.  All detection logic lives in the shared
    lib (``project_coordinator_lib``); this class manages graph I/O,
    checkpoint, dedup, and annotation loading.

    Args:
        interval: Seconds between scans.
        config: ProjectCoordinatorConfig instance.
        threads_dir: Override threads directory (None = resolve at tick time).
        enabled: Whether this daemon is active.
    """

    def __init__(
        self,
        *,
        interval: float = 600.0,
        config: ProjectCoordinatorConfig | None = None,
        threads_dir: Path | None = None,
        enabled: bool = True,
    ) -> None:
        super().__init__(
            name="project_coordinator",
            interval=interval,
            enabled=enabled,
            tick_on_interval=True,
        )
        self._config = config or ProjectCoordinatorConfig()
        self._threads_dir_override = threads_dir
        self._resolved_threads_dir: Path | None = None
        self._scope_id: str = ""
        # Dedup cache: finding_id str → already reported
        self._existing_keys: set[str] = set()
        self._ticks_since_resync: int = 0
        # Rolling state persisted in checkpoint extras
        self._extras = CoordinatorExtras()
        # Per-tick metrics
        self._last_tick_threads: int = 0
        self._last_tick_findings: int = 0
        self._last_tick_skipped: int = 0
        self._last_tick_leads: int = 0
        # v1B stance observability
        self._last_snapshot_available: bool = False
        self._last_snapshot_age_hours: float | None = None
        self._last_stance_outcome: str = "disabled"
        self._last_stance_levels: dict[str, int] = {}

    def _resolve_threads_dir(self) -> Path | None:
        """Resolve the threads directory for scanning.

        Cached after first successful resolution.
        """
        if self._threads_dir_override is not None:
            return self._threads_dir_override

        if self._resolved_threads_dir is not None:
            return self._resolved_threads_dir

        try:
            from watercooler_mcp.config import resolve_thread_context

            ctx = resolve_thread_context(Path.cwd())
            self._resolved_threads_dir = ctx.threads_dir
            # Derive scope_id for deterministic finding IDs
            try:
                from watercooler.pulse_snapshot_lib import derive_repo_key

                self._scope_id = derive_repo_key(ctx.code_root)
            except Exception:
                self._scope_id = ""
            return self._resolved_threads_dir
        except Exception as exc:
            logger.debug(
                "DAEMON[project_coordinator]: could not resolve threads_dir: %s", exc
            )
            return None

    def _load_extras(self) -> None:
        """Load extras from checkpoint on first tick."""
        raw = self._checkpoint.extras or {}
        self._extras = CoordinatorExtras.from_dict(raw)

    def _save_extras(self) -> None:
        """Persist extras to checkpoint."""
        self._checkpoint.extras = self._extras.to_dict()

    def _load_thread_tags(self, graph_dir: Path, topic: str) -> set[str]:
        """Load annotation tags for a thread (read-only)."""
        try:
            from watercooler.baseline_graph.annotations import load_or_rebuild_state

            thread_dir = storage.get_thread_graph_dir(graph_dir, topic)
            states = load_or_rebuild_state(thread_dir, read_only=True)
            # Thread-level annotations are keyed by topic
            thread_state = states.get(topic)
            if thread_state and thread_state.tags:
                return set(thread_state.tags)
        except Exception as exc:
            logger.debug(
                "DAEMON[project_coordinator]: could not load annotations for %s: %s",
                topic,
                exc,
            )
        return set()

    def _accumulate_contributors(
        self,
        entries: list[EntryView],
        topic: str,
        all_contributors: dict[str, float],
        contributor_threads: dict[str, set[str]],
        normalize_fn: Callable[[str], str],
    ) -> None:
        """Accumulate contributor timestamps and thread associations."""
        for e in entries:
            if e["agent"]:
                contrib = normalize_fn(e["agent"])
                ts = parse_entry_timestamp(e)
                if ts is not None:
                    all_contributors[contrib] = max(
                        all_contributors.get(contrib, 0.0), ts
                    )
                    contributor_threads.setdefault(contrib, set()).add(topic)

    def tick(self) -> list[Finding]:
        """Run one coordination scan cycle."""
        if is_daemon_hosted_mode():
            return self._tick_hosted()

        threads_dir = self._resolve_threads_dir()
        if threads_dir is None or not threads_dir.exists():
            logger.debug("DAEMON[project_coordinator]: no threads_dir, skipping")
            self._update_tick_metrics(0, 0, 0)
            return []

        graph_dir = get_graph_dir(threads_dir)

        # Guard: if the graph's threads directory doesn't exist yet (startup,
        # first-run, or transient unavailability), skip without touching state.
        # This is distinct from the intentional empty case — when all thread
        # directories have been genuinely removed, the threads/ dir still exists
        # and list_thread_topics returns [] with it present. In that case we
        # fall through so that pruning and tombstone emission run correctly.
        if not (graph_dir / "threads").exists():
            self._update_tick_metrics(0, 0, 0)
            return []

        topics = storage.list_thread_topics(graph_dir)

        # Load rolling state from checkpoint extras (always, even when topics is
        # empty — so that stance pruning and tombstone emission run for cleared threads)
        self._load_extras()
        # Tick-scoped corpus signal counts — cleared at start, populated in the
        # corpus-level branch below, consumed by _emit_stance_advisories().
        self._extras.corpus_signal_inputs.clear()

        # Bootstrap or periodically re-sync dedup set from disk
        self._ticks_since_resync += 1
        if (
            not self._existing_keys
            or self._ticks_since_resync >= _DEDUP_RESYNC_INTERVAL
        ):
            existing = load_findings(
                self.name, limit=_DEDUP_LIMIT, unacknowledged_only=True
            )
            if len(existing) >= _DEDUP_LIMIT:
                logger.warning(
                    "DAEMON[project_coordinator]: dedup cache truncated at %d findings; "
                    "duplicates may occur",
                    _DEDUP_LIMIT,
                )
            self._existing_keys = {f.finding_id for f in existing}
            # Filter out stance fids explicitly cleared by tombstone emission —
            # prevents disk-based resync from re-blocking re-escalation (todo 282)
            self._existing_keys -= self._extras.cleared_stance_fids
            self._ticks_since_resync = 0

        cfg = self._config
        suppression_tags = set(cfg.suppression_tags)
        tick_time = time.time()
        findings: list[Finding] = []
        skipped = 0
        # v1B follow-on: tick-scoped deferred lead buffer. Populated per-thread
        # after active_signals update; drained in Phase C after the corpus-level
        # detect_new_contributors() branch so v1A findings win the cap.
        pending_leads: list[CoordinatorFinding] = []
        leads_count = 0
        # v1B follow-on: per-thread checkpoint/baseline updates are buffered
        # here during Phase A and only committed after Phase C confirms every
        # pending lead for the topic has landed. This prevents the gate at
        # is_thread_changed() from skipping a thread whose lead was dropped
        # by the cap in Phase C — on the next tick the thread re-scans, the
        # v1A source is blocked by _existing_keys, but generate_leads_for_thread
        # re-mints the lead from the still-present thread state and Phase C
        # gets another chance to drain it.
        pending_checkpoints: dict[str, tuple[float, int, BurstBaseline]] = {}
        # Corpus-level accumulators for new-contributor detector
        all_contributors: dict[str, float] = {}
        contributor_threads: dict[str, set[str]] = {}

        from watercooler.analysis_lib import normalize_agent

        already_scanned_this_tick: set[str] = set()

        for topic in topics:
            if len(findings) >= cfg.max_findings_per_run:
                break

            # Read thread metadata
            try:
                thread_node = get_thread_from_graph(threads_dir, topic)
                status = (thread_node or {}).get("status", "OPEN")
            except (OSError, KeyError, ValueError):
                status = "OPEN"

            # Incremental: skip unchanged threads
            try:
                meta_file = storage.get_thread_graph_dir(graph_dir, topic) / "meta.json"
                mtime = meta_file.stat().st_mtime if meta_file.exists() else 0.0
            except OSError:
                mtime = 0.0

            try:
                raw_entries = get_entries_for_thread(threads_dir, topic)
                entry_count = len(raw_entries)
            except (OSError, KeyError, ValueError) as exc:
                logger.debug(
                    "DAEMON[project_coordinator]: error reading graph for %s: %s",
                    topic,
                    exc,
                )
                continue

            if not self._checkpoint.is_thread_changed(topic, mtime, entry_count):
                # Still accumulate contributor data for corpus-level detector
                entries = entries_to_views(raw_entries)
                self._accumulate_contributors(
                    entries,
                    topic,
                    all_contributors,
                    contributor_threads,
                    normalize_agent,
                )
                skipped += 1
                continue

            entries = entries_to_views(raw_entries)

            # Load annotation tags for suppression
            thread_tags = self._load_thread_tags(graph_dir, topic)

            # Accumulate contributor data
            self._accumulate_contributors(
                entries,
                topic,
                all_contributors,
                contributor_threads,
                normalize_agent,
            )

            # --- Per-thread detectors ---
            cap_hit = False
            thread_findings: list[CoordinatorFinding] = []

            # Detector 1: stalled_open_loop
            f = detect_stalled_open_loops(
                entries,
                topic,
                status,
                suppression_tags,
                thread_tags,
                tick_time=tick_time,
            )
            if f:
                thread_findings.append(f)

            # Detector 2: stalled_dropout
            thread_findings.extend(
                detect_stalled_dropout(
                    entries,
                    topic,
                    status,
                    suppression_tags,
                    thread_tags,
                    normalize_agent_fn=normalize_agent,
                )
            )

            # Detector 3: aware_burst
            baseline = self._extras.burst_baselines.get(topic)
            burst_finding, updated_baseline = detect_aware_burst(
                entries, topic, baseline, tick_time
            )
            if burst_finding:
                thread_findings.append(burst_finding)

            # Detector 5: aware_role_concentration
            rc = detect_role_concentration(entries, topic, status)
            if rc:
                thread_findings.append(rc)

            # v1B: update active_signals for this thread (detectors ran)
            already_scanned_this_tick.add(topic)
            detected_categories = {cf.category for cf in thread_findings}
            self._extras.active_signals[topic] = ActiveSignalEntry(
                categories=detected_categories,
                last_evaluated_at=tick_time,
            )

            # v1B follow-on: generate leads but DO NOT materialize inline.
            # Leads are drained in Phase C after v1A per-thread + corpus
            # detectors have claimed the cap. This enforces the priority
            # v1A per-thread > v1A corpus > coordinator_lead.
            if cfg.leads_enabled:
                pending_leads.extend(generate_leads_for_thread(thread_findings))

            # Materialize into Finding objects with dedup
            for cf in thread_findings:
                if len(findings) >= cfg.max_findings_per_run:
                    cap_hit = True
                    break

                fid = build_finding_id(
                    scope_id=self._scope_id,
                    daemon_name=self.name,
                    topic=cf.topic,
                    category=cf.category,
                    entry_id=cf.entry_id,
                    dedup_signature=cf.dedup_signature,
                )
                if fid in self._existing_keys:
                    continue

                findings.append(
                    Finding(
                        finding_id=fid,
                        daemon_name=self.name,
                        severity=cf.severity,
                        category=cf.category,
                        topic=cf.topic,
                        entry_id=cf.entry_id,
                        message=cf.message,
                        details=cf.details,
                    )
                )
                self._existing_keys.add(fid)

            # Checkpoint safety: only buffer if fully scanned. The actual
            # commit happens post-Phase-C so dropped leads can force a rescan
            # on the next tick. If the cap interrupted materialization, the
            # burst finding was lost, so the baseline must not advance (rescan
            # next tick will re-detect it).
            if not cap_hit:
                pending_checkpoints[topic] = (mtime, entry_count, updated_baseline)
            elif baseline is not None:
                # Cap hit: preserve the previous baseline so burst can re-fire
                self._extras.burst_baselines[topic] = baseline

        # --- Corpus-level detector: aware_new_contributor ---
        if len(findings) < cfg.max_findings_per_run:
            nc_findings, updated_seen = detect_new_contributors(
                all_contributors=all_contributors,
                seen_set=dict(self._extras.seen_contributors),
                tick_time=tick_time,
                contributor_threads={
                    k: sorted(v) for k, v in contributor_threads.items()
                },
            )
            # P2.1: corpus-level signal transport for stance coord_counts.
            # Uses raw detected count (pre-cap) so stance reacts to presence
            # of new contributors even when emission is capped.
            if nc_findings:
                self._extras.corpus_signal_inputs["aware_new_contributor"] = len(
                    nc_findings
                )
            # Track which contributors produced a finding (emitted or not)
            contributors_with_findings: set[str] = set()
            for cf in nc_findings:
                contributors_with_findings.add(cf.details.get("contributor", ""))

            emitted_contributors: set[str] = set()
            for cf in nc_findings:
                if len(findings) >= cfg.max_findings_per_run:
                    break
                fid = build_finding_id(
                    scope_id=self._scope_id,
                    daemon_name=self.name,
                    topic=cf.topic,
                    category=cf.category,
                    entry_id=cf.entry_id,
                    dedup_signature=cf.dedup_signature,
                )
                if fid in self._existing_keys:
                    continue
                findings.append(
                    Finding(
                        finding_id=fid,
                        daemon_name=self.name,
                        severity=cf.severity,
                        category=cf.category,
                        topic=cf.topic,
                        entry_id=cf.entry_id,
                        message=cf.message,
                        details=cf.details,
                    )
                )
                self._existing_keys.add(fid)
                emitted_contributors.add(cf.details.get("contributor", ""))

            # Advance seen-set only for:
            # - Contributors whose finding was actually emitted
            # - Contributors who had no finding (known/recently-seen, just update ts)
            # Do NOT advance contributors whose finding was dropped by the cap —
            # they must re-fire on the next tick.
            dropped_contributors = contributors_with_findings - emitted_contributors
            for contributor, ts in updated_seen.items():
                if contributor in dropped_contributors:
                    continue
                old_ts = self._extras.seen_contributors.get(contributor)
                if old_ts is not None or contributor in emitted_contributors:
                    self._extras.seen_contributors[contributor] = ts
            # Apply pruning from the updated set (but not for dropped contributors)
            for contributor in list(self._extras.seen_contributors):
                if (
                    contributor not in updated_seen
                    and contributor not in dropped_contributors
                ):
                    del self._extras.seen_contributors[contributor]

        # --- Phase C: drain deferred leads last (after v1A + corpus) ---
        # Leads consume only residual cap, so v1A per-thread and corpus-level
        # findings are guaranteed to land first. Stance advisories (emitted
        # later) are cap-exempt and sit outside this ordering.
        # Walk the full list even after the cap fills so every topic whose
        # lead was dropped gets recorded — its checkpoint will be held back
        # below so the next tick re-scans and re-mints the lead.
        topics_with_dropped_leads: set[str] = set()
        if cfg.leads_enabled and pending_leads:
            for cf in pending_leads:
                if len(findings) >= cfg.max_findings_per_run:
                    topics_with_dropped_leads.add(cf.topic)
                    continue
                fid = build_finding_id(
                    scope_id=self._scope_id,
                    daemon_name=self.name,
                    topic=cf.topic,
                    category=cf.category,
                    entry_id=cf.entry_id,
                    dedup_signature=cf.dedup_signature,
                )
                if fid in self._existing_keys:
                    continue
                findings.append(
                    Finding(
                        finding_id=fid,
                        daemon_name=self.name,
                        severity=cf.severity,
                        category=cf.category,
                        topic=cf.topic,
                        entry_id=cf.entry_id,
                        message=cf.message,
                        details=cf.details,
                    )
                )
                self._existing_keys.add(fid)
                leads_count += 1

        # --- Commit deferred checkpoints (minus topics with dropped leads) ---
        # Threads whose lead was dropped in Phase C are intentionally left
        # un-checkpointed so is_thread_changed() returns True on the next
        # tick. The v1A source is blocked by _existing_keys (already emitted),
        # but generate_leads_for_thread re-mints the lead and Phase C gets
        # another chance to drain it under a lifted cap.
        for topic, (
            mtime,
            entry_count,
            baseline_to_commit,
        ) in pending_checkpoints.items():
            if topic in topics_with_dropped_leads:
                continue
            self._checkpoint.update_thread(topic, mtime, entry_count)
            self._extras.burst_baselines[topic] = baseline_to_commit

        # --- v1B: periodic re-evaluation for time-sensitive signals ---
        for topic, sig_entry in list(self._extras.active_signals.items()):
            if topic in already_scanned_this_tick:
                continue
            age = tick_time - sig_entry.last_evaluated_at
            if age < _ACTIVE_SIGNALS_RESCAN_AGE:
                continue
            # Only re-evaluate if the topic has (or had) time-sensitive signals
            if not (sig_entry.categories & _TIME_SENSITIVE_CATEGORIES):
                continue
            try:
                raw_entries = get_entries_for_thread(threads_dir, topic)
                re_entries = entries_to_views(raw_entries)
            except (OSError, KeyError, ValueError):
                continue

            try:
                thread_node = get_thread_from_graph(threads_dir, topic)
                re_status = (thread_node or {}).get("status", "OPEN")
            except (OSError, KeyError, ValueError):
                re_status = "OPEN"

            re_thread_tags = self._load_thread_tags(graph_dir, topic)
            re_findings: list[CoordinatorFinding] = []
            f = detect_stalled_open_loops(
                re_entries,
                topic,
                re_status,
                suppression_tags,
                re_thread_tags,
                tick_time=tick_time,
            )
            if f:
                re_findings.append(f)
            re_baseline = self._extras.burst_baselines.get(topic)
            burst_f, re_updated_baseline = detect_aware_burst(
                re_entries, topic, re_baseline, tick_time
            )
            if burst_f:
                re_findings.append(burst_f)
            # Merge: keep content-stable categories, replace time-sensitive
            content_stable = sig_entry.categories - _TIME_SENSITIVE_CATEGORIES
            time_sensitive = {cf.category for cf in re_findings}
            self._extras.active_signals[topic] = ActiveSignalEntry(
                categories=content_stable | time_sensitive,
                last_evaluated_at=tick_time,
            )
            self._extras.burst_baselines[topic] = re_updated_baseline

        # Prune stale checkpoint entries for topics no longer in graph.
        # active_signals is pruned here — before stance emission — so that
        # removed topics don't contribute coordinator signal inputs to the
        # advisory computation for this tick.
        live_topics = set(topics)
        stale = [t for t in self._checkpoint.thread_state if t not in live_topics]
        for t in stale:
            del self._checkpoint.thread_state[t]
        # Prune stale burst baselines
        stale_baselines = [
            t for t in self._extras.burst_baselines if t not in live_topics
        ]
        for t in stale_baselines:
            del self._extras.burst_baselines[t]
        # Prune stale active_signals before stance computation so removed
        # topics don't inflate coordinator signal counts this tick
        stale_signals = [t for t in self._extras.active_signals if t not in live_topics]
        for t in stale_signals:
            del self._extras.active_signals[t]

        # --- v1B: stance advisory emission ---
        if cfg.stance_enabled:
            self._emit_stance_advisories(findings, tick_time)
        else:
            self._last_stance_outcome = "disabled"
            self._last_stance_levels = {}

        # Persist rolling state
        self._save_extras()

        self._update_tick_metrics(len(topics), len(findings), skipped, leads_count)

        logger.debug(
            "DAEMON[project_coordinator]: scanned %d threads, %d findings, %d skipped",
            len(topics),
            len(findings),
            skipped,
        )
        return findings

    # ------------------------------------------------------------------
    # v1B: Stance advisory emission
    # ------------------------------------------------------------------

    def _emit_stance_advisories(
        self, findings: list[Finding], tick_time: float
    ) -> None:
        """Compute and emit stance advisories for all Phase 1 roles."""
        # Build coordinator signal inputs from active_signals map
        coordinator_signal_inputs = [
            {"category": cat}
            for entry in self._extras.active_signals.values()
            for cat in entry.categories
        ]
        # P2.1: merge corpus-level signal counts as repeated {"category": cat}
        # entries — extract_stance_signals() counts by category from the list
        # (pulse_stance_lib.py:190–193), so repetition is semantically equal
        # to a count.
        for cat, count in self._extras.corpus_signal_inputs.items():
            coordinator_signal_inputs.extend({"category": cat} for _ in range(count))

        # Load pulse snapshot (degraded if unavailable)
        snapshot = self._load_pulse_snapshot()

        # Phase 2: wire TrendSnapshotDaemon.supersession_rate once supersedes edge exists
        advisories = build_stance_advisories(
            snapshot,
            coordinator_findings=coordinator_signal_inputs,
        )

        self._last_stance_levels = {}
        any_emitted = False

        # "stance_advisory" is an intentional v1B namespace separate from the
        # "stalled_" / "aware_" v1A prefixes — it identifies cross-role advisories
        # rather than per-thread detector events.
        for advisory in advisories:
            self._last_stance_levels[advisory.role] = advisory.level
            prev_sig = self._extras.last_stance_signatures.get(advisory.role, "")

            if advisory.level == 0:
                if prev_sig:
                    # Previously elevated → now clear. Emit tombstone.
                    # Also discard the previous advisory's dedup key so
                    # a future re-escalation with the same signature can
                    # re-emit after a full clear cycle.
                    prev_fid = build_finding_id(
                        scope_id=self._scope_id,
                        daemon_name=self.name,
                        topic=f"stance:{advisory.role}",
                        category="stance_advisory",
                        entry_id="",
                        dedup_signature=prev_sig,
                    )
                    self._existing_keys.discard(prev_fid)
                    self._extras.cleared_stance_fids.add(
                        prev_fid
                    )  # persist clear across resync

                    fid = build_finding_id(
                        scope_id=self._scope_id,
                        daemon_name=self.name,
                        topic=f"stance:{advisory.role}",
                        category="stance_advisory",
                        entry_id="",
                        dedup_signature="cleared",
                    )
                    if fid not in self._existing_keys:
                        findings.append(
                            Finding(
                                finding_id=fid,
                                daemon_name=self.name,
                                severity="info",
                                category="stance_advisory",
                                topic=f"stance:{advisory.role}",
                                entry_id="",
                                message=(
                                    f"{advisory.role.title()} stance cleared"
                                    " — all signals below thresholds"
                                ),
                                details={"advisory": asdict(advisory)},
                            )
                        )
                        self._existing_keys.add(fid)
                        any_emitted = True
                    self._extras.last_stance_signatures[advisory.role] = ""
                continue  # L0, no prior → no-op

            # L1+ advisory: replace-on-change
            if prev_sig == "":
                # Escalating from L0 — clear the tombstone dedup key so it
                # can re-emit on a future L1/L2 → L0 transition.
                tombstone_fid = build_finding_id(
                    scope_id=self._scope_id,
                    daemon_name=self.name,
                    topic=f"stance:{advisory.role}",
                    category="stance_advisory",
                    entry_id="",
                    dedup_signature="cleared",
                )
                self._existing_keys.discard(tombstone_fid)
                self._extras.cleared_stance_fids.add(
                    tombstone_fid
                )  # persist across resync
            elif prev_sig != advisory.advisory_signature:
                # Signature changed while staying elevated (A→B).
                # Clear the old fid so a future cycle back to the prior
                # signature (B→A) can re-emit rather than being suppressed
                # by the stale dedup key.
                prev_fid = build_finding_id(
                    scope_id=self._scope_id,
                    daemon_name=self.name,
                    topic=f"stance:{advisory.role}",
                    category="stance_advisory",
                    entry_id="",
                    dedup_signature=prev_sig,
                )
                self._existing_keys.discard(prev_fid)
                self._extras.cleared_stance_fids.add(prev_fid)

            fid = build_finding_id(
                scope_id=self._scope_id,
                daemon_name=self.name,
                topic=f"stance:{advisory.role}",
                category="stance_advisory",
                entry_id="",
                dedup_signature=advisory.advisory_signature,
            )
            if fid not in self._existing_keys:
                findings.append(
                    Finding(
                        finding_id=fid,
                        daemon_name=self.name,
                        severity="warning" if advisory.level >= 2 else "info",
                        category="stance_advisory",
                        topic=f"stance:{advisory.role}",
                        entry_id="",
                        message=advisory.summary,
                        details={"advisory": asdict(advisory)},
                    )
                )
                self._existing_keys.add(fid)
                self._extras.cleared_stance_fids.discard(
                    fid
                )  # no longer needs filtering
                any_emitted = True

            self._extras.last_stance_signatures[advisory.role] = (
                advisory.advisory_signature
            )

        # Update observability. Expected values for deployment health monitoring:
        #   "emitted"      — one or more new advisory findings written this tick
        #   "all_l0"       — all roles at level 0, no snapshot issue
        #   "steady_state" — L1/L2 advisory unchanged (deduped), regardless of snapshot status
        #   "degraded"     — all roles at L0 AND pulse snapshot unavailable
        #   "disabled"     — stance_enabled=False in config
        if any_emitted:
            self._last_stance_outcome = "emitted"
        else:
            all_l0 = all(v == 0 for v in self._last_stance_levels.values())
            if not all_l0:
                # Elevated advisory exists and is unchanged (deduped this tick)
                self._last_stance_outcome = "steady_state"
            elif snapshot is None:
                # All roles at L0 and no pulse snapshot — degraded idle
                self._last_stance_outcome = "degraded"
            else:
                self._last_stance_outcome = "all_l0"

    def _load_pulse_snapshot(self) -> dict[str, Any] | None:
        """Load PulseSnapshotDaemon's snapshot, in-process then on-disk.

        Returns None when unavailable or stale — caller should proceed
        with degraded coordinator-only stance.
        """
        self._last_snapshot_available = False
        self._last_snapshot_age_hours = None

        # Path 1: in-process daemon (duck-typed to avoid sibling import coupling)
        try:
            from watercooler_mcp.daemons import get_daemon_manager

            manager = get_daemon_manager()
            if manager is not None:
                ps = manager.get_daemon("pulse_snapshot")
                if hasattr(ps, "get_snapshot"):
                    snap = ps.get_snapshot(self._scope_id or "")
                    if snap is not None and self._is_snapshot_fresh(snap):
                        self._last_snapshot_available = True
                        return snap
        except Exception as exc:
            logger.debug(
                "DAEMON[project_coordinator]: error reading in-process pulse_snapshot: %s",
                exc,
            )

        # Path 2: on-disk checkpoint
        try:
            from .state import load_checkpoint

            cp = load_checkpoint("pulse_snapshot", namespace=self.state_namespace)
            repo_key = self._scope_id or ""
            projects = cp.extras.get("projects", {})
            snap = projects.get(repo_key, {}).get("pulse_snapshot")
            if snap is not None and self._is_snapshot_fresh(snap):
                self._last_snapshot_available = True
                return snap
        except Exception as exc:
            logger.debug(
                "DAEMON[project_coordinator]: error loading pulse_snapshot checkpoint: %s",
                exc,
            )

        return None  # degraded mode

    def _is_snapshot_fresh(self, snapshot: dict[str, Any]) -> bool:
        """Check if a pulse snapshot is fresh enough for stance computation."""
        max_age_s = self._config.stance_snapshot_max_age_hours * 3600
        try:
            from datetime import datetime, timezone

            gen_at = snapshot.get("generated_at", "")
            if not gen_at:
                return False
            dt = datetime.fromisoformat(gen_at.replace("Z", "+00:00"))
            age_s = (datetime.now(timezone.utc) - dt).total_seconds()
            self._last_snapshot_age_hours = round(age_s / 3600, 2)
            return age_s <= max_age_s
        except (ValueError, TypeError):
            return False

    def _tick_hosted(self) -> list[Finding]:
        """Hosted mode — explicit no-op in v1A."""
        self._update_tick_metrics(0, 0, 0, 0)
        return []

    def _update_tick_metrics(
        self,
        threads: int,
        findings_count: int,
        skipped: int,
        leads: int = 0,
    ) -> None:
        self._last_tick_threads = threads
        self._last_tick_findings = findings_count
        self._last_tick_skipped = skipped
        self._last_tick_leads = leads

    def status_summary(self) -> dict[str, Any]:
        """Health summary with coordinator-specific metrics."""
        base = super().status_summary()
        base["last_tick_threads"] = self._last_tick_threads
        base["last_tick_findings"] = self._last_tick_findings
        base["last_tick_skipped"] = self._last_tick_skipped
        base["suppression_tags"] = list(self._config.suppression_tags)
        base["hosted_mode"] = is_daemon_hosted_mode()
        # v1B stance observability
        base["stance_enabled"] = self._config.stance_enabled
        base["stance_snapshot_available"] = self._last_snapshot_available
        base["stance_snapshot_age_hours"] = self._last_snapshot_age_hours
        base["stance_last_outcome"] = self._last_stance_outcome
        base["stance_last_levels"] = dict(self._last_stance_levels)
        # v1B follow-on: coordinator leads observability
        base["leads_enabled"] = self._config.leads_enabled
        base["last_tick_leads"] = self._last_tick_leads
        return base
