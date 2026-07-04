"""Hosted-mode tests for ``watercooler_list_decisions`` (#408).

Verifies that ``_list_decisions_impl`` takes the hosted branch when
``context`` is the ``HOSTED_MODE_SENTINEL`` and fetches data through
``load_all_entries_hosted`` + ``get_annotations_hosted`` rather than the
local baseline-graph storage.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from watercooler_mcp.tools import decisions as decisions_mod
from watercooler_mcp.tools.decisions import _list_decisions_impl
from watercooler_mcp.validation import HOSTED_MODE_SENTINEL

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------


SOURCE_ENTRY = {
    "id": "SRC0000000000000000000000",
    "entry_type": "Note",
    "title": "We decided to go with option B",
    "body": "Discussion body — the source of the decision.",
    "timestamp": "2026-04-20T10:00:00Z",
    "agent": "Claude",
    "role": "implementer",
}

DECISION_ENTRY = {
    "id": "DEC0000000000000000000000",
    "entry_type": "Decision",
    "title": "Adopt option B",
    "body": (
        "Confidence: 4/5\n\n"
        "We will adopt option B because it avoids the migration risk "
        "raised in the discussion."
    ),
    "timestamp": "2026-04-20T10:05:00Z",
    "agent": "Claude",
    "role": "implementer",
}

ENTRIES_BY_TOPIC: dict[str, list[dict]] = {
    "feat-option-b": [SOURCE_ENTRY, DECISION_ENTRY],
}

ANNOTATIONS_BY_TOPIC: dict[str, dict[str, dict]] = {
    "feat-option-b": {
        DECISION_ENTRY["id"]: {
            "tags": ["reviewed"],
            "xrefs": [SOURCE_ENTRY["id"]],
        },
        SOURCE_ENTRY["id"]: {
            "tags": ["decision_extracted"],
            "xrefs": [DECISION_ENTRY["id"]],
        },
    },
}


def _hosted_context() -> MagicMock:
    ctx = MagicMock()
    ctx.threads_dir = HOSTED_MODE_SENTINEL
    ctx.code_root = None
    ctx.code_repo = "org/demo-threads"
    ctx.code_branch = "main"
    return ctx


def _fake_load_all_entries_hosted(topics=None, max_workers=10):
    if topics is not None:
        return (None, {t: ENTRIES_BY_TOPIC[t] for t in topics if t in ENTRIES_BY_TOPIC})
    return (None, dict(ENTRIES_BY_TOPIC))


def _fake_get_annotations_hosted(topic: str, target_id: str = ""):
    states = ANNOTATIONS_BY_TOPIC.get(topic, {})
    if target_id:
        return (
            None,
            {"target_id": target_id, "annotation_state": states.get(target_id, {})},
        )
    return (None, {"topic": topic, "annotation_states": states})


def _fake_list_topic_dirs_hosted():
    return (None, sorted(ENTRIES_BY_TOPIC.keys()))


def _dirs_stub(topics):
    """Build a ``list_topic_dirs_hosted`` stub that returns ``topics``."""

    def _stub():
        return (None, sorted(topics))

    return _stub


def _explode(*args, **kwargs):
    raise AssertionError(
        "hosted list_decisions path must not call local filesystem storage"
    )


@pytest.fixture(autouse=True)
def _decision_index_absent():
    """Default every hosted test to the full-scan FALLBACK (no decisions index).

    Since PR2, ``_list_decisions_hosted`` tries the repo-level decisions index
    first. The existing tests in this module assert the legacy full-scan
    behaviour, which is now the fallback — so default the index to absent.
    The index fast-path is exercised explicitly in ``TestListDecisionsFromIndex``
    (which re-patches this target to return records).
    """
    with patch(
        "watercooler_mcp.hosted_ops.load_decision_index_hosted",
        return_value=(None, None),
    ):
        yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListDecisionsHosted:
    def test_returns_decisions_via_hosted_ops(self):
        ctx = MagicMock()

        with (
            patch.object(
                decisions_mod.validation,
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
            result = _list_decisions_impl(ctx=ctx, code_path="")

        payload = json.loads(result.content[0].text)
        assert payload["schema_version"] == 1
        assert payload["total"] == 1
        assert payload["returned"] == 1

        decision = payload["decisions"][0]
        assert decision["entry_id"] == DECISION_ENTRY["id"]
        assert decision["topic"] == "feat-option-b"
        assert decision["title"] == DECISION_ENTRY["title"]
        assert decision["confidence"] == 4
        assert decision["extracted"] is True
        assert decision["tags"] == ["reviewed"]
        assert decision["xrefs"] == [SOURCE_ENTRY["id"]]

        source = decision["source"]
        assert source is not None
        assert source["entry_id"] == SOURCE_ENTRY["id"]
        assert source["topic"] == "feat-option-b"
        assert source["title"] == SOURCE_ENTRY["title"]

    def test_no_filesystem_storage_reads(self):
        """Hosted path must not touch ``storage.list_thread_topics``/``load_thread_entries``."""
        ctx = MagicMock()

        with (
            patch.object(
                decisions_mod.validation,
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
            patch.object(
                decisions_mod.storage, "list_thread_topics", side_effect=_explode
            ),
            patch.object(
                decisions_mod.storage, "load_thread_entries", side_effect=_explode
            ),
            patch.object(
                decisions_mod.storage, "get_thread_graph_dir", side_effect=_explode
            ),
        ):
            result = _list_decisions_impl(ctx=ctx, code_path="")

        payload = json.loads(result.content[0].text)
        assert payload["total"] == 1

    def test_only_extracted_filter_keeps_extraction(self):
        ctx = MagicMock()

        with (
            patch.object(
                decisions_mod.validation,
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
            result = _list_decisions_impl(ctx=ctx, only_extracted=True, code_path="")

        payload = json.loads(result.content[0].text)
        assert payload["total"] == 1
        assert payload["decisions"][0]["extracted"] is True

    def test_confidence_min_filters_out_low_confidence(self):
        ctx = MagicMock()

        with (
            patch.object(
                decisions_mod.validation,
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
            result = _list_decisions_impl(ctx=ctx, confidence_min=5, code_path="")

        payload = json.loads(result.content[0].text)
        assert payload["total"] == 0


# ---------------------------------------------------------------------------
# Cross-thread xref fixture data — Decision in topic A xrefs source in topic B
# ---------------------------------------------------------------------------


CROSS_SOURCE_ENTRY = {
    "id": "SRC1111111111111111111111",
    "entry_type": "Note",
    "title": "Discussion that led to the decision",
    "body": "Live discussion captured in topic B.",
    "timestamp": "2026-04-18T09:00:00Z",
    "agent": "Claude",
    "role": "implementer",
}

CROSS_DECISION_ENTRY = {
    "id": "DEC1111111111111111111111",
    "entry_type": "Decision",
    "title": "Adopt approach from discussion B",
    "body": "Confidence: 3/5\n\nDerived from the cross-thread discussion.",
    "timestamp": "2026-04-19T09:00:00Z",
    "agent": "Claude",
    "role": "implementer",
}

CROSS_ENTRIES_BY_TOPIC: dict[str, list[dict]] = {
    "topic-a": [CROSS_DECISION_ENTRY],
    "topic-b": [CROSS_SOURCE_ENTRY],
}

CROSS_ANNOTATIONS_BY_TOPIC: dict[str, dict[str, dict]] = {
    "topic-a": {
        CROSS_DECISION_ENTRY["id"]: {
            "tags": [],
            "xrefs": [CROSS_SOURCE_ENTRY["id"]],
        },
    },
    "topic-b": {
        CROSS_SOURCE_ENTRY["id"]: {
            "tags": ["decision_extracted"],
            "xrefs": [CROSS_DECISION_ENTRY["id"]],
        },
    },
}


def _full_loader(entries_by_topic):
    """Return a loader that always does a single snapshot-consistent load.

    The new hosted flow discovers topic directory names via
    ``list_topic_dirs_hosted`` and passes that list to
    ``load_all_entries_hosted``. We accept any list covering the fixture
    topics — the invariant we care about is "one call" — and reject a second
    recovery-style invocation.
    """

    call_count = {"n": 0}

    def _loader(topics=None, max_workers=10):
        call_count["n"] += 1
        if call_count["n"] > 1:
            raise AssertionError(
                f"hosted path must issue a single load; got second call with topics={topics}"
            )
        if topics is None:
            return (None, dict(entries_by_topic))
        return (None, {t: entries_by_topic[t] for t in topics if t in entries_by_topic})

    return _loader


def _ann_factory(annotations_by_topic):
    def _ann(topic: str, target_id: str = ""):
        states = annotations_by_topic.get(topic, {})
        if target_id:
            return (
                None,
                {
                    "target_id": target_id,
                    "annotation_state": states.get(target_id, {}),
                },
            )
        return (None, {"topic": topic, "annotation_states": states})

    return _ann


class TestListDecisionsHostedCrossThread:
    """Coverage for cross-thread xref resolution in topic-filtered hosted queries.

    The hosted path uses a single full ``load_all_entries_hosted(topics=None)``
    call to avoid snapshot-consistency issues that arise when mixing two
    separate HTTP snapshots. Iteration is pinned to the filter topic so
    annotation fetches stay scoped; source resolution uses the full corpus
    map so cross-thread xrefs still resolve.
    """

    def test_single_full_load_always(self):
        """Hosted path must always issue exactly one full load.

        A restricted first load + conditional rehydrate cannot produce a
        snapshot-consistent view. The implementation must call
        ``load_all_entries_hosted(topics=None)`` unconditionally, whether or
        not the query has cross-thread xrefs.
        """
        ctx = MagicMock()

        with (
            patch.object(
                decisions_mod.validation,
                "_require_context",
                return_value=(None, _hosted_context()),
            ),
            patch(
                "watercooler_mcp.hosted_ops.list_topic_dirs_hosted",
                side_effect=_dirs_stub(CROSS_ENTRIES_BY_TOPIC.keys()),
            ),
            patch(
                "watercooler_mcp.hosted_ops.load_all_entries_hosted",
                side_effect=_full_loader(CROSS_ENTRIES_BY_TOPIC),
            ),
            patch(
                "watercooler_mcp.hosted_ops.get_annotations_hosted",
                side_effect=_ann_factory(CROSS_ANNOTATIONS_BY_TOPIC),
            ),
        ):
            result = _list_decisions_impl(ctx=ctx, topic="topic-a", code_path="")

        payload = json.loads(result.content[0].text)
        assert payload["total"] == 1

    def test_topic_filter_resolves_cross_thread_source(self):
        """A topic-filtered query must still resolve sources in other threads."""
        ctx = MagicMock()

        with (
            patch.object(
                decisions_mod.validation,
                "_require_context",
                return_value=(None, _hosted_context()),
            ),
            patch(
                "watercooler_mcp.hosted_ops.list_topic_dirs_hosted",
                side_effect=_dirs_stub(CROSS_ENTRIES_BY_TOPIC.keys()),
            ),
            patch(
                "watercooler_mcp.hosted_ops.load_all_entries_hosted",
                side_effect=_full_loader(CROSS_ENTRIES_BY_TOPIC),
            ),
            patch(
                "watercooler_mcp.hosted_ops.get_annotations_hosted",
                side_effect=_ann_factory(CROSS_ANNOTATIONS_BY_TOPIC),
            ),
        ):
            result = _list_decisions_impl(ctx=ctx, topic="topic-a", code_path="")

        payload = json.loads(result.content[0].text)
        assert payload["total"] == 1
        decision = payload["decisions"][0]
        assert decision["entry_id"] == CROSS_DECISION_ENTRY["id"]
        assert decision["topic"] == "topic-a"

        source = decision["source"]
        assert (
            source is not None
        ), "hosted topic-filtered query must resolve cross-thread xrefs"
        assert source["entry_id"] == CROSS_SOURCE_ENTRY["id"]
        assert source["topic"] == "topic-b"
        assert decision["extracted"] is True
        assert decision["confidence"] == 3

    def test_source_entry_id_filter_works_for_cross_thread_source(self):
        """source_entry_id filter must match sources that live in other threads."""
        ctx = MagicMock()

        with (
            patch.object(
                decisions_mod.validation,
                "_require_context",
                return_value=(None, _hosted_context()),
            ),
            patch(
                "watercooler_mcp.hosted_ops.list_topic_dirs_hosted",
                side_effect=_dirs_stub(CROSS_ENTRIES_BY_TOPIC.keys()),
            ),
            patch(
                "watercooler_mcp.hosted_ops.load_all_entries_hosted",
                side_effect=_full_loader(CROSS_ENTRIES_BY_TOPIC),
            ),
            patch(
                "watercooler_mcp.hosted_ops.get_annotations_hosted",
                side_effect=_ann_factory(CROSS_ANNOTATIONS_BY_TOPIC),
            ),
        ):
            result = _list_decisions_impl(
                ctx=ctx,
                topic="topic-a",
                source_entry_id=CROSS_SOURCE_ENTRY["id"],
                code_path="",
            )

        payload = json.loads(result.content[0].text)
        assert payload["total"] == 1
        assert payload["decisions"][0]["source"]["entry_id"] == CROSS_SOURCE_ENTRY["id"]

    def test_topic_filter_does_not_fan_out_annotations_to_unrelated_topics(self):
        """Annotation fetches must stay scoped to the filter topic + xref sources.

        With a single full load the corpus contains every topic, but iteration
        is pinned to the filter topic. ``_ensure_annotations`` is therefore
        called only for topic-a (the decision) and topic-b (the xref source),
        never for an unrelated topic-c that happens to have its own Decision.
        """
        ctx = MagicMock()
        annotation_calls: list[str] = []

        unrelated_decision = {
            "id": "DEC2222222222222222222222",
            "entry_type": "Decision",
            "title": "Unrelated decision in topic-c",
            "body": "Confidence: 2/5\n\nShould not be inspected.",
            "timestamp": "2026-04-17T09:00:00Z",
            "agent": "Claude",
            "role": "implementer",
        }
        full_entries = dict(CROSS_ENTRIES_BY_TOPIC)
        full_entries["topic-c"] = [unrelated_decision]

        full_annotations = dict(CROSS_ANNOTATIONS_BY_TOPIC)
        full_annotations["topic-c"] = {
            unrelated_decision["id"]: {"tags": [], "xrefs": []},
        }

        def _loader(topics=None, max_workers=10):
            return (None, dict(full_entries))

        def _annotations(topic: str, target_id: str = ""):
            annotation_calls.append(topic)
            states = full_annotations.get(topic, {})
            if target_id:
                return (
                    None,
                    {
                        "target_id": target_id,
                        "annotation_state": states.get(target_id, {}),
                    },
                )
            return (None, {"topic": topic, "annotation_states": states})

        with (
            patch.object(
                decisions_mod.validation,
                "_require_context",
                return_value=(None, _hosted_context()),
            ),
            patch(
                "watercooler_mcp.hosted_ops.list_topic_dirs_hosted",
                side_effect=_dirs_stub(full_entries.keys()),
            ),
            patch(
                "watercooler_mcp.hosted_ops.load_all_entries_hosted",
                side_effect=_loader,
            ),
            patch(
                "watercooler_mcp.hosted_ops.get_annotations_hosted",
                side_effect=_annotations,
            ),
        ):
            result = _list_decisions_impl(ctx=ctx, topic="topic-a", code_path="")

        payload = json.loads(result.content[0].text)
        assert payload["total"] == 1
        assert payload["decisions"][0]["topic"] == "topic-a"

        assert (
            "topic-c" not in annotation_calls
        ), f"unrelated topic-c must not be touched: {annotation_calls}"
        assert set(annotation_calls) <= {"topic-a", "topic-b"}

    def test_mixed_same_thread_and_cross_thread_sources_resolve(self):
        """Both same-thread and cross-thread sources resolve from the single load.

        topic-a has two Decisions: D_same (xrefs S_same in topic-a) and
        D_cross (xrefs S_cross in topic-b). Both must resolve in a single
        full load without any merging or rehydration.
        """
        ctx = MagicMock()

        same_source = {
            "id": "SRCAAAAAAAAAAAAAAAAAAAAA1",
            "entry_type": "Note",
            "title": "Same-thread source",
            "body": "Discussion in topic-a.",
            "timestamp": "2026-04-18T08:00:00Z",
            "agent": "Claude",
            "role": "implementer",
        }
        decision_same = {
            "id": "DECAAAAAAAAAAAAAAAAAAAAA1",
            "entry_type": "Decision",
            "title": "Same-thread decision",
            "body": "Confidence: 5/5\n\nIn-topic.",
            "timestamp": "2026-04-18T08:05:00Z",
            "agent": "Claude",
            "role": "implementer",
        }
        decision_cross = {
            "id": "DECBBBBBBBBBBBBBBBBBBBBB1",
            "entry_type": "Decision",
            "title": "Cross-thread decision",
            "body": "Confidence: 4/5\n\nOther thread.",
            "timestamp": "2026-04-19T09:00:00Z",
            "agent": "Claude",
            "role": "implementer",
        }
        cross_source = {
            "id": "SRCBBBBBBBBBBBBBBBBBBBBB1",
            "entry_type": "Note",
            "title": "Cross-thread source",
            "body": "In topic-b.",
            "timestamp": "2026-04-19T08:00:00Z",
            "agent": "Claude",
            "role": "implementer",
        }

        entries = {
            "topic-a": [same_source, decision_same, decision_cross],
            "topic-b": [cross_source],
        }
        annotations = {
            "topic-a": {
                decision_same["id"]: {"tags": [], "xrefs": [same_source["id"]]},
                decision_cross["id"]: {"tags": [], "xrefs": [cross_source["id"]]},
                same_source["id"]: {
                    "tags": ["decision_extracted"],
                    "xrefs": [decision_same["id"]],
                },
            },
            "topic-b": {
                cross_source["id"]: {
                    "tags": ["decision_extracted"],
                    "xrefs": [decision_cross["id"]],
                },
            },
        }

        with (
            patch.object(
                decisions_mod.validation,
                "_require_context",
                return_value=(None, _hosted_context()),
            ),
            patch(
                "watercooler_mcp.hosted_ops.list_topic_dirs_hosted",
                side_effect=_dirs_stub(entries.keys()),
            ),
            patch(
                "watercooler_mcp.hosted_ops.load_all_entries_hosted",
                side_effect=_full_loader(entries),
            ),
            patch(
                "watercooler_mcp.hosted_ops.get_annotations_hosted",
                side_effect=_ann_factory(annotations),
            ),
        ):
            result = _list_decisions_impl(ctx=ctx, topic="topic-a", code_path="")

        payload = json.loads(result.content[0].text)
        assert payload["total"] == 2

        by_id = {d["entry_id"]: d for d in payload["decisions"]}

        same = by_id[decision_same["id"]]
        assert same["source"] is not None
        assert same["source"]["entry_id"] == same_source["id"]
        assert same["source"]["topic"] == "topic-a"
        assert same["extracted"] is True
        assert same["confidence"] == 5

        cross = by_id[decision_cross["id"]]
        assert cross["source"] is not None
        assert cross["source"]["entry_id"] == cross_source["id"]
        assert cross["source"]["topic"] == "topic-b"
        assert cross["extracted"] is True
        assert cross["confidence"] == 4

    def test_filter_topic_with_broken_meta_json_still_loads(self):
        """Regression: a thread with missing/malformed ``meta.json`` whose
        ``entries.jsonl`` is readable must still surface its decisions.

        The hosted path discovers topics via ``list_topic_dirs_hosted`` (raw
        directory listing), bypassing the ``meta.json`` read that
        ``list_threads_hosted`` silently drops on failure. So a filter on
        ``topic-a`` returns the decision whether or not ``meta.json`` parses.
        """
        ctx = MagicMock()

        decision = {
            "id": "DECBROKENMETAJSONXXXXXXX1",
            "entry_type": "Decision",
            "title": "Decision in a thread with broken meta.json",
            "body": "Confidence: 4/5\n\nStill reachable via raw-dir discovery.",
            "timestamp": "2026-04-20T10:00:00Z",
            "agent": "Claude",
            "role": "implementer",
        }
        entries = {"topic-a": [decision]}
        annotations = {
            "topic-a": {
                decision["id"]: {"tags": ["decision_extracted"], "xrefs": []},
            },
        }

        with (
            patch.object(
                decisions_mod.validation,
                "_require_context",
                return_value=(None, _hosted_context()),
            ),
            patch(
                "watercooler_mcp.hosted_ops.list_topic_dirs_hosted",
                side_effect=_dirs_stub(["topic-a"]),
            ),
            patch(
                "watercooler_mcp.hosted_ops.load_all_entries_hosted",
                side_effect=_full_loader(entries),
            ),
            patch(
                "watercooler_mcp.hosted_ops.get_annotations_hosted",
                side_effect=_ann_factory(annotations),
            ),
        ):
            result = _list_decisions_impl(ctx=ctx, topic="topic-a", code_path="")

        payload = json.loads(result.content[0].text)
        assert payload["total"] == 1
        assert payload["decisions"][0]["entry_id"] == decision["id"]
        assert payload["skipped_topics"] == []

    def test_skipped_topics_reported_when_entries_load_fails(self):
        """Topics discovered by list_topic_dirs_hosted but dropped by the
        entries load must be surfaced in ``skipped_topics`` so callers can
        detect partial results.
        """
        ctx = MagicMock()

        def _loader(topics=None, max_workers=10):
            # Simulate entries load dropping topic-b (e.g. corrupt entries.jsonl)
            return (
                None,
                {t: CROSS_ENTRIES_BY_TOPIC[t] for t in topics or [] if t == "topic-a"},
            )

        with (
            patch.object(
                decisions_mod.validation,
                "_require_context",
                return_value=(None, _hosted_context()),
            ),
            patch(
                "watercooler_mcp.hosted_ops.list_topic_dirs_hosted",
                side_effect=_dirs_stub(["topic-a", "topic-b"]),
            ),
            patch(
                "watercooler_mcp.hosted_ops.load_all_entries_hosted",
                side_effect=_loader,
            ),
            patch(
                "watercooler_mcp.hosted_ops.get_annotations_hosted",
                side_effect=_ann_factory(CROSS_ANNOTATIONS_BY_TOPIC),
            ),
        ):
            result = _list_decisions_impl(ctx=ctx, code_path="")

        payload = json.loads(result.content[0].text)
        assert payload["skipped_topics"] == ["topic-b"]

    def test_single_entries_load_no_recovery(self):
        """Hosted path must issue exactly one ``load_all_entries_hosted`` call.

        The recovery path that existed pre-P2.1 is gone — ``list_topic_dirs_hosted``
        now supplies the topic set directly, so no conditional second load can
        fire. ``_full_loader`` enforces the single-call invariant by raising on
        a second invocation.
        """
        ctx = MagicMock()

        with (
            patch.object(
                decisions_mod.validation,
                "_require_context",
                return_value=(None, _hosted_context()),
            ),
            patch(
                "watercooler_mcp.hosted_ops.list_topic_dirs_hosted",
                side_effect=_dirs_stub(CROSS_ENTRIES_BY_TOPIC.keys()),
            ),
            patch(
                "watercooler_mcp.hosted_ops.load_all_entries_hosted",
                side_effect=_full_loader(CROSS_ENTRIES_BY_TOPIC),
            ),
            patch(
                "watercooler_mcp.hosted_ops.get_annotations_hosted",
                side_effect=_ann_factory(CROSS_ANNOTATIONS_BY_TOPIC),
            ),
        ):
            result = _list_decisions_impl(ctx=ctx, topic="topic-a", code_path="")

        payload = json.loads(result.content[0].text)
        assert payload["total"] == 1
        assert payload["skipped_topics"] == []


# ---------------------------------------------------------------------------
# Prefixed entry-id regression (production data uses ``id = "entry:<ULID>"``)
# ---------------------------------------------------------------------------


PREFIXED_SRC_ULID = "01KCS6F2MRHGK4RBT8HV9JF9YS"
PREFIXED_DEC_ULID = "01KPX8NC75TB8RSXKZCP5PXSE2"

PREFIXED_SOURCE_ENTRY = {
    "id": f"entry:{PREFIXED_SRC_ULID}",
    "entry_type": "Note",
    "title": "Revised Scope: Read-Only Graphiti Tools (Phase 1)",
    "body": "Implementing 4 read-only tools only.",
    "timestamp": "2025-12-18T17:00:07Z",
    "agent": "Claude Code (jay)",
    "role": "planner",
}

PREFIXED_DECISION_ENTRY = {
    "id": f"entry:{PREFIXED_DEC_ULID}",
    "entry_type": "Decision",
    "title": "Implement Phase 1 as read-only Graphiti MCP tools only",
    "body": "Confidence: 5/5\n\n## Decision\nAdopt read-only scope.",
    "timestamp": "2026-04-23T13:32:25Z",
    "agent": "ExtractDecisionsDaemon (system)",
    "role": "scribe",
}

# Annotation state + xrefs carry the BARE ULID — mirrors the on-disk
# annotations.jsonl format. The bug was that _list_decisions compared the
# prefixed node id against the bare xref value and never matched.
PREFIXED_ENTRIES_BY_TOPIC = {
    "graphiti-mcp-tools": [PREFIXED_SOURCE_ENTRY, PREFIXED_DECISION_ENTRY],
}
PREFIXED_ANNOTATIONS_BY_TOPIC = {
    "graphiti-mcp-tools": {
        PREFIXED_DEC_ULID: {"tags": [], "xrefs": [PREFIXED_SRC_ULID]},
        PREFIXED_SRC_ULID: {
            "tags": ["decision_extracted"],
            "xrefs": [PREFIXED_DEC_ULID],
        },
    },
}


def _fake_load_prefixed_entries(topics=None, max_workers=10):
    if topics is not None:
        return (
            None,
            {
                t: PREFIXED_ENTRIES_BY_TOPIC[t]
                for t in topics
                if t in PREFIXED_ENTRIES_BY_TOPIC
            },
        )
    return (None, dict(PREFIXED_ENTRIES_BY_TOPIC))


def _fake_get_prefixed_annotations(topic: str, target_id: str = ""):
    states = PREFIXED_ANNOTATIONS_BY_TOPIC.get(topic, {})
    if target_id:
        return (
            None,
            {"target_id": target_id, "annotation_state": states.get(target_id, {})},
        )
    return (None, {"topic": topic, "annotation_states": states})


class TestPrefixedEntryIds:
    """Production entries.jsonl stores ``id = "entry:<ULID>"``; annotation
    state and xrefs use the bare ULID. The tool must normalize both sides
    of the comparison or every extracted Decision silently reports
    ``extracted=false, confidence=null, xrefs=[], source=null``."""

    def _run(self, **kwargs):
        ctx = MagicMock()
        with (
            patch.object(
                decisions_mod.validation,
                "_require_context",
                return_value=(None, _hosted_context()),
            ),
            patch(
                "watercooler_mcp.hosted_ops.list_topic_dirs_hosted",
                return_value=(None, sorted(PREFIXED_ENTRIES_BY_TOPIC.keys())),
            ),
            patch(
                "watercooler_mcp.hosted_ops.load_all_entries_hosted",
                side_effect=_fake_load_prefixed_entries,
            ),
            patch(
                "watercooler_mcp.hosted_ops.get_annotations_hosted",
                side_effect=_fake_get_prefixed_annotations,
            ),
        ):
            result = _list_decisions_impl(ctx=ctx, code_path="", **kwargs)
        return json.loads(result.content[0].text)

    def test_extracted_resolved_against_prefixed_node_id(self):
        payload = self._run()
        assert payload["total"] == 1

        decision = payload["decisions"][0]
        # Output normalizes to bare — consistent with xrefs and source.entry_id.
        assert decision["entry_id"] == PREFIXED_DEC_ULID
        assert decision["xrefs"] == [PREFIXED_SRC_ULID]
        assert decision["extracted"] is True
        assert decision["confidence"] == 5

        source = decision["source"]
        assert source is not None
        assert source["entry_id"] == PREFIXED_SRC_ULID
        assert source["topic"] == "graphiti-mcp-tools"

    def test_only_extracted_filter_with_prefixed_ids(self):
        payload = self._run(only_extracted=True)
        assert payload["total"] == 1
        assert payload["decisions"][0]["extracted"] is True

    def test_source_entry_id_filter_accepts_bare_ulid(self):
        payload = self._run(source_entry_id=PREFIXED_SRC_ULID)
        assert payload["total"] == 1
        assert payload["decisions"][0]["source"]["entry_id"] == PREFIXED_SRC_ULID

    def test_source_entry_id_filter_accepts_prefixed_input(self):
        # Callers that obtain an entry id from a raw entries.jsonl read see
        # the prefixed form. The tool must accept either shape and match
        # against the bare-form source.entry_id it returns.
        payload = self._run(source_entry_id=f"entry:{PREFIXED_SRC_ULID}")
        assert payload["total"] == 1
        assert payload["decisions"][0]["source"]["entry_id"] == PREFIXED_SRC_ULID


class _SupersessionBackend:
    """Minimal GraphitiBackend surface the hosted supersession path depends on.

    Mirrors the contract in test_decisions_supersession.py: ``episode_uuids_for_entry``
    never raises; ``get_edges_by_episodes`` returns edges whose ``episodes`` list
    intersects the requested UUIDs.
    """

    def __init__(self, episodes_by_entry, edges):
        self._episodes = episodes_by_entry
        self._edges = edges
        self.edge_queries: list[list[str]] = []

    def episode_uuids_for_entry(self, entry_id):
        return list(self._episodes.get(entry_id, []))

    def get_edges_by_episodes(self, episode_uuids, limit=2000):
        self.edge_queries.append(list(episode_uuids))
        wanted = set(episode_uuids)
        return [e for e in self._edges if wanted & set(e["episodes"])]


def _sedge(episodes, invalid_at=None):
    return {"episodes": list(episodes), "invalid_at": invalid_at, "fact": "x"}


class TestListDecisionsHostedSupersession:
    """The hosted server is co-located with its T2 (Railway runs FalkorDB next to
    the MCP). ``include_supersession=True`` must therefore consult that T2 — not
    hardcode ``unknown``. ``is_hosted_context`` means "list threads via GitHub
    API", not "no T2". Regression for the ``hosted_no_t2`` stub from #894.
    """

    def _run(self, *, backend, **kwargs):
        ctx = MagicMock()
        with (
            patch.object(
                decisions_mod.validation,
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
            patch.object(
                decisions_mod, "_acquire_graphiti_backend", return_value=backend
            ) as acquire,
        ):
            result = _list_decisions_impl(ctx=ctx, code_path="", **kwargs)
        return json.loads(result.content[0].text), acquire

    def test_resolves_real_state_when_t2_present(self):
        # The decision's episode carries one superseded fact edge → "superseded",
        # NOT the old hardcoded "hosted_no_t2" unknown.
        backend = _SupersessionBackend(
            episodes_by_entry={DECISION_ENTRY["id"]: ["epD"]},
            edges=[_sedge(["epD"], invalid_at="2026-05-01T00:00:00Z")],
        )
        payload, acquire = self._run(backend=backend, include_supersession=True)

        assert payload["total"] == 1
        supersession = payload["decisions"][0]["supersession"]
        assert supersession["state"] == "superseded"
        assert supersession["as_of"] == "2026-05-01T00:00:00Z"
        assert supersession["reason"] != "hosted_no_t2"
        # Acquire with NO code_path so http_ctx.repo (the tenant boundary) dominates
        # the T2 DB derivation — a request can't steer reads into another tenant.
        acquire.assert_called_once_with("")
        assert backend.edge_queries == [["epD"]]

    def test_in_force_when_t2_has_only_active_edges(self):
        backend = _SupersessionBackend(
            episodes_by_entry={DECISION_ENTRY["id"]: ["epD"]},
            edges=[_sedge(["epD"])],  # no invalid_at → active
        )
        payload, _ = self._run(backend=backend, include_supersession=True)
        assert payload["decisions"][0]["supersession"]["state"] == "in_force"

    def test_unknown_when_no_t2_backend(self):
        # A genuinely T2-less hosted surface (no FalkorDB) degrades to an honest
        # unknown — but with reason "t2_unavailable", never a false in_force and
        # never the misleading "hosted_no_t2".
        payload, acquire = self._run(backend=None, include_supersession=True)

        supersession = payload["decisions"][0]["supersession"]
        assert supersession["state"] == "unknown"
        assert supersession["reason"] == "t2_unavailable"
        assert supersession["reason"] != "hosted_no_t2"
        acquire.assert_called_once()

    def test_backend_not_acquired_when_flag_off(self):
        # Default listing stays a pure baseline read — no T2 acquisition, no
        # supersession field.
        payload, acquire = self._run(backend=None)
        assert "supersession" not in payload["decisions"][0]
        acquire.assert_not_called()


class TestBareEntryIdHelper:
    """Direct unit coverage for the prefix-stripping helper."""

    def test_strips_prefix(self):
        from watercooler_mcp.tools.decisions import _bare_entry_id

        assert (
            _bare_entry_id("entry:01ABCDEFGHIJKLMNOPQRSTUVWX")
            == "01ABCDEFGHIJKLMNOPQRSTUVWX"
        )

    def test_passes_through_bare_ulid(self):
        from watercooler_mcp.tools.decisions import _bare_entry_id

        assert (
            _bare_entry_id("01ABCDEFGHIJKLMNOPQRSTUVWX") == "01ABCDEFGHIJKLMNOPQRSTUVWX"
        )

    def test_handles_empty_and_none(self):
        from watercooler_mcp.tools.decisions import _bare_entry_id

        assert _bare_entry_id("") == ""
        assert _bare_entry_id(None) == ""


# ---------------------------------------------------------------------------
# PR2: hosted reader fast-path (decisions index) + fallback
# ---------------------------------------------------------------------------


def _index_rec(**overrides):
    """A decisions-index record for DECISION_ENTRY (source = SOURCE_ENTRY)."""
    rec = {
        "entry_id": DECISION_ENTRY["id"],
        "topic": "feat-option-b",
        "title": DECISION_ENTRY["title"],
        "timestamp": DECISION_ENTRY["timestamp"],
        "agent": "Claude",
        "role": "implementer",
        "confidence": 4,
        "extracted": True,
        "decision_origin": None,
        "source": {
            "entry_id": SOURCE_ENTRY["id"],
            "topic": "feat-option-b",
            "title": SOURCE_ENTRY["title"],
            "timestamp": SOURCE_ENTRY["timestamp"],
        },
    }
    rec.update(overrides)
    return rec


class TestListDecisionsFromIndex:
    def _run(self, index_records, *, full_scan_forbidden=True, **impl_kwargs):
        """Run _list_decisions_impl with the index present; by default assert
        the full per-thread scan is NOT touched."""
        ctx = MagicMock()
        load_all = MagicMock(
            side_effect=AssertionError("full scan must not run when index present")
            if full_scan_forbidden
            else _fake_load_all_entries_hosted
        )
        dirs = MagicMock(
            side_effect=AssertionError("dir listing must not run when index present")
            if full_scan_forbidden
            else _fake_list_topic_dirs_hosted
        )
        with (
            patch.object(
                decisions_mod.validation,
                "_require_context",
                return_value=(None, _hosted_context()),
            ),
            patch(
                "watercooler_mcp.hosted_ops.load_decision_index_hosted",
                return_value=(None, index_records),
            ),
            patch(
                "watercooler_mcp.hosted_ops.get_annotations_hosted",
                side_effect=_fake_get_annotations_hosted,
            ),
            patch("watercooler_mcp.hosted_ops.load_all_entries_hosted", load_all),
            patch("watercooler_mcp.hosted_ops.list_topic_dirs_hosted", dirs),
        ):
            result = _list_decisions_impl(ctx=ctx, code_path="", **impl_kwargs)
        return json.loads(result.content[0].text), load_all, dirs

    def test_uses_index_and_skips_full_scan(self):
        payload, load_all, dirs = self._run([_index_rec()])
        assert payload["total"] == 1
        assert payload["meta"]["index_status"] == "used"
        d = payload["decisions"][0]
        assert d["entry_id"] == DECISION_ENTRY["id"]
        assert d["source"]["entry_id"] == SOURCE_ENTRY["id"]
        assert d["confidence"] == 4
        assert d["extracted"] is True
        # tags/xrefs are NOT in the index — live-fetched from annotations
        assert d["tags"] == ["reviewed"]
        assert d["xrefs"] == [SOURCE_ENTRY["id"]]
        load_all.assert_not_called()
        dirs.assert_not_called()

    def test_cross_thread_source_resolved_from_index(self):
        rec = _index_rec(
            source={
                "entry_id": SOURCE_ENTRY["id"],
                "topic": "some-other-thread",
                "title": "cross-thread source",
                "timestamp": "2026-01-01T00:00:00Z",
            }
        )
        payload, load_all, _ = self._run([rec])
        # resolved without any entries fan-out
        assert payload["decisions"][0]["source"]["topic"] == "some-other-thread"
        load_all.assert_not_called()

    def test_topic_filter_scopes_index(self):
        other = _index_rec(entry_id="DECX000000000000000000000", topic="other-topic")
        payload, _, _ = self._run([_index_rec(), other], topic="feat-option-b")
        assert payload["total"] == 1
        assert payload["decisions"][0]["topic"] == "feat-option-b"

    def test_source_entry_id_filter_uses_index_source(self):
        payload, _, _ = self._run(
            [_index_rec()], source_entry_id=SOURCE_ENTRY["id"]
        )
        assert payload["total"] == 1
        payload2, _, _ = self._run([_index_rec()], source_entry_id="NOPE")
        assert payload2["total"] == 0

    def test_supersession_applied_on_index_path(self):
        # No T2 backend in the test env → honest "unknown", never a crash.
        payload, _, _ = self._run([_index_rec()], include_supersession=True)
        assert payload["total"] == 1
        assert "supersession" in payload["decisions"][0]

    def test_fallback_marks_index_status_missing(self):
        # autouse fixture already returns (None, None) → full-scan fallback.
        ctx = MagicMock()
        with (
            patch.object(
                decisions_mod.validation,
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
            result = _list_decisions_impl(ctx=ctx, code_path="")
        payload = json.loads(result.content[0].text)
        assert payload["meta"]["index_status"] == "missing"
        assert payload["total"] == 1  # fallback still returns the decision
