"""PR3: local write/delete maintenance of the decisions index.

Covers the shared helpers (``upsert_decision_index_local`` /
``prune_decision_index_local``) and the three hook sites that keep the
repo-level index current on the library write path:
- ``commands_graph.append_entry`` (Decision write),
- ``writer.delete_entry_node`` (Decision delete prunes; non-Decision doesn't).
"""

from __future__ import annotations

from unittest.mock import patch

from watercooler.baseline_graph import storage
from watercooler.baseline_graph.annotations import AnnotationState
from watercooler.baseline_graph.decision_index import (
    prune_decision_index_local,
    upsert_decision_index_local,
)
from watercooler.decision_extraction import DECISION_EXTRACTED_TAG

SRC = "SRC0000000000000000000000"
DEC = "DEC0000000000000000000000"


def _write_graph(graph_dir, topic, nodes):
    """nodes: list of entry dicts (with bare-less 'entry:'-prefixed ids)."""
    entries = {n["id"]: n for n in nodes}
    storage.write_thread_graph(
        graph_dir, topic, {"topic": topic, "entry_count": len(entries)}, entries, {}
    )


def _dec_node(entry_id=DEC, body="Confidence: 4/5\n\nAdopt B."):
    return {
        "id": f"entry:{entry_id}",
        "entry_type": "Decision",
        "title": "Adopt option B",
        "body": body,
        "timestamp": "2026-04-20T10:05:00Z",
        "agent": "Claude",
        "role": "planner",
        "decision_origin": "agent_authored",
    }


def _src_node():
    return {
        "id": f"entry:{SRC}",
        "entry_type": "Note",
        "title": "Source discussion",
        "timestamp": "2026-04-20T10:00:00Z",
        "agent": "Claude",
        "role": "planner",
    }


def _ann(states):
    """Patch get_annotation_state to resolve per target_id from `states`."""
    def _fake(thread_dir, target_id, read_only=False):
        return states.get(target_id, AnnotationState())

    return patch(
        "watercooler.baseline_graph.annotations.get_annotation_state", side_effect=_fake
    )


# ---------------------------------------------------------------------------
# upsert helper
# ---------------------------------------------------------------------------


def test_upsert_same_thread_source_extracted(tmp_path):
    graph_dir = storage.get_graph_dir(tmp_path)
    _write_graph(graph_dir, "feat-b", [_src_node(), _dec_node()])
    states = {
        DEC: AnnotationState(xrefs=[SRC]),
        SRC: AnnotationState(tags=[DECISION_EXTRACTED_TAG]),
    }
    with _ann(states):
        upsert_decision_index_local(graph_dir, "feat-b", DEC)

    recs = storage.load_decision_index(graph_dir)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["entry_id"] == DEC
    assert rec["extracted"] is True
    assert rec["confidence"] == 4
    assert rec["source"]["entry_id"] == SRC
    assert rec["source"]["topic"] == "feat-b"


def test_upsert_cross_thread_source(tmp_path):
    graph_dir = storage.get_graph_dir(tmp_path)
    _write_graph(graph_dir, "feat-b", [_dec_node()])
    _write_graph(graph_dir, "other-thread", [_src_node()])
    states = {
        DEC: AnnotationState(xrefs=[SRC]),
        SRC: AnnotationState(tags=[DECISION_EXTRACTED_TAG]),
    }
    with _ann(states):
        upsert_decision_index_local(graph_dir, "feat-b", DEC)

    rec = storage.load_decision_index(graph_dir)[0]
    assert rec["source"]["topic"] == "other-thread"
    assert rec["extracted"] is True


def test_upsert_no_xref_source_none(tmp_path):
    graph_dir = storage.get_graph_dir(tmp_path)
    _write_graph(graph_dir, "feat-b", [_dec_node()])
    with _ann({DEC: AnnotationState()}):
        upsert_decision_index_local(graph_dir, "feat-b", DEC)
    rec = storage.load_decision_index(graph_dir)[0]
    assert rec["source"] is None
    assert rec["extracted"] is False
    assert rec["confidence"] is None  # only parsed when extracted


def test_upsert_non_decision_is_noop(tmp_path):
    graph_dir = storage.get_graph_dir(tmp_path)
    _write_graph(graph_dir, "feat-b", [_src_node()])  # a Note
    with _ann({}):
        upsert_decision_index_local(graph_dir, "feat-b", SRC)
    assert storage.load_decision_index(graph_dir) == []


def test_upsert_idempotent_replaces_row(tmp_path):
    graph_dir = storage.get_graph_dir(tmp_path)
    _write_graph(graph_dir, "feat-b", [_dec_node()])
    with _ann({DEC: AnnotationState()}):
        upsert_decision_index_local(graph_dir, "feat-b", DEC)
        upsert_decision_index_local(graph_dir, "feat-b", DEC)
    assert len(storage.load_decision_index(graph_dir)) == 1


# ---------------------------------------------------------------------------
# prune helper
# ---------------------------------------------------------------------------


def test_prune_removes_row(tmp_path):
    graph_dir = storage.get_graph_dir(tmp_path)
    storage.write_decision_index(
        graph_dir, [{"entry_id": DEC, "topic": "feat-b"}, {"entry_id": "X", "topic": "t"}]
    )
    prune_decision_index_local(graph_dir, DEC)
    remaining = storage.load_decision_index(graph_dir)
    assert [r["entry_id"] for r in remaining] == ["X"]


def test_prune_absent_is_noop(tmp_path):
    graph_dir = storage.get_graph_dir(tmp_path)
    storage.write_decision_index(graph_dir, [{"entry_id": "X", "topic": "t"}])
    prune_decision_index_local(graph_dir, DEC)
    assert len(storage.load_decision_index(graph_dir)) == 1


# ---------------------------------------------------------------------------
# Hook wiring: append_entry + delete_entry_node
# ---------------------------------------------------------------------------


def test_append_entry_decision_writes_index(tmp_path):
    from watercooler.commands_graph import append_entry

    append_entry(
        "feat-b",
        threads_dir=tmp_path,
        agent="Claude",
        role="planner",
        title="Adopt option B",
        entry_type="Decision",
        body="Confidence: 3/5\n\nAdopt B.",
        entry_id=DEC,
    )
    recs = storage.load_decision_index(storage.get_graph_dir(tmp_path))
    assert [r["entry_id"] for r in recs] == [DEC]
    assert recs[0]["source"] is None  # hand-authored, no xref


def test_append_entry_note_does_not_write_index(tmp_path):
    from watercooler.commands_graph import append_entry

    append_entry(
        "feat-b",
        threads_dir=tmp_path,
        agent="Claude",
        role="planner",
        title="Just a note",
        entry_type="Note",
        body="hi",
        entry_id="NOTE00000000000000000000AA",
    )
    assert storage.load_decision_index(storage.get_graph_dir(tmp_path)) == []


def test_delete_entry_node_prunes_decision(tmp_path):
    from watercooler.baseline_graph.writer import delete_entry_node

    graph_dir = storage.get_graph_dir(tmp_path)
    _write_graph(graph_dir, "feat-b", [_dec_node()])
    storage.write_decision_index(graph_dir, [{"entry_id": DEC, "topic": "feat-b"}])

    assert delete_entry_node(tmp_path, "feat-b", DEC) is True
    assert storage.load_decision_index(graph_dir) == []


def test_delete_entry_node_note_leaves_index(tmp_path):
    from watercooler.baseline_graph.writer import delete_entry_node

    graph_dir = storage.get_graph_dir(tmp_path)
    _write_graph(graph_dir, "feat-b", [_src_node()])  # a Note
    storage.write_decision_index(graph_dir, [{"entry_id": DEC, "topic": "feat-b"}])

    delete_entry_node(tmp_path, "feat-b", SRC)
    # the Decision row is untouched by a Note deletion
    assert [r["entry_id"] for r in storage.load_decision_index(graph_dir)] == [DEC]


# ---------------------------------------------------------------------------
# PR4: full rebuild (backfill / export pipeline)
# ---------------------------------------------------------------------------


def test_rebuild_indexes_all_decisions(tmp_path):
    from watercooler.baseline_graph.decision_index import rebuild_decision_index

    graph_dir = storage.get_graph_dir(tmp_path)
    _write_graph(graph_dir, "t1", [_src_node(), _dec_node()])  # Note + Decision
    _write_graph(graph_dir, "t2", [_dec_node("DEC2000000000000000000000")])

    n = rebuild_decision_index(graph_dir)
    assert n == 2
    recs = storage.load_decision_index(graph_dir)
    assert {r["entry_id"] for r in recs} == {DEC, "DEC2000000000000000000000"}


def test_rebuild_resolves_source_and_extracted(tmp_path):
    from watercooler.baseline_graph import annotations as ann_mod
    from watercooler.baseline_graph.decision_index import rebuild_decision_index

    graph_dir = storage.get_graph_dir(tmp_path)
    _write_graph(graph_dir, "feat-b", [_src_node(), _dec_node()])

    def _fake_states(thread_dir, read_only=False):
        return {
            DEC: AnnotationState(xrefs=[SRC]),
            SRC: AnnotationState(tags=[DECISION_EXTRACTED_TAG]),
        }

    with patch.object(ann_mod, "load_or_rebuild_state", side_effect=_fake_states):
        rebuild_decision_index(graph_dir)

    rec = storage.load_decision_index(graph_dir)[0]
    assert rec["source"]["entry_id"] == SRC
    assert rec["extracted"] is True
    assert rec["confidence"] == 4


def test_rebuild_overwrites_stale_index(tmp_path):
    from watercooler.baseline_graph.decision_index import rebuild_decision_index

    graph_dir = storage.get_graph_dir(tmp_path)
    # Stale row for a decision that no longer exists in the graph.
    storage.write_decision_index(graph_dir, [{"entry_id": "GHOST", "topic": "old"}])
    _write_graph(graph_dir, "feat-b", [_dec_node()])

    rebuild_decision_index(graph_dir)
    recs = storage.load_decision_index(graph_dir)
    assert [r["entry_id"] for r in recs] == [DEC]  # ghost dropped
