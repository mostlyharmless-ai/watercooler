"""Decision listing tools for watercooler MCP server.

Tools:
- watercooler_list_decisions: List Decision entries with filters + xref
  resolution. Surfaces both hand-authored and daemon-extracted decisions.
- watercooler_list_pending_candidates: List open candidate Notes awaiting
  human judgment (C1 ask, thread candidate-research-backend-support) — the
  server-authoritative feed for dashboard candidate queues.
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
from watercooler.promotion import (
    candidate_expires_at,
    parse_candidate_body,
    resolve_candidate_state,
)

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

# Cap on edges returned by the single group-agnostic edges-by-episode query
# (#894) that backs supersession for a whole decisions page. The query already
# filters to edges derived from the page's episodes, so this bounds only a
# pathological fan-out (an episode mentioned by an unusually large number of
# facts), not a whole thread's edges.
_SUPERSESSION_EDGE_LIMIT = 2000


# Module-level references (populated on registration)
list_decisions = None
list_pending_candidates = None


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
        # Provenance scalar written onto the entry node at write/promotion time
        # (e.g. "human_promoted" via build_promotion_authority_fields, "agent_authored"
        # for hand-authored). None for legacy/unstamped Decisions — surfaced as-is so a
        # decision_origin filter can scope to promoted Decisions without counting legacy
        # records as promoted.
        "decision_origin": node.get("decision_origin"),
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
    decision_origin: str | None = None,
) -> bool:
    if topic and decision["topic"] != topic:
        return False
    if only_extracted and not decision["extracted"]:
        return False
    if decision_origin and decision.get("decision_origin") != decision_origin:
        # Legacy/unstamped Decisions carry decision_origin=None and are excluded by
        # any non-empty filter — never counted as the requested origin.
        return False
    if confidence_min > 0:
        conf = decision.get("confidence")
        if conf is None or conf < confidence_min:
            return False
    ts = decision.get("timestamp")
    if since_dt or until_dt:
        dt = _parse_iso(ts or "")
        if dt is None:
            # Unparseable-timestamp records are silently dropped under a window
            # filter. The deferred early_supersession_hazard denominator contract
            # (#897a producer phase) must account for this: such records vanish from
            # promoted_total before coverage is computed.
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
    index_status: str | None = None,
) -> ToolResult:
    """Sort, truncate, and JSON-serialise the decision list.

    ``skipped_topics`` lists topic directories that were present in the
    hosted repository but whose entries could not be loaded (missing or
    malformed ``meta.json``/``entries.jsonl``). Always present on hosted
    calls so callers can detect partial results; omitted on local calls.

    ``index_status`` (hosted only) records how the read was served:
    ``"used"`` (decisions index), ``"missing"`` (no index → full per-thread
    scan fallback), or ``"error"`` (index load failed → fallback). Surfaced
    under ``meta`` so existing top-level keys are untouched.
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
    if index_status is not None:
        payload["meta"] = {"index_status": index_status}
    return ToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2))]
    )


def _unknown_supersession(reason: str) -> dict[str, Any]:
    """A supersession summary for the cases where T2 cannot answer.

    Distinct from ``in_force`` on purpose: a missing T2 signal must never read
    as a positive "still in force" assertion (epistemic-custody §6.5).
    """
    return {
        "state": "unknown",
        "active_facts": 0,
        "superseded_facts": 0,
        "as_of": None,
        "reason": reason,
    }


def _acquire_graphiti_backend(code_path: str):
    """Best-effort acquire the T2 (Graphiti) backend for supersession lookups.

    Returns ``None`` when T2 is not configured/enabled or the backend cannot be
    initialized — the caller renders ``supersession: unknown`` rather than a
    false ``in_force``.
    """
    try:
        from .. import memory as mem

        config = mem.load_graphiti_config(code_path=code_path or None)
        if not config:
            return None
        backend = mem.get_graphiti_backend(config)
        if backend is None or isinstance(backend, dict):
            return None
        return backend
    except Exception as exc:  # backend acquisition is best-effort enrichment
        log_debug(f"list_decisions: graphiti backend acquire failed: {exc}")
        return None


def _supersession_is_ratified(
    topic: str, entry_id: str, successor_entry_id: str, code_path: str
) -> bool:
    """Authored iff an ``xref_supersedes`` annotation records ``entry_id`` → successor.

    The durable, append-only authored record of a ratified supersession (earned-edge
    RFC P3) — replacing the removed mutable T2 ``superseded_ratified`` flag. Degrades to
    ``False`` (afforded) on any resolution failure — never a false ``authored`` (§6.5).
    """
    if not topic or not successor_entry_id:
        return False
    try:
        error, context = validation._require_context(code_path)
        if error or context is None:
            return False

        # Hosted: annotations live on GitHub (append_annotation_hosted), not a local
        # filesystem graph — so read them back the same way, else a hosted ratification
        # never flips the badge to authored (the local path always misses in hosted).
        if is_hosted_context(context):
            from ..hosted_ops import get_annotations_hosted

            read_err, result = get_annotations_hosted(topic, entry_id)
            if read_err or not isinstance(result, dict):
                return False
            state = result.get("annotation_state") or {}
            return successor_entry_id in (state.get("xref_supersedes") or [])

        # Local: filesystem-backed baseline graph.
        from watercooler.baseline_graph.annotations import get_annotation_state
        from watercooler.baseline_graph.storage import (
            get_graph_dir,
            get_thread_graph_dir,
        )

        if not context.threads_dir:
            return False
        thread_dir = get_thread_graph_dir(get_graph_dir(context.threads_dir), topic)
        state = get_annotation_state(thread_dir, entry_id, read_only=True)
        return successor_entry_id in (getattr(state, "xref_supersedes", None) or [])
    except Exception as exc:
        log_debug(f"list_decisions: xref_supersedes read failed for {entry_id}: {exc}")
        return False


def _apply_supersession(
    collected: list[dict[str, Any]], backend, code_path: str = ""
) -> None:
    """Attach a T2-derived ``supersession`` summary to each decision in place.

    ``backend`` is an acquired GraphitiBackend (or ``None``). The whole page
    costs a single group-agnostic edges-by-episode query: episodes are ingested
    under the repo/project group (not per-thread), so supersession must be keyed
    on episode membership, not the thread topic (#894 P2#1). Any failure
    degrades a decision to an honest ``unknown`` — never a false ``in_force``
    (epistemic-custody §6.5).
    """
    if backend is None:
        for decision in collected:
            decision["supersession"] = _unknown_supersession("t2_unavailable")
        return

    # Import only after the None-check: watercooler_memory is private and
    # absent from the open-core build, where backend acquisition always
    # yields None — importing first made this degrade path unreachable on
    # public installs (ModuleNotFoundError instead of honest unknown).
    from watercooler_memory.supersession import summarize_supersession

    # Recover the entry→episode index when a node-local cache was wiped (a hosted
    # ephemeral-filesystem redeploy empties it). Baseline-sync episodes carry no
    # entry_id in their fields, but each episode's valid_at == the entry
    # timestamp, so pass this page's {timestamp: entry_id} hints to rebuild the
    # mapping from the surviving episodes — no LLM re-extraction.
    #
    # Trigger whenever ANY collected Decision is missing from the index — NOT
    # only when the whole cache is empty. Timestamp matching is the only way
    # baseline-sync episodes are recoverable, so a first topic-scoped/filtered
    # page must not leave later Decisions stranded at no_episode_mapping once the
    # index is non-empty (review #1012).
    try:
        idx = getattr(backend, "entry_episode_index", None)
        if idx is not None:
            ts_hints = {
                d["timestamp"]: d["entry_id"]
                for d in collected
                if d.get("timestamp") and d.get("entry_id")
            }
            needs_recovery = bool(ts_hints) and any(
                not idx.has_any_mapping(d["entry_id"])
                for d in collected
                if d.get("entry_id")
            )
            if needs_recovery:
                backend.rebuild_entry_episode_index_from_graph(
                    timestamp_to_entry_id=ts_hints
                )
    except Exception as exc:
        log_debug(f"list_decisions: entry-episode index recovery skipped: {exc}")

    # Resolve each decision's episode UUID(s) (best-effort; never raises) and
    # collect the union so one query covers the page.
    episodes_by_id: dict[str, list[str]] = {}
    all_episodes: set[str] = set()
    for decision in collected:
        eps = backend.episode_uuids_for_entry(decision["entry_id"])
        episodes_by_id[decision["entry_id"]] = eps
        all_episodes.update(eps)

    try:
        edges = (
            backend.get_edges_by_episodes(
                sorted(all_episodes), limit=_SUPERSESSION_EDGE_LIMIT
            )
            if all_episodes
            else []
        )
    except Exception as exc:
        log_debug(f"list_decisions: supersession edge fetch failed: {exc}")
        edges = None

    for decision in collected:
        if edges is None:
            decision["supersession"] = _unknown_supersession("lookup_error")
            continue
        summary = summarize_supersession(edges, episodes_by_id[decision["entry_id"]])
        # RFC §3b: surface the successor ENTRY so the dashboard can render a clickable
        # "superseded by → jump to entry" badge. Direction = superseded_by (#991)
        # resolved to an entry; best-effort — never downgrades the state summary.
        if summary.get("state") in ("superseded", "partially_superseded"):
            try:
                succ = backend.get_superseding_entry(decision["entry_id"])
                if succ:
                    summary["superseded_by"] = succ["entry_id"]
                    summary["superseded_by_thread"] = succ.get("thread")
                    # Authored = an xref_supersedes annotation records this A→B link
                    # (RFC P3); afforded otherwise. Replaces the removed T2 flag.
                    summary["superseded_by_ratified"] = _supersession_is_ratified(
                        decision.get("topic", ""), decision["entry_id"],
                        succ["entry_id"], code_path,
                    )
            except Exception as exc:
                log_debug(f"list_decisions: superseding-entry resolution failed: {exc}")
        decision["supersession"] = summary


def _list_decisions_from_index(
    index_records: list[dict[str, Any]],
    *,
    topic_filter: str | None,
    confidence_min: int,
    since_dt: datetime | None,
    until_dt: datetime | None,
    source_filter: str | None,
    only_extracted: bool,
    limit: int,
    include_supersession: bool = False,
    decision_origin: str | None = None,
) -> ToolResult:
    """Build the list_decisions payload from the repo-level decisions index.

    The index already carries each Decision's *resolved source* and
    ``extracted`` flag, so cross-thread source resolution is O(1) and there is
    no per-thread ``entries.jsonl`` fan-out (the rate-limit cause). Only the
    Decision's own mutable ``tags``/``xrefs`` are fetched live, and only for the
    decision-bearing topics that survive the topic filter.
    """
    from ..hosted_ops import get_annotations_hosted

    annotation_cache: dict[str, dict[str, dict[str, Any]]] = {}

    def _ensure_annotations(topic: str) -> dict[str, dict[str, Any]]:
        if topic in annotation_cache:
            return annotation_cache[topic]
        ann_err, ann_bundle = get_annotations_hosted(topic, target_id="")
        if ann_err:
            log_debug(
                f"list_decisions_from_index: annotations load failed for "
                f"{topic}: {ann_err}"
            )
            annotation_cache[topic] = {}
        else:
            annotation_cache[topic] = ann_bundle.get("annotation_states") or {}
        return annotation_cache[topic]

    collected: list[dict[str, Any]] = []
    for rec in index_records:
        topic = rec.get("topic") or ""
        if topic_filter and topic != topic_filter:
            continue
        entry_id = rec.get("entry_id") or ""
        state = _ensure_annotations(topic).get(entry_id) or {}
        xrefs = [_bare_entry_id(x) for x in (state.get("xrefs") or [])]
        tags = list(state.get("tags") or [])

        # The index record IS the read payload minus the mutable annotation
        # state (tags/xrefs); graft the live values back on. Source + extracted
        # + confidence already come pre-resolved from the index.
        decision = {**rec, "xrefs": xrefs, "tags": tags}

        if _decision_matches_filters(
            decision,
            topic=topic_filter,
            confidence_min=confidence_min,
            since_dt=since_dt,
            until_dt=until_dt,
            source_entry_id=source_filter,
            only_extracted=only_extracted,
            decision_origin=decision_origin,
        ):
            collected.append(decision)

    if include_supersession:
        _apply_supersession(collected, _acquire_graphiti_backend(""))

    return _finalize_payload(
        collected, limit, skipped_topics=[], index_status="used"
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
    include_supersession: bool = False,
    decision_origin: str | None = None,
) -> ToolResult:
    """Hosted (GitHub-backed) implementation of list_decisions.

    Fast path: read the repo-level decisions index in one fetch
    (``load_decision_index_hosted`` → ``_list_decisions_from_index``).

    Fallback (index absent on older/pre-backfill repos, or a load error): the
    legacy full per-thread scan via ``load_all_entries_hosted`` +
    ``get_annotations_hosted``. Discovery uses ``list_topic_dirs_hosted`` — the
    raw directory listing — rather than ``list_threads_hosted``, so a topic with
    a missing/malformed ``meta.json`` but readable ``entries.jsonl`` is still
    surfaced. Topics whose entries genuinely can't be loaded are reported via
    ``skipped_topics``. The fallback path is marked ``meta.index_status`` =
    ``"missing"``/``"error"`` so the (slow, rate-limit-prone) path is observable.
    """
    from ..hosted_ops import (
        get_annotations_hosted,
        list_topic_dirs_hosted,
        load_all_entries_hosted,
        load_decision_index_hosted,
    )

    # Fast path: single-fetch decisions index. ``index_records is not None``
    # means the index file exists (possibly empty); ``None`` means absent (404)
    # or a load error — both fall back to the full scan below.
    idx_err, index_records = load_decision_index_hosted()
    if index_records is not None:
        return _list_decisions_from_index(
            index_records,
            topic_filter=topic_filter,
            confidence_min=confidence_min,
            since_dt=since_dt,
            until_dt=until_dt,
            source_filter=source_filter,
            only_extracted=only_extracted,
            limit=limit,
            include_supersession=include_supersession,
            decision_origin=decision_origin,
        )
    fallback_status = "error" if idx_err else "missing"
    if idx_err:
        log_warning(
            "list_decisions_hosted: decisions index load failed, falling back "
            f"to full per-thread scan: {idx_err}"
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
            decision_origin=decision_origin,
        ):
            collected.append(decision)

    if include_supersession:
        # The hosted server is co-located with its T2 (e.g. Railway runs FalkorDB
        # alongside the MCP), so query it directly. ``is_hosted_context`` only means
        # "list threads via the GitHub API" — it does NOT imply "no T2". Acquisition
        # is best-effort: a genuinely T2-less hosted surface (no FalkorDB) yields an
        # honest ``unknown`` via _apply_supersession, never a false in_force (§6.5).
        # Pass no code_path on purpose: in hosted-request scope the per-tenant T2
        # database derives from ``http_ctx.repo`` (the multi-tenant boundary), which
        # must dominate any caller-supplied path — so a request can never steer
        # supersession reads into another tenant's graph.
        _apply_supersession(collected, _acquire_graphiti_backend(""))

    return _finalize_payload(
        collected, limit, skipped_topics=skipped_topics, index_status=fallback_status
    )


def _list_decisions_impl(
    ctx: Context,
    topic: str = "",
    confidence_min: int = 0,
    since: str = "",
    until: str = "",
    source_entry_id: str = "",
    only_extracted: bool = False,
    limit: int = 50,
    include_supersession: bool = False,
    decision_origin: str = "",
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
        decision_origin: If set, only return Decisions whose entry-node
            `decision_origin` provenance scalar matches exactly (e.g.
            `"human_promoted"` for candidate-promoted Decisions, `"agent_authored"`
            for hand-authored). Legacy/unstamped Decisions carry no `decision_origin`
            and are excluded by any non-empty filter — they are never counted as the
            requested origin. Empty (default) disables the filter. A pure
            `decision_origin` filter is a baseline read (it does not route to T2).
        limit: Max decisions to return (default 50, max 500).
        include_supersession: If True, attach a ``supersession`` summary to each
            decision from the T2 (Graphiti) temporal graph — so a consumer can
            see whether a Decision's derived facts are still in force, never
            mistaking a superseded record for a current one (the warrant ledger
            travels with supersession status). Each summary is
            ``{state, active_facts, superseded_facts, as_of, reason}`` where
            ``state`` is ``in_force`` / ``partially_superseded`` / ``superseded``
            / ``unknown``. ``unknown`` (T2 disabled, hosted, no episode mapping,
            or lookup error — see ``reason``) is never a false ``in_force``.
            Off by default: it issues T2 queries, so it adds cost.
        code_path: Path to the code repository containing threads.

    Returns:
        JSON ToolResult with `{schema_version, total, decisions: [...]}`. Each
        decision carries a ``supersession`` field only when
        ``include_supersession`` is True.
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
        origin_filter = decision_origin.strip() or None

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
                include_supersession=include_supersession,
                decision_origin=origin_filter,
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
                    decision_origin=origin_filter,
                ):
                    collected.append(decision)

        if include_supersession:
            _apply_supersession(collected, _acquire_graphiti_backend(code_path), code_path)

        return _finalize_payload(collected, limit)

    except (ValidationError, ContextError, HostedModeError):
        raise
    except Exception as exc:
        log_error(f"list_decisions failed: {exc}")
        raise


# ---------------------------------------------------------------------------
# Pending candidates (C1, thread candidate-research-backend-support)
# ---------------------------------------------------------------------------

# The candidate-status marker value that means "awaiting human judgment"
# (decision_extraction.format_candidate_note_body). A candidate's own body is
# append-only, so pending-ness is this status MINUS a terminal disposition.
_PENDING_STATUS = "needs_human_confirmation"


def _effective_candidate_ttl_days() -> int:
    """The configured F1 TTL ([mcp.daemons.learnings].candidate_ttl_days).

    One resolution shared by the listing's ``expires_at`` and (via config) the
    sweep, so the API can never report an expiry date the sweep won't honor.
    """
    try:
        from watercooler.config_loader import load_config

        return int(load_config().mcp.daemons.learnings.candidate_ttl_days)
    except Exception:  # noqa: BLE001 — config trouble degrades to the default
        from watercooler.promotion import DEFAULT_CANDIDATE_TTL_DAYS

        return DEFAULT_CANDIDATE_TTL_DAYS


def _collect_pending_for_topic(
    topic: str,
    nodes: list[dict[str, Any]],
    *,
    include_expired: bool = False,
    thread_ball: str | None = None,
    ttl_days: int | None = None,
) -> list[dict[str, Any]]:
    """Pending-candidate records for one thread's entry nodes.

    A pending candidate is a Note whose body carries
    ``Candidate-Status: needs_human_confirmation`` and whose resolved
    lifecycle state (``resolve_candidate_state``: state-machine fold over the
    thread's entries — MCP ``Disposition-Target:`` marker, the dashboard's
    ``Candidate-Entry:`` marker, or a ``Promoted-From:``-stamped promoted
    entry) is ``pending``. With ``include_expired``, dormant ``expired``
    candidates are included too (their ``state`` field distinguishes them);
    ``promoted``/``rejected`` are never listed.

    F1 lifecycle fields per record: ``state``, ``expires_at``
    (emission + TTL), ``disposition_owner`` and ``owner_source`` —
    the immutable ``Disposition-Owner:`` emission stamp when present
    (``emission_stamp``), else the source thread's current ball-holder
    (``ball_holder``; ``unavailable`` when the caller has no thread meta,
    e.g. hosted scans).
    """
    listed_states = {"pending", "expired"} if include_expired else {"pending"}
    # Resolve the effective TTL once per topic (config-backed unless the caller
    # pins it) so every row's expires_at reflects the policy the sweep enforces.
    effective_ttl = (
        ttl_days if ttl_days is not None else _effective_candidate_ttl_days()
    )
    pending: list[dict[str, Any]] = []
    for node in nodes:
        if node.get("entry_type") != "Note":
            continue
        body = node.get("body")
        if not isinstance(body, str) or "Candidate-Status:" not in body:
            continue
        entry_id = _bare_entry_id(node.get("id", ""))
        if not entry_id:
            continue
        meta = parse_candidate_body(body, entry_id, topic)
        if meta.candidate_status != _PENDING_STATUS:
            continue
        state = resolve_candidate_state(entry_id, nodes).state
        if state not in listed_states:
            continue
        if meta.disposition_owner:
            owner, owner_source = meta.disposition_owner, "emission_stamp"
        elif thread_ball:
            owner, owner_source = thread_ball, "ball_holder"
        else:
            owner, owner_source = None, "unavailable"
        pending.append(
            {
                "entry_id": entry_id,
                "topic": topic,
                "index": node.get("index"),
                "title": node.get("title") or "",
                "timestamp": node.get("timestamp") or "",
                "agent": node.get("agent") or "",
                "candidate_type": meta.candidate_type,
                "surface_kind": meta.surface_kind,
                "confidence": meta.confidence,
                "source_entry_id": (
                    _bare_entry_id(meta.source_entry_id)
                    if meta.source_entry_id
                    else None
                ),
                "state": state,
                "expires_at": candidate_expires_at(
                    node.get("timestamp") or "", effective_ttl
                ),
                "disposition_owner": owner,
                "owner_source": owner_source,
            }
        )
    return pending


def _finalize_candidates_payload(
    collected: list[dict[str, Any]],
    limit: int,
    skipped_topics: list[str] | None = None,
) -> ToolResult:
    """Sort (newest first), truncate, and JSON-serialise the candidate list."""
    collected.sort(key=lambda c: c.get("timestamp") or "", reverse=True)
    total = len(collected)
    truncated = collected[:limit]
    payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "total": total,
        "returned": len(truncated),
        "candidates": truncated,
    }
    if skipped_topics is not None:
        payload["skipped_topics"] = skipped_topics
    return ToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2))]
    )


def _list_pending_candidates_hosted(
    *, topic_filter: str | None, limit: int, include_expired: bool = False
) -> ToolResult:
    """Hosted (GitHub-backed) implementation of list_pending_candidates.

    There is no candidates index (unlike decisions), so this is always the
    per-thread scan: ``list_topic_dirs_hosted`` for discovery, then
    ``load_all_entries_hosted`` (one snapshot-consistent fan-out; topics whose
    entries can't load are reported via ``skipped_topics``, mirroring
    ``_list_decisions_hosted``'s fallback path). With ``topic_filter`` set,
    only that topic is loaded — the cheap path dashboards should prefer when
    they already know the thread.
    """
    from ..hosted_ops import list_topic_dirs_hosted, load_all_entries_hosted

    if topic_filter:
        topics = [topic_filter]
    else:
        dirs_err, topics = list_topic_dirs_hosted()
        if dirs_err:
            log_error(
                f"list_pending_candidates_hosted: list_topic_dirs_hosted "
                f"failed: {dirs_err}"
            )
            raise HostedModeError(
                f"Failed to enumerate threads in hosted repository: {dirs_err}",
                operation="list_pending_candidates",
            )

    err, entries_by_topic = load_all_entries_hosted(topics=topics)
    if err:
        log_error(
            f"list_pending_candidates_hosted: load_all_entries_hosted "
            f"failed: {err}"
        )
        raise HostedModeError(
            f"Failed to load entries from hosted repository: {err}",
            operation="list_pending_candidates",
        )

    skipped_topics = sorted(set(topics) - set(entries_by_topic.keys()))
    if skipped_topics:
        log_warning(
            f"list_pending_candidates_hosted: {len(skipped_topics)} topic(s) "
            f"skipped during entries load: {skipped_topics!r}"
        )

    from ..hosted_ops import load_thread_metadata_hosted

    collected: list[dict[str, Any]] = []
    for t, nodes in entries_by_topic.items():
        # F1 historical-owner fallback works on hosted too: the thread's ball
        # comes from hosted metadata (stamp still wins when present).
        thread_ball: str | None = None
        try:
            meta_err, t_meta = load_thread_metadata_hosted(t)
            if not meta_err and isinstance(t_meta, dict):
                thread_ball = (t_meta.get("ball") or "").strip() or None
        except Exception:  # noqa: BLE001 — owner fallback is best-effort
            thread_ball = None
        collected.extend(
            _collect_pending_for_topic(
                t, nodes, include_expired=include_expired, thread_ball=thread_ball
            )
        )

    return _finalize_candidates_payload(collected, limit, skipped_topics=skipped_topics)


def _list_pending_candidates_impl(
    ctx: Context,
    topic: str = "",
    limit: int = 50,
    code_path: str = "",
    include_expired: bool = False,
) -> ToolResult:
    """List open candidate Notes awaiting human judgment, across threads.

    A candidate Note's body is append-only — its ``Candidate-Status`` stays
    ``needs_human_confirmation`` forever — so "pending" is computed here as
    status match MINUS a terminal disposition: a ``CandidateDisposition:
    promoted|rejected`` Note referencing the candidate via
    ``Disposition-Target:`` (MCP promote path) or ``Candidate-Entry:`` (the
    dashboard judgment route), or a ``Promoted-From:``-stamped promoted entry
    (#886). Non-terminal dispositions (keep_exploring, reframe) leave a
    candidate pending by design (§5.4).

    This is the server-authoritative feed for candidate-review queues (C1,
    thread ``candidate-research-backend-support``) — dashboards previously
    composed it client-side from loaded thread bodies, which cannot see
    beyond loaded threads. Pure L1 read: asserts no authority, mutates
    nothing.

    Args:
        topic: Filter to a single thread topic (empty = all threads). Prefer
            setting it when the thread is known — hosted mode then loads one
            topic instead of fanning out across the repository.
        limit: Max candidates to return (default 50, max 500).
        code_path: Path to the code repository containing threads.
        include_expired: Also list dormant ``expired`` candidates (TTL-swept;
            still directly promotable). Default False — only ``pending``.

    Returns:
        JSON ToolResult ``{schema_version, total, returned, candidates: [...]}``,
        newest first. Each candidate carries ``entry_id``, ``topic``, ``index``,
        ``title``, ``timestamp``, ``agent``, ``candidate_type``,
        ``surface_kind``, ``confidence`` (0-5 or null), ``source_entry_id``
        (bare ULID of the entry it was extracted from, or null), and the F1
        lifecycle fields ``state`` (pending|expired), ``expires_at``,
        ``disposition_owner`` + ``owner_source`` (emission_stamp |
        ball_holder | unavailable).
        ``skipped_topics`` is present on hosted calls so callers can detect
        partial results.
    """
    try:
        if limit < 1 or limit > _MAX_LIMIT:
            raise ValidationError(
                f"limit must be between 1 and {_MAX_LIMIT}", field="limit"
            )

        error, context = validation._require_context(code_path)
        if error:
            raise ContextError(error, code_path=code_path)
        if context is None:
            raise ContextError(
                "Unable to resolve code context for the provided code_path.",
                code_path=code_path,
            )

        topic_filter = topic.strip() or None

        if is_hosted_context(context):
            log_debug(
                f"list_pending_candidates: hosted path, topic={topic_filter!r}"
            )
            return _list_pending_candidates_hosted(
                topic_filter=topic_filter,
                limit=limit,
                include_expired=include_expired,
            )

        threads_dir = context.threads_dir
        graph_dir = get_graph_dir(threads_dir)

        topics_to_scan = (
            [topic_filter] if topic_filter else storage.list_thread_topics(graph_dir)
        )
        log_debug(
            f"list_pending_candidates: scanning {len(topics_to_scan)} topic(s)"
        )

        collected: list[dict[str, Any]] = []
        for t in topics_to_scan:
            thread_dir = storage.get_thread_graph_dir(graph_dir, t)
            if not thread_dir.exists():
                continue
            nodes = list(storage.load_thread_entries(graph_dir, t))
            # F1 owner fallback: the source thread's current ball-holder,
            # used only when the candidate carries no Disposition-Owner stamp.
            meta = storage.load_thread_meta(graph_dir, t) or {}
            collected.extend(
                _collect_pending_for_topic(
                    t,
                    nodes,
                    include_expired=include_expired,
                    thread_ball=(meta.get("ball") or None),
                )
            )

        return _finalize_candidates_payload(collected, limit)

    except (ValidationError, ContextError, HostedModeError):
        raise
    except Exception as exc:
        log_error(f"list_pending_candidates failed: {exc}")
        raise


def _build_hybrid_list_decisions_wrapper(runtime):
    """Build a hybrid wrapper for ``watercooler_list_decisions``.

    Default list_decisions is a baseline read and stays local. When
    include_supersession=True, the call needs T2/Graphiti and routes through
    memory_query in hybrid instead of trying to acquire local Graphiti.
    """
    import functools
    from ..capabilities import tool_capability

    @functools.wraps(_list_decisions_impl)
    async def _hybrid_list_decisions(ctx, **kwargs):
        capability = tool_capability("watercooler_list_decisions", kwargs)
        target = runtime.capability_profile.resolve_execution_target(
            capability,
            local_available=True,
            remote_available=runtime.premium_client is not None,
        )
        if target == "remote":
            if runtime.premium_client is None:
                return ToolResult([TextContent(
                    type="text",
                    text=json.dumps({
                        "error": "remote_unavailable",
                        "capability": capability,
                        "message": "Remote premium client is not configured.",
                    }),
                )])
            from ..premium_client import select_pool_client

            remote_text = await select_pool_client(
                runtime, kwargs.get("code_path")
            ).call_tool_text(
                "watercooler_list_decisions", kwargs
            )
            return ToolResult([TextContent(type="text", text=remote_text)])
        if target == "disabled":
            return ToolResult([TextContent(
                type="text",
                text=json.dumps(
                    {"error": "capability_disabled", "capability": capability},
                    indent=2,
                ),
            )])
        return _list_decisions_impl(ctx, **kwargs)

    return _hybrid_list_decisions


def register_decisions_tools(mcp, *, runtime=None) -> None:
    """Register decision-listing tools with the MCP server."""
    global list_decisions, list_pending_candidates
    surface = getattr(runtime, "surface", None) if runtime is not None else None
    hybrid = surface == "local_hybrid"
    actual_impl = _build_hybrid_list_decisions_wrapper(runtime) if hybrid else _list_decisions_impl
    list_decisions = mcp.tool(name="watercooler_list_decisions")(actual_impl)
    # Pure baseline read (no T2 leg) — no hybrid wrapper needed, mirroring
    # register_promotion_tools' direct registration.
    #
    # NOT on hosted_premium: server_factory mounts this registrar on the
    # premium surface only as a special case so watercooler_list_decisions'
    # remote memory-query leg (include_supersession=True) has a target — the
    # rest of the baseline thread-tool bundle is intentionally excluded there,
    # and this baseline_search/L1 dashboard feed must not leak with it
    # (PR #1074 review).
    if surface != "hosted_premium":
        list_pending_candidates = mcp.tool(
            name="watercooler_list_pending_candidates"
        )(_list_pending_candidates_impl)
