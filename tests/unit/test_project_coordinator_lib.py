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
    _build_t2_context,
    _has_xref_decision,
    _resolve_related_threads,
    _suppression_details,
    detect_aware_burst,
    detect_new_contributors,
    detect_role_complement,
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
# Suppression primitive (Phase 3a-3)
# ---------------------------------------------------------------------------


class TestSuppressionDetails:
    """Tests for the ``_suppression_details`` tag-match primitive.

    Shared by ``stalled_*`` (downgrades severity) and ``aware_*`` (annotates
    only). The primitive itself returns the marker dict; severity policy is
    owned by the caller.
    """

    def test_suppression_details_added_when_thread_tag_matches(self) -> None:
        details = _suppression_details({"parked"}, {"parked", "deferred"})
        assert details == {"suppressed_by": "tag:parked"}

    def test_returns_empty_dict_when_no_match(self) -> None:
        details = _suppression_details({"in-progress"}, {"parked", "deferred"})
        assert details == {}

    def test_returns_empty_dict_when_suppression_tags_empty(self) -> None:
        assert _suppression_details({"parked"}, set()) == {}

    def test_returns_empty_dict_when_thread_tags_empty(self) -> None:
        assert _suppression_details(set(), {"parked"}) == {}

    def test_picks_lowest_lexicographic_tag_on_multi_match(self) -> None:
        """Deterministic marker when multiple suppression tags match."""
        details = _suppression_details(
            {"wontfix", "parked", "deferred"},
            {"parked", "deferred", "wontfix"},
        )
        assert details == {"suppressed_by": "tag:deferred"}


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

    def test_xref_resolves_to_cross_thread_decision_emits_suppression_finding(
        self, tmp_path
    ) -> None:
        """Cross-thread Decision xref replaces stalled_open_loop with a
        coordinator_xref_suppression info finding (agent-native observability)."""
        from watercooler.baseline_graph.annotations import (
            AnnotationEvent,
            append_annotation,
        )
        from watercooler.baseline_graph.storage import (
            get_graph_dir,
            get_thread_graph_dir,
        )
        from watercooler.baseline_graph.writer import EntryData, upsert_entry_node

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        source_topic = "my-thread"
        other_topic = "decisions-thread"
        decision_entry_id = "DEC-001"

        # Write the Decision entry in a different thread's graph.
        upsert_entry_node(
            threads_dir,
            EntryData(
                entry_id=decision_entry_id,
                thread_topic=other_topic,
                index=0,
                agent="Alice",
                role="implementer",
                entry_type="Decision",
                title="Cross-thread decision",
                body="Decided to proceed.",
                summary="",
            ),
        )

        # Add an xref annotation in the source thread pointing at that Decision.
        source_thread_dir = get_thread_graph_dir(
            get_graph_dir(threads_dir), source_topic
        )
        source_thread_dir.mkdir(parents=True, exist_ok=True)
        append_annotation(
            source_thread_dir,
            AnnotationEvent(
                id="evt-001",
                target_id="src-entry-01",
                target_type="entry",
                kind="xref",
                value=decision_entry_id,
                actor="Alice",
                timestamp="2024-04-01T12:00:00+00:00",
            ),
        )

        entries = [
            _entry(entry_type="Note", index=0),
            _entry(entry_type="Plan", index=1),
            _entry(entry_type="Note", index=2),
        ]
        finding = detect_stalled_open_loops(
            entries, source_topic, "OPEN", set(), set(), threads_dir=threads_dir
        )
        assert finding is not None
        assert finding.category == "coordinator_xref_suppression"
        assert finding.severity == "info"
        assert finding.details["xref_resolves_to"] == decision_entry_id
        assert finding.details["suppressed_by"] == f"xref:{decision_entry_id}"

    def test_finding_emitted_when_xref_resolves_to_note_not_decision(
        self, tmp_path
    ) -> None:
        """Xref to a Note in another thread does not suppress the finding."""
        from watercooler.baseline_graph.annotations import (
            AnnotationEvent,
            append_annotation,
        )
        from watercooler.baseline_graph.storage import (
            get_graph_dir,
            get_thread_graph_dir,
        )
        from watercooler.baseline_graph.writer import EntryData, upsert_entry_node

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        source_topic = "my-thread"
        other_topic = "other-thread"
        note_entry_id = "NOTE-001"

        upsert_entry_node(
            threads_dir,
            EntryData(
                entry_id=note_entry_id,
                thread_topic=other_topic,
                index=0,
                agent="Alice",
                role="implementer",
                entry_type="Note",
                title="Just a note",
                body="Nothing decided yet.",
                summary="",
            ),
        )

        source_thread_dir = get_thread_graph_dir(
            get_graph_dir(threads_dir), source_topic
        )
        source_thread_dir.mkdir(parents=True, exist_ok=True)
        append_annotation(
            source_thread_dir,
            AnnotationEvent(
                id="evt-001",
                target_id="src-entry-01",
                target_type="entry",
                kind="xref",
                value=note_entry_id,
                actor="Alice",
                timestamp="2024-04-01T12:00:00+00:00",
            ),
        )

        entries = [
            _entry(entry_type="Note", index=0),
            _entry(entry_type="Plan", index=1),
            _entry(entry_type="Note", index=2),
        ]
        finding = detect_stalled_open_loops(
            entries, source_topic, "OPEN", set(), set(), threads_dir=threads_dir
        )
        assert finding is not None

    def test_finding_emitted_when_xref_target_missing(self, tmp_path) -> None:
        """Missing xref target is skipped (fail-open); finding is still emitted."""
        from watercooler.baseline_graph.annotations import (
            AnnotationEvent,
            append_annotation,
        )
        from watercooler.baseline_graph.storage import (
            get_graph_dir,
            get_thread_graph_dir,
        )

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        source_topic = "my-thread"
        source_thread_dir = get_thread_graph_dir(
            get_graph_dir(threads_dir), source_topic
        )
        source_thread_dir.mkdir(parents=True, exist_ok=True)

        # Xref to a non-existent entry ID — should not raise, should return False.
        append_annotation(
            source_thread_dir,
            AnnotationEvent(
                id="evt-001",
                target_id="src-entry-01",
                target_type="entry",
                kind="xref",
                value="MISSING-999",
                actor="Alice",
                timestamp="2024-04-01T12:00:00+00:00",
            ),
        )

        entries = [
            _entry(entry_type="Note", index=0),
            _entry(entry_type="Plan", index=1),
            _entry(entry_type="Note", index=2),
        ]
        finding = detect_stalled_open_loops(
            entries, source_topic, "OPEN", set(), set(), threads_dir=threads_dir
        )
        assert finding is not None

    def test_no_suppression_when_threads_dir_none(self) -> None:
        """When threads_dir is omitted the xref check is skipped entirely."""
        entries = [
            _entry(entry_type="Note", index=0),
            _entry(entry_type="Plan", index=1),
            _entry(entry_type="Note", index=2),
        ]
        finding = detect_stalled_open_loops(entries, "my-thread", "OPEN", set(), set())
        assert finding is not None


# ---------------------------------------------------------------------------
# _has_xref_decision unit tests
# ---------------------------------------------------------------------------


class TestHasXrefDecision:
    def test_returns_none_for_empty_thread(self, tmp_path) -> None:
        from watercooler.baseline_graph.storage import (
            get_graph_dir,
            get_thread_graph_dir,
        )

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        topic = "empty-topic"
        thread_dir = get_thread_graph_dir(get_graph_dir(threads_dir), topic)
        thread_dir.mkdir(parents=True)

        assert _has_xref_decision(threads_dir, topic, {}) is None

    def test_returns_none_on_bad_threads_dir(self, tmp_path) -> None:
        """Non-existent threads dir is fail-open → None."""
        assert _has_xref_decision(tmp_path / "no-such-dir", "topic", {}) is None

    def test_same_thread_xref_not_counted(self, tmp_path) -> None:
        """Xref to an entry in the same thread is skipped; only cross-thread counts."""
        from watercooler.baseline_graph.annotations import (
            AnnotationEvent,
            append_annotation,
        )
        from watercooler.baseline_graph.storage import (
            get_graph_dir,
            get_thread_graph_dir,
        )
        from watercooler.baseline_graph.writer import upsert_entry_node, EntryData

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        topic = "my-thread"

        # Decision entry in the SAME thread.
        upsert_entry_node(
            threads_dir,
            EntryData(
                entry_id="DEC-SAME",
                thread_topic=topic,
                index=0,
                agent="Alice",
                role="implementer",
                entry_type="Decision",
                title="Same-thread decision",
                body="In-thread decision.",
                summary="",
            ),
        )

        thread_dir = get_thread_graph_dir(get_graph_dir(threads_dir), topic)
        append_annotation(
            thread_dir,
            AnnotationEvent(
                id="evt-001",
                target_id="other-entry",
                target_type="entry",
                kind="xref",
                value="DEC-SAME",
                actor="Alice",
                timestamp="2024-04-01T12:00:00+00:00",
            ),
        )

        # Same-thread Decision xref must not suppress (detect_stalled_open_loops
        # already handles same-thread Decisions via has_resolution). Index maps
        # the xref target back to the source topic → detector skips it.
        index = {"DEC-SAME": topic}
        assert _has_xref_decision(threads_dir, topic, index) is None

    def test_returns_none_on_malformed_xrefs_field(self, tmp_path) -> None:
        """Malformed annotation cache (xrefs is not iterable) must not raise — fail open."""
        import json
        from watercooler.baseline_graph.storage import (
            get_graph_dir,
            get_thread_graph_dir,
        )

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        topic = "my-thread"

        # Write a malformed annotation_state.json where xrefs is an int, not a list.
        thread_dir = get_thread_graph_dir(get_graph_dir(threads_dir), topic)
        thread_dir.mkdir(parents=True, exist_ok=True)
        state_path = thread_dir / "annotation_state.json"
        state_path.write_text(
            json.dumps({"entry-1": {"xrefs": 123, "annotations": []}})
        )

        # Must not raise TypeError — fail open and return None.
        assert _has_xref_decision(threads_dir, topic, {}) is None

    def test_returns_none_when_xrefs_is_null(self, tmp_path) -> None:
        """Regression for #338: xrefs stored as JSON null must not abort the
        annotation-state loop. The ``AnnotationState.from_dict`` fix coerces
        None → [], so the detector iterates a real list and returns None
        without a TypeError being caught by the broad outer except."""
        import json
        from watercooler.baseline_graph.storage import (
            get_graph_dir,
            get_thread_graph_dir,
        )

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        topic = "my-thread"

        thread_dir = get_thread_graph_dir(get_graph_dir(threads_dir), topic)
        thread_dir.mkdir(parents=True, exist_ok=True)
        state_path = thread_dir / "annotation_state.json"
        # xrefs explicitly null — historically caused TypeError("NoneType is not iterable").
        state_path.write_text(json.dumps({"entry-1": {"xrefs": None}}))

        assert _has_xref_decision(threads_dir, topic, {}) is None

    def test_uses_injected_index_without_disk_io(self, tmp_path) -> None:
        """The detector must rely on the injected index mapping and must not
        touch disk for the reverse lookup. With an injected mapping it resolves
        the xref target topic in O(1)."""
        from watercooler.baseline_graph.annotations import (
            AnnotationEvent,
            append_annotation,
        )
        from watercooler.baseline_graph.storage import (
            get_graph_dir,
            get_thread_graph_dir,
        )
        from watercooler.baseline_graph.writer import EntryData, upsert_entry_node

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        source_topic = "source-thread"
        other_topic = "target-thread"
        decision_id = "DEC-INJ-001"

        upsert_entry_node(
            threads_dir,
            EntryData(
                entry_id=decision_id,
                thread_topic=other_topic,
                index=0,
                agent="Alice",
                role="implementer",
                entry_type="Decision",
                title="Injected-index decision",
                body="Decided.",
                summary="",
            ),
        )

        source_dir = get_thread_graph_dir(get_graph_dir(threads_dir), source_topic)
        source_dir.mkdir(parents=True, exist_ok=True)
        append_annotation(
            source_dir,
            AnnotationEvent(
                id="evt-inj-001",
                target_id="src-entry-01",
                target_type="entry",
                kind="xref",
                value=decision_id,
                actor="Alice",
                timestamp="2024-04-01T12:00:00+00:00",
            ),
        )

        # Inject a pre-built index (as the daemon would). No disk index is read.
        index = {decision_id: other_topic}
        assert _has_xref_decision(threads_dir, source_topic, index) == decision_id

    def test_missing_from_index_is_treated_as_no_target(self, tmp_path) -> None:
        """An xref whose target is absent from the injected index is silently
        skipped — correctness degrades to a false negative (no suppression),
        never a false positive. The detector must not fall back to a disk scan."""
        from watercooler.baseline_graph.annotations import (
            AnnotationEvent,
            append_annotation,
        )
        from watercooler.baseline_graph.storage import (
            get_graph_dir,
            get_thread_graph_dir,
        )

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        source_topic = "source-thread"

        source_dir = get_thread_graph_dir(get_graph_dir(threads_dir), source_topic)
        source_dir.mkdir(parents=True, exist_ok=True)
        append_annotation(
            source_dir,
            AnnotationEvent(
                id="evt-absent-001",
                target_id="src-entry-01",
                target_type="entry",
                kind="xref",
                value="MISSING-999",
                actor="Alice",
                timestamp="2024-04-01T12:00:00+00:00",
            ),
        )

        # Empty index → xref target "MISSING-999" is not resolvable → return None.
        assert _has_xref_decision(threads_dir, source_topic, {}) is None

    def test_caps_xref_fan_out_at_50(self, tmp_path) -> None:
        """#343: no more than _MAX_XREFS_PER_STATE (50) targets are inspected per
        annotation state, regardless of how many xrefs are stored. This bounds
        traversal cost against runaway or adversarial annotation volume."""
        import json
        from unittest.mock import patch
        from watercooler.baseline_graph.storage import (
            get_graph_dir,
            get_thread_graph_dir,
        )
        from watercooler.project_coordinator_lib import _MAX_XREFS_PER_STATE

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        topic = "source-thread"

        thread_dir = get_thread_graph_dir(get_graph_dir(threads_dir), topic)
        thread_dir.mkdir(parents=True, exist_ok=True)

        # 100 xrefs, each pointing to a distinct target topic in the index.
        xref_ids = [f"ENT-{i:03d}" for i in range(100)]
        index = {xref_id: f"topic-{i}" for i, xref_id in enumerate(xref_ids)}

        state_path = thread_dir / "annotation_state.json"
        state_path.write_text(json.dumps({"entry-1": {"xrefs": xref_ids}}))

        # Stub get_entry_node_from_graph so the test is independent of graph data.
        with patch(
            "watercooler.baseline_graph.writer.get_entry_node_from_graph",
            return_value=None,
        ) as mock_get:
            _has_xref_decision(threads_dir, topic, index)

        # The cap bounds fan-out: we should see at most _MAX_XREFS_PER_STATE calls
        # (dedup can lower it further, but never raise it above the cap).
        assert mock_get.call_count <= _MAX_XREFS_PER_STATE

    def test_caps_total_xref_fan_out_across_states(self, tmp_path) -> None:
        """Codex Medium #1: even when many annotation states each carry xrefs
        within the per-state cap, the overall traversal must be bounded by
        ``_MAX_XREFS_TOTAL``. The per-state cap alone cannot protect against a
        thread with thousands of annotation states."""
        import json
        from unittest.mock import patch
        from watercooler.baseline_graph.storage import (
            get_graph_dir,
            get_thread_graph_dir,
        )
        from watercooler.project_coordinator_lib import (
            _MAX_XREFS_PER_STATE,
            _MAX_XREFS_TOTAL,
        )

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        topic = "source-thread"

        thread_dir = get_thread_graph_dir(get_graph_dir(threads_dir), topic)
        thread_dir.mkdir(parents=True, exist_ok=True)

        # 40 annotation states, each with 40 unique xrefs → 1600 distinct
        # xref targets in total. Per-state cap (50) would allow all 40 per
        # state through on its own; only the total cap can bound this.
        states_payload: dict[str, dict[str, list[str]]] = {}
        index: dict[str, str] = {}
        per_state = 40
        num_states = 40
        assert per_state <= _MAX_XREFS_PER_STATE, (
            "test assumes per_state stays under the per-state cap so the "
            "global cap is the only active ceiling"
        )
        for s in range(num_states):
            xref_ids = [f"ENT-{s:03d}-{i:03d}" for i in range(per_state)]
            states_payload[f"entry-{s}"] = {"xrefs": xref_ids}
            for xref_id in xref_ids:
                # Each target points to a distinct cross-thread topic so the
                # same-topic short-circuit never fires.
                index[xref_id] = f"target-{xref_id}"

        total_unique = num_states * per_state
        assert (
            total_unique > _MAX_XREFS_TOTAL
        ), "test must actually overshoot the global cap to be meaningful"

        state_path = thread_dir / "annotation_state.json"
        state_path.write_text(json.dumps(states_payload))

        with patch(
            "watercooler.baseline_graph.writer.get_entry_node_from_graph",
            return_value=None,
        ) as mock_get:
            _has_xref_decision(threads_dir, topic, index)

        # Total distinct fetches never exceed the global cap, even though the
        # per-state cap alone would have permitted ``total_unique`` of them.
        assert mock_get.call_count <= _MAX_XREFS_TOTAL

    def test_per_call_entry_dedup_repeated_ids(self, tmp_path) -> None:
        """Dedup key is xref_entry_id (the actual fetch key). Repeated entry_ids
        — whether duplicated within a single annotation state or appearing across
        multiple states — are fetched at most once per _has_xref_decision call.

        Deliberately NOT topic-based: two distinct entry_ids in the same target
        topic can differ in entry_type (Note vs Decision), so topic-level dedup
        would cause a false-negative stalled_open_loop. See
        test_cross_state_same_topic_different_entries_both_inspected below.
        """
        import json
        from unittest.mock import patch
        from watercooler.baseline_graph.storage import (
            get_graph_dir,
            get_thread_graph_dir,
        )

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        topic = "source-thread"

        thread_dir = get_thread_graph_dir(get_graph_dir(threads_dir), topic)
        thread_dir.mkdir(parents=True, exist_ok=True)

        # State A has [ENT-1, ENT-1, ENT-2] (intra-state duplicate).
        # State B has [ENT-1, ENT-3] (cross-state duplicate of ENT-1).
        # Unique ids across the call: {ENT-1, ENT-2, ENT-3} → 3 fetches.
        state_path = thread_dir / "annotation_state.json"
        state_path.write_text(
            json.dumps(
                {
                    "entry-a": {"xrefs": ["ENT-1", "ENT-1", "ENT-2"]},
                    "entry-b": {"xrefs": ["ENT-1", "ENT-3"]},
                }
            )
        )
        index = {"ENT-1": "target-1", "ENT-2": "target-2", "ENT-3": "target-3"}

        with patch(
            "watercooler.baseline_graph.writer.get_entry_node_from_graph",
            return_value={"entry_type": "Note", "thread_topic": "target-x"},
        ) as mock_get:
            _has_xref_decision(threads_dir, topic, index)

        assert mock_get.call_count == 3
        fetched_ids = {call.args[1] for call in mock_get.call_args_list}
        assert fetched_ids == {"ENT-1", "ENT-2", "ENT-3"}

    def test_cross_state_same_topic_different_entries_both_inspected(
        self, tmp_path
    ) -> None:
        """Codex P1 regression: two annotation states xref different entries
        that happen to resolve to the SAME target topic. An earlier draft
        deduped on target_topic, which caused the second xref to be skipped —
        if that skipped entry was the Decision, suppression would silently
        fail and produce a false-negative stalled_open_loop.

        The correct behavior: dedup is by xref_entry_id, so distinct entries
        in the same target topic are both fetched and the Decision is found
        regardless of state iteration order.

        Both iteration orders are exercised because
        ``materialize_all_states()`` builds per-target state order from a
        ``set`` in real caches (nondeterministic in production).
        """
        import json
        from unittest.mock import patch
        from watercooler.baseline_graph.storage import (
            get_graph_dir,
            get_thread_graph_dir,
        )

        target_topic = "target-topic"
        index = {"NOTE-1": target_topic, "DEC-1": target_topic}

        def _fake_get(threads_dir_arg, entry_id, topic=None):
            if entry_id == "NOTE-1":
                return {"entry_type": "Note", "thread_topic": target_topic}
            if entry_id == "DEC-1":
                return {"entry_type": "Decision", "thread_topic": target_topic}
            return None

        # Exercise both iteration orders — Python dicts preserve insertion
        # order from json.load, so writing the JSON keys in a specific order
        # deterministically controls which state the detector sees first.
        orderings = [
            # Note first, Decision second — triggers the old topic-dedup bug.
            {"state-a": {"xrefs": ["NOTE-1"]}, "state-b": {"xrefs": ["DEC-1"]}},
            # Decision first, Note second — passed under the buggy code
            # (Decision returned before dedup mattered). Included so the test
            # also guards the reverse order after the fix.
            {"state-b": {"xrefs": ["DEC-1"]}, "state-a": {"xrefs": ["NOTE-1"]}},
        ]

        for ordering in orderings:
            threads_dir = tmp_path / f"threads-{'-'.join(ordering.keys())}"
            threads_dir.mkdir()
            topic = "source-thread"
            thread_dir = get_thread_graph_dir(get_graph_dir(threads_dir), topic)
            thread_dir.mkdir(parents=True, exist_ok=True)
            state_path = thread_dir / "annotation_state.json"
            state_path.write_text(json.dumps(ordering))

            with patch(
                "watercooler.baseline_graph.writer.get_entry_node_from_graph",
                side_effect=_fake_get,
            ) as mock_get:
                result = _has_xref_decision(threads_dir, topic, index)

            assert result == "DEC-1", (
                f"Decision must be found for state ordering "
                f"{list(ordering.keys())}; got {result!r}"
            )
            fetched_ids = {call.args[1] for call in mock_get.call_args_list}
            assert "DEC-1" in fetched_ids, (
                f"Decision entry never fetched for ordering "
                f"{list(ordering.keys())}; fetched: {fetched_ids}"
            )

    def test_detect_stalled_open_loops_builds_index_when_none_passed(
        self, tmp_path
    ) -> None:
        """CLI/test convenience: when no index is passed and threads_dir is set,
        detect_stalled_open_loops builds a local index rather than failing."""
        from watercooler.baseline_graph.annotations import (
            AnnotationEvent,
            append_annotation,
        )
        from watercooler.baseline_graph.storage import (
            get_graph_dir,
            get_thread_graph_dir,
        )
        from watercooler.baseline_graph.writer import EntryData, upsert_entry_node

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        source_topic = "source-thread"
        other_topic = "other-thread"
        decision_id = "DEC-CLI-001"

        upsert_entry_node(
            threads_dir,
            EntryData(
                entry_id=decision_id,
                thread_topic=other_topic,
                index=0,
                agent="Alice",
                role="implementer",
                entry_type="Decision",
                title="CLI decision",
                body="Decided.",
                summary="",
            ),
        )

        source_dir = get_thread_graph_dir(get_graph_dir(threads_dir), source_topic)
        source_dir.mkdir(parents=True, exist_ok=True)
        append_annotation(
            source_dir,
            AnnotationEvent(
                id="evt-cli-001",
                target_id="src-entry-01",
                target_type="entry",
                kind="xref",
                value=decision_id,
                actor="Alice",
                timestamp="2024-04-01T12:00:00+00:00",
            ),
        )

        entries = [
            _entry(entry_type="Note", index=0),
            _entry(entry_type="Plan", index=1),
            _entry(entry_type="Note", index=2),
        ]
        # entry_topic_index omitted entirely — the detector builds it locally.
        finding = detect_stalled_open_loops(
            entries, source_topic, "OPEN", set(), set(), threads_dir=threads_dir
        )
        assert finding is not None
        assert finding.category == "coordinator_xref_suppression"


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

    def test_aware_burst_suppression_preserves_info_severity(self) -> None:
        """Phase 3a-3: suppression tag annotates burst but keeps native `info`."""
        baseline = BurstBaseline(
            baseline_rate=1.0,
            last_entry_count=2,
            last_tick_time=_NOW - _DAY,
        )
        entries = [_entry(timestamp=self._old_ts(10), index=0)] + [
            _entry(timestamp=self._old_ts(0.5), index=i) for i in range(1, 23)
        ]
        finding, _ = detect_aware_burst(
            entries,
            "topic-a",
            baseline,
            _NOW,
            suppression_tags={"parked", "deferred"},
            thread_tags={"parked"},
        )
        assert finding is not None
        assert finding.category == "aware_burst"
        assert finding.severity == "info"  # preserved, not upgraded/downgraded
        assert finding.details["suppressed_by"] == "tag:parked"
        assert "suppressed by tag:parked" in finding.message

    def test_aware_burst_backward_compat_without_kwargs(self) -> None:
        """Phase 3a-3: detector still works when new kwargs omitted."""
        baseline = BurstBaseline(
            baseline_rate=1.0,
            last_entry_count=2,
            last_tick_time=_NOW - _DAY,
        )
        entries = [_entry(timestamp=self._old_ts(10), index=0)] + [
            _entry(timestamp=self._old_ts(0.5), index=i) for i in range(1, 23)
        ]
        finding, _ = detect_aware_burst(entries, "topic-a", baseline, _NOW)
        assert finding is not None
        assert finding.severity == "info"
        assert "suppressed_by" not in finding.details


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

    def test_role_concentration_suppression_preserves_info_severity(self) -> None:
        """Phase 3a-3: suppression tag annotates concentration but keeps `info`."""
        entries = _entries(5, role="implementer")
        finding = detect_role_concentration(
            entries,
            "my-thread",
            "OPEN",
            suppression_tags={"wontfix"},
            thread_tags={"wontfix"},
        )
        assert finding is not None
        assert finding.category == "aware_role_concentration"
        assert finding.severity == "info"  # preserved, not upgraded
        assert finding.details["suppressed_by"] == "tag:wontfix"
        assert "suppressed by tag:wontfix" in finding.message

    def test_role_concentration_backward_compat_without_kwargs(self) -> None:
        """Phase 3a-3: detector still works when new kwargs omitted."""
        entries = _entries(5, role="implementer")
        finding = detect_role_concentration(entries, "my-thread", "OPEN")
        assert finding is not None
        assert finding.severity == "info"
        assert "suppressed_by" not in finding.details

    def test_role_concentration_no_suppression_when_tags_do_not_match(self) -> None:
        """Phase 3a-3: no marker when suppression_tags and thread_tags disjoint."""
        entries = _entries(5, role="implementer")
        finding = detect_role_concentration(
            entries,
            "my-thread",
            "OPEN",
            suppression_tags={"parked"},
            thread_tags={"in-progress"},
        )
        assert finding is not None
        assert finding.severity == "info"
        assert "suppressed_by" not in finding.details


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
        assert t2["schema_version"] == 2
        assert t2["days_since_last"] == 18
        assert t2["workflow_shape_id"] == "wf1"
        assert t2["workflow_shape_name"] == "linear"
        assert t2["workflow_confidence"] == 0.9
        assert t2["analysis_stalled"] is True
        assert "stalled" not in t2
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


class TestBuildT2Context:
    """_build_t2_context: schema_version 2 and analysis_stalled key."""

    def test_build_t2_context_schema_version_2(self) -> None:
        """test_build_t2_context_schema_version_2: schema_version is 2."""
        result = _build_t2_context({})
        assert result["schema_version"] == 2

    def test_build_t2_context_uses_analysis_stalled_key(self) -> None:
        """test_build_t2_context_uses_analysis_stalled_key: emits analysis_stalled, not stalled."""
        result = _build_t2_context({"stalled": True})
        assert result["analysis_stalled"] is True
        assert "stalled" not in result

    def test_build_t2_context_analysis_stalled_false_by_default(self) -> None:
        """analysis_stalled defaults to False when source key is absent."""
        result = _build_t2_context({})
        assert result["analysis_stalled"] is False
        assert "stalled" not in result


class TestCoordinatorLeadFromDictV1Migration:
    """CoordinatorLead.from_dict: v1 → v2 migration for stalled → analysis_stalled."""

    def test_coordinator_lead_from_dict_v1_migration(self) -> None:
        """test_coordinator_lead_from_dict_v1_migration: v1 stalled key → analysis_stalled."""
        v1_dict = {
            "schema_version": 1,
            "source_category": "stalled_open_loop",
            "source_topic": "t",
            "summary": "s",
            "relevance_tags": [],
            "t2_context": {
                "schema_version": 1,
                "stalled": True,
                "days_since_last": 10,
                "has_decision": False,
                "has_closure": False,
            },
        }
        lead = CoordinatorLead.from_dict(v1_dict)
        assert lead.t2_context is not None
        assert lead.t2_context["analysis_stalled"] is True
        assert "stalled" not in lead.t2_context
        assert lead.t2_context["schema_version"] == 2

    def test_from_dict_v2_payload_unchanged(self) -> None:
        """v2 payloads with analysis_stalled are passed through without modification."""
        v2_dict = {
            "schema_version": 2,
            "source_category": "stalled_open_loop",
            "source_topic": "t",
            "summary": "s",
            "relevance_tags": [],
            "t2_context": {
                "schema_version": 2,
                "analysis_stalled": False,
                "days_since_last": 5,
            },
        }
        lead = CoordinatorLead.from_dict(v2_dict)
        assert lead.t2_context is not None
        assert lead.t2_context["analysis_stalled"] is False
        assert "stalled" not in lead.t2_context

    def test_from_dict_null_t2_context_unchanged(self) -> None:
        """None t2_context is not affected by migration logic."""
        lead = CoordinatorLead.from_dict({"t2_context": None})
        assert lead.t2_context is None

    def test_from_dict_both_keys_present_no_clobber(self) -> None:
        """Migration skipped when both stalled and analysis_stalled are present."""
        lead = CoordinatorLead.from_dict(
            {
                "t2_context": {
                    "stalled": False,
                    "analysis_stalled": True,
                    "schema_version": 1,
                }
            }
        )
        assert lead.t2_context["analysis_stalled"] is True
        assert "stalled" in lead.t2_context  # migration skipped — no clobber


# ---------------------------------------------------------------------------
# _resolve_related_threads — relation-helper tests (1-9)
# ---------------------------------------------------------------------------


def _xref_pair(source_topic: str, b_topic: str, *, src_entry: str = "E-SRC", tgt_entry: str = "E-TGT") -> dict:
    """Build an xref_pairs dict for the (source_topic, b_topic) edge."""
    return {frozenset({source_topic, b_topic}): {"source_topic": source_topic, "source_entry_id": src_entry, "target_entry_id": tgt_entry}}


class TestBuildXrefTopicGraphCap:
    """Regression for PR #627 claude[bot] Medium: inner-loop cap overrun.

    The outer state-key loop had ``if len(seen) >= _MAX_XREFS_TOTAL: break``
    but the inner xref loop did not, so ``seen`` could reach
    ``_MAX_XREFS_TOTAL + _MAX_XREFS_PER_STATE - 1`` per source topic
    before the outer guard fired.
    """

    def test_inner_loop_respects_total_cap(self, tmp_path) -> None:
        import json
        from watercooler.baseline_graph.storage import (
            get_graph_dir,
            get_thread_graph_dir,
        )
        from watercooler.project_coordinator_lib import (
            _MAX_XREFS_PER_STATE,
            _MAX_XREFS_TOTAL,
            _build_xref_topic_graph,
        )

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        topic = "source-thread"

        thread_dir = get_thread_graph_dir(get_graph_dir(threads_dir), topic)
        thread_dir.mkdir(parents=True, exist_ok=True)

        # 12 states × 50 xrefs each = 600 distinct targets. With only the
        # outer guard, seen could reach 549 (500 + 49). With both guards,
        # the function must stop creating new `pairs` entries the moment
        # seen hits 500 — we verify by bounding |pairs| from the first
        # source topic.
        per_state = _MAX_XREFS_PER_STATE  # 50
        num_states = 12
        states_payload: dict[str, dict[str, list[str]]] = {}
        entry_topic_index: dict[str, str] = {}
        for s in range(num_states):
            xref_ids = [f"ENT-{s:03d}-{i:03d}" for i in range(per_state)]
            states_payload[f"entry-{s}"] = {"xrefs": xref_ids}
            for xref_id in xref_ids:
                entry_topic_index[xref_id] = f"target-{xref_id}"

        (thread_dir / "annotation_state.json").write_text(json.dumps(states_payload))

        # Include target topics in all_topics so pair entries get recorded.
        all_topics = [topic] + [f"target-{xid}" for xid in entry_topic_index]

        pairs = _build_xref_topic_graph(threads_dir, all_topics, entry_topic_index)

        # Each new pair corresponds to a distinct cross-topic target added
        # to `seen` during this source topic's iteration. The fix caps seen
        # at _MAX_XREFS_TOTAL, which bounds the number of cross-topic pairs
        # produced from this single source.
        assert len(pairs) <= _MAX_XREFS_TOTAL, (
            f"inner-loop cap overrun: got {len(pairs)} pairs, "
            f"expected ≤ {_MAX_XREFS_TOTAL}"
        )


class TestResolveRelatedThreads:
    """_resolve_related_threads: relation evidence contract."""

    # --- Tier 1: xref ---

    def test_xref_tier_fires_a_to_b(self) -> None:
        """Test 1: Xref tier fires when A→B xref exists (a_to_b direction)."""
        tags = {"thread-a": set(), "thread-b": set()}
        pairs = _xref_pair("thread-a", "thread-b")
        result = _resolve_related_threads("thread-a", tags, pairs, None, None, "pair:")
        assert "thread-b" in result
        ev = result["thread-b"]
        assert any(e["tier"] == "xref" for e in ev)
        xref_ev = next(e for e in ev if e["tier"] == "xref")
        assert xref_ev["direction"] == "a_to_b"

    def test_xref_tier_fires_b_to_a(self) -> None:
        """Test 2: Xref tier fires when B→A xref exists (b_to_a direction from A's view)."""
        tags = {"thread-a": set(), "thread-b": set()}
        # B is the source (B xrefs into A)
        pairs = {frozenset({"thread-a", "thread-b"}): {"source_topic": "thread-b", "source_entry_id": "E-B", "target_entry_id": "E-A"}}
        result = _resolve_related_threads("thread-a", tags, pairs, None, None, "pair:")
        assert "thread-b" in result
        xref_ev = next(e for e in result["thread-b"] if e["tier"] == "xref")
        assert xref_ev["direction"] == "b_to_a"

    def test_xref_tier_empty_when_no_xref(self) -> None:
        """Test 3: Xref tier yields empty when no cross-thread xref exists."""
        tags = {"thread-a": set(), "thread-b": set()}
        result = _resolve_related_threads("thread-a", tags, {}, None, None, "pair:")
        assert result == {}

    # --- Tier 2: pair_tag ---

    def test_pair_tag_tier_fires_on_shared_prefix(self) -> None:
        """Test 4: Pair-tag tier fires when both threads share a pair: tag."""
        tags = {"thread-a": {"pair:auth-rework", "other"}, "thread-b": {"pair:auth-rework", "unrelated"}}
        result = _resolve_related_threads("thread-a", tags, {}, None, None, "pair:")
        assert "thread-b" in result
        ev = result["thread-b"]
        assert any(e["tier"] == "pair_tag" for e in ev)
        tag_ev = next(e for e in ev if e["tier"] == "pair_tag")
        assert tag_ev["tags"] == ["pair:auth-rework"]

    def test_pair_tag_tier_does_not_fire_on_arbitrary_shared_tag(self) -> None:
        """Test 5: Pair-tag tier does NOT fire on arbitrary shared tags without pair: prefix."""
        tags = {"thread-a": {"security", "p1"}, "thread-b": {"security", "p1"}}
        result = _resolve_related_threads("thread-a", tags, {}, None, None, "pair:")
        assert result == {}

    # --- Tier 3/4 combination ---

    def test_pulse_block_alone_does_not_qualify(self) -> None:
        """Test 6: Pulse-block alone (without shared workflow shape) does NOT qualify."""
        tags = {"thread-a": set(), "thread-b": set()}
        analysis = {"thread-a": {"workflow_shape": {"name": "sprint"}}, "thread-b": {"workflow_shape": {"name": "OTHER"}}}
        clusters = [("R01", "risk text", frozenset({"thread-a", "thread-b"}))]
        result = _resolve_related_threads("thread-a", tags, {}, analysis, clusters, "pair:")
        assert result == {}

    def test_workflow_shape_alone_does_not_qualify(self) -> None:
        """Test 7: Shared workflow shape alone (without pulse-block cluster) does NOT qualify."""
        tags = {"thread-a": set(), "thread-b": set()}
        analysis = {"thread-a": {"workflow_shape": {"name": "sprint"}}, "thread-b": {"workflow_shape": {"name": "sprint"}}}
        result = _resolve_related_threads("thread-a", tags, {}, analysis, None, "pair:")
        assert result == {}

    def test_pulse_block_and_workflow_shape_together_qualify(self) -> None:
        """Test 8: Pulse-block ∩ workflow-shape qualifies a pair."""
        tags = {"thread-a": set(), "thread-b": set()}
        analysis = {
            "thread-a": {"workflow_shape": {"name": "sprint"}},
            "thread-b": {"workflow_shape": {"name": "sprint"}},
        }
        clusters = [("R01", "risk text", frozenset({"thread-a", "thread-b"}))]
        result = _resolve_related_threads("thread-a", tags, {}, analysis, clusters, "pair:")
        assert "thread-b" in result
        ev = result["thread-b"]
        assert any(e["tier"] == "pulse_block+workflow_shape" for e in ev)
        t3t4_ev = next(e for e in ev if e["tier"] == "pulse_block+workflow_shape")
        assert t3t4_ev["risk_rule_id"] == "R01"
        assert t3t4_ev["workflow_shape_name"] == "sprint"

    def test_stale_pulse_block_ignored(self) -> None:
        """Test 9: No Tier 3 evidence when risk_clusters is None (stale/unavailable)."""
        tags = {"thread-a": set(), "thread-b": set()}
        analysis = {
            "thread-a": {"workflow_shape": {"name": "sprint"}},
            "thread-b": {"workflow_shape": {"name": "sprint"}},
        }
        # risk_clusters=None simulates stale/unavailable pulse_block
        result = _resolve_related_threads("thread-a", tags, {}, analysis, None, "pair:")
        assert result == {}

    # --- Post-review: multi-tag provenance + defensive guards ---

    def test_pair_tag_records_all_shared_tags(self) -> None:
        """Multiple shared pair: tags must all be recorded in provenance.

        Regression for PR #627 claude[bot] Medium finding: earlier code
        stored only `sorted(shared)[0]`, dropping information needed to
        audit the pairing signal without re-running the detector.
        """
        tags = {
            "thread-a": {"pair:alpha", "pair:beta", "pair:gamma", "other"},
            "thread-b": {"pair:gamma", "pair:alpha", "unrelated"},
        }
        result = _resolve_related_threads("thread-a", tags, {}, None, None, "pair:")
        ev = next(e for e in result["thread-b"] if e["tier"] == "pair_tag")
        assert ev["tags"] == ["pair:alpha", "pair:gamma"]

    def test_non_dict_analysis_entry_does_not_lose_other_tiers(self) -> None:
        """Tier 3+4 resolution must tolerate non-dict analysis_by_topic values.

        Previously a peer whose analysis value was None/str raised
        AttributeError inside the shape_peers comprehension, which the
        outer try in detect_role_complement caught — silently dropping
        Tier 1 and Tier 2 evidence already collected for the whole thread.
        """
        tags = {
            "thread-a": {"pair:x"},
            "thread-b": {"pair:x"},
            "corrupt": set(),
        }
        # thread-a has a valid workflow_shape; 'corrupt' entry is malformed.
        analysis = {
            "thread-a": {"workflow_shape": {"name": "sprint"}},
            "thread-b": {"workflow_shape": {"name": "sprint"}},
            "corrupt": None,
        }
        clusters = [("R01", "risk", frozenset({"thread-a", "thread-b"}))]
        result = _resolve_related_threads(
            "thread-a", tags, {}, analysis, clusters, "pair:"
        )
        # Tier 2 evidence must survive even though Tier 3+4 sees a bad peer.
        assert "thread-b" in result
        assert any(e["tier"] == "pair_tag" for e in result["thread-b"])

    def test_non_dict_workflow_shape_does_not_lose_other_tiers(self) -> None:
        """Tier 3+4 must tolerate ``workflow_shape`` that is not a dict.

        Previously ``(ta.get("workflow_shape") or {}).get("name")`` only
        handled the falsy case; a malformed string like
        ``{"workflow_shape": "not-a-dict"}`` would still raise
        AttributeError and the outer try would discard Tier 1/2
        evidence for the whole thread.
        """
        tags = {
            "thread-a": {"pair:x"},
            "thread-b": {"pair:x"},
            "malformed": set(),
        }
        analysis = {
            "thread-a": {"workflow_shape": {"name": "sprint"}},
            "thread-b": {"workflow_shape": {"name": "sprint"}},
            "malformed": {"workflow_shape": "not-a-dict"},
        }
        clusters = [("R01", "risk", frozenset({"thread-a", "thread-b"}))]
        result = _resolve_related_threads(
            "thread-a", tags, {}, analysis, clusters, "pair:"
        )
        assert "thread-b" in result
        assert any(e["tier"] == "pair_tag" for e in result["thread-b"])

    def test_non_dict_workflow_shape_on_source_thread_falls_back(self) -> None:
        """Source thread with malformed workflow_shape: Tier 3+4 silently off.

        The source thread itself may carry a malformed ``workflow_shape``.
        When that happens, ``a_shape_name`` must resolve to None and
        Tier 3+4 resolution is skipped; Tier 2 evidence must survive.
        """
        tags = {"thread-a": {"pair:x"}, "thread-b": {"pair:x"}}
        analysis = {
            "thread-a": {"workflow_shape": 42},  # not a dict
            "thread-b": {"workflow_shape": {"name": "sprint"}},
        }
        clusters = [("R01", "risk", frozenset({"thread-a", "thread-b"}))]
        result = _resolve_related_threads(
            "thread-a", tags, {}, analysis, clusters, "pair:"
        )
        assert "thread-b" in result
        assert any(e["tier"] == "pair_tag" for e in result["thread-b"])
        assert not any(
            e["tier"] == "pulse_block+workflow_shape" for e in result["thread-b"]
        )


# ---------------------------------------------------------------------------
# detect_role_complement — detector tests (10-19)
# ---------------------------------------------------------------------------


def _rc_entries(roles: list[str], topic: str = "t") -> list[EntryView]:
    """Build EntryView list with the given role sequence."""
    return [
        EntryView(entry_id=f"{topic}-E{i:02d}", agent="Alice", role=r,
                  entry_type="Note", timestamp="2024-04-01T12:00:00Z", index=i)
        for i, r in enumerate(roles)
    ]


class TestDetectRoleComplement:
    """detect_role_complement: detector contract (tests 10-19)."""

    def _run(
        self,
        all_entries: dict[str, list[EntryView]],
        all_tags: dict[str, set[str]],
        *,
        monitored_roles: list[str] | None = None,
        max_per_thread: int = 3,
        min_role_entries_in_related: int = 2,
    ) -> list[CoordinatorFinding]:
        """Run detector with pair: tags providing Tier 2 evidence (no file system)."""
        return detect_role_complement(
            all_entries,
            all_tags,
            None,  # threads_dir=None disables xref graph build
            {},    # empty entry_topic_index
            None,  # analysis_by_topic
            None,  # risk_clusters
            monitored_roles=monitored_roles or ["tester", "critic"],
            max_per_thread=max_per_thread,
            min_role_entries_in_related=min_role_entries_in_related,
        )

    def test_emits_one_finding_per_triple(self) -> None:
        """Test 10: Emits one finding per (A, role, B) triple."""
        all_entries = {
            "thread-a": _rc_entries(["planner"] * 3, "a"),
            "thread-b": _rc_entries(["tester"] * 3, "b"),
        }
        all_tags = {"thread-a": {"pair:work"}, "thread-b": {"pair:work"}}
        findings = self._run(all_entries, all_tags)
        rc = [f for f in findings if f.category == "connect_role_complement"]
        assert len(rc) == 1
        assert rc[0].topic == "thread-a"
        assert rc[0].details["missing_role"] == "tester"
        assert rc[0].details["related_thread_topic"] == "thread-b"

    def test_no_emission_when_a_has_role(self) -> None:
        """Test 11: Does NOT emit when thread A already has the monitored role."""
        all_entries = {
            "thread-a": _rc_entries(["tester"] * 3, "a"),
            "thread-b": _rc_entries(["tester"] * 3, "b"),
        }
        all_tags = {"thread-a": {"pair:work"}, "thread-b": {"pair:work"}}
        findings = self._run(all_entries, all_tags)
        assert not any(f.category == "connect_role_complement" for f in findings)

    def test_no_emission_when_b_has_single_role_entry(self) -> None:
        """Test 12: Does NOT emit when B has only 1 entry with the role (below threshold)."""
        all_entries = {
            "thread-a": _rc_entries(["planner"] * 3, "a"),
            "thread-b": _rc_entries(["tester"], "b"),  # only 1 tester entry
        }
        all_tags = {"thread-a": {"pair:work"}, "thread-b": {"pair:work"}}
        findings = self._run(all_entries, all_tags, min_role_entries_in_related=2)
        assert not any(f.category == "connect_role_complement" for f in findings)

    def test_no_emission_when_role_not_monitored(self) -> None:
        """Test 13: Does NOT emit when the missing role is not in monitored_roles."""
        all_entries = {
            "thread-a": _rc_entries(["planner"] * 3, "a"),  # missing tester but also missing planner
            "thread-b": _rc_entries(["tester"] * 3, "b"),
        }
        all_tags = {"thread-a": {"pair:work"}, "thread-b": {"pair:work"}}
        findings = self._run(all_entries, all_tags, monitored_roles=["critic"])  # tester not monitored
        assert not any(f.category == "connect_role_complement" for f in findings)

    def test_no_emission_when_disabled(self) -> None:
        """Test 14: Does NOT emit when monitored_roles is empty (disabled state)."""
        all_entries = {
            "thread-a": _rc_entries(["planner"] * 3, "a"),
            "thread-b": _rc_entries(["tester"] * 3, "b"),
        }
        all_tags = {"thread-a": {"pair:work"}, "thread-b": {"pair:work"}}
        findings = detect_role_complement(
            all_entries, all_tags, None, {}, None, None, monitored_roles=[]
        )
        assert not any(f.category == "connect_role_complement" for f in findings)

    def test_cap_truncation_honoured(self) -> None:
        """Test 15: Cap truncation honoured; truncation marker on first surviving finding."""
        all_entries = {
            "thread-a": _rc_entries(["planner"] * 3, "a"),
            "thread-b": _rc_entries(["tester"] * 3, "b"),
            "thread-c": _rc_entries(["tester"] * 3, "c"),
            "thread-d": _rc_entries(["tester"] * 3, "d"),
            "thread-e": _rc_entries(["tester"] * 3, "e"),
        }
        # All paired with A so multiple (A, tester, *) triples are found
        tags = {
            "thread-a": {"pair:work"},
            "thread-b": {"pair:work"},
            "thread-c": {"pair:work"},
            "thread-d": {"pair:work"},
            "thread-e": {"pair:work"},
        }
        findings = self._run(all_entries, tags, max_per_thread=2)
        rc = [f for f in findings if f.category == "connect_role_complement" and f.topic == "thread-a"]
        assert len(rc) == 2
        assert rc[0].details.get("role_complement_truncated") is True

    def test_fail_open_on_annotation_load_error(self) -> None:
        """Test 16: Fail-open: annotation-state load error → no emission, no raise."""
        # threads_dir pointing to non-existent path; xref graph build fails open → empty pairs
        all_entries = {
            "thread-a": _rc_entries(["planner"] * 3, "a"),
            "thread-b": _rc_entries(["tester"] * 3, "b"),
        }
        # No pair: tags, no xref pairs → no relation evidence → no emission
        all_tags: dict[str, set[str]] = {"thread-a": set(), "thread-b": set()}
        from pathlib import Path
        findings = detect_role_complement(
            all_entries, all_tags, Path("/nonexistent/path"), {}, None, None,
            monitored_roles=["tester"],
        )
        # Should not raise; no emission because no relation evidence
        assert not any(f.category == "connect_role_complement" for f in findings)

    def test_relation_evidence_non_empty_in_every_finding(self) -> None:
        """Test 17: relation_evidence list is non-empty in every emitted finding."""
        all_entries = {
            "thread-a": _rc_entries(["planner"] * 3, "a"),
            "thread-b": _rc_entries(["tester"] * 3, "b"),
        }
        all_tags = {"thread-a": {"pair:work"}, "thread-b": {"pair:work"}}
        findings = self._run(all_entries, all_tags)
        rc = [f for f in findings if f.category == "connect_role_complement"]
        assert rc, "expected at least one finding"
        for f in rc:
            assert f.details.get("relation_evidence"), "relation_evidence must be non-empty"

    def test_dedup_signature_stable_across_ticks(self) -> None:
        """Test 19: Dedup signature is stable for the same triple across two runs."""
        all_entries = {
            "thread-a": _rc_entries(["planner"] * 3, "a"),
            "thread-b": _rc_entries(["tester"] * 3, "b"),
        }
        all_tags = {"thread-a": {"pair:work"}, "thread-b": {"pair:work"}}
        findings1 = self._run(all_entries, all_tags)
        findings2 = self._run(all_entries, all_tags)
        sigs1 = {f.dedup_signature for f in findings1 if f.category == "connect_role_complement"}
        sigs2 = {f.dedup_signature for f in findings2 if f.category == "connect_role_complement"}
        assert sigs1 == sigs2
        assert "thread-a|tester|thread-b" in sigs1


class TestRoleComplementConfigValidation:
    """ProjectCoordinatorConfig: role_complement_monitored_roles validator."""

    def test_config_rejects_unknown_canonical_role(self) -> None:
        """Test 18: Config validation rejects unknown canonical role strings."""
        import pytest
        from pydantic import ValidationError
        from watercooler.config_schema import ProjectCoordinatorConfig

        with pytest.raises(ValidationError, match="unknown canonical roles"):
            ProjectCoordinatorConfig(role_complement_monitored_roles=["tester", "ninja"])

    def test_config_accepts_valid_subset(self) -> None:
        """Config accepts valid subset of canonical roles."""
        from watercooler.config_schema import ProjectCoordinatorConfig

        cfg = ProjectCoordinatorConfig(role_complement_monitored_roles=["tester", "critic"])
        assert cfg.role_complement_monitored_roles == ["tester", "critic"]

    def test_config_accepts_empty_list(self) -> None:
        """Config accepts empty list (disables monitoring)."""
        from watercooler.config_schema import ProjectCoordinatorConfig

        cfg = ProjectCoordinatorConfig(role_complement_monitored_roles=[])
        assert cfg.role_complement_monitored_roles == []

    def test_role_complement_disabled_by_default(self) -> None:
        """Config default: role_complement_enabled is False."""
        from watercooler.config_schema import ProjectCoordinatorConfig

        cfg = ProjectCoordinatorConfig()
        assert cfg.role_complement_enabled is False

    def test_config_deduplicates_monitored_roles(self) -> None:
        """Duplicate entries in monitored_roles are silently removed, preserving order."""
        from watercooler.config_schema import ProjectCoordinatorConfig

        cfg = ProjectCoordinatorConfig(
            role_complement_monitored_roles=["tester", "tester", "critic"]
        )
        assert cfg.role_complement_monitored_roles == ["tester", "critic"]

    def test_config_rejects_empty_pair_tag_prefix(self) -> None:
        """Empty pair_tag_prefix is rejected — it would match all tags."""
        import pytest
        from pydantic import ValidationError
        from watercooler.config_schema import ProjectCoordinatorConfig

        with pytest.raises(ValidationError, match="non-empty"):
            ProjectCoordinatorConfig(role_complement_pair_tag_prefix="")

    def test_config_accepts_custom_pair_tag_prefix(self) -> None:
        """Non-empty pair_tag_prefix is accepted."""
        from watercooler.config_schema import ProjectCoordinatorConfig

        cfg = ProjectCoordinatorConfig(role_complement_pair_tag_prefix="related:")
        assert cfg.role_complement_pair_tag_prefix == "related:"


class TestRoleComplementTruncationPriority:
    """Regression: global weakest-first truncation across all monitored roles."""

    def test_xref_beats_pair_tag_under_cap(self):
        """Fix 2 regression: xref-backed finding kept over pair_tag when max_per_thread=1.

        thread-a is missing tester (pair_tag relation to thread-b) and critic
        (xref relation to thread-c). With cap=1, the xref finding (stronger) must
        survive; the pair_tag finding (weaker) must be dropped.

        We patch _resolve_related_threads to inject controlled Tier 1 and Tier 2
        evidence without needing a real threads_dir or graph on disk.
        """
        import unittest.mock as mock
        from watercooler.project_coordinator_lib import detect_role_complement

        # thread-a: only planner → missing tester AND critic (tester listed first)
        # thread-b: has tester entries
        # thread-c: has critic entries
        all_active_entries = {
            "thread-a": _rc_entries(["planner"] * 3, "thread-a"),
            "thread-b": _rc_entries(["tester"] * 3, "thread-b"),
            "thread-c": _rc_entries(["critic"] * 3, "thread-c"),
        }
        all_active_tags: dict = {
            "thread-a": set(),
            "thread-b": set(),
            "thread-c": set(),
        }

        # Inject relations: thread-b via pair_tag (Tier 2, weaker),
        # thread-c via xref (Tier 1, stronger).
        def fake_resolve(topic, *args, **kwargs):
            if topic == "thread-a":
                return {
                    "thread-b": [{"tier": "pair_tag", "tags": ["pair:feature-x"]}],
                    "thread-c": [{"tier": "xref", "source_entry_id": "a-E01", "target_entry_id": "c-E01", "direction": "a_to_b"}],
                }
            return {}

        with mock.patch(
            "watercooler.project_coordinator_lib._resolve_related_threads",
            side_effect=fake_resolve,
        ):
            findings = detect_role_complement(
                all_active_entries,
                all_active_tags,
                threads_dir=None,
                entry_topic_index={},
                analysis_by_topic=None,
                risk_clusters=None,
                monitored_roles=["tester", "critic"],  # tester listed first (weaker evidence)
                max_per_thread=1,
                pair_tag_prefix="pair:",
                min_role_entries_in_related=2,
            )

        assert len(findings) == 1, f"expected 1 finding under cap, got {len(findings)}"
        f = findings[0]
        # The xref-backed critic finding (stronger) must be kept over pair_tag tester
        assert f.details["missing_role"] == "critic", (
            f"Expected critic (xref, stronger) to survive cap; got {f.details['missing_role']}"
        )
        assert f.details["role_complement_truncated"] is True

    def test_sort_is_deterministic_across_dict_orderings(self):
        """Tie on evidence tier must resolve by b_topic (lexicographic), not dict order.

        Regression: when multiple related threads have the same tier rank and role,
        the survivor under max_per_thread varied with filesystem/dict insertion order.
        """
        import unittest.mock as mock
        from watercooler.project_coordinator_lib import detect_role_complement

        all_active_entries = {
            "thread-a": _rc_entries(["planner"] * 3, "thread-a"),
            "thread-b": _rc_entries(["tester"] * 3, "thread-b"),
            "thread-c": _rc_entries(["tester"] * 3, "thread-c"),
        }
        all_active_tags: dict = {"thread-a": set(), "thread-b": set(), "thread-c": set()}

        # Both thread-b and thread-c relate to thread-a via pair_tag (same tier rank).
        # With cap=1, the lexicographically first b_topic ("thread-b") must win
        # regardless of which order the dict presents them.
        def fake_resolve_bc_first(topic, *args, **kwargs):
            if topic == "thread-a":
                return {
                    "thread-b": [{"tier": "pair_tag", "tags": ["pair:x"]}],
                    "thread-c": [{"tier": "pair_tag", "tags": ["pair:x"]}],
                }
            return {}

        def fake_resolve_cb_first(topic, *args, **kwargs):
            if topic == "thread-a":
                return {
                    "thread-c": [{"tier": "pair_tag", "tags": ["pair:x"]}],
                    "thread-b": [{"tier": "pair_tag", "tags": ["pair:x"]}],
                }
            return {}

        common_kwargs = dict(
            all_active_tags=all_active_tags,
            threads_dir=None,
            entry_topic_index={},
            analysis_by_topic=None,
            risk_clusters=None,
            monitored_roles=["tester"],
            max_per_thread=1,
            pair_tag_prefix="pair:",
            min_role_entries_in_related=2,
        )

        with mock.patch(
            "watercooler.project_coordinator_lib._resolve_related_threads",
            side_effect=fake_resolve_bc_first,
        ):
            findings_bc = detect_role_complement(all_active_entries, **common_kwargs)

        with mock.patch(
            "watercooler.project_coordinator_lib._resolve_related_threads",
            side_effect=fake_resolve_cb_first,
        ):
            findings_cb = detect_role_complement(all_active_entries, **common_kwargs)

        assert len(findings_bc) == 1
        assert len(findings_cb) == 1
        survivor_bc = findings_bc[0].details["related_thread_topic"]
        survivor_cb = findings_cb[0].details["related_thread_topic"]
        assert survivor_bc == survivor_cb == "thread-b", (
            f"Sort must be deterministic: bc-order={survivor_bc}, cb-order={survivor_cb}"
        )


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
