"""meta.json annotation embedding is canonically ordered.

``_sync_annotations_to_meta`` writes ``meta["annotations"]`` in sorted
target-id order, so a no-op re-materialization yields a byte-identical
meta.json — removing the spurious annotation-reorder churn that bloated diffs
(bug-sync-worktree-poisoning #14).
"""

from __future__ import annotations

import json
from pathlib import Path

from watercooler.baseline_graph.annotations import AnnotationEvent, append_annotation
from watercooler.baseline_graph.storage import get_graph_dir, get_thread_graph_dir


def _thread_dir(tmp_path: Path, topic: str) -> Path:
    td = get_thread_graph_dir(get_graph_dir(tmp_path), topic)
    td.mkdir(parents=True, exist_ok=True)
    (td / "meta.json").write_text(json.dumps({"topic": topic}), encoding="utf-8")
    return td


def _tag(target_id: str, eid: str) -> AnnotationEvent:
    return AnnotationEvent(
        id=eid,
        target_id=target_id,
        target_type="thread",
        kind="tag",
        value="x",
        actor="Test",
        timestamp="2026-06-20T00:00:00+00:00",
    )


def test_meta_annotation_keys_sorted_regardless_of_insertion_order(tmp_path):
    td = _thread_dir(tmp_path, "topic-x")

    # Insert out of sorted order.
    append_annotation(td, _tag("zzz-target", "01EVENTZZZ0000000000000001"))
    append_annotation(td, _tag("mmm-target", "01EVENTMMM0000000000000002"))
    append_annotation(td, _tag("aaa-target", "01EVENTAAA0000000000000003"))

    meta = json.loads((td / "meta.json").read_text(encoding="utf-8"))
    keys = list(meta["annotations"].keys())

    assert keys == sorted(keys)
    assert keys == ["aaa-target", "mmm-target", "zzz-target"]
