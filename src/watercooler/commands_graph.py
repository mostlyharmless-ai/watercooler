"""Graph-canonical command implementations.

This module provides the canonical thread commands where:
1. Data is written to graph first (nodes.jsonl, edges.jsonl)
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
    append_entry_and_project,
    update_header_and_write,
    create_thread_file,
)
from .lock import AdvisoryLock
from .fs import lock_path_for_topic, thread_path

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
    1. Thread node in graph (nodes.jsonl)
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

    Returns:
        Path to updated thread file
    """
    if not entry_id:
        raise ValueError("entry_id is required for graph-canonical append")

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
        entry_data = EntryData(
            entry_id=entry_id,
            thread_topic=topic,
            index=entry_index,
            agent=canonical,
            role=role,
            entry_type=entry_type,
            title=title,
            body=body,
            timestamp=now,
            summary="",  # Summary generated later by enrichment
        )

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

        # 6. Project to markdown
        # Get the entry node we just created for projection
        from .baseline_graph.writer import get_entry_node_from_graph
        entry_node = get_entry_node_from_graph(threads_dir, entry_id)

        if entry_node:
            append_entry_and_project(threads_dir, topic, entry_node)
        else:
            # Fallback: full regeneration
            project_and_write_thread(threads_dir, topic)

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
) -> Path:
    """Quick team note with auto-ball-flip using graph-canonical approach.

    Args:
        topic: Thread topic
        threads_dir: Directory containing threads
        agent: Agent name (defaults to Team)
        role: Agent role (defaults to role from registry)
        title: Entry title (required)
        entry_type: Entry type (default: "Note")
        body: Entry body text
        status: Optional status update
        ball: Optional ball update (if not provided, auto-flips)
        registry: Optional agent registry
        user_tag: Optional user tag
        entry_id: Entry ID (required for graph-canonical)

    Returns:
        Path to updated thread file
    """
    # Default agent to Team
    default_agent, default_role = _default_agent_and_role(registry)
    final_agent = agent if agent is not None else default_agent
    final_role = role if role is not None else default_role

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
) -> Path:
    """Acknowledge without auto-flipping ball using graph-canonical approach.

    Args:
        topic: Thread topic
        threads_dir: Directory containing threads
        agent: Agent name (defaults to Team)
        role: Agent role (defaults to role from registry)
        title: Entry title (defaults to "Ack")
        entry_type: Entry type (default: "Note")
        body: Entry body text (defaults to "ack")
        status: Optional status update
        ball: Optional ball update (does NOT auto-flip)
        registry: Optional agent registry
        user_tag: Optional user tag
        entry_id: Entry ID (required for graph-canonical)

    Returns:
        Path to updated thread file
    """
    # Default agent to Team
    default_agent, default_role = _default_agent_and_role(registry)
    final_agent = agent if agent is not None else default_agent
    final_role = role if role is not None else default_role
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
    2. Update Status: line in markdown

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

        # 3. Update markdown header (efficient: just modify Status line)
        update_header_and_write(threads_dir, topic, status=status.upper())

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
    2. Update Ball: line in markdown

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

        # 3. Update markdown header (efficient: just modify Ball line)
        update_header_and_write(threads_dir, topic, ball=ball)

        logger.debug(f"Graph-canonical set_ball complete: {topic} -> {ball}")
        return tp


# ============================================================================
# NOTE: Graph-canonical mode is now ALWAYS enabled. The WATERCOOLER_GRAPH_FIRST env var
# and the enable/disable functions have been removed. All thread operations go through
# the graph-canonical functions in this module. Enrichment (summaries/embeddings) is handled
# by the middleware after the structural write completes.
