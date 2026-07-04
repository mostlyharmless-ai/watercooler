"""Unit tests for the decisions index (PR1: builder + storage I/O + staging).

Covers:
- ``build_decision_index_records`` — source resolution (same/cross-thread),
  ``extracted`` sourced from the *source* entry's annotation, confidence-if-
  extracted, non-Decision skip, no-xref source=None.
- storage I/O round-trip (``decision_index_path`` / ``write_decision_index`` /
  ``load_decision_index``).
- ``paths_to_stage_for_topic(include_decision_index=...)`` staging the repo-level
  index file (the gap that would otherwise leave it uncommitted on the orphan
  branch).
"""

from __future__ import annotations

from pathlib import Path

from watercooler.baseline_graph import storage
from watercooler.baseline_graph.decision_index import (
    bare_entry_id,
    build_decision_index_records,
    index_records_to_jsonl,
    parse_confidence,
    remove_record_from_list,
    upsert_record_in_list,
)
from watercooler.decision_extraction import DECISION_EXTRACTED_TAG
from watercooler.sync_common import paths_to_stage_for_topic

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

SRC = "SRC0000000000000000000000"
DEC = "DEC0000000000000000000000"
DEC2 = "DEC2000000000000000000000"


def _src_node():
    return {
        "id": f"entry:{SRC}",
        "entry_type": "Note",
        "title": "Discussion that led to the decision",
        "timestamp": "2026-04-20T10:00:00Z",
        "agent": "Claude",
        "role": "implementer",
    }


def _dec_node(entry_id=DEC, body="Confidence: 4/5\n\nWe will adopt option B."):
    return {
        "id": f"entry:{entry_id}",
        "entry_type": "Decision",
        "title": "Adopt option B",
        "body": body,
        "timestamp": "2026-04-20T10:05:00Z",
        "agent": "Claude",
        "role": "implementer",
        "decision_origin": "agent_authored",
    }


# ---------------------------------------------------------------------------
# Leaf helpers
# ---------------------------------------------------------------------------


def test_bare_entry_id_strips_prefix():
    assert bare_entry_id(f"entry:{DEC}") == DEC
    assert bare_entry_id(DEC) == DEC
    assert bare_entry_id(None) == ""


def test_parse_confidence_bounds():
    assert parse_confidence("Confidence: 4/5") == 4
    assert parse_confidence("Confidence: 0/5") == 0
    assert parse_confidence("Confidence: 10/5") is None  # out of range
    assert parse_confidence("no confidence here") is None
    assert parse_confidence(None) is None
    assert parse_confidence({"not": "a string"}) is None


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def test_same_thread_source_extracted_with_confidence():
    entries = {"feat-b": [_src_node(), _dec_node()]}
    annotations = {
        "feat-b": {
            DEC: {"xrefs": [SRC], "tags": []},
            SRC: {"xrefs": [DEC], "tags": [DECISION_EXTRACTED_TAG]},
        }
    }
    records = build_decision_index_records(entries, annotations)
    assert len(records) == 1
    rec = records[0]
    assert rec["entry_id"] == DEC
    assert rec["topic"] == "feat-b"
    assert rec["extracted"] is True
    assert rec["confidence"] == 4
    assert rec["decision_origin"] == "agent_authored"
    assert rec["source"] == {
        "entry_id": SRC,
        "topic": "feat-b",
        "title": "Discussion that led to the decision",
        "timestamp": "2026-04-20T10:00:00Z",
    }
    # mutable annotation state is NOT indexed
    assert "tags" not in rec
    assert "xrefs" not in rec


def test_cross_thread_source_resolves():
    entries = {
        "feat-b": [_dec_node()],
        "other-thread": [_src_node()],
    }
    annotations = {
        "feat-b": {DEC: {"xrefs": [SRC], "tags": []}},
        "other-thread": {SRC: {"xrefs": [DEC], "tags": [DECISION_EXTRACTED_TAG]}},
    }
    records = build_decision_index_records(entries, annotations)
    assert len(records) == 1
    assert records[0]["source"]["topic"] == "other-thread"
    assert records[0]["extracted"] is True


def test_not_extracted_when_source_lacks_tag():
    entries = {"feat-b": [_src_node(), _dec_node()]}
    annotations = {
        "feat-b": {
            DEC: {"xrefs": [SRC], "tags": []},
            SRC: {"xrefs": [DEC], "tags": []},  # no decision_extracted
        }
    }
    rec = build_decision_index_records(entries, annotations)[0]
    assert rec["extracted"] is False
    assert rec["confidence"] is None  # confidence only parsed when extracted


def test_no_xref_source_none():
    entries = {"feat-b": [_dec_node()]}
    annotations = {"feat-b": {DEC: {"xrefs": [], "tags": []}}}
    rec = build_decision_index_records(entries, annotations)[0]
    assert rec["source"] is None
    assert rec["extracted"] is False


def test_non_decision_entries_skipped():
    entries = {"feat-b": [_src_node()]}  # only a Note
    assert build_decision_index_records(entries, {}) == []


def test_multiple_decisions_indexed():
    entries = {"feat-b": [_dec_node(DEC), _dec_node(DEC2, body="no confidence")]}
    annotations = {"feat-b": {}}
    records = build_decision_index_records(entries, annotations)
    assert {r["entry_id"] for r in records} == {DEC, DEC2}


# ---------------------------------------------------------------------------
# Storage I/O
# ---------------------------------------------------------------------------


def test_decision_index_path():
    graph_dir = Path("/tmp/x/graph/baseline")
    assert storage.decision_index_path(graph_dir).name == "decisions-index.jsonl"
    assert storage.decision_index_path(graph_dir).parent == graph_dir


def test_load_decision_index_absent_returns_empty(tmp_path):
    graph_dir = tmp_path / "graph" / "baseline"
    assert storage.load_decision_index(graph_dir) == []


def test_write_then_load_round_trip(tmp_path):
    graph_dir = tmp_path / "graph" / "baseline"
    records = [{"entry_id": DEC, "topic": "feat-b", "source": None}]
    storage.write_decision_index(graph_dir, records)
    assert storage.decision_index_path(graph_dir).exists()
    assert storage.load_decision_index(graph_dir) == records


# ---------------------------------------------------------------------------
# Staging contract
# ---------------------------------------------------------------------------


def test_staging_includes_index_when_present(tmp_path):
    graph_dir = tmp_path / "graph" / "baseline"
    storage.write_decision_index(graph_dir, [{"entry_id": DEC}])
    paths = paths_to_stage_for_topic(
        tmp_path, "feat-b", include_missing=True, include_decision_index=True
    )
    assert "graph/baseline/decisions-index.jsonl" in paths


def test_staging_omits_index_when_absent(tmp_path):
    paths = paths_to_stage_for_topic(
        tmp_path, "feat-b", include_missing=True, include_decision_index=True
    )
    assert "graph/baseline/decisions-index.jsonl" not in paths


def test_staging_omits_index_when_flag_off(tmp_path):
    graph_dir = tmp_path / "graph" / "baseline"
    storage.write_decision_index(graph_dir, [{"entry_id": DEC}])
    paths = paths_to_stage_for_topic(tmp_path, "feat-b", include_missing=True)
    assert "graph/baseline/decisions-index.jsonl" not in paths


# ---------------------------------------------------------------------------
# Pure list ops (hosted hooks, PR5)
# ---------------------------------------------------------------------------


def test_upsert_record_in_list_adds_and_replaces():
    recs = [{"entry_id": "A", "topic": "t"}]
    out = upsert_record_in_list(recs, {"entry_id": "B", "topic": "t"})
    assert {r["entry_id"] for r in out} == {"A", "B"}
    out2 = upsert_record_in_list(out, {"entry_id": "A", "topic": "t2"})
    assert next(r for r in out2 if r["entry_id"] == "A")["topic"] == "t2"


def test_upsert_preserves_resolved_source_against_none():
    recs = [{"entry_id": "A", "source": {"topic": "x"}, "extracted": True, "confidence": 4}]
    out = upsert_record_in_list(
        recs, {"entry_id": "A", "source": None, "extracted": False, "confidence": None}
    )
    a = out[0]
    assert a["source"] == {"topic": "x"}  # cross-thread source not downgraded
    assert a["extracted"] is True
    assert a["confidence"] == 4


def test_remove_record_from_list():
    recs = [{"entry_id": "A"}, {"entry_id": "B"}]
    assert [r["entry_id"] for r in remove_record_from_list(recs, "A")] == ["B"]


def test_index_records_to_jsonl_roundtrip():
    import json

    recs = [{"entry_id": "A", "topic": "t"}, {"entry_id": "B", "topic": "t"}]
    parsed = [json.loads(line) for line in index_records_to_jsonl(recs).splitlines()]
    assert parsed == recs
