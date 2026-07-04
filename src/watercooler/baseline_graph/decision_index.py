"""Decisions index — a small repo-level projection of Decision entries.

The hosted ``list_decisions`` read path loads a single repo-level
``graph/baseline/decisions-index.jsonl`` instead of fanning out one GitHub
fetch per thread (the ~518-call rate-limit that masked results as ``total:0``).
Each record carries the *resolved source* so cross-thread source resolution is
O(1) at read time without scanning the whole corpus.

This module holds the pure record-building logic (no file I/O — that lives in
``storage.py`` alongside the search-index helpers). The builder mirrors the
hosted read path (``watercooler_mcp.tools.decisions._list_decisions_hosted``)
exactly so the index can never drift from what a live scan would produce:

- source is resolved from the Decision's xref (same-thread first, then
  cross-thread),
- ``extracted`` is read from the *source* entry's ``decision_extracted``
  annotation tag (not inferred from the Decision's shape),
- ``confidence`` is parsed from the body only when ``extracted`` is true.

The Decision's own ``tags``/``xrefs`` are intentionally NOT indexed — they are
annotation state that mutates independently of entry writes, so they stay
live-fetched (per decision-bearing topic) at read time.
"""

from __future__ import annotations

import json
import re
from typing import Any

from watercooler.decision_extraction import DECISION_EXTRACTED_TAG

_ENTRY_ID_PREFIX = "entry:"
_CONFIDENCE_RE = re.compile(r"^Confidence:\s*(\d+)\s*/\s*5", re.MULTILINE)


def bare_entry_id(value: Any) -> str:
    """Return the bare ULID for an entry id, stripping the ``entry:`` prefix.

    Entries.jsonl stores node ids as ``"entry:<ULID>"``, but annotation state
    and xref values carry the bare ULID. Lookups must compare on the bare form.
    """
    text = str(value or "")
    if text.startswith(_ENTRY_ID_PREFIX):
        return text[len(_ENTRY_ID_PREFIX) :]
    return text


def parse_confidence(body: Any) -> int | None:
    """Extract ``Confidence: N/5`` from an extracted Decision body.

    Non-string/empty inputs and out-of-range (``<0`` or ``>5``) values return
    ``None`` — matching the read-path parser so the index can't surface a
    schema-violating confidence.
    """
    if not isinstance(body, str) or not body:
        return None
    m = _CONFIDENCE_RE.search(body)
    if not m:
        return None
    try:
        value = int(m.group(1))
    except (TypeError, ValueError):
        return None
    if value < 0 or value > 5:
        return None
    return value


def _resolve_source(
    entries_by_topic: dict[str, list[dict[str, Any]]],
    topic: str,
    xrefs: list[str],
) -> dict[str, Any] | None:
    """Resolve a Decision's source-entry xref to ``{entry_id, topic, title,
    timestamp}``.

    Same-thread first (cheap), then cross-thread. Mirrors
    ``_resolve_source_from_map`` in the hosted read path.
    """
    if not xrefs:
        return None

    candidate_id = bare_entry_id(xrefs[0])

    for node in entries_by_topic.get(topic, []):
        if bare_entry_id(node.get("id", "")) == candidate_id:
            return {
                "entry_id": candidate_id,
                "timestamp": node.get("timestamp"),
                "title": node.get("title"),
                "topic": topic,
            }

    for other_topic, nodes in entries_by_topic.items():
        if other_topic == topic:
            continue
        for node in nodes:
            if bare_entry_id(node.get("id", "")) == candidate_id:
                return {
                    "entry_id": candidate_id,
                    "timestamp": node.get("timestamp"),
                    "title": node.get("title"),
                    "topic": other_topic,
                }
    return None


def build_decision_index_record(
    *,
    node: dict[str, Any],
    topic: str,
    source: dict[str, Any] | None,
    extracted: bool,
) -> dict[str, Any]:
    """Assemble one decision-index record from a Decision node + resolved bits.

    The shape is the read-path decision record MINUS the mutable
    ``tags``/``xrefs`` (live-fetched at read time).
    """
    body = node.get("body", "") or ""
    return {
        "entry_id": bare_entry_id(node.get("id", "")),
        "topic": topic,
        "title": node.get("title"),
        "timestamp": node.get("timestamp"),
        "agent": node.get("agent"),
        "role": node.get("role"),
        "confidence": parse_confidence(body) if extracted else None,
        "extracted": extracted,
        "decision_origin": node.get("decision_origin"),
        "source": source,
    }


def build_decision_index_records(
    entries_by_topic: dict[str, list[dict[str, Any]]],
    annotations_by_topic: dict[str, dict[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Build all decision-index records from per-topic entries + annotations.

    Args:
        entries_by_topic: ``{topic: [entry_node, ...]}`` (entry nodes as stored
            in entries.jsonl).
        annotations_by_topic: ``{topic: {bare_entry_id: {"tags": [...],
            "xrefs": [...]}}}`` — materialized annotation state per topic.

    Returns:
        One record per ``Decision`` entry, with source resolved and
        ``extracted`` read from the source entry's annotation tags.
    """
    records: list[dict[str, Any]] = []
    for topic, nodes in entries_by_topic.items():
        topic_ann = annotations_by_topic.get(topic, {})
        for node in nodes:
            if node.get("entry_type") != "Decision":
                continue
            entry_id = bare_entry_id(node.get("id", ""))
            state = topic_ann.get(entry_id) or {}
            xrefs = list(state.get("xrefs") or [])

            source = _resolve_source(entries_by_topic, topic, xrefs)

            extracted = False
            if source:
                source_state = (
                    annotations_by_topic.get(source["topic"], {}).get(
                        source["entry_id"]
                    )
                    or {}
                )
                extracted = DECISION_EXTRACTED_TAG in (source_state.get("tags") or [])

            records.append(
                build_decision_index_record(
                    node=node, topic=topic, source=source, extracted=extracted
                )
            )
    return records


def rebuild_decision_index(graph_dir) -> int:
    """Rebuild the entire repo-level decisions index from the local per-thread graph.

    Corpus-scan builder used by the export pipeline and by local backfill. Loads
    every topic's entries + materialized annotation state, rebuilds via
    ``build_decision_index_records``, and writes the index. Returns the number of
    Decision records written.
    """
    from watercooler.baseline_graph import storage
    from watercooler.baseline_graph.annotations import load_or_rebuild_state

    entries_by_topic: dict[str, list[dict[str, Any]]] = {}
    annotations_by_topic: dict[str, dict[str, dict[str, Any]]] = {}
    for topic in storage.list_thread_topics(graph_dir):
        entries_by_topic[topic] = list(storage.load_thread_entries(graph_dir, topic))
        thread_dir = storage.get_thread_graph_dir(graph_dir, topic)
        states = load_or_rebuild_state(thread_dir, read_only=True)
        annotations_by_topic[topic] = {
            tid: {"tags": list(st.tags or []), "xrefs": list(st.xrefs or [])}
            for tid, st in states.items()
        }

    records = build_decision_index_records(entries_by_topic, annotations_by_topic)
    storage.write_decision_index(graph_dir, records)
    return len(records)


# ---------------------------------------------------------------------------
# Incremental maintenance against the LOCAL per-thread graph
# ---------------------------------------------------------------------------
#
# These keep the repo-level index current as Decisions are written/deleted via
# the library write path (say/ack/handoff + daemons). They read the FINAL graph
# state, so the upsert MUST be called after any annotation events for the entry
# have been applied (the daemon applies the source xref + decision_extracted tag
# after the entry write). The repo-level file is staged with the entry's commit
# via ``paths_to_stage_for_topic(include_decision_index=True)``.


def _resolve_source_local(
    graph_dir, topic: str, xrefs: list[str]
) -> dict[str, Any] | None:
    """Resolve a source xref against the local per-thread graph (same-thread first)."""
    from watercooler.baseline_graph import storage

    if not xrefs:
        return None
    candidate_id = bare_entry_id(xrefs[0])

    def _scan(scan_topic: str) -> dict[str, Any] | None:
        for node in storage.load_thread_entries(graph_dir, scan_topic):
            if bare_entry_id(node.get("id", "")) == candidate_id:
                return {
                    "entry_id": candidate_id,
                    "timestamp": node.get("timestamp"),
                    "title": node.get("title"),
                    "topic": scan_topic,
                }
        return None

    found = _scan(topic)
    if found:
        return found
    for other_topic in storage.list_thread_topics(graph_dir):
        if other_topic == topic:
            continue
        found = _scan(other_topic)
        if found:
            return found
    return None


def upsert_decision_index_local(graph_dir, topic: str, entry_id: str) -> None:
    """Upsert one Decision's record into the repo-level index from local state.

    No-op if the entry is missing or not a Decision (defensive). Reads the
    entry's current annotation state, so call AFTER annotation events are
    applied.
    """
    from watercooler.baseline_graph import storage
    from watercooler.baseline_graph.annotations import get_annotation_state

    bare_id = bare_entry_id(entry_id)
    node = None
    for candidate in storage.load_thread_entries(graph_dir, topic):
        if bare_entry_id(candidate.get("id", "")) == bare_id:
            node = candidate
            break
    if node is None or node.get("entry_type") != "Decision":
        return

    thread_dir = storage.get_thread_graph_dir(graph_dir, topic)
    dec_state = get_annotation_state(thread_dir, bare_id, read_only=True)
    xrefs = list(getattr(dec_state, "xrefs", None) or [])
    source = _resolve_source_local(graph_dir, topic, xrefs)

    extracted = False
    if source:
        src_thread_dir = storage.get_thread_graph_dir(graph_dir, source["topic"])
        src_state = get_annotation_state(
            src_thread_dir, source["entry_id"], read_only=True
        )
        extracted = DECISION_EXTRACTED_TAG in (getattr(src_state, "tags", None) or [])

    record = build_decision_index_record(
        node=node, topic=topic, source=source, extracted=extracted
    )
    by_id = {
        bare_entry_id(r.get("entry_id")): r
        for r in storage.load_decision_index(graph_dir)
        if isinstance(r, dict)
    }
    by_id[bare_id] = record
    storage.write_decision_index(graph_dir, list(by_id.values()))


def prune_decision_index_local(graph_dir, entry_id: str) -> None:
    """Remove a Decision's row from the repo-level index (no-op if absent)."""
    from watercooler.baseline_graph import storage

    bare_id = bare_entry_id(entry_id)
    records = storage.load_decision_index(graph_dir)
    kept = [
        r
        for r in records
        if isinstance(r, dict) and bare_entry_id(r.get("entry_id")) != bare_id
    ]
    if len(kept) != len(records):
        storage.write_decision_index(graph_dir, kept)


# ---------------------------------------------------------------------------
# Pure list operations — used by the HOSTED write/delete hooks, which keep the
# index in memory and fold it into the same GitHub commit as the entry.
# ---------------------------------------------------------------------------


def index_records_to_jsonl(records: list[dict[str, Any]]) -> str:
    """Serialize index records to JSONL (one compact object per line)."""
    return "".join(
        json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n"
        for r in records
        if isinstance(r, dict)
    )


def upsert_record_in_list(
    records: list[dict[str, Any]], record: dict[str, Any]
) -> list[dict[str, Any]]:
    """Upsert ``record`` by entry_id.

    Never downgrades an already-resolved ``source`` to ``None``: a same-thread-
    only writer (the hosted write path) can't see a cross-thread source that a
    full rebuild resolved, so it must not clobber it.
    """
    bare = bare_entry_id(record.get("entry_id"))
    out: list[dict[str, Any]] = []
    replaced = False
    for r in records:
        if not isinstance(r, dict):
            continue
        if bare_entry_id(r.get("entry_id")) == bare:
            merged = dict(record)
            if merged.get("source") is None and r.get("source") is not None:
                merged["source"] = r.get("source")
                merged["extracted"] = r.get("extracted", merged.get("extracted"))
                merged["confidence"] = r.get("confidence", merged.get("confidence"))
            out.append(merged)
            replaced = True
        else:
            out.append(r)
    if not replaced:
        out.append(record)
    return out


def remove_record_from_list(
    records: list[dict[str, Any]], entry_id: str
) -> list[dict[str, Any]]:
    """Return ``records`` without the row for ``entry_id`` (non-dict rows dropped)."""
    bare = bare_entry_id(entry_id)
    return [
        r
        for r in records
        if isinstance(r, dict) and bare_entry_id(r.get("entry_id")) != bare
    ]
