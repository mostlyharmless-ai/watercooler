"""Tests for project_coordinator_lib — pure detector functions and typed structures."""

from __future__ import annotations

from dataclasses import asdict

from watercooler.project_coordinator_lib import (
    BURST_MULTIPLIER,
    NEW_CONTRIBUTOR_PRUNE_DAYS,
    NEW_CONTRIBUTOR_REAPPEARANCE_DAYS,
    ROLE_CONCENTRATION_THRESHOLD,
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
)
from watercooler.pulse_stance_lib import AdvisoryAction, CoordinatorLead

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DAY = 86400.0
_NOW = 1712793600.0  # 2024-04-11T00:00:00Z (arbitrary fixed reference)


def _entry(
    *,
    entry_id: str = "01",
    agent: str = "Alice",
    role: str = "implementer",
    entry_type: str = "Note",
    timestamp: str = "2024-04-01T12:00:00Z",
    index: int = 0,
) -> EntryView:
    return EntryView(
        entry_id=entry_id,
        agent=agent,
        role=role,
        entry_type=entry_type,
        timestamp=timestamp,
        index=index,
    )


def _entries(
    n: int,
    *,
    agent: str = "Alice",
    role: str = "implementer",
    entry_type: str = "Note",
    base_ts: str = "2024-04-01T12:00:00Z",
) -> list[EntryView]:
    return [
        _entry(
            entry_id=f"E{i:02d}",
            agent=agent,
            role=role,
            entry_type=entry_type,
            timestamp=base_ts,
            index=i,
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# EntryView / entries_to_views
# ---------------------------------------------------------------------------


class TestEntriesToViews:
    def test_converts_raw_dicts(self) -> None:
        raw = [
            {
                "entry_id": "E01",
                "agent": "Bob",
                "role": "planner",
                "entry_type": "Plan",
                "timestamp": "2024-04-01T00:00:00Z",
                "index": 0,
            }
        ]
        views = entries_to_views(raw)
        assert len(views) == 1
        assert views[0]["entry_id"] == "E01"
        assert views[0]["role"] == "planner"

    def test_handles_missing_fields(self) -> None:
        raw = [{"entry_id": "E01"}]
        views = entries_to_views(raw)
        assert views[0]["agent"] == ""
        assert views[0]["entry_type"] == "Note"
        assert views[0]["index"] == 0


# ---------------------------------------------------------------------------
# BurstBaseline / CoordinatorExtras round-trip
# ---------------------------------------------------------------------------


class TestDataclassRoundTrip:
    def test_burst_baseline_round_trip(self) -> None:
        b = BurstBaseline(baseline_rate=1.5, last_entry_count=10, last_tick_time=_NOW)
        restored = BurstBaseline.from_dict(b.to_dict())
        assert restored == b

    def test_coordinator_extras_round_trip(self) -> None:
        extras = CoordinatorExtras(
            seen_contributors={"alice": _NOW - _DAY},
            burst_baselines={"topic-a": BurstBaseline(1.0, 5, _NOW)},
        )
        restored = CoordinatorExtras.from_dict(extras.to_dict())
        assert restored.seen_contributors == extras.seen_contributors
        assert restored.burst_baselines["topic-a"] == extras.burst_baselines["topic-a"]

    def test_coordinator_extras_empty(self) -> None:
        extras = CoordinatorExtras.from_dict({})
        assert extras.seen_contributors == {}
        assert extras.burst_baselines == {}


# ---------------------------------------------------------------------------
# Detector 1: stalled_open_loop
# ---------------------------------------------------------------------------


class TestStalledOpenLoop:
    def test_detects_plan_without_resolution(self) -> None:
        entries = [
            _entry(entry_type="Note", index=0),
            _entry(entry_type="Plan", index=1),
            _entry(entry_type="Note", index=2),
        ]
        finding = detect_stalled_open_loops(entries, "my-thread", "OPEN", set(), set())
        assert finding is not None
        assert finding.category == "stalled_open_loop"
        assert finding.severity == "warning"
        assert finding.details["plan_count"] == 1

    def test_no_finding_when_resolved(self) -> None:
        entries = [
            _entry(entry_type="Note", index=0),
            _entry(entry_type="Plan", index=1),
            _entry(entry_type="Decision", index=2),
        ]
        finding = detect_stalled_open_loops(entries, "my-thread", "OPEN", set(), set())
        assert finding is None

    def test_no_finding_when_closed(self) -> None:
        entries = _entries(5, entry_type="Plan")
        finding = detect_stalled_open_loops(
            entries, "my-thread", "CLOSED", set(), set()
        )
        assert finding is None

    def test_no_finding_below_min_entries(self) -> None:
        entries = [_entry(entry_type="Plan", index=0)]
        finding = detect_stalled_open_loops(entries, "my-thread", "OPEN", set(), set())
        assert finding is None

    def test_no_finding_without_plans(self) -> None:
        entries = _entries(5)
        finding = detect_stalled_open_loops(entries, "my-thread", "OPEN", set(), set())
        assert finding is None

    def test_soft_suppression_by_tag(self) -> None:
        entries = [
            _entry(entry_type="Note", index=0),
            _entry(entry_type="Plan", index=1),
            _entry(entry_type="Note", index=2),
        ]
        finding = detect_stalled_open_loops(
            entries,
            "my-thread",
            "OPEN",
            suppression_tags={"parked", "deferred"},
            thread_tags={"parked"},
        )
        assert finding is not None
        assert finding.severity == "info"
        assert "suppressed_by" in finding.details

    def test_closure_also_resolves(self) -> None:
        entries = [
            _entry(entry_type="Note", index=0),
            _entry(entry_type="Plan", index=1),
            _entry(entry_type="Closure", index=2),
        ]
        finding = detect_stalled_open_loops(entries, "my-thread", "OPEN", set(), set())
        assert finding is None

    def test_dedup_signature_format(self) -> None:
        entries = [
            _entry(entry_type="Plan", index=0),
            _entry(entry_type="Note", index=1),
            _entry(entry_type="Note", index=2),
        ]
        finding = detect_stalled_open_loops(entries, "topic-x", "OPEN", set(), set())
        assert finding is not None
        assert finding.dedup_signature == "stalled_open_loop|topic-x"

    def test_no_finding_when_recent_activity(self) -> None:
        """Threads with entries younger than OPEN_LOOP_MIN_STALE_DAYS aren't stalled."""
        recent_ts = "2024-04-10T12:00:00Z"  # 1 day before _NOW
        entries = [
            _entry(entry_type="Note", index=0, timestamp=recent_ts),
            _entry(entry_type="Plan", index=1, timestamp=recent_ts),
            _entry(entry_type="Note", index=2, timestamp=recent_ts),
        ]
        finding = detect_stalled_open_loops(
            entries,
            "my-thread",
            "OPEN",
            set(),
            set(),
            tick_time=_NOW,
        )
        assert finding is None

    def test_fires_when_stale(self) -> None:
        """Threads with entries older than OPEN_LOOP_MIN_STALE_DAYS fire."""
        old_ts = "2024-03-01T12:00:00Z"  # ~41 days before _NOW
        entries = [
            _entry(entry_type="Note", index=0, timestamp=old_ts),
            _entry(entry_type="Plan", index=1, timestamp=old_ts),
            _entry(entry_type="Note", index=2, timestamp=old_ts),
        ]
        finding = detect_stalled_open_loops(
            entries,
            "my-thread",
            "OPEN",
            set(),
            set(),
            tick_time=_NOW,
        )
        assert finding is not None
        assert "days_stale" in finding.details
        assert finding.details["days_stale"] > 7

    def test_fires_when_timestamps_unparseable(self) -> None:
        """Graceful fallback: bad timestamps don't block detection."""
        entries = [
            _entry(entry_type="Note", index=0, timestamp=""),
            _entry(entry_type="Plan", index=1, timestamp="not-a-date"),
            _entry(entry_type="Note", index=2, timestamp=""),
        ]
        finding = detect_stalled_open_loops(
            entries,
            "my-thread",
            "OPEN",
            set(),
            set(),
            tick_time=_NOW,
        )
        assert finding is not None

    def test_fires_without_tick_time(self) -> None:
        """Backward compat: tick_time=0 (default) skips staleness gate."""
        recent_ts = "2024-04-10T12:00:00Z"
        entries = [
            _entry(entry_type="Note", index=0, timestamp=recent_ts),
            _entry(entry_type="Plan", index=1, timestamp=recent_ts),
            _entry(entry_type="Note", index=2, timestamp=recent_ts),
        ]
        finding = detect_stalled_open_loops(
            entries,
            "my-thread",
            "OPEN",
            set(),
            set(),
        )
        assert finding is not None  # No staleness gate without tick_time


# ---------------------------------------------------------------------------
# Detector 2: stalled_dropout
# ---------------------------------------------------------------------------


class TestStalledDropout:
    def test_detects_dropout(self) -> None:
        """Alice contributes 3 entries, then Bob continues for 3+ more."""
        entries = [
            _entry(agent="Alice", index=0),
            _entry(agent="Alice", index=1),
            _entry(agent="Alice", index=2),
            _entry(agent="Bob", index=3),
            _entry(agent="Bob", index=4),
            _entry(agent="Bob", index=5),
        ]
        findings = detect_stalled_dropout(
            entries,
            "my-thread",
            "OPEN",
            set(),
            set(),
            normalize_agent_fn=lambda a: a.lower(),
        )
        assert len(findings) == 1
        assert findings[0].details["contributor"] == "alice"

    def test_no_finding_when_closed(self) -> None:
        entries = [
            _entry(agent="Alice", index=0),
            _entry(agent="Alice", index=1),
            _entry(agent="Alice", index=2),
            _entry(agent="Bob", index=3),
            _entry(agent="Bob", index=4),
            _entry(agent="Bob", index=5),
        ]
        findings = detect_stalled_dropout(
            entries,
            "my-thread",
            "CLOSED",
            set(),
            set(),
            normalize_agent_fn=lambda a: a.lower(),
        )
        assert findings == []

    def test_no_finding_single_contributor(self) -> None:
        entries = _entries(10, agent="Alice")
        findings = detect_stalled_dropout(
            entries,
            "my-thread",
            "OPEN",
            set(),
            set(),
            normalize_agent_fn=lambda a: a.lower(),
        )
        assert findings == []

    def test_no_finding_below_min_entries(self) -> None:
        """Contributor with only 2 entries (below DROPOUT_MIN_ENTRIES=3) — no finding."""
        entries = [
            _entry(agent="Alice", index=0),
            _entry(agent="Alice", index=1),
            _entry(agent="Bob", index=2),
            _entry(agent="Bob", index=3),
            _entry(agent="Bob", index=4),
            _entry(agent="Bob", index=5),
        ]
        findings = detect_stalled_dropout(
            entries,
            "my-thread",
            "OPEN",
            set(),
            set(),
            normalize_agent_fn=lambda a: a.lower(),
        )
        assert findings == []

    def test_no_finding_gap_too_small(self) -> None:
        """Alice's last entry is only 2 positions from end (< DROPOUT_CONTINUATION_GAP)."""
        entries = [
            _entry(agent="Alice", index=0),
            _entry(agent="Alice", index=1),
            _entry(agent="Alice", index=2),
            _entry(agent="Bob", index=3),
            _entry(agent="Bob", index=4),
        ]
        findings = detect_stalled_dropout(
            entries,
            "my-thread",
            "OPEN",
            set(),
            set(),
            normalize_agent_fn=lambda a: a.lower(),
        )
        assert findings == []

    def test_soft_suppression(self) -> None:
        entries = [
            _entry(agent="Alice", index=0),
            _entry(agent="Alice", index=1),
            _entry(agent="Alice", index=2),
            _entry(agent="Bob", index=3),
            _entry(agent="Bob", index=4),
            _entry(agent="Bob", index=5),
        ]
        findings = detect_stalled_dropout(
            entries,
            "my-thread",
            "OPEN",
            suppression_tags={"wontfix"},
            thread_tags={"wontfix"},
            normalize_agent_fn=lambda a: a.lower(),
        )
        assert len(findings) == 1
        assert findings[0].severity == "info"
        assert "suppressed_by" in findings[0].details


# ---------------------------------------------------------------------------
# Detector 3: aware_burst
# ---------------------------------------------------------------------------


class TestAwareBurst:
    def _old_ts(self, days_ago: float) -> str:
        from datetime import datetime, timezone

        ts = _NOW - (days_ago * _DAY)
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def test_detects_burst(self) -> None:
        """Window rate exceeds prior baseline rate by >= BURST_MULTIPLIER (3x)."""
        baseline = BurstBaseline(
            baseline_rate=1.0,
            last_entry_count=2,
            last_tick_time=_NOW - _DAY,
        )
        # 1 old entry (outside 7-day window) + 22 recent entries (inside window)
        # → current_rate = 22/7 ≈ 3.14 entries/day vs baseline 1.0 → 3.14x
        entries = [_entry(timestamp=self._old_ts(10), index=0)] + [
            _entry(timestamp=self._old_ts(0.5), index=i) for i in range(1, 23)
        ]
        finding, updated = detect_aware_burst(entries, "topic-a", baseline, _NOW)
        assert finding is not None
        assert finding.category == "aware_burst"
        assert finding.details["multiplier"] >= BURST_MULTIPLIER

    def test_no_burst_below_multiplier(self) -> None:
        baseline = BurstBaseline(
            baseline_rate=2.0,
            last_entry_count=5,
            last_tick_time=_NOW - _DAY,
        )
        # 3 new entries in 1 day → 3/day vs 2.0 baseline → 1.5x (below 3x threshold)
        entries = _entries(8, base_ts=self._old_ts(10))
        finding, _ = detect_aware_burst(entries, "topic-a", baseline, _NOW)
        assert finding is None

    def test_seeds_baseline_on_first_observation(self) -> None:
        entries = [
            _entry(timestamp=self._old_ts(10), index=0),
            _entry(timestamp=self._old_ts(5), index=1),
            _entry(timestamp=self._old_ts(1), index=2),
            _entry(timestamp=self._old_ts(0.5), index=3),
        ]
        finding, baseline = detect_aware_burst(entries, "topic-a", None, _NOW)
        assert finding is None  # First observation — no finding
        assert baseline.last_entry_count == 4

    def test_skips_young_thread(self) -> None:
        entries = [
            _entry(timestamp=self._old_ts(1), index=0),
            _entry(timestamp=self._old_ts(0.5), index=1),
        ]
        finding, _ = detect_aware_burst(entries, "topic-a", None, _NOW)
        assert finding is None

    def test_no_dated_entries(self) -> None:
        entries = [_entry(timestamp="", index=0)]
        finding, baseline = detect_aware_burst(entries, "topic-a", None, _NOW)
        assert finding is None
        assert baseline.last_entry_count == 1

    def test_below_min_new_entries(self) -> None:
        """Only 2 new entries (below BURST_MIN_ENTRIES=3) — no finding."""
        baseline = BurstBaseline(
            baseline_rate=0.5,
            last_entry_count=5,
            last_tick_time=_NOW - _DAY,
        )
        entries = _entries(7, base_ts=self._old_ts(10))
        finding, _ = detect_aware_burst(entries, "topic-a", baseline, _NOW)
        assert finding is None


# ---------------------------------------------------------------------------
# Detector 4: aware_new_contributor
# ---------------------------------------------------------------------------


class TestNewContributors:
    def test_detects_new_contributor(self) -> None:
        findings, updated = detect_new_contributors(
            all_contributors={"alice": _NOW},
            seen_set={},
            tick_time=_NOW,
        )
        assert len(findings) == 1
        assert findings[0].category == "aware_new_contributor"
        assert findings[0].details["is_reappearance"] is False
        assert "alice" in updated

    def test_detects_reappearance(self) -> None:
        old_ts = _NOW - ((NEW_CONTRIBUTOR_REAPPEARANCE_DAYS + 1) * _DAY)
        findings, updated = detect_new_contributors(
            all_contributors={"alice": _NOW},
            seen_set={"alice": old_ts},
            tick_time=_NOW,
        )
        assert len(findings) == 1
        assert findings[0].details["is_reappearance"] is True
        assert findings[0].details["days_absent"] >= NEW_CONTRIBUTOR_REAPPEARANCE_DAYS

    def test_no_finding_for_recently_seen(self) -> None:
        recent_ts = _NOW - (1 * _DAY)
        findings, _ = detect_new_contributors(
            all_contributors={"alice": _NOW},
            seen_set={"alice": recent_ts},
            tick_time=_NOW,
        )
        assert findings == []

    def test_prunes_old_entries(self) -> None:
        very_old = _NOW - ((NEW_CONTRIBUTOR_PRUNE_DAYS + 1) * _DAY)
        _, updated = detect_new_contributors(
            all_contributors={},
            seen_set={"stale-bob": very_old},
            tick_time=_NOW,
        )
        assert "stale-bob" not in updated

    def test_dedup_signature_includes_event_kind(self) -> None:
        findings, _ = detect_new_contributors(
            all_contributors={"carol": _NOW},
            seen_set={},
            tick_time=_NOW,
        )
        assert len(findings) == 1
        assert "|new|" in findings[0].dedup_signature

    def test_dedup_signature_reappearance(self) -> None:
        old_ts = _NOW - ((NEW_CONTRIBUTOR_REAPPEARANCE_DAYS + 1) * _DAY)
        findings, _ = detect_new_contributors(
            all_contributors={"carol": _NOW},
            seen_set={"carol": old_ts},
            tick_time=_NOW,
        )
        assert len(findings) == 1
        assert "|reappearance|" in findings[0].dedup_signature

    def test_seen_set_stores_tick_time_not_entry_timestamp(self) -> None:
        """Seen-set must store tick_time, not latest_ts — prevents perpetual re-fires.

        If a contributor's most recent entry is old (> reappearance threshold),
        storing latest_ts would cause re-detection on every tick.
        """
        old_entry_ts = _NOW - ((NEW_CONTRIBUTOR_REAPPEARANCE_DAYS + 10) * _DAY)
        # First call: detect new contributor with stale entry timestamp
        findings, updated = detect_new_contributors(
            all_contributors={"stale-alice": old_entry_ts},
            seen_set={},
            tick_time=_NOW,
        )
        assert len(findings) == 1
        # Key assertion: seen-set records tick_time, not the stale entry timestamp
        assert updated["stale-alice"] == _NOW

        # Second call with same inputs: should NOT re-fire because seen_set has tick_time
        findings2, _ = detect_new_contributors(
            all_contributors={"stale-alice": old_entry_ts},
            seen_set=updated,
            tick_time=_NOW + _DAY,
        )
        assert findings2 == [], "Must not re-fire — seen-set advanced to tick_time"

    def test_reappearance_seen_set_stores_tick_time(self) -> None:
        """Reappearance must also store tick_time to prevent perpetual re-fires."""
        old_seen = _NOW - ((NEW_CONTRIBUTOR_REAPPEARANCE_DAYS + 10) * _DAY)
        old_entry = _NOW - ((NEW_CONTRIBUTOR_REAPPEARANCE_DAYS + 5) * _DAY)
        findings, updated = detect_new_contributors(
            all_contributors={"bob": old_entry},
            seen_set={"bob": old_seen},
            tick_time=_NOW,
        )
        assert len(findings) == 1
        assert findings[0].details["is_reappearance"] is True
        # Key assertion: seen-set records tick_time, not the stale entry timestamp
        assert updated["bob"] == _NOW

    def test_includes_thread_list(self) -> None:
        findings, _ = detect_new_contributors(
            all_contributors={"dave": _NOW},
            seen_set={},
            tick_time=_NOW,
            contributor_threads={"dave": ["thread-a", "thread-b"]},
        )
        assert findings[0].details["observed_threads"] == ["thread-a", "thread-b"]


# ---------------------------------------------------------------------------
# Detector 5: aware_role_concentration
# ---------------------------------------------------------------------------


class TestRoleConcentration:
    def test_detects_concentration(self) -> None:
        entries = _entries(5, role="implementer")
        finding = detect_role_concentration(entries, "my-thread", "OPEN")
        assert finding is not None
        assert finding.category == "aware_role_concentration"
        assert finding.details["dominant_role"] == "implementer"
        assert finding.details["concentration"] >= ROLE_CONCENTRATION_THRESHOLD

    def test_no_finding_when_balanced(self) -> None:
        entries = [
            _entry(role="implementer", index=0),
            _entry(role="planner", index=1),
            _entry(role="critic", index=2),
            _entry(role="tester", index=3),
            _entry(role="pm", index=4),
        ]
        finding = detect_role_concentration(entries, "my-thread", "OPEN")
        assert finding is None

    def test_no_finding_when_closed(self) -> None:
        entries = _entries(10, role="implementer")
        finding = detect_role_concentration(entries, "my-thread", "CLOSED")
        assert finding is None

    def test_no_finding_below_min_entries(self) -> None:
        entries = _entries(2, role="implementer")
        finding = detect_role_concentration(entries, "my-thread", "OPEN")
        assert finding is None

    def test_missing_roles_listed(self) -> None:
        entries = _entries(5, role="implementer")
        finding = detect_role_concentration(entries, "my-thread", "OPEN")
        assert finding is not None
        # implementer is present, so it should not be in missing_roles
        assert "implementer" not in finding.details["missing_roles"]
        # But other canonical roles should be missing
        assert "planner" in finding.details["missing_roles"]

    def test_empty_roles_ignored(self) -> None:
        entries = [_entry(role="", index=i) for i in range(5)]
        finding = detect_role_concentration(entries, "my-thread", "OPEN")
        assert finding is None

    def test_dedup_signature_format(self) -> None:
        entries = _entries(5, role="planner")
        finding = detect_role_concentration(entries, "topic-y", "OPEN")
        assert finding is not None
        assert finding.dedup_signature == "aware_role_concentration|topic-y|planner"


# ---------------------------------------------------------------------------
# Coordinator leads (v1B follow-on)
# ---------------------------------------------------------------------------


class TestGenerateLeadsForThread:
    """generate_leads_for_thread: one lead per triggering v1A finding."""

    def test_stalled_open_loop_generates_lead(self) -> None:
        cf = CoordinatorFinding(
            category="stalled_open_loop",
            topic="test-thread",
            severity="warning",
            message="Thread has open plan",
            details={"plan_count": 2, "days_stale": 10.0},
            dedup_signature="stalled_open_loop|test-thread",
        )
        leads = generate_leads_for_thread([cf])
        assert len(leads) == 1
        lead_cf = leads[0]
        assert lead_cf.category == "coordinator_lead"
        assert lead_cf.topic == "test-thread"
        assert lead_cf.severity == "warning"
        assert (
            lead_cf.dedup_signature == "coordinator_lead|stalled_open_loop|test-thread"
        )
        lead = CoordinatorLead.from_dict(lead_cf.details["lead"])
        assert lead.source_category == "stalled_open_loop"
        assert lead.source_topic == "test-thread"
        assert "2 Plan entries" in lead.summary
        assert "10 days stale" in lead.summary
        assert lead.suggested_action is not None
        assert lead.suggested_action.tool == "watercooler_read_thread"
        assert lead.suggested_action.arguments == {
            "topic": "test-thread",
            "summary_only": True,
        }
        assert lead.relevance_tags == ("implementer", "pm")
        assert lead.t2_context is None

    def test_stalled_open_loop_without_days_stale(self) -> None:
        cf = CoordinatorFinding(
            category="stalled_open_loop",
            topic="t",
            severity="warning",
            message="m",
            details={"plan_count": 3},  # days_stale absent
            dedup_signature="stalled_open_loop|t",
        )
        leads = generate_leads_for_thread([cf])
        assert len(leads) == 1
        lead = CoordinatorLead.from_dict(leads[0].details["lead"])
        assert "3 Plan entries" in lead.summary
        assert "days stale" not in lead.summary

    def test_aware_new_contributor_does_not_generate_lead(self) -> None:
        cf = CoordinatorFinding(
            category="aware_new_contributor",
            topic="test-thread",
            severity="info",
            message="New contributor",
            details={"contributor": "alice"},
            dedup_signature="aware_new_contributor|alice|new|2026-04-13",
        )
        assert generate_leads_for_thread([cf]) == []

    def test_multiple_dropout_contributors_produce_separate_leads(self) -> None:
        cf_alice = CoordinatorFinding(
            category="stalled_dropout",
            topic="my-thread",
            severity="warning",
            message="alice dropped out",
            details={"contributor": "alice", "contributor_entries": 5},
            dedup_signature="stalled_dropout|my-thread|alice",
        )
        cf_bob = CoordinatorFinding(
            category="stalled_dropout",
            topic="my-thread",
            severity="warning",
            message="bob dropped out",
            details={"contributor": "bob", "contributor_entries": 3},
            dedup_signature="stalled_dropout|my-thread|bob",
        )
        leads = generate_leads_for_thread([cf_alice, cf_bob])
        assert len(leads) == 2
        dedup_keys = {cf.dedup_signature for cf in leads}
        assert dedup_keys == {
            "coordinator_lead|stalled_dropout|my-thread|alice",
            "coordinator_lead|stalled_dropout|my-thread|bob",
        }

    def test_stalled_dropout_uses_thread_scoped_search(self) -> None:
        cf = CoordinatorFinding(
            category="stalled_dropout",
            topic="my-thread",
            severity="warning",
            message="",
            details={"contributor": "alice", "contributor_entries": 4},
            dedup_signature="stalled_dropout|my-thread|alice",
        )
        leads = generate_leads_for_thread([cf])
        lead = CoordinatorLead.from_dict(leads[0].details["lead"])
        assert lead.suggested_action is not None
        assert lead.suggested_action.tool == "watercooler_search"
        assert lead.suggested_action.arguments == {
            "query": "alice",
            "thread_topic": "my-thread",
            "limit": 20,
        }
        assert lead.relevance_tags == ("pm", "planner")

    def test_role_concentration_uses_actual_field_names(self) -> None:
        cf = CoordinatorFinding(
            category="aware_role_concentration",
            topic="arch-thread",
            severity="info",
            message="Thread is 90% planner",
            details={
                "dominant_role": "planner",
                "concentration": 0.9,
                "missing_roles": ["tester", "implementer"],
                "entry_count": 10,
            },
            dedup_signature="aware_role_concentration|arch-thread|planner",
        )
        leads = generate_leads_for_thread([cf])
        assert len(leads) == 1
        lead = CoordinatorLead.from_dict(leads[0].details["lead"])
        assert "90%" in lead.summary
        assert "planner" in lead.summary
        assert "tester" in lead.summary  # sorted -> "implementer, tester"
        assert lead.suggested_action is not None
        assert lead.suggested_action.tool == "watercooler_list_thread_entries"

    def test_role_concentration_deterministic_relevance_tags(self) -> None:
        # missing_roles order should not affect the result
        cf_a = CoordinatorFinding(
            category="aware_role_concentration",
            topic="t",
            severity="info",
            message="",
            details={
                "dominant_role": "planner",
                "concentration": 0.85,
                "missing_roles": ["tester", "implementer"],
                "entry_count": 10,
            },
            dedup_signature="aware_role_concentration|t|planner",
        )
        cf_b = CoordinatorFinding(
            category="aware_role_concentration",
            topic="t",
            severity="info",
            message="",
            details={
                "dominant_role": "planner",
                "concentration": 0.85,
                "missing_roles": ["implementer", "tester"],  # reversed
                "entry_count": 10,
            },
            dedup_signature="aware_role_concentration|t|planner",
        )
        la = CoordinatorLead.from_dict(
            generate_leads_for_thread([cf_a])[0].details["lead"]
        )
        lb = CoordinatorLead.from_dict(
            generate_leads_for_thread([cf_b])[0].details["lead"]
        )
        # Sorted first-missing is "implementer"; dedup "pm"
        assert la.relevance_tags == ("implementer", "pm")
        assert la.relevance_tags == lb.relevance_tags

    def test_role_concentration_missing_roles_contains_only_pm(self) -> None:
        cf = CoordinatorFinding(
            category="aware_role_concentration",
            topic="t",
            severity="info",
            message="",
            details={
                "dominant_role": "planner",
                "concentration": 0.9,
                "missing_roles": ["pm"],
                "entry_count": 10,
            },
            dedup_signature="aware_role_concentration|t|planner",
        )
        lead = CoordinatorLead.from_dict(
            generate_leads_for_thread([cf])[0].details["lead"]
        )
        assert lead.relevance_tags == ("pm",)

    def test_role_concentration_empty_missing_roles(self) -> None:
        cf = CoordinatorFinding(
            category="aware_role_concentration",
            topic="t",
            severity="info",
            message="",
            details={
                "dominant_role": "planner",
                "concentration": 0.9,
                "missing_roles": [],
                "entry_count": 10,
            },
            dedup_signature="aware_role_concentration|t|planner",
        )
        lead = CoordinatorLead.from_dict(
            generate_leads_for_thread([cf])[0].details["lead"]
        )
        assert lead.relevance_tags == ("pm",)
        assert "none identified" in lead.summary

    def test_burst_lead_uses_multiplier_not_burst_multiplier(self) -> None:
        cf = CoordinatorFinding(
            category="aware_burst",
            topic="hot-thread",
            severity="info",
            message="Burst detected",
            details={"multiplier": 4.5, "new_entries": 12},
            dedup_signature="aware_burst|hot-thread|2026-04-13",
        )
        leads = generate_leads_for_thread([cf])
        assert len(leads) == 1
        lead = CoordinatorLead.from_dict(leads[0].details["lead"])
        assert "4.5x" in lead.summary
        assert "12 new entries" in lead.summary
        assert lead.relevance_tags == ("pm",)

    def test_empty_dedup_signature_skipped_with_warning(self, caplog) -> None:
        cf = CoordinatorFinding(
            category="stalled_open_loop",
            topic="t",
            severity="warning",
            message="m",
            details={"plan_count": 2},
            dedup_signature="",  # empty
        )
        import logging

        with caplog.at_level(
            logging.WARNING, logger="watercooler.project_coordinator_lib"
        ):
            leads = generate_leads_for_thread([cf])
        assert leads == []
        assert any("empty dedup_signature" in r.message for r in caplog.records)

    def test_suppression_inheritance_propagates_severity_and_tag(self) -> None:
        cf = CoordinatorFinding(
            category="stalled_open_loop",
            topic="parked-thread",
            severity="info",  # downgraded by suppression
            message="",
            details={
                "plan_count": 2,
                "days_stale": 15.0,
                "suppressed_by": "tag:parked",
            },
            dedup_signature="stalled_open_loop|parked-thread",
        )
        leads = generate_leads_for_thread([cf])
        assert len(leads) == 1
        lead_cf = leads[0]
        assert lead_cf.severity == "info"
        assert lead_cf.details["suppressed_by"] == "tag:parked"

    def test_non_trigger_categories_filtered(self) -> None:
        # aware_new_contributor plus a trigger — only trigger produces a lead.
        cfs = [
            CoordinatorFinding(
                category="aware_new_contributor",
                topic="t",
                severity="info",
                message="",
                details={"contributor": "eve"},
                dedup_signature="aware_new_contributor|eve|new|x",
            ),
            CoordinatorFinding(
                category="stalled_open_loop",
                topic="t",
                severity="warning",
                message="",
                details={"plan_count": 2},
                dedup_signature="stalled_open_loop|t",
            ),
        ]
        leads = generate_leads_for_thread(cfs)
        assert len(leads) == 1
        assert leads[0].dedup_signature == "coordinator_lead|stalled_open_loop|t"

    def test_empty_thread_findings(self) -> None:
        assert generate_leads_for_thread([]) == []

    # ---- Phase 2 t2_context tests (tests 1–5) ----

    def test_t2_context_populated_when_analysis_data_present(self) -> None:
        """Test 1: generate_leads_for_thread passes matching thread analysis → non-None t2_context."""
        cf = CoordinatorFinding(
            category="stalled_open_loop",
            topic="my-thread",
            severity="warning",
            message="stalled",
            details={"plan_count": 2},
            dedup_signature="stalled_open_loop|my-thread",
        )
        analysis_by_topic = {
            "my-thread": {
                "topic": "my-thread",
                "days_since_last": 18,
                "workflow_shape": {"id": "wf1", "name": "linear", "confidence": 0.9},
                "has_decision": False,
                "has_closure": False,
                "stalled": True,
                "entry_count_total": 25,
            }
        }
        leads = generate_leads_for_thread([cf], analysis_by_topic=analysis_by_topic)
        assert len(leads) == 1
        lead = CoordinatorLead.from_dict(leads[0].details["lead"])
        assert lead.t2_context is not None
        t2 = lead.t2_context
        assert t2["schema_version"] == 1
        assert t2["days_since_last"] == 18
        assert t2["workflow_shape_id"] == "wf1"
        assert t2["workflow_shape_name"] == "linear"
        assert t2["workflow_confidence"] == 0.9
        assert t2["stalled"] is True
        assert t2["has_decision"] is False
        assert t2["has_closure"] is False
        assert t2["entry_count_total"] == 25
        assert t2["recommendation_rule_ids"] == []

    def test_t2_context_none_when_topic_not_in_analysis(self) -> None:
        """Test 2: topic absent from analysis_by_topic → t2_context remains None."""
        cf = CoordinatorFinding(
            category="stalled_open_loop",
            topic="my-thread",
            severity="warning",
            message="stalled",
            details={"plan_count": 2},
            dedup_signature="stalled_open_loop|my-thread",
        )
        analysis_by_topic = {
            "other-thread": {"topic": "other-thread", "days_since_last": 5}
        }
        leads = generate_leads_for_thread([cf], analysis_by_topic=analysis_by_topic)
        assert len(leads) == 1
        lead = CoordinatorLead.from_dict(leads[0].details["lead"])
        assert lead.t2_context is None

    def test_t2_context_none_when_no_analysis_data(self) -> None:
        """Test 3: both new params are None → t2_context stays None, no regression."""
        cf = CoordinatorFinding(
            category="stalled_open_loop",
            topic="my-thread",
            severity="warning",
            message="stalled",
            details={"plan_count": 2},
            dedup_signature="stalled_open_loop|my-thread",
        )
        leads = generate_leads_for_thread([cf])  # old signature, no kwargs
        assert len(leads) == 1
        lead = CoordinatorLead.from_dict(leads[0].details["lead"])
        assert lead.t2_context is None
        assert lead.source_category == "stalled_open_loop"

    def test_recommendation_rule_ids_in_t2_context(self) -> None:
        """Test 4: rule IDs are deduplicated, sorted, empty strings excluded."""
        cf = CoordinatorFinding(
            category="stalled_open_loop",
            topic="my-thread",
            severity="warning",
            message="stalled",
            details={"plan_count": 2},
            dedup_signature="stalled_open_loop|my-thread",
        )
        analysis_by_topic = {"my-thread": {"topic": "my-thread", "days_since_last": 10}}
        analysis_rule_flags = {"my-thread": ["R05", "R03", "R05", ""]}
        leads = generate_leads_for_thread(
            [cf],
            analysis_by_topic=analysis_by_topic,
            analysis_rule_flags=analysis_rule_flags,
        )
        lead = CoordinatorLead.from_dict(leads[0].details["lead"])
        assert lead.t2_context is not None
        assert lead.t2_context["recommendation_rule_ids"] == ["R03", "R05"]

    def test_generate_leads_signature_backward_compat(self) -> None:
        """Test 5: calling generate_leads_for_thread without new kwargs works (no TypeError)."""
        cf = CoordinatorFinding(
            category="stalled_open_loop",
            topic="t",
            severity="warning",
            message="m",
            details={"plan_count": 2},
            dedup_signature="stalled_open_loop|t",
        )
        leads = generate_leads_for_thread([cf])
        assert len(leads) == 1


class TestCoordinatorLeadFromDict:
    """CoordinatorLead.from_dict: safe reconstruction from asdict() output."""

    def test_asdict_from_dict_roundtrip(self) -> None:
        lead = CoordinatorLead(
            schema_version=1,
            source_category="stalled_open_loop",
            source_topic="t",
            summary="s",
            relevance_tags=("implementer", "pm"),
            suggested_action=AdvisoryAction(
                phase="pre",
                tool="watercooler_read_thread",
                arguments={"topic": "t", "summary_only": True},
                reason="r",
            ),
            t2_context=None,
        )
        assert CoordinatorLead.from_dict(asdict(lead)) == lead

    def test_missing_suggested_action_returns_none(self) -> None:
        base = {
            "schema_version": 1,
            "source_category": "stalled_open_loop",
            "source_topic": "t",
            "summary": "s",
            "relevance_tags": [],
            "t2_context": None,
        }
        assert (
            CoordinatorLead.from_dict(
                {**base, "suggested_action": None}
            ).suggested_action
            is None
        )

    def test_partial_suggested_action_returns_none(self) -> None:
        base = {
            "schema_version": 1,
            "source_category": "stalled_open_loop",
            "source_topic": "t",
            "summary": "s",
            "relevance_tags": [],
            "t2_context": None,
        }
        partial = {
            "phase": "pre",
            "tool": "watercooler_read_thread",
        }  # missing args/reason
        assert (
            CoordinatorLead.from_dict(
                {**base, "suggested_action": partial}
            ).suggested_action
            is None
        )

    def test_invalid_tool_returns_none(self) -> None:
        base = {
            "schema_version": 1,
            "source_category": "stalled_open_loop",
            "source_topic": "t",
            "summary": "s",
            "relevance_tags": [],
            "t2_context": None,
        }
        bad_tool = {
            "phase": "pre",
            "tool": "watercooler_say",  # write tool not in _READ_ONLY_TOOLS
            "arguments": {},
            "reason": "x",
        }
        assert (
            CoordinatorLead.from_dict(
                {**base, "suggested_action": bad_tool}
            ).suggested_action
            is None
        )

    def test_from_dict_handles_missing_top_level_fields(self) -> None:
        # Backward compat: old records might lack some fields; from_dict uses .get()
        lead = CoordinatorLead.from_dict({})
        assert lead.schema_version == 1
        assert lead.source_category == ""
        assert lead.source_topic == ""
        assert lead.summary == ""
        assert lead.relevance_tags == ()
        assert lead.suggested_action is None
        assert lead.t2_context is None


class TestStdlibOnlyBoundary:
    """project_coordinator_lib must not import from watercooler_mcp."""

    def test_project_coordinator_lib_is_stdlib_only(self) -> None:
        import ast
        from pathlib import Path

        src = Path("src/watercooler/project_coordinator_lib.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(
                    "watercooler_mcp"
                ), f"stdlib-only violation (ImportFrom): {node.module}"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(
                        "watercooler_mcp"
                    ), f"stdlib-only violation (Import): {alias.name}"
