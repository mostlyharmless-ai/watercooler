"""Tests for decision extraction annotation events.

Verifies that ``_build_decision_annotation_events`` produces the expected
AnnotationEvents. They are applied here directly (the events would be committed
inside ``run_with_sync`` via ``daemon_write_entry(annotation_events=...)`` in
production) and the folded annotation state is asserted.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from watercooler.baseline_graph.annotations import (
    AnnotationEvent,
    append_annotation,
    get_annotation_state,
    load_annotation_events,
)
from watercooler.baseline_graph.storage import get_graph_dir, get_thread_graph_dir
from watercooler_mcp.daemons.decision_extractor import (
    _build_decision_annotation_events,
)


def _setup_thread_dir(tmp_path: Path, topic: str = "test-topic") -> Path:
    """Create the per-topic graph directory structure."""
    graph_dir = get_graph_dir(tmp_path)
    thread_dir = get_thread_graph_dir(graph_dir, topic)
    thread_dir.mkdir(parents=True, exist_ok=True)
    return thread_dir


def _apply(thread_dir: Path, events: Sequence[AnnotationEvent]) -> None:
    """Apply built events the way the synced write transaction would."""
    for event in events:
        append_annotation(thread_dir, event)


class TestBuildDecisionAnnotationEvents:
    def test_writes_four_events(self, tmp_path):
        topic = "test-topic"
        thread_dir = _setup_thread_dir(tmp_path, topic)

        _apply(thread_dir, _build_decision_annotation_events(topic, "SRC_ENTRY", "DEC_ENTRY"))

        events = load_annotation_events(thread_dir)
        assert len(events) == 4

    def test_source_entry_tagged_decision_extracted(self, tmp_path):
        topic = "test-topic"
        thread_dir = _setup_thread_dir(tmp_path, topic)

        _apply(thread_dir, _build_decision_annotation_events(topic, "SRC_ENTRY", "DEC_ENTRY"))

        state = get_annotation_state(thread_dir, "SRC_ENTRY")
        assert "decision_extracted" in state.tags

    def test_source_to_decision_xref(self, tmp_path):
        topic = "test-topic"
        thread_dir = _setup_thread_dir(tmp_path, topic)

        _apply(thread_dir, _build_decision_annotation_events(topic, "SRC_ENTRY", "DEC_ENTRY"))

        state = get_annotation_state(thread_dir, "SRC_ENTRY")
        assert "DEC_ENTRY" in state.xrefs

    def test_decision_to_source_xref(self, tmp_path):
        topic = "test-topic"
        thread_dir = _setup_thread_dir(tmp_path, topic)

        _apply(thread_dir, _build_decision_annotation_events(topic, "SRC_ENTRY", "DEC_ENTRY"))

        state = get_annotation_state(thread_dir, "DEC_ENTRY")
        assert "SRC_ENTRY" in state.xrefs

    def test_thread_tagged_has_decisions(self, tmp_path):
        topic = "test-topic"
        thread_dir = _setup_thread_dir(tmp_path, topic)

        _apply(thread_dir, _build_decision_annotation_events(topic, "SRC_ENTRY", "DEC_ENTRY"))

        state = get_annotation_state(thread_dir, topic)
        assert "has_decisions" in state.tags

    def test_has_decisions_tag_idempotent(self, tmp_path):
        """Applying two decisions' events should not duplicate the thread tag."""
        topic = "test-topic"
        thread_dir = _setup_thread_dir(tmp_path, topic)

        _apply(thread_dir, _build_decision_annotation_events(topic, "SRC1", "DEC1"))
        _apply(thread_dir, _build_decision_annotation_events(topic, "SRC2", "DEC2"))

        state = get_annotation_state(thread_dir, topic)
        # Tag list is deduplicated at materialization level
        assert state.tags.count("has_decisions") == 1

    def test_actor_is_daemon_name(self, tmp_path):
        topic = "test-topic"
        thread_dir = _setup_thread_dir(tmp_path, topic)

        _apply(thread_dir, _build_decision_annotation_events(topic, "SRC_ENTRY", "DEC_ENTRY"))

        events = load_annotation_events(thread_dir)
        for event in events:
            assert event.actor == "ExtractDecisionsDaemon"

    def test_all_events_have_unique_ids(self, tmp_path):
        topic = "test-topic"
        events = _build_decision_annotation_events(topic, "SRC_ENTRY", "DEC_ENTRY")
        ids = [e.id for e in events]
        assert len(ids) == len(set(ids))
