"""Annotation cross-reference traversal tools.

Tools:
- watercooler_follow_xref: Resolve an entry's annotation xrefs into entry
  summaries in a single call.

Motivation:
    When an agent reads a Decision (or any annotated entry), the ``xrefs``
    field on the annotation state is a list of raw entry IDs. To "follow"
    them today an agent must call ``watercooler_get_annotations`` and then
    ``watercooler_get_thread_entry`` once per xref. This convenience tool
    bundles those calls into a single round-trip and returns a list of
    summary records (entry_id, topic, title, type, role, agent, summary).

Modes:
- Local (stdio): Reads annotation state + entries.jsonl from the local
  baseline graph.
- Hosted (HTTP): Uses ``get_annotations_hosted`` and
  ``load_all_entries_hosted`` to resolve xrefs against the GitHub-backed
  thread store.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp import Context
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from ..errors import (
    ContextError,
    HostedModeError,
    ThreadNotFoundError,
    ValidationError,
)
from ..observability import log_debug, log_error, log_warning
from ..validation import is_hosted_context
from .. import (
    validation,
)  # noqa: F401  # Imported as module for runtime patching in tests.

# Module-level reference (populated on registration; kept for parity with
# the rest of the tools package and for direct test access).
follow_xref = None


_SCHEMA_VERSION = 1
_ENTRY_ID_PREFIX = "entry:"


def _bare_entry_id(value: Any) -> str:
    """Return the bare ULID for an entry id.

    Entries.jsonl stores node ids as ``"entry:<ULID>"`` while annotation
    xrefs carry the bare ULID. The two must be compared on the bare form
    or every lookup silently misses.
    """
    text = str(value or "")
    if text.startswith(_ENTRY_ID_PREFIX):
        return text[len(_ENTRY_ID_PREFIX) :]
    return text


def _entry_summary_record(
    *,
    entry_id: str,
    topic: str,
    node: dict[str, Any],
    summary: str | None = None,
) -> dict[str, Any]:
    """Build the per-xref summary record returned by the tool.

    ``node`` is an entry node dict from entries.jsonl (local or hosted
    surface — both expose the same field names). ``summary`` overrides the
    LLM-generated summary captured on the node when available; pass
    ``None`` to use the on-node value.
    """
    return {
        "entry_id": entry_id,
        "topic": topic,
        "title": node.get("title"),
        "type": node.get("entry_type"),
        "role": node.get("role"),
        "agent": node.get("agent"),
        "timestamp": node.get("timestamp"),
        "summary": summary if summary is not None else (node.get("summary") or ""),
    }


def _missing_xref_record(entry_id: str, reason: str) -> dict[str, Any]:
    """Build a placeholder record for an xref that could not be resolved.

    Surfacing missing/dangling xrefs as placeholder records (rather than
    silently dropping them) preserves the 1:1 ordering with
    ``annotation_state.xrefs`` and is what the issue requests: "skip with
    a note in the response, don't 500".
    """
    return {
        "entry_id": entry_id,
        "topic": None,
        "title": None,
        "type": None,
        "role": None,
        "agent": None,
        "timestamp": None,
        "summary": "",
        "missing": True,
        "note": reason,
    }


# ---------------------------------------------------------------------------
# Local-mode resolution
# ---------------------------------------------------------------------------


def _follow_xref_local(
    *,
    topic: str,
    target_id: str,
    context: Any,
) -> ToolResult:
    """Local (filesystem-backed) implementation of follow_xref."""
    from watercooler.baseline_graph import storage
    from watercooler.baseline_graph.annotations import get_annotation_state
    from watercooler.baseline_graph.storage import (
        get_graph_dir,
        get_thread_graph_dir,
    )

    threads_dir = context.threads_dir
    if not threads_dir:
        raise ContextError("Unable to resolve threads directory.", code_path="")

    graph_dir = get_graph_dir(threads_dir)
    source_thread_dir = get_thread_graph_dir(graph_dir, topic)
    if not source_thread_dir.exists():
        raise ThreadNotFoundError(topic=topic)

    # Read annotation state for the source entry.
    bare_target = _bare_entry_id(target_id)
    try:
        state = get_annotation_state(source_thread_dir, bare_target, read_only=True)
    except Exception as exc:
        log_error(
            f"follow_xref(local): annotation read failed for {topic}/{bare_target}: {exc}"
        )
        raise ContextError(f"Failed to read annotation state: {exc}", code_path="")

    xrefs = [_bare_entry_id(x) for x in (state.xrefs or [])]

    if not xrefs:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "topic": topic,
            "target_id": bare_target,
            "count": 0,
            "xrefs": [],
        }
        return ToolResult(
            content=[TextContent(type="text", text=json.dumps(payload, indent=2))]
        )

    # Build a lookup index across topics on demand. Most xrefs target the
    # same thread, so try the source topic first before scanning others.
    topics_to_scan = [topic]
    other_topics = [t for t in storage.list_thread_topics(graph_dir) if t != topic]

    pending: set[str] = set(xrefs)
    found: dict[str, tuple[str, dict[str, Any]]] = {}

    def _index_topic(t: str) -> None:
        if not pending:
            return
        try:
            for node in storage.load_thread_entries(graph_dir, t):
                eid = _bare_entry_id(node.get("id", "")) or _bare_entry_id(
                    node.get("entry_id", "")
                )
                if eid and eid in pending:
                    found[eid] = (t, node)
                    pending.discard(eid)
                if not pending:
                    break
        except Exception as exc:  # pragma: no cover — defensive
            log_warning(f"follow_xref(local): failed to load entries for {t}: {exc}")

    for t in topics_to_scan + other_topics:
        _index_topic(t)
        if not pending:
            break

    # Build records preserving original xref order so callers can pair the
    # response 1:1 with annotation_state.xrefs.
    results: list[dict[str, Any]] = []
    for xref_id in xrefs:
        entry = found.get(xref_id)
        if entry is None:
            results.append(
                _missing_xref_record(
                    xref_id,
                    "xref entry_id not found in any thread on the local baseline graph",
                )
            )
            continue
        host_topic, node = entry
        results.append(
            _entry_summary_record(
                entry_id=xref_id,
                topic=host_topic,
                node=node,
            )
        )

    payload = {
        "schema_version": _SCHEMA_VERSION,
        "topic": topic,
        "target_id": bare_target,
        "count": len(results),
        "xrefs": results,
    }
    return ToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2))]
    )


# ---------------------------------------------------------------------------
# Hosted-mode resolution
# ---------------------------------------------------------------------------


def _follow_xref_hosted(
    *,
    topic: str,
    target_id: str,
) -> ToolResult:
    """Hosted (GitHub-backed) implementation of follow_xref."""
    from ..hosted_ops import (
        get_annotations_hosted,
        list_topic_dirs_hosted,
        load_all_entries_hosted,
    )

    bare_target = _bare_entry_id(target_id)

    # Read annotation state for the source entry.
    ann_err, ann_bundle = get_annotations_hosted(topic, target_id=bare_target)
    if ann_err:
        log_error(
            f"follow_xref(hosted): annotations load failed for {topic}: {ann_err}"
        )
        raise HostedModeError(
            f"Failed to read annotations: {ann_err}", operation="follow_xref"
        )

    state = ann_bundle.get("annotation_state") or {}
    xrefs = [_bare_entry_id(x) for x in (state.get("xrefs") or [])]

    if not xrefs:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "topic": topic,
            "target_id": bare_target,
            "count": 0,
            "xrefs": [],
        }
        return ToolResult(
            content=[TextContent(type="text", text=json.dumps(payload, indent=2))]
        )

    # Discover topics, then load entries across them so we can resolve
    # cross-thread xrefs. ``load_all_entries_hosted`` fans out to one
    # request per topic; if the source topic carries every xref the
    # network cost is the same as a focused load_entries_hosted call.
    dirs_err, all_topics = list_topic_dirs_hosted()
    if dirs_err:
        log_error(f"follow_xref(hosted): list_topic_dirs_hosted failed: {dirs_err}")
        raise HostedModeError(
            f"Failed to enumerate hosted threads: {dirs_err}",
            operation="follow_xref",
        )

    topics_to_scan = sorted(set(all_topics) | {topic})

    err, entries_by_topic = load_all_entries_hosted(topics=topics_to_scan)
    if err:
        log_error(f"follow_xref(hosted): load_all_entries_hosted failed: {err}")
        raise HostedModeError(
            f"Failed to load hosted entries: {err}", operation="follow_xref"
        )

    # Build a (entry_id → (topic, node)) lookup once. Local mode lazily
    # short-circuits per-topic; here we already paid for every topic so
    # building the index up front is simpler and equivalent in cost.
    lookup: dict[str, tuple[str, dict[str, Any]]] = {}
    # Walk source topic first so duplicates (very rare) prefer same-thread
    # resolution. This matches local mode's bias.
    ordered_topics = [topic] + [t for t in entries_by_topic if t != topic]
    for t in ordered_topics:
        for node in entries_by_topic.get(t, []):
            eid = _bare_entry_id(node.get("id", "")) or _bare_entry_id(
                node.get("entry_id", "")
            )
            if eid and eid not in lookup:
                lookup[eid] = (t, node)

    results: list[dict[str, Any]] = []
    for xref_id in xrefs:
        entry = lookup.get(xref_id)
        if entry is None:
            results.append(
                _missing_xref_record(
                    xref_id,
                    "xref entry_id not found in any thread in the hosted repository",
                )
            )
            continue
        host_topic, node = entry
        results.append(
            _entry_summary_record(
                entry_id=xref_id,
                topic=host_topic,
                node=node,
            )
        )

    payload = {
        "schema_version": _SCHEMA_VERSION,
        "topic": topic,
        "target_id": bare_target,
        "count": len(results),
        "xrefs": results,
    }
    return ToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2))]
    )


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


def _follow_xref_impl(
    ctx: Context,
    topic: str = "",
    target_id: str = "",
    code_path: str = "",
) -> ToolResult:
    """Resolve an entry's annotation xrefs into entry summaries.

    Reads the annotation state for ``target_id`` in ``topic`` and returns
    a list of summary records — one per xref — so an agent does not have
    to follow up with a separate ``watercooler_get_thread_entry`` call
    for each cross-reference.

    Args:
        ctx: MCP request context (unused; reserved for future hooks).
        topic: Thread topic that contains the source entry.
        target_id: Entry ID (ULID, with or without ``entry:`` prefix) whose
            xrefs should be resolved.
        code_path: Path to the code repository root. Required in local
            mode; ignored in hosted mode.

    Returns:
        ``ToolResult`` whose text payload is JSON of shape::

            {
              "schema_version": 1,
              "topic": "<source topic>",
              "target_id": "<bare ULID>",
              "count": <number of xref records>,
              "xrefs": [
                {
                  "entry_id": "...",
                  "topic": "...",   # null when missing=True
                  "title": "...",
                  "type": "Decision",
                  "role": "implementer",
                  "agent": "Claude (user)",
                  "timestamp": "2026-04-20T10:00:00Z",
                  "summary": "...",
                  ...
                  // For unresolved xrefs:
                  "missing": true,
                  "note": "xref entry_id not found ..."
                }
              ]
            }

    Behaviour:
        - Empty xrefs list → ``count=0`` and an empty array (never an
          error, never a 500).
        - An xref pointing to an unknown entry_id is returned as a
          ``missing=True`` placeholder record with a human-readable note
          rather than aborting the call.
        - Output ordering mirrors ``annotation_state.xrefs`` so callers
          can pair the two arrays positionally.
    """
    if not topic:
        raise ValidationError("topic is required", field="topic")
    if not target_id:
        raise ValidationError("target_id is required", field="target_id")

    error, context = validation._require_context(code_path)
    if error:
        raise ContextError(error, code_path=code_path)
    if context is None:
        raise ContextError(
            "Unable to resolve code context for the provided code_path.",
            code_path=code_path,
        )

    if is_hosted_context(context):
        log_debug(f"follow_xref: hosted path topic={topic!r} target_id={target_id!r}")
        return _follow_xref_hosted(topic=topic, target_id=target_id)

    log_debug(f"follow_xref: local path topic={topic!r} target_id={target_id!r}")
    return _follow_xref_local(topic=topic, target_id=target_id, context=context)


def register_annotations_xref_tools(mcp) -> None:
    """Register annotation xref-traversal tools with the MCP server."""
    global follow_xref
    follow_xref = mcp.tool(name="watercooler_follow_xref")(_follow_xref_impl)
