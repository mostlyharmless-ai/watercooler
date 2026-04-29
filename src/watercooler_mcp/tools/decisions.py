"""Decision listing tools for watercooler MCP server.

Tools:
- watercooler_list_decisions: List Decision entries with filters + xref
  resolution. Surfaces both hand-authored and daemon-extracted decisions.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from fastmcp import Context
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from watercooler.baseline_graph import storage
from watercooler.baseline_graph.annotations import get_annotation_state
from watercooler.baseline_graph.storage import get_graph_dir
from watercooler.decision_extraction import DECISION_EXTRACTED_TAG

from ..errors import ContextError, HostedModeError, ValidationError
from ..observability import log_debug, log_error, log_warning
from ..validation import is_hosted_context
from .. import (
    validation,
)  # noqa: F401  # Import module for runtime access (enables test patching)

_SCHEMA_VERSION = 1
_MAX_LIMIT = 500
_CONFIDENCE_RE = re.compile(r"^Confidence:\s*(\d+)\s*/\s*5", re.MULTILINE)
_ENTRY_ID_PREFIX = "entry:"


# Module-level reference (populated on registration)
list_decisions = None


def _bare_entry_id(value: Any) -> str:
    """Return the bare ULID for an entry id, stripping the ``entry:`` prefix.

    Entries.jsonl stores node ids as ``"entry:<ULID>"`` (a baseline-graph
    convention), but annotation state and xref values carry the bare ULID.
    Lookups and cross-references must compare on the bare form; otherwise
    a Decision written by the extractor never matches its own annotation
    state and source resolution silently fails.
    """
    text = str(value or "")
    if text.startswith(_ENTRY_ID_PREFIX):
        return text[len(_ENTRY_ID_PREFIX) :]
    return text


def _parse_confidence(body: Any) -> int | None:
    """Extract `Confidence: N/5` from an extracted Decision body.

    Accepts any value; non-string or empty inputs return None. A corrupted
    entries.jsonl could carry a dict or list ``body`` — the explicit
    ``isinstance`` guard stops ``_CONFIDENCE_RE.search(<non-str>)`` from
    raising ``TypeError``.

    Values outside the documented 0..5 range are rejected. The regex only
    captures ``\\d+`` so a malformed LLM body like ``Confidence: 10/5``
    would otherwise propagate as a schema-violating ``10`` through the
    payload and defeat downstream ``confidence_min`` filtering.
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


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO 8601 string; return None on any unparseable input.

    Accepts any value. Non-string inputs (int, list, dict from a corrupted
    entries.jsonl ``timestamp`` field) return None instead of raising
    ``AttributeError`` from ``value.replace``. Extreme-but-valid inputs
    (e.g. absurd timezone offsets) can raise ``OverflowError`` on some
    Python builds; the catch clause covers that alongside ``ValueError``
    and ``TypeError``.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, OverflowError):
        return None


def _resolve_source(
    graph_dir: Path,
    topic: str,
    xrefs: list[str],
) -> dict[str, Any] | None:
    """Resolve the source-entry xref for an extracted Decision.

    The extractor writes a bidirectional xref between the Decision entry and
    its source entry. For a Decision entry the single (or first) xref is the
    source. We resolve it within the same thread first (cheap), then fall back
    to a broader lookup if needed.
    """
    if not xrefs:
        return None

    candidate_id = _bare_entry_id(xrefs[0])

    # Fast path: within same thread
    for node in storage.load_thread_entries(graph_dir, topic):
        if _bare_entry_id(node.get("id", "")) == candidate_id:
            return {
                "entry_id": candidate_id,
                "timestamp": node.get("timestamp"),
                "title": node.get("title"),
                "topic": topic,
            }

    # Slow path: cross-thread — only hit if source lives elsewhere
    for other_topic in storage.list_thread_topics(graph_dir):
        if other_topic == topic:
            continue
        for node in storage.load_thread_entries(graph_dir, other_topic):
            if _bare_entry_id(node.get("id", "")) == candidate_id:
                return {
                    "entry_id": candidate_id,
                    "timestamp": node.get("timestamp"),
                    "title": node.get("title"),
                    "topic": other_topic,
                }
    return None


def _resolve_source_from_map(
    entries_by_topic: dict[str, list[dict[str, Any]]],
    topic: str,
    xrefs: list[str],
) -> dict[str, Any] | None:
    """Hosted analogue of ``_resolve_source`` — no filesystem/API calls.

    Candidate entries are already in-memory from ``load_all_entries_hosted``,
    so we resolve the xref by scanning that map. Fast path is the Decision's
    own topic; slow path iterates the remaining topics.
    """
    if not xrefs:
        return None

    candidate_id = _bare_entry_id(xrefs[0])

    for node in entries_by_topic.get(topic, []):
        if _bare_entry_id(node.get("id", "")) == candidate_id:
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
            if _bare_entry_id(node.get("id", "")) == candidate_id:
                return {
                    "entry_id": candidate_id,
                    "timestamp": node.get("timestamp"),
                    "title": node.get("title"),
                    "topic": other_topic,
                }
    return None


def _build_decision_record(
    *,
    node: dict[str, Any],
    topic: str,
    xrefs: list[str],
    tags: list[str],
    source: dict[str, Any] | None,
    extracted: bool,
) -> dict[str, Any]:
    """Assemble the canonical decision dict returned by the tool.

    Shared by the local and hosted paths so the two implementations cannot
    drift on payload shape.
    """
    body = node.get("body", "") or ""
    confidence = _parse_confidence(body) if extracted else None
    return {
        "entry_id": _bare_entry_id(node.get("id", "")),
        "topic": topic,
        "title": node.get("title"),
        "timestamp": node.get("timestamp"),
        "agent": node.get("agent"),
        "role": node.get("role"),
        "confidence": confidence,
        "extracted": extracted,
        "source": source,
        "xrefs": [_bare_entry_id(x) for x in xrefs],
        "tags": tags,
    }


def _decision_matches_filters(
    decision: dict[str, Any],
    *,
    topic: str | None,
    confidence_min: int,
    since_dt: datetime | None,
    until_dt: datetime | None,
    source_entry_id: str | None,
    only_extracted: bool,
) -> bool:
    if topic and decision["topic"] != topic:
        return False
    if only_extracted and not decision["extracted"]:
        return False
    if confidence_min > 0:
        conf = decision.get("confidence")
        if conf is None or conf < confidence_min:
            return False
    ts = decision.get("timestamp")
    if since_dt or until_dt:
        dt = _parse_iso(ts or "")
        if dt is None:
            return False
        if since_dt and dt < since_dt:
            return False
        if until_dt and dt > until_dt:
            return False
    if source_entry_id:
        source = decision.get("source") or {}
        if source.get("entry_id") != source_entry_id:
            return False
    return True


def _finalize_payload(
    collected: list[dict[str, Any]],
    limit: int,
    skipped_topics: list[str] | None = None,
) -> ToolResult:
    """Sort, truncate, and JSON-serialise the decision list.

    ``skipped_topics`` lists topic directories that were present in the
    hosted repository but whose entries could not be loaded (missing or
    malformed ``meta.json``/``entries.jsonl``). Always present on hosted
    calls so callers can detect partial results; omitted on local calls.
    """
    collected.sort(key=lambda d: d.get("timestamp") or "", reverse=True)
    total = len(collected)
    truncated = collected[:limit]

    payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "total": total,
        "returned": len(truncated),
        "decisions": truncated,
    }
    if skipped_topics is not None:
        payload["skipped_topics"] = skipped_topics
    return ToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2))]
    )


def _list_decisions_hosted(
    *,
    topic_filter: str | None,
    confidence_min: int,
    since_dt: datetime | None,
    until_dt: datetime | None,
    source_filter: str | None,
    only_extracted: bool,
    limit: int,
) -> ToolResult:
    """Hosted (GitHub-backed) implementation of list_decisions.

    Uses ``load_all_entries_hosted`` + ``get_annotations_hosted`` so the tool
    works on hosted surfaces where the local baseline graph is not present.

    Discovery uses ``list_topic_dirs_hosted`` — the raw directory listing —
    rather than ``list_threads_hosted``. This bypasses the ``meta.json`` read
    that ``list_threads_hosted`` silently drops on failure, so a topic with a
    missing/malformed ``meta.json`` but readable ``entries.jsonl`` is still
    surfaced. Topics whose entries genuinely can't be loaded are reported to
    the caller via ``skipped_topics`` on the payload.
    """
    from ..hosted_ops import (
        get_annotations_hosted,
        list_topic_dirs_hosted,
        load_all_entries_hosted,
    )

    dirs_err, all_topic_dirs = list_topic_dirs_hosted()
    if dirs_err:
        log_error(f"list_decisions_hosted: list_topic_dirs_hosted failed: {dirs_err}")
        raise HostedModeError(
            f"Failed to enumerate threads in hosted repository: {dirs_err}",
            operation="list_decisions",
        )

    # Pass the explicit directory list so the entries load doesn't depend on
    # meta.json parsing. A single snapshot-consistent request fans out to
    # per-topic readers internally (see load_all_entries_hosted).
    err, entries_by_topic = load_all_entries_hosted(topics=all_topic_dirs)
    if err:
        log_error(f"list_decisions_hosted: load_all_entries_hosted failed: {err}")
        raise HostedModeError(
            f"Failed to load entries from hosted repository: {err}",
            operation="list_decisions",
        )

    skipped_topics = sorted(set(all_topic_dirs) - set(entries_by_topic.keys()))
    if skipped_topics:
        log_warning(
            f"list_decisions_hosted: {len(skipped_topics)} topic(s) skipped "
            f"during entries load: {skipped_topics!r}"
        )

    annotation_cache: dict[str, dict[str, dict[str, Any]]] = {}

    def _ensure_annotations(topic: str) -> dict[str, dict[str, Any]]:
        """Fetch and cache annotation state for *topic* on first access."""
        if topic in annotation_cache:
            return annotation_cache[topic]
        ann_err, ann_bundle = get_annotations_hosted(topic, target_id="")
        if ann_err:
            log_debug(
                f"list_decisions_hosted: annotations load failed for {topic}: "
                f"{ann_err}"
            )
            annotation_cache[topic] = {}
        else:
            annotation_cache[topic] = ann_bundle.get("annotation_states") or {}
        return annotation_cache[topic]

    # Pin iteration to the filter topic so annotation fetches don't fan out
    # across every decision thread in the repo. Source xref resolution always
    # uses the complete corpus map (entries_by_topic) so cross-thread sources
    # resolve regardless of where the filter is set.
    iteration_map: dict[str, list[dict[str, Any]]] = (
        {topic_filter: entries_by_topic.get(topic_filter, [])}
        if topic_filter
        else entries_by_topic
    )

    def _iter_decisions(source_map: dict[str, list[dict[str, Any]]]):
        for t, nodes in source_map.items():
            for node in nodes:
                if node.get("entry_type") == "Decision":
                    yield t, node

    collected: list[dict[str, Any]] = []
    for t, node in _iter_decisions(iteration_map):
        entry_id = _bare_entry_id(node.get("id", ""))
        state = _ensure_annotations(t).get(entry_id) or {}
        xrefs = list(state.get("xrefs") or [])
        tags = list(state.get("tags") or [])

        source = _resolve_source_from_map(entries_by_topic, t, xrefs)

        extracted = False
        if source:
            source_state = (
                _ensure_annotations(source["topic"]).get(source["entry_id"]) or {}
            )
            source_tags = source_state.get("tags") or []
            extracted = DECISION_EXTRACTED_TAG in source_tags

        decision = _build_decision_record(
            node=node,
            topic=t,
            xrefs=xrefs,
            tags=tags,
            source=source,
            extracted=extracted,
        )

        if _decision_matches_filters(
            decision,
            topic=topic_filter,
            confidence_min=confidence_min,
            since_dt=since_dt,
            until_dt=until_dt,
            source_entry_id=source_filter,
            only_extracted=only_extracted,
        ):
            collected.append(decision)

    return _finalize_payload(collected, limit, skipped_topics=skipped_topics)


def _list_decisions_impl(
    ctx: Context,
    topic: str = "",
    confidence_min: int = 0,
    since: str = "",
    until: str = "",
    source_entry_id: str = "",
    only_extracted: bool = False,
    limit: int = 50,
    code_path: str = "",
) -> ToolResult:
    """List Decision entries across threads with xref resolution.

    Surfaces both hand-authored Decisions and daemon-extracted ones. Daemon
    extractions include a confidence score (parsed from the entry body) and a
    resolved source-entry reference from the xref annotation.

    Args:
        topic: Filter to a single thread topic (empty = all threads).
        confidence_min: 0-5, minimum confidence. 0 disables the filter. Hand-
            authored Decisions (no confidence) are excluded when > 0.
        since: ISO 8601 lower bound on entry timestamp.
        until: ISO 8601 upper bound on entry timestamp.
        source_entry_id: Only return decisions extracted from this source entry.
        only_extracted: If True, exclude hand-authored Decisions (requires
            the `decision_extracted` tag on the xref'd source).
        limit: Max decisions to return (default 50, max 500).
        code_path: Path to the code repository containing threads.

    Returns:
        JSON ToolResult with `{schema_version, total, decisions: [...]}`.
    """
    try:
        if limit < 1 or limit > _MAX_LIMIT:
            raise ValidationError(
                f"limit must be between 1 and {_MAX_LIMIT}", field="limit"
            )
        if confidence_min < 0 or confidence_min > 5:
            raise ValidationError("confidence_min must be 0..5", field="confidence_min")

        error, context = validation._require_context(code_path)
        if error:
            raise ContextError(error, code_path=code_path)
        if context is None:
            raise ContextError(
                "Unable to resolve code context for the provided code_path.",
                code_path=code_path,
            )

        since_dt = _parse_iso(since) if since else None
        until_dt = _parse_iso(until) if until else None
        topic_filter = topic.strip() or None
        # Normalize to the bare ULID so callers can pass either ``entry:<ULID>``
        # (as it appears in entries.jsonl) or the bare form — both match against
        # source.entry_id, which is always bare after ``_bare_entry_id``.
        source_filter = _bare_entry_id(source_entry_id.strip()) or None

        # Hosted surfaces have no local baseline graph — delegate to the
        # GitHub-backed implementation.
        if is_hosted_context(context):
            log_debug(
                f"list_decisions: hosted path, only_extracted={only_extracted}, "
                f"confidence_min={confidence_min}"
            )
            return _list_decisions_hosted(
                topic_filter=topic_filter,
                confidence_min=confidence_min,
                since_dt=since_dt,
                until_dt=until_dt,
                source_filter=source_filter,
                only_extracted=only_extracted,
                limit=limit,
            )

        threads_dir = context.threads_dir
        graph_dir = get_graph_dir(threads_dir)

        topics_to_scan = (
            [topic_filter] if topic_filter else storage.list_thread_topics(graph_dir)
        )
        log_debug(
            f"list_decisions: scanning {len(topics_to_scan)} topic(s), "
            f"only_extracted={only_extracted}, confidence_min={confidence_min}"
        )

        collected: list[dict[str, Any]] = []
        for t in topics_to_scan:
            thread_dir = storage.get_thread_graph_dir(graph_dir, t)
            if not thread_dir.exists():
                continue

            for node in storage.load_thread_entries(graph_dir, t):
                if node.get("entry_type") != "Decision":
                    continue

                entry_id = _bare_entry_id(node.get("id", ""))

                ann_state = get_annotation_state(thread_dir, entry_id, read_only=True)
                xrefs = list(ann_state.xrefs)
                tags = list(ann_state.tags)

                source = _resolve_source(graph_dir, t, xrefs)

                # "Extracted" iff the xref'd source carries decision_extracted tag,
                # which is the marker written by ExtractDecisionsDaemon.
                extracted = False
                if source:
                    source_thread_dir = storage.get_thread_graph_dir(
                        graph_dir, source["topic"]
                    )
                    source_ann = get_annotation_state(
                        source_thread_dir, source["entry_id"], read_only=True
                    )
                    extracted = DECISION_EXTRACTED_TAG in source_ann.tags

                decision = _build_decision_record(
                    node=node,
                    topic=t,
                    xrefs=xrefs,
                    tags=tags,
                    source=source,
                    extracted=extracted,
                )

                if _decision_matches_filters(
                    decision,
                    topic=topic_filter,
                    confidence_min=confidence_min,
                    since_dt=since_dt,
                    until_dt=until_dt,
                    source_entry_id=source_filter,
                    only_extracted=only_extracted,
                ):
                    collected.append(decision)

        return _finalize_payload(collected, limit)

    except (ValidationError, ContextError, HostedModeError):
        raise
    except Exception as exc:
        log_error(f"list_decisions failed: {exc}")
        raise


def register_decisions_tools(mcp) -> None:
    """Register decision-listing tools with the MCP server."""
    global list_decisions
    list_decisions = mcp.tool(name="watercooler_list_decisions")(_list_decisions_impl)
