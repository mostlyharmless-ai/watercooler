"""Tests for ``watercooler_follow_xref`` (#485).

Covers both local-mode (filesystem-backed baseline graph) and hosted-mode
(GitHub-backed entries via hosted_ops) execution paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from watercooler_mcp.config import ThreadContext
from watercooler_mcp.tools import annotations_xref as xref_mod
from watercooler_mcp.tools.annotations_xref import _follow_xref_impl
from watercooler_mcp.validation import HOSTED_MODE_SENTINEL

# ===========================================================================
# Helpers
# ===========================================================================


def _hosted_context() -> MagicMock:
    """Build a hosted-mode ThreadContext-shaped mock."""
    ctx = MagicMock()
    ctx.threads_dir = HOSTED_MODE_SENTINEL
    ctx.code_root = None
    ctx.code_repo = "org/demo-threads"
    ctx.code_branch = "main"
    return ctx


def _local_context(threads_dir: Path) -> ThreadContext:
    """Build a local-mode ThreadContext rooted at *threads_dir*."""
    return ThreadContext(
        code_root=threads_dir.parent,
        threads_dir=threads_dir,
        code_repo="org/demo",
        code_branch="main",
        code_commit="abc1234",
        code_remote="origin",
        explicit_dir=True,
    )


def _seed_local_thread(
    threads_dir: Path,
    *,
    topic: str,
    entries: list[dict],
    annotations: dict[str, list[tuple[str, str]]] | None = None,
) -> None:
    """Seed a local baseline graph with *entries* and optional xref annotations.

    ``annotations`` maps target_entry_id → list of (event_id, xref_value).
    """
    from watercooler.baseline_graph.annotations import (
        AnnotationEvent,
        append_annotation,
    )
    from watercooler.baseline_graph.storage import (
        get_graph_dir,
        get_thread_graph_dir,
    )
    from watercooler.baseline_graph.writer import (
        EntryData,
        init_thread_in_graph,
        upsert_entry_node,
    )

    init_thread_in_graph(
        threads_dir,
        topic,
        title=topic,
        status="OPEN",
        ball="User",
    )
    for e in entries:
        upsert_entry_node(
            threads_dir,
            EntryData(
                entry_id=e["entry_id"],
                thread_topic=topic,
                index=e.get("index", 0),
                agent=e.get("agent", "Claude"),
                role=e.get("role", "implementer"),
                entry_type=e.get("entry_type", "Note"),
                title=e.get("title", ""),
                body=e.get("body", ""),
                timestamp=e.get("timestamp", "2026-04-22T10:00:00Z"),
                summary=e.get("summary", ""),
            ),
        )

    if annotations:
        graph_dir = get_graph_dir(threads_dir)
        thread_dir = get_thread_graph_dir(graph_dir, topic)
        for target_id, events in annotations.items():
            for event_id, xref_value in events:
                append_annotation(
                    thread_dir,
                    AnnotationEvent(
                        id=event_id,
                        target_id=target_id,
                        target_type="entry",
                        kind="xref",
                        value=xref_value,
                        actor="alice",
                        timestamp="2026-04-22T11:00:00Z",
                    ),
                )


# ===========================================================================
# Hosted-mode fixtures
# ===========================================================================


HOSTED_DECISION = {
    "id": "DEC0000000000000000000001",
    "entry_id": "DEC0000000000000000000001",
    "entry_type": "Decision",
    "title": "Adopt option B",
    "body": "We will adopt option B.",
    "timestamp": "2026-04-22T10:05:00Z",
    "agent": "Claude (user)",
    "role": "implementer",
    "summary": "Adopt option B per the discussion.",
}

HOSTED_SOURCE = {
    "id": "SRC0000000000000000000001",
    "entry_id": "SRC0000000000000000000001",
    "entry_type": "Note",
    "title": "Discussion of options",
    "body": "Long discussion comparing A and B.",
    "timestamp": "2026-04-22T09:55:00Z",
    "agent": "Claude (user)",
    "role": "implementer",
    "summary": "Compared options A and B.",
}

HOSTED_OTHER = {
    "id": "OTH0000000000000000000001",
    "entry_id": "OTH0000000000000000000001",
    "entry_type": "Note",
    "title": "Cross-thread context",
    "body": "Context from a different thread.",
    "timestamp": "2026-04-22T08:00:00Z",
    "agent": "Claude (user)",
    "role": "scribe",
    "summary": "Context from a sibling thread.",
}


HOSTED_ENTRIES_BY_TOPIC: dict[str, list[dict]] = {
    "feat-option-b": [HOSTED_SOURCE, HOSTED_DECISION],
    "feat-sibling": [HOSTED_OTHER],
}

HOSTED_ANNOTATIONS_BY_TOPIC: dict[str, dict[str, dict]] = {
    "feat-option-b": {
        HOSTED_DECISION["entry_id"]: {
            "tags": [],
            "xrefs": [HOSTED_SOURCE["entry_id"], HOSTED_OTHER["entry_id"]],
        },
        HOSTED_SOURCE["entry_id"]: {
            "tags": [],
            "xrefs": [],
        },
    },
}


def _fake_load_all_entries_hosted(topics=None, max_workers=10):
    if topics is not None:
        return (
            None,
            {
                t: HOSTED_ENTRIES_BY_TOPIC[t]
                for t in topics
                if t in HOSTED_ENTRIES_BY_TOPIC
            },
        )
    return (None, dict(HOSTED_ENTRIES_BY_TOPIC))


def _fake_get_annotations_hosted(topic: str, target_id: str = ""):
    states = HOSTED_ANNOTATIONS_BY_TOPIC.get(topic, {})
    if target_id:
        return (
            None,
            {"target_id": target_id, "annotation_state": states.get(target_id, {})},
        )
    return (None, {"topic": topic, "annotation_states": states})


def _fake_list_topic_dirs_hosted():
    return (None, sorted(HOSTED_ENTRIES_BY_TOPIC.keys()))


# ===========================================================================
# Local-mode tests
# ===========================================================================


class TestFollowXrefLocal:
    """Local (filesystem) execution path."""

    def test_returns_summary_for_each_xref_within_same_thread(self, tmp_path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        decision_id = "DECLOCAL00000000000000001"
        source_id = "SRCLOCAL00000000000000001"

        _seed_local_thread(
            threads_dir,
            topic="feat-x",
            entries=[
                {
                    "entry_id": source_id,
                    "index": 0,
                    "entry_type": "Note",
                    "title": "Source discussion",
                    "summary": "Source discussion summary.",
                    "timestamp": "2026-04-22T09:00:00Z",
                },
                {
                    "entry_id": decision_id,
                    "index": 1,
                    "entry_type": "Decision",
                    "title": "Adopt option B",
                    "summary": "Adopt option B.",
                    "timestamp": "2026-04-22T10:00:00Z",
                },
            ],
            annotations={decision_id: [("evt-1", source_id)]},
        )

        ctx = MagicMock()
        with patch.object(
            xref_mod.validation,
            "_require_context",
            return_value=(None, _local_context(threads_dir)),
        ):
            result = _follow_xref_impl(
                ctx=ctx,
                topic="feat-x",
                target_id=decision_id,
                code_path=str(threads_dir.parent),
            )

        payload = json.loads(result.content[0].text)
        assert payload["schema_version"] == 1
        assert payload["topic"] == "feat-x"
        assert payload["target_id"] == decision_id
        assert payload["count"] == 1
        assert len(payload["xrefs"]) == 1

        record = payload["xrefs"][0]
        assert record["entry_id"] == source_id
        assert record["topic"] == "feat-x"
        assert record["title"] == "Source discussion"
        assert record["type"] == "Note"
        assert record["role"] == "implementer"
        assert record["agent"] == "Claude"
        assert record["summary"] == "Source discussion summary."
        assert "missing" not in record

    def test_no_xrefs_returns_empty_list(self, tmp_path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        decision_id = "DECLOCAL00000000000000002"

        _seed_local_thread(
            threads_dir,
            topic="feat-no-xref",
            entries=[
                {
                    "entry_id": decision_id,
                    "index": 0,
                    "entry_type": "Decision",
                    "title": "Decision with no xrefs",
                    "timestamp": "2026-04-22T10:00:00Z",
                }
            ],
            annotations=None,
        )

        ctx = MagicMock()
        with patch.object(
            xref_mod.validation,
            "_require_context",
            return_value=(None, _local_context(threads_dir)),
        ):
            result = _follow_xref_impl(
                ctx=ctx,
                topic="feat-no-xref",
                target_id=decision_id,
                code_path=str(threads_dir.parent),
            )

        payload = json.loads(result.content[0].text)
        assert payload["count"] == 0
        assert payload["xrefs"] == []

    def test_missing_xref_target_is_reported_as_placeholder(self, tmp_path):
        """An xref pointing to a non-existent entry_id must NOT 500.

        It should appear in the response with ``missing=True`` and a note,
        preserving 1:1 ordering with annotation_state.xrefs.
        """
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        decision_id = "DECLOCAL00000000000000003"
        real_source_id = "SRCLOCAL00000000000000003"
        ghost_id = "GHOSTLOCAL000000000000000"

        _seed_local_thread(
            threads_dir,
            topic="feat-ghost",
            entries=[
                {
                    "entry_id": real_source_id,
                    "index": 0,
                    "entry_type": "Note",
                    "title": "Real source",
                    "timestamp": "2026-04-22T09:00:00Z",
                },
                {
                    "entry_id": decision_id,
                    "index": 1,
                    "entry_type": "Decision",
                    "title": "Decision with ghost xref",
                    "timestamp": "2026-04-22T10:00:00Z",
                },
            ],
            annotations={
                decision_id: [
                    ("evt-1", real_source_id),
                    ("evt-2", ghost_id),
                ]
            },
        )

        ctx = MagicMock()
        with patch.object(
            xref_mod.validation,
            "_require_context",
            return_value=(None, _local_context(threads_dir)),
        ):
            result = _follow_xref_impl(
                ctx=ctx,
                topic="feat-ghost",
                target_id=decision_id,
                code_path=str(threads_dir.parent),
            )

        payload = json.loads(result.content[0].text)
        assert payload["count"] == 2

        ids = [r["entry_id"] for r in payload["xrefs"]]
        assert ids == [real_source_id, ghost_id]

        real, ghost = payload["xrefs"]
        assert real.get("missing") is not True
        assert real["topic"] == "feat-ghost"

        assert ghost.get("missing") is True
        assert ghost["topic"] is None
        assert ghost["title"] is None
        assert "not found" in ghost["note"].lower()

    def test_topic_with_entry_prefix_in_target_is_normalized(self, tmp_path):
        """``target_id`` may arrive with an ``entry:`` prefix (graph node id form)."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        decision_id = "DECLOCAL00000000000000004"
        source_id = "SRCLOCAL00000000000000004"

        _seed_local_thread(
            threads_dir,
            topic="feat-prefix",
            entries=[
                {
                    "entry_id": source_id,
                    "index": 0,
                    "entry_type": "Note",
                    "title": "Source",
                    "timestamp": "2026-04-22T09:00:00Z",
                },
                {
                    "entry_id": decision_id,
                    "index": 1,
                    "entry_type": "Decision",
                    "title": "Decision",
                    "timestamp": "2026-04-22T10:00:00Z",
                },
            ],
            annotations={decision_id: [("evt-1", source_id)]},
        )

        ctx = MagicMock()
        with patch.object(
            xref_mod.validation,
            "_require_context",
            return_value=(None, _local_context(threads_dir)),
        ):
            result = _follow_xref_impl(
                ctx=ctx,
                topic="feat-prefix",
                target_id=f"entry:{decision_id}",
                code_path=str(threads_dir.parent),
            )

        payload = json.loads(result.content[0].text)
        assert payload["target_id"] == decision_id  # bare form
        assert payload["count"] == 1
        assert payload["xrefs"][0]["entry_id"] == source_id


# ===========================================================================
# Hosted-mode tests
# ===========================================================================


class TestFollowXrefHosted:
    """Hosted (GitHub-backed) execution path."""

    def test_returns_summary_via_hosted_ops(self):
        ctx = MagicMock()

        with (
            patch.object(
                xref_mod.validation,
                "_require_context",
                return_value=(None, _hosted_context()),
            ),
            patch(
                "watercooler_mcp.hosted_ops.list_topic_dirs_hosted",
                side_effect=_fake_list_topic_dirs_hosted,
            ),
            patch(
                "watercooler_mcp.hosted_ops.load_all_entries_hosted",
                side_effect=_fake_load_all_entries_hosted,
            ),
            patch(
                "watercooler_mcp.hosted_ops.get_annotations_hosted",
                side_effect=_fake_get_annotations_hosted,
            ),
        ):
            result = _follow_xref_impl(
                ctx=ctx,
                topic="feat-option-b",
                target_id=HOSTED_DECISION["entry_id"],
                code_path="",
            )

        payload = json.loads(result.content[0].text)
        assert payload["schema_version"] == 1
        assert payload["topic"] == "feat-option-b"
        assert payload["target_id"] == HOSTED_DECISION["entry_id"]
        assert payload["count"] == 2

        # Same-thread xref resolves to source thread, cross-thread xref
        # resolves to feat-sibling.
        ids = [r["entry_id"] for r in payload["xrefs"]]
        assert ids == [HOSTED_SOURCE["entry_id"], HOSTED_OTHER["entry_id"]]

        same, cross = payload["xrefs"]
        assert same["topic"] == "feat-option-b"
        assert same["title"] == HOSTED_SOURCE["title"]
        assert same["summary"] == HOSTED_SOURCE["summary"]

        assert cross["topic"] == "feat-sibling"
        assert cross["title"] == HOSTED_OTHER["title"]

    def test_no_xrefs_returns_empty_list_hosted(self):
        """Source entry with empty xrefs returns count=0 (no GitHub fan-out for entries)."""
        ctx = MagicMock()

        with (
            patch.object(
                xref_mod.validation,
                "_require_context",
                return_value=(None, _hosted_context()),
            ),
            patch(
                "watercooler_mcp.hosted_ops.list_topic_dirs_hosted",
                side_effect=_fake_list_topic_dirs_hosted,
            ),
            patch(
                "watercooler_mcp.hosted_ops.load_all_entries_hosted",
                side_effect=_fake_load_all_entries_hosted,
            ) as load_mock,
            patch(
                "watercooler_mcp.hosted_ops.get_annotations_hosted",
                side_effect=_fake_get_annotations_hosted,
            ),
        ):
            result = _follow_xref_impl(
                ctx=ctx,
                topic="feat-option-b",
                target_id=HOSTED_SOURCE["entry_id"],
                code_path="",
            )

        payload = json.loads(result.content[0].text)
        assert payload["count"] == 0
        assert payload["xrefs"] == []
        # Optimisation: when xrefs is empty we never load entries.
        assert load_mock.call_count == 0

    def test_missing_xref_target_is_reported_hosted(self):
        """Hosted-mode xref pointing to an unknown id returns missing=True."""
        ctx = MagicMock()

        ghost_id = "GHOST00000000000000000001"

        # Patch a one-off annotation state that includes a ghost xref.
        def _ghost_get_annotations(topic: str, target_id: str = ""):
            if topic == "feat-option-b" and target_id == HOSTED_DECISION["entry_id"]:
                return (
                    None,
                    {
                        "target_id": target_id,
                        "annotation_state": {
                            "tags": [],
                            "xrefs": [HOSTED_SOURCE["entry_id"], ghost_id],
                        },
                    },
                )
            return _fake_get_annotations_hosted(topic, target_id)

        with (
            patch.object(
                xref_mod.validation,
                "_require_context",
                return_value=(None, _hosted_context()),
            ),
            patch(
                "watercooler_mcp.hosted_ops.list_topic_dirs_hosted",
                side_effect=_fake_list_topic_dirs_hosted,
            ),
            patch(
                "watercooler_mcp.hosted_ops.load_all_entries_hosted",
                side_effect=_fake_load_all_entries_hosted,
            ),
            patch(
                "watercooler_mcp.hosted_ops.get_annotations_hosted",
                side_effect=_ghost_get_annotations,
            ),
        ):
            result = _follow_xref_impl(
                ctx=ctx,
                topic="feat-option-b",
                target_id=HOSTED_DECISION["entry_id"],
                code_path="",
            )

        payload = json.loads(result.content[0].text)
        assert payload["count"] == 2
        ids = [r["entry_id"] for r in payload["xrefs"]]
        assert ids == [HOSTED_SOURCE["entry_id"], ghost_id]

        real, ghost = payload["xrefs"]
        assert real.get("missing") is not True
        assert ghost["missing"] is True
        assert "not found" in ghost["note"].lower()


# ===========================================================================
# Validation
# ===========================================================================


class TestFollowXrefValidation:
    def test_missing_topic_raises(self):
        from watercooler_mcp.errors import ValidationError

        ctx = MagicMock()
        with pytest.raises(ValidationError):
            _follow_xref_impl(ctx=ctx, topic="", target_id="abc", code_path=".")

    def test_missing_target_id_raises(self):
        from watercooler_mcp.errors import ValidationError

        ctx = MagicMock()
        with pytest.raises(ValidationError):
            _follow_xref_impl(ctx=ctx, topic="feat", target_id="", code_path=".")
