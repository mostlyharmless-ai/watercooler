"""Tests for decision extraction annotation hooks.

Verifies that _build_decision_annotation_hook writes the expected
AnnotationEvents when invoked as a post_write_hooks callback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from watercooler.baseline_graph.annotations import (
    AnnotationEvent,
    get_annotation_state,
    load_annotation_events,
)
from watercooler.baseline_graph.storage import get_graph_dir, get_thread_graph_dir
from watercooler_mcp.daemons.decision_extractor import (
    _build_decision_annotation_hook,
)


def _setup_thread_dir(tmp_path: Path, topic: str = "test-topic") -> Path:
    """Create the per-topic graph directory structure."""
    graph_dir = get_graph_dir(tmp_path)
    thread_dir = get_thread_graph_dir(graph_dir, topic)
    thread_dir.mkdir(parents=True, exist_ok=True)
    return thread_dir


class TestBuildDecisionAnnotationHook:
    def test_writes_four_events(self, tmp_path):
        topic = "test-topic"
        thread_dir = _setup_thread_dir(tmp_path, topic)

        hook = _build_decision_annotation_hook("SRC_ENTRY", "DEC_ENTRY")
        hook(topic, tmp_path, "DEC_ENTRY")

        events = load_annotation_events(thread_dir)
        assert len(events) == 4

    def test_source_entry_tagged_decision_extracted(self, tmp_path):
        topic = "test-topic"
        thread_dir = _setup_thread_dir(tmp_path, topic)

        hook = _build_decision_annotation_hook("SRC_ENTRY", "DEC_ENTRY")
        hook(topic, tmp_path, "DEC_ENTRY")

        state = get_annotation_state(thread_dir, "SRC_ENTRY")
        assert "decision_extracted" in state.tags

    def test_source_to_decision_xref(self, tmp_path):
        topic = "test-topic"
        thread_dir = _setup_thread_dir(tmp_path, topic)

        hook = _build_decision_annotation_hook("SRC_ENTRY", "DEC_ENTRY")
        hook(topic, tmp_path, "DEC_ENTRY")

        state = get_annotation_state(thread_dir, "SRC_ENTRY")
        assert "DEC_ENTRY" in state.xrefs

    def test_decision_to_source_xref(self, tmp_path):
        topic = "test-topic"
        thread_dir = _setup_thread_dir(tmp_path, topic)

        hook = _build_decision_annotation_hook("SRC_ENTRY", "DEC_ENTRY")
        hook(topic, tmp_path, "DEC_ENTRY")

        state = get_annotation_state(thread_dir, "DEC_ENTRY")
        assert "SRC_ENTRY" in state.xrefs

    def test_thread_tagged_has_decisions(self, tmp_path):
        topic = "test-topic"
        thread_dir = _setup_thread_dir(tmp_path, topic)

        hook = _build_decision_annotation_hook("SRC_ENTRY", "DEC_ENTRY")
        hook(topic, tmp_path, "DEC_ENTRY")

        state = get_annotation_state(thread_dir, topic)
        assert "has_decisions" in state.tags

    def test_has_decisions_tag_idempotent(self, tmp_path):
        """Calling the hook twice should not duplicate the thread tag."""
        topic = "test-topic"
        thread_dir = _setup_thread_dir(tmp_path, topic)

        hook1 = _build_decision_annotation_hook("SRC1", "DEC1")
        hook1(topic, tmp_path, "DEC1")

        hook2 = _build_decision_annotation_hook("SRC2", "DEC2")
        hook2(topic, tmp_path, "DEC2")

        state = get_annotation_state(thread_dir, topic)
        # Tag list is deduplicated at materialization level
        assert state.tags.count("has_decisions") == 1

    def test_actor_is_daemon_name(self, tmp_path):
        topic = "test-topic"
        thread_dir = _setup_thread_dir(tmp_path, topic)

        hook = _build_decision_annotation_hook("SRC_ENTRY", "DEC_ENTRY")
        hook(topic, tmp_path, "DEC_ENTRY")

        events = load_annotation_events(thread_dir)
        for event in events:
            assert event.actor == "ExtractDecisionsDaemon"

    def test_all_events_have_unique_ids(self, tmp_path):
        topic = "test-topic"
        thread_dir = _setup_thread_dir(tmp_path, topic)

        hook = _build_decision_annotation_hook("SRC_ENTRY", "DEC_ENTRY")
        hook(topic, tmp_path, "DEC_ENTRY")

        events = load_annotation_events(thread_dir)
        ids = [e.id for e in events]
        assert len(ids) == len(set(ids))
