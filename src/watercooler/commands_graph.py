"""Graph-canonical command implementations.

This module provides the canonical thread commands where:
1. Data is written to per-thread graph first (threads/<topic>/{meta.json,entries.jsonl,edges.jsonl})
2. Markdown is projected as a derived file

These are the primary implementations; the MD-only fallbacks
live in commands.py for graceful degradation.

Usage:
    from watercooler.commands_graph import (
        say,
        ack,
        handoff,
        set_status,
        set_ball,
        init_thread,
    )
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .agents import _counterpart_of, _canonical_agent, _default_agent_and_role
from .role_loader import validate_role
from .baseline_graph.writer import (
    ThreadData,
    EntryData,
    upsert_thread_node,
    upsert_entry_node,
    update_thread_metadata,
    get_thread_from_graph,
    get_last_entry_id,
    get_next_entry_index,
    init_thread_in_graph,
)
from .baseline_graph.projector import (
    project_and_write_thread,
    create_thread_file,
)
from .lock import AdvisoryLock
from .fs import lock_path_for_topic, thread_path
from .promotion import ULID_PATTERN, scrub_authority_identifier

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


# ============================================================================
# Graph-Canonical Thread Initialization
# ============================================================================


def init_thread(
    topic: str,
    *,
    threads_dir: Path,
    title: Optional[str] = None,
    status: str = "OPEN",
    ball: str = "codex",
) -> Path:
    """Initialize a new thread using graph-canonical approach.

    Creates:
    1. Thread node in per-thread graph (threads/<topic>/meta.json)
    2. Thread markdown file as projection

    Args:
        topic: Thread topic identifier
        threads_dir: Directory containing threads
        title: Optional title override
        status: Initial status (default: "OPEN")
        ball: Initial ball owner (default: "codex")

    Returns:
        Path to the created thread file
    """
    # Normalize status to uppercase (canonical form)
    status = status.upper()

    # Ensure threads directory exists before acquiring lock
    threads_dir.mkdir(parents=True, exist_ok=True)

    tp = thread_path(topic, threads_dir)
    if tp.exists():
        return tp

    lp = lock_path_for_topic(topic, threads_dir)
    with AdvisoryLock(lp, timeout=2, ttl=10, force_break=False):
        if tp.exists():
            return tp

        # 1. Create thread node in graph
        hdr_title = title or topic.replace("-", " ").strip()
        init_thread_in_graph(
            threads_dir,
            topic,
            title=hdr_title,
            status=status,
            ball=ball,
        )

        # 2. Project to markdown
        now = _now_iso()
        create_thread_file(
            threads_dir,
            topic,
            title=hdr_title,
            status=status,
            ball=ball,
            created=now,
        )

        logger.debug(f"Graph-canonical init_thread complete: {topic}")
        return tp


# ============================================================================
# Graph-Canonical Entry Commands
# ============================================================================


def append_entry(
    topic: str,
    *,
    threads_dir: Path,
    agent: str,
    role: str,
    title: str,
    entry_type: str = "Note",
    body: str,
    status: Optional[str] = None,
    ball: Optional[str] = None,
    registry: dict | None = None,
    user_tag: str | None = None,
    entry_id: str | None = None,
    code_branch: Optional[str] = None,
    code_root: Optional[Path] = None,
    authority_fields: Optional[dict] = None,
    support_fields: Optional[dict] = None,
) -> Path:
    """Append a structured entry using graph-canonical approach.

    Flow:
    1. Ensure thread exists in graph (create if needed)
    2. Create entry node in graph
    3. Update thread metadata (entry_count, ball, status)
    4. Project entry to markdown (append to file)

    Args:
        topic: Thread topic
        threads_dir: Directory containing threads
        agent: Agent name (will be canonicalized with user tag)
        role: Agent role
        title: Entry title
        entry_type: Entry type (Note, Plan, Decision, PR, Closure)
        body: Entry body text
        status: Optional status update
        ball: Optional ball update (if None, uses counterpart logic in caller)
        registry: Optional agent registry
        user_tag: Optional user tag for agent identification
        entry_id: Entry ID (required for graph-canonical)
        authority_fields: Optional dict with Phase 1a authority-ladder
            provenance fields to attach to the entry node. Recognised keys:
            ``actor_class``, ``decision_origin``, ``authority_source``,
            ``authority_basis``, ``source_entry_id``, ``human_authorized_by``,
            ``confidence``, ``gate_results``. Unknown keys are ignored. ``None`` (default)
            leaves the entry's authority fields as their EntryData defaults
            (all ``None``), producing the legacy node shape.

    Returns:
        Path to updated thread file
    """
    if not entry_id:
        raise ValueError("entry_id is required for graph-canonical append")

    # Validate role against the active role set (raises ValueError for invalid roles)
    role = validate_role(role, code_path=code_root) or role

    # Normalize status to uppercase (canonical form)
    if status:
        status = status.upper()

    # Ensure threads directory exists before acquiring lock
    threads_dir.mkdir(parents=True, exist_ok=True)

    tp = thread_path(topic, threads_dir)
    lp = lock_path_for_topic(topic, threads_dir)

    with AdvisoryLock(lp, timeout=2, ttl=10, force_break=False):
        # 1. Ensure thread exists in graph
        thread = get_thread_from_graph(threads_dir, topic)
        if not thread:
            # Initialize thread in graph first
            hdr_title = topic.replace("-", " ").strip()
            init_thread_in_graph(threads_dir, topic, title=hdr_title, status="OPEN", ball="codex")
            thread = get_thread_from_graph(threads_dir, topic)

        # 2. Get next entry index and previous entry ID
        entry_index = get_next_entry_index(threads_dir, topic)
        prev_entry_id = get_last_entry_id(threads_dir, topic)

        # 3. Canonicalize agent name
        canonical = _canonical_agent(agent, registry, user_tag=user_tag)
        now = _now_iso()

        # 4. Create entry node in graph
        entry_kwargs: dict = {
            "entry_id": entry_id,
            "thread_topic": topic,
            "index": entry_index,
            "agent": canonical,
            "role": role,
            "entry_type": entry_type,
            "title": title,
            "body": body,
            "timestamp": now,
            "summary": "",  # Summary generated later by enrichment
            "code_branch": code_branch,
        }
        if authority_fields:
            # Whitelist the recognised keys; ignore unknowns.
            for key in (
                "actor_class",
                "decision_origin",
                "authority_source",
                "authority_basis",
                "source_entry_id",
                "human_authorized_by",
                "confidence",
                "gate_results",
            ):
                if key in authority_fields and authority_fields[key] is not None:
                    entry_kwargs[key] = authority_fields[key]
            # Boundary clamp on ``confidence``: the entry_schema.json
            # rubric is 1-5; a 0 (the extractor's "not a decision"
            # value) would produce a schema-invalid node. The producer
            # side (``_build_authority_fields``) already drops 0, but this
            # boundary clamp closes the bug class structurally for any
            # future caller passing ``authority_fields={"confidence": 0}``
            # directly. Done here so a second daemon / promotion-helper
            # caller can't silently reintroduce the schema-invalid path.
            conf = entry_kwargs.get("confidence")
            if conf is not None and not (
                isinstance(conf, int) and 1 <= conf <= 5
            ):
                entry_kwargs.pop("confidence")
            # Boundary scrub on ``human_authorized_by``: this value is durable,
            # git-committed, and federation-visible, so it is sanitized at the
            # write boundary (Cf/zero-width/bidi dropped, control/separator
            # collapsed, angle brackets stripped, bounded to schema maxLength).
            # The MCP/CLI producers already scrub, but doing it here closes the
            # bug class structurally for any future caller passing the field
            # directly. A value that scrubs to empty is dropped.
            hab = entry_kwargs.get("human_authorized_by")
            if hab is not None:
                scrubbed = scrub_authority_identifier(str(hab))
                if scrubbed:
                    entry_kwargs["human_authorized_by"] = scrubbed
                else:
                    entry_kwargs.pop("human_authorized_by")
            # Boundary clamp on ``source_entry_id``: that schema field is
            # ULID-typed. Drop a non-ULID value rather than persist a node that
            # would fail validation once it is enforced (mirrors the confidence
            # clamp above; the producers already guard, this is defense in depth).
            sid = entry_kwargs.get("source_entry_id")
            if sid is not None and not ULID_PATTERN.match(str(sid)):
                entry_kwargs.pop("source_entry_id")
        if support_fields:
            # §6 tether read-model (#896 Leg 2) — structured counterpart to the
            # body markers. Whitelist the 5 recognised keys; ignore unknowns.
            for key in (
                "support_counts",
                "dominant_tether",
                "thin_support",
                "thin_support_reason",
                "support_evidence",
            ):
                if key in support_fields and support_fields[key] is not None:
                    entry_kwargs[key] = support_fields[key]
        entry_data = EntryData(**entry_kwargs)

        success = upsert_entry_node(
            threads_dir,
            entry_data,
            prev_entry_id=prev_entry_id,
        )

        if not success:
            raise RuntimeError(f"Failed to upsert entry node for {topic}/{entry_id}")

        # 5. Update thread metadata if needed
        if status or ball:
            update_thread_metadata(
                threads_dir,
                topic,
                status=status,
                ball=ball,
            )

        # 6. Reconstruct .md from graph (single source of truth)
        project_and_write_thread(threads_dir, topic)

        # 7. Maintain the repo-level decisions index for Decision entries so the
        # hosted reader needn't fan out over every thread. Best-effort: index
        # maintenance must never fail a write. (Daemon-extracted Decisions apply
        # their source xref after this point and re-upsert post-annotation in
        # daemon_write_entry; here the source is whatever is in the graph now.)
        if entry_type == "Decision":
            try:
                from watercooler.baseline_graph.decision_index import (
                    upsert_decision_index_local,
                )
                from watercooler.baseline_graph.storage import get_graph_dir

                upsert_decision_index_local(
                    get_graph_dir(threads_dir), topic, entry_id
                )
            except Exception as idx_err:  # pragma: no cover - defensive
                logger.warning(
                    "decisions-index upsert failed (non-fatal): %s", idx_err
                )

        logger.debug(f"Graph-canonical append_entry complete: {topic}/{entry_id}")
        return tp


def say(
    topic: str,
    *,
    threads_dir: Path,
    agent: str | None = None,
    role: str | None = None,
    title: str,
    entry_type: str = "Note",
    body: str,
    status: str | None = None,
    ball: str | None = None,
    registry: dict | None = None,
    user_tag: str | None = None,
    entry_id: str | None = None,
    code_branch: Optional[str] = None,
    code_root: Optional[Path] = None,
    authority_fields: Optional[dict] = None,
    support_fields: Optional[dict] = None,
) -> Path:
    """Quick team note with auto-ball-flip using graph-canonical approach.

    See ``append_entry`` for the ``authority_fields`` / ``support_fields`` params.
    """
    # Default agent to Team
    default_agent, _ = _default_agent_and_role(registry)
    final_agent = agent if agent is not None else default_agent
    # Use "implementer" as role fallback — the git username from _default_agent_and_role
    # is not a valid role and would fail validation.
    final_role = role if role is not None else "implementer"

    # Determine ball: auto-flip if not provided
    final_ball = ball
    if final_ball is None:
        canonical = _canonical_agent(final_agent, registry, user_tag=user_tag)
        final_ball = _counterpart_of(canonical, registry)

    return append_entry(
        topic,
        threads_dir=threads_dir,
        agent=final_agent,
        role=final_role,
        title=title,
        entry_type=entry_type,
        body=body,
        status=status,
        ball=final_ball,
        registry=registry,
        user_tag=user_tag,
        entry_id=entry_id,
        code_branch=code_branch,
        code_root=code_root,
        authority_fields=authority_fields,
        support_fields=support_fields,
    )


def ack(
    topic: str,
    *,
    threads_dir: Path,
    agent: str | None = None,
    role: str | None = None,
    title: str | None = None,
    entry_type: str = "Note",
    body: str | None = None,
    status: str | None = None,
    ball: str | None = None,
    registry: dict | None = None,
    user_tag: str | None = None,
    entry_id: str | None = None,
    code_branch: Optional[str] = None,
    code_root: Optional[Path] = None,
    authority_fields: Optional[dict] = None,
    support_fields: Optional[dict] = None,
) -> Path:
    """Acknowledge without auto-flipping ball using graph-canonical approach.

    See ``append_entry`` for the ``authority_fields`` / ``support_fields`` params.
    """
    # Default agent to Team
    default_agent, _ = _default_agent_and_role(registry)
    final_agent = agent if agent is not None else default_agent
    # Use "implementer" as role fallback — the git username from _default_agent_and_role
    # is not a valid role and would fail validation.
    final_role = role if role is not None else "implementer"
    final_title = title if title is not None else "Ack"
    final_body = body if body is not None else "ack"

    # For ack, preserve current ball if not specified
    final_ball = ball
    if final_ball is None:
        thread = get_thread_from_graph(threads_dir, topic)
        if thread:
            final_ball = thread.get("ball", "codex")

    return append_entry(
        topic,
        threads_dir=threads_dir,
        agent=final_agent,
        role=final_role,
        title=final_title,
        entry_type=entry_type,
        body=final_body,
        status=status,
        ball=final_ball,
        registry=registry,
        user_tag=user_tag,
        entry_id=entry_id,
        code_branch=code_branch,
        code_root=code_root,
        authority_fields=authority_fields,
        support_fields=support_fields,
    )


def handoff(
    topic: str,
    *,
    threads_dir: Path,
    agent: str | None = None,
    role: str = "pm",
    note: str | None = None,
    registry: dict | None = None,
    user_tag: str | None = None,
    entry_id: str | None = None,
    code_branch: Optional[str] = None,
    code_root: Optional[Path] = None,
) -> Path:
    """Flip the ball to the counterpart using graph-canonical approach.

    Args:
        topic: Thread topic
        threads_dir: Directory containing threads
        agent: Agent performing handoff (defaults to Team)
        role: Agent role (default: "pm")
        note: Optional custom handoff message
        registry: Optional agent registry
        user_tag: Optional user tag
        entry_id: Entry ID (required for graph-canonical)

    Returns:
        Path to updated thread file
    """
    # 1. Ensure thread exists
    thread = get_thread_from_graph(threads_dir, topic)
    if not thread:
        init_thread(topic, threads_dir=threads_dir)
        thread = get_thread_from_graph(threads_dir, topic)

    # 2. Determine target based on current ball
    current_ball = thread.get("ball", "codex") if thread else "codex"
    target = _counterpart_of(current_ball, registry)

    # 3. Default agent
    default_agent, default_role = _default_agent_and_role(registry)
    final_agent = agent if agent is not None else default_agent

    # 4. Create handoff entry
    text = note or f"handoff to {target}"
    handoff_title = f"Handoff to {target}"

    return append_entry(
        topic,
        threads_dir=threads_dir,
        agent=final_agent,
        role=role,
        title=handoff_title,
        entry_type="Note",
        body=text,
        ball=target,  # Explicitly set target
        registry=registry,
        user_tag=user_tag,
        entry_id=entry_id,
        code_branch=code_branch,
        code_root=code_root,
    )


# ============================================================================
# Graph-Canonical Metadata Commands
# ============================================================================


def set_status(
    topic: str,
    *,
    threads_dir: Path,
    status: str,
) -> Path:
    """Update thread status using graph-canonical approach.

    Flow:
    1. Update status in graph node
    2. Regenerate full .md projection via project_and_write_thread()

    Note:
        The .md projection is regenerated in full (O(entries)) rather than
        patched in-place, to keep the projector stateless.  This is acceptable
        for current thread sizes.

    Args:
        topic: Thread topic
        threads_dir: Directory containing threads
        status: New status value

    Returns:
        Path to updated thread file
    """
    tp = thread_path(topic, threads_dir)
    lp = lock_path_for_topic(topic, threads_dir)

    with AdvisoryLock(lp, timeout=2, ttl=10, force_break=False):
        # 1. Ensure thread exists in graph
        thread = get_thread_from_graph(threads_dir, topic)
        if not thread:
            raise FileNotFoundError(f"Thread '{topic}' not found in graph")

        # 2. Update status in graph
        success = update_thread_metadata(
            threads_dir,
            topic,
            status=status.upper(),
        )

        if not success:
            raise RuntimeError(f"Failed to update status in graph for {topic}")

        # 3. Reconstruct .md from graph
        project_and_write_thread(threads_dir, topic)

        logger.debug(f"Graph-canonical set_status complete: {topic} -> {status}")
        return tp


def set_ball(
    topic: str,
    *,
    threads_dir: Path,
    ball: str,
) -> Path:
    """Update thread ball owner using graph-canonical approach.

    Flow:
    1. Update ball in graph node
    2. Regenerate full .md projection via project_and_write_thread()

    Note:
        The .md projection is regenerated in full (O(entries)) rather than
        patched in-place, to keep the projector stateless.  This is acceptable
        for current thread sizes.

    Args:
        topic: Thread topic
        threads_dir: Directory containing threads
        ball: New ball owner

    Returns:
        Path to updated thread file
    """
    tp = thread_path(topic, threads_dir)
    lp = lock_path_for_topic(topic, threads_dir)

    with AdvisoryLock(lp, timeout=2, ttl=10, force_break=False):
        # 1. Ensure thread exists in graph
        thread = get_thread_from_graph(threads_dir, topic)
        if not thread:
            # Create thread if missing
            init_thread(topic, threads_dir=threads_dir, ball=ball)
            return tp

        # 2. Update ball in graph
        success = update_thread_metadata(
            threads_dir,
            topic,
            ball=ball,
        )

        if not success:
            raise RuntimeError(f"Failed to update ball in graph for {topic}")

        # 3. Reconstruct .md from graph
        project_and_write_thread(threads_dir, topic)

        logger.debug(f"Graph-canonical set_ball complete: {topic} -> {ball}")
        return tp


# ============================================================================
# NOTE: Graph-canonical mode is now ALWAYS enabled. The WATERCOOLER_GRAPH_FIRST env var
# and the enable/disable functions have been removed. All thread operations go through
# the graph-canonical functions in this module. Enrichment (summaries/embeddings) is handled
# by the middleware after the structural write completes.
