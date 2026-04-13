"""Tests for baseline_graph/annotations.py — event store and materialization."""

import json
import pytest
from pathlib import Path

from watercooler.baseline_graph.annotations import (
    VALID_KINDS,
    VALID_TARGET_TYPES,
    AnnotationEvent,
    AnnotationState,
    append_annotation,
    get_annotation_state,
    load_annotation_events,
    load_or_rebuild_state,
    materialize_all_states,
    materialize_state,
    rebuild_state_cache,
    update_last_touched,
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def thread_dir(tmp_path):
    """Create a thread directory for annotation testing."""
    d = tmp_path / "graph" / "baseline" / "threads" / "test-topic"
    d.mkdir(parents=True)
    return d


def _make_event(
    target_id="entry-1",
    target_type="entry",
    kind="reaction",
    value="thumbsup",
    actor="alice",
    event_id="evt-001",
    timestamp="2025-01-15T10:00:00+00:00",
):
    return AnnotationEvent(
        id=event_id,
        target_id=target_id,
        target_type=target_type,
        kind=kind,
        value=value,
        actor=actor,
        timestamp=timestamp,
    )


# ============================================================================
# Event Append / Load Round-Trip
# ============================================================================


class TestEventRoundTrip:
    """Test that events can be appended and loaded back correctly."""

    def test_append_and_load_single(self, thread_dir):
        event = _make_event()
        append_annotation(thread_dir, event)

        events = load_annotation_events(thread_dir)
        assert len(events) == 1
        assert events[0].id == "evt-001"
        assert events[0].kind == "reaction"
        assert events[0].value == "thumbsup"
        assert events[0].actor == "alice"

    def test_append_multiple_events(self, thread_dir):
        for i in range(5):
            append_annotation(thread_dir, _make_event(event_id=f"evt-{i}"))

        events = load_annotation_events(thread_dir)
        assert len(events) == 5

    def test_load_from_nonexistent_dir(self, tmp_path):
        events = load_annotation_events(tmp_path / "nonexistent")
        assert events == []

    def test_append_creates_dir_if_missing(self, tmp_path):
        new_dir = tmp_path / "new-thread"
        event = _make_event()
        append_annotation(new_dir, event)
        assert (new_dir / "annotations.jsonl").exists()

    def test_append_rebuilds_cache(self, thread_dir):
        """Appending an event should rebuild the state cache with current state."""
        # Create a stale cache file
        cache = thread_dir / "annotation_state.json"
        cache.write_text("{}", encoding="utf-8")
        assert cache.exists()

        append_annotation(thread_dir, _make_event())
        # Cache should still exist but with updated content (not the stale "{}")
        assert cache.exists()
        import json
        data = json.loads(cache.read_text(encoding="utf-8"))
        # Should contain the target from the appended event (default target_id is "entry-1")
        assert "entry-1" in data

    def test_malformed_lines_skipped(self, thread_dir):
        """Malformed JSONL lines should be skipped, not crash."""
        ann_file = thread_dir / "annotations.jsonl"
        ann_file.write_text(
            '{"id":"e1","target_id":"a","target_type":"entry","kind":"tag","value":"v","actor":"x","timestamp":"t"}\n'
            "NOT JSON\n"
            '{"id":"e2","target_id":"b","target_type":"entry","kind":"tag","value":"w","actor":"y","timestamp":"t"}\n',
            encoding="utf-8",
        )

        events = load_annotation_events(thread_dir)
        assert len(events) == 2
        assert events[0].id == "e1"
        assert events[1].id == "e2"


# ============================================================================
# State Materialization
# ============================================================================


class TestMaterializeState:
    """Test folding events into materialized state."""

    def test_empty_events(self):
        state = materialize_state([], "entry-1")
        assert state.reactions == {}
        assert state.tags == []
        assert state.flags == []
        assert state.pinned is False
        assert state.vote_score == 0

    def test_reaction_thumbsup(self):
        events = [_make_event(kind="reaction", value="thumbsup", actor="alice")]
        state = materialize_state(events, "entry-1")
        assert state.reactions == {"thumbsup": ["alice"]}
        assert state.vote_score == 1

    def test_reaction_thumbsdown(self):
        events = [_make_event(kind="reaction", value="thumbsdown", actor="bob")]
        state = materialize_state(events, "entry-1")
        assert state.vote_score == -1

    def test_multiple_reactions_same_emoji(self):
        events = [
            _make_event(kind="reaction", value="heart", actor="alice", event_id="e1"),
            _make_event(kind="reaction", value="heart", actor="bob", event_id="e2"),
        ]
        state = materialize_state(events, "entry-1")
        assert state.reactions["heart"] == ["alice", "bob"]

    def test_duplicate_reaction_same_actor(self):
        events = [
            _make_event(kind="reaction", value="heart", actor="alice", event_id="e1"),
            _make_event(kind="reaction", value="heart", actor="alice", event_id="e2"),
        ]
        state = materialize_state(events, "entry-1")
        # Same actor shouldn't appear twice
        assert state.reactions["heart"] == ["alice"]

    def test_tag_add_remove(self):
        events = [
            _make_event(kind="tag", value="important", event_id="e1"),
            _make_event(kind="tag", value="blocked", event_id="e2"),
            _make_event(kind="tag_remove", value="important", event_id="e3"),
        ]
        state = materialize_state(events, "entry-1")
        assert state.tags == ["blocked"]

    def test_tag_remove_nonexistent(self):
        events = [_make_event(kind="tag_remove", value="nope")]
        state = materialize_state(events, "entry-1")
        assert state.tags == []

    def test_flag_and_clear(self):
        events = [
            _make_event(
                kind="flag", value="needs review", actor="alice", event_id="e1",
                timestamp="2025-01-15T10:00:00+00:00",
            ),
            _make_event(
                kind="flag_clear", value="needs review", actor="alice", event_id="e2",
                timestamp="2025-01-15T11:00:00+00:00",
            ),
        ]
        state = materialize_state(events, "entry-1")
        assert state.flags == []

    def test_flag_stays_if_different_reason(self):
        events = [
            _make_event(
                kind="flag", value="needs review", actor="alice", event_id="e1",
                timestamp="2025-01-15T10:00:00+00:00",
            ),
            _make_event(
                kind="flag_clear", value="outdated", actor="bob", event_id="e2",
                timestamp="2025-01-15T11:00:00+00:00",
            ),
        ]
        state = materialize_state(events, "entry-1")
        assert len(state.flags) == 1
        assert state.flags[0]["reason"] == "needs review"

    def test_xref_add_remove(self):
        events = [
            _make_event(kind="xref", value="entry-99", event_id="e1"),
            _make_event(kind="xref", value="entry-88", event_id="e2"),
            _make_event(kind="xref_remove", value="entry-99", event_id="e3"),
        ]
        state = materialize_state(events, "entry-1")
        assert state.xrefs == ["entry-88"]

    def test_pin_unpin(self):
        events = [
            _make_event(kind="pin", value="", event_id="e1"),
            _make_event(kind="unpin", value="", event_id="e2"),
        ]
        state = materialize_state(events, "entry-1")
        assert state.pinned is False

    def test_pin_stays(self):
        events = [_make_event(kind="pin", value="", event_id="e1")]
        state = materialize_state(events, "entry-1")
        assert state.pinned is True

    def test_last_touched_tracks_latest(self):
        events = [
            _make_event(kind="tag", value="a", event_id="e1", timestamp="2025-01-01T00:00:00+00:00"),
            _make_event(kind="tag", value="b", event_id="e2", timestamp="2025-06-01T00:00:00+00:00"),
        ]
        state = materialize_state(events, "entry-1")
        assert state.last_touched == "2025-06-01T00:00:00+00:00"

    def test_events_filtered_by_target(self):
        """Events for other targets should be ignored."""
        events = [
            _make_event(target_id="entry-1", kind="tag", value="mine", event_id="e1"),
            _make_event(target_id="entry-2", kind="tag", value="theirs", event_id="e2"),
        ]
        state = materialize_state(events, "entry-1")
        assert state.tags == ["mine"]

    def test_vote_score_net(self):
        events = [
            _make_event(kind="reaction", value="thumbsup", actor="a", event_id="e1"),
            _make_event(kind="reaction", value="thumbsup", actor="b", event_id="e2"),
            _make_event(kind="reaction", value="thumbsdown", actor="c", event_id="e3"),
        ]
        state = materialize_state(events, "entry-1")
        assert state.vote_score == 1  # 2 up - 1 down


class TestMaterializeAllStates:
    """Test materializing all targets at once."""

    def test_multiple_targets(self):
        events = [
            _make_event(target_id="e1", kind="tag", value="a", event_id="ev1"),
            _make_event(target_id="e2", kind="tag", value="b", event_id="ev2"),
            _make_event(target_id="e1", kind="tag", value="c", event_id="ev3"),
        ]
        states = materialize_all_states(events)
        assert len(states) == 2
        assert states["e1"].tags == ["a", "c"]
        assert states["e2"].tags == ["b"]


# ============================================================================
# Cache-Aware Loading
# ============================================================================


class TestCacheLoading:
    """Test load_or_rebuild_state with caching."""

    def test_rebuild_on_first_load(self, thread_dir):
        append_annotation(thread_dir, _make_event(kind="tag", value="test"))

        states = load_or_rebuild_state(thread_dir)
        assert "entry-1" in states
        assert states["entry-1"].tags == ["test"]

        # Cache should now exist
        assert (thread_dir / "annotation_state.json").exists()

    def test_cache_used_when_fresh(self, thread_dir):
        append_annotation(thread_dir, _make_event(kind="tag", value="test"))

        # First load creates cache
        load_or_rebuild_state(thread_dir)

        # Second load should use cache (we can verify by checking the cache is still there)
        states = load_or_rebuild_state(thread_dir)
        assert states["entry-1"].tags == ["test"]

    def test_cache_invalidated_on_append(self, thread_dir):
        append_annotation(thread_dir, _make_event(kind="tag", value="first", event_id="e1"))
        load_or_rebuild_state(thread_dir)

        # Append invalidates cache
        append_annotation(thread_dir, _make_event(kind="tag", value="second", event_id="e2"))

        states = load_or_rebuild_state(thread_dir)
        assert "first" in states["entry-1"].tags
        assert "second" in states["entry-1"].tags

    def test_rebuild_state_cache_idempotent(self, thread_dir):
        append_annotation(thread_dir, _make_event(kind="tag", value="x", event_id="e1"))

        rebuild_state_cache(thread_dir)
        states1 = load_or_rebuild_state(thread_dir)

        rebuild_state_cache(thread_dir)
        states2 = load_or_rebuild_state(thread_dir)

        assert states1["entry-1"].tags == states2["entry-1"].tags

    def test_empty_thread_returns_empty(self, thread_dir):
        states = load_or_rebuild_state(thread_dir)
        assert states == {}


# ============================================================================
# AnnotationState Serialization
# ============================================================================


class TestAnnotationStateSerialization:
    """Test AnnotationState to_dict/from_dict."""

    def test_round_trip(self):
        state = AnnotationState(
            reactions={"heart": ["alice"]},
            tags=["important"],
            flags=[{"agent": "bob", "reason": "review", "timestamp": "t"}],
            xrefs=["entry-2"],
            pinned=True,
            last_touched="2025-01-15T10:00:00+00:00",
            vote_score=3,
        )
        d = state.to_dict()
        restored = AnnotationState.from_dict(d)

        assert restored.reactions == state.reactions
        assert restored.tags == state.tags
        assert restored.flags == state.flags
        assert restored.xrefs == state.xrefs
        assert restored.pinned == state.pinned
        assert restored.last_touched == state.last_touched
        assert restored.vote_score == state.vote_score

    def test_from_dict_defaults(self):
        state = AnnotationState.from_dict({})
        assert state.reactions == {}
        assert state.tags == []
        assert state.pinned is False
        assert state.vote_score == 0


# ============================================================================
# get_annotation_state convenience
# ============================================================================


class TestGetAnnotationState:
    """Test get_annotation_state shortcut."""

    def test_returns_empty_for_missing(self, thread_dir):
        state = get_annotation_state(thread_dir, "nonexistent")
        assert state.tags == []
        assert state.vote_score == 0

    def test_returns_populated_state(self, thread_dir):
        append_annotation(thread_dir, _make_event(kind="tag", value="hot"))
        state = get_annotation_state(thread_dir, "entry-1")
        assert state.tags == ["hot"]


# ============================================================================
# update_last_touched
# ============================================================================


class TestUpdateLastTouched:
    """Test update_last_touched for say/ack/handoff integration."""

    def test_touch_new_target(self, thread_dir):
        update_last_touched(thread_dir, "entry-new", "2025-06-01T00:00:00+00:00")

        states = load_or_rebuild_state(thread_dir)
        assert states["entry-new"].last_touched == "2025-06-01T00:00:00+00:00"

    def test_touch_preserves_existing_annotations(self, thread_dir):
        append_annotation(thread_dir, _make_event(kind="tag", value="keep-me"))
        update_last_touched(thread_dir, "entry-1", "2025-06-01T00:00:00+00:00")

        states = load_or_rebuild_state(thread_dir)
        assert states["entry-1"].tags == ["keep-me"]
        assert states["entry-1"].last_touched == "2025-06-01T00:00:00+00:00"

    def test_touch_defaults_to_now(self, thread_dir):
        update_last_touched(thread_dir, "entry-now")

        states = load_or_rebuild_state(thread_dir)
        assert states["entry-now"].last_touched is not None


# ============================================================================
# read_only mode (worktree decontamination)
# ============================================================================


class TestReadOnlyMode:
    """Verify read_only=True prevents annotation_state.json writes."""

    def test_read_only_does_not_write_cache(self, thread_dir):
        """load_or_rebuild_state(read_only=True) must NOT write annotation_state.json."""
        # Create some annotation events so there's something to materialize
        append_annotation(thread_dir, _make_event(kind="tag", value="hot"))

        # Remove any existing cache
        cache = thread_dir / "annotation_state.json"
        if cache.exists():
            cache.unlink()

        # Read with read_only=True
        states = load_or_rebuild_state(thread_dir, read_only=True)

        # Should still return correct data
        assert "entry-1" in states
        assert states["entry-1"].tags == ["hot"]

        # But must NOT have written the cache file
        assert not cache.exists(), "read_only=True should not write annotation_state.json"

    def test_read_only_false_writes_cache(self, thread_dir):
        """load_or_rebuild_state(read_only=False) writes annotation_state.json as before."""
        append_annotation(thread_dir, _make_event(kind="tag", value="hot"))

        cache = thread_dir / "annotation_state.json"
        if cache.exists():
            cache.unlink()

        states = load_or_rebuild_state(thread_dir, read_only=False)
        assert "entry-1" in states
        assert cache.exists(), "read_only=False should write annotation_state.json"

    def test_get_annotation_state_read_only(self, thread_dir):
        """get_annotation_state with read_only=True doesn't write cache."""
        append_annotation(thread_dir, _make_event(kind="tag", value="hot"))

        cache = thread_dir / "annotation_state.json"
        if cache.exists():
            cache.unlink()

        state = get_annotation_state(thread_dir, "entry-1", read_only=True)
        assert state.tags == ["hot"]
        assert not cache.exists()
