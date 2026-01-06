"""Slack synchronization for watercooler threads.

Handles bidirectional sync between Watercooler threads and Slack:
- Outbound: Entry → Slack threaded reply
- Inbound: Slack reply → Entry (via Events API, handled by watercooler-site)

This module provides the outbound sync logic, called after thread writes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from ..config import get_slack_config, is_slack_bot_enabled
from .mapping import (
    SlackThreadMapping,
    SlackMappingStore,
    get_mapping_store,
)
from .client import SlackClient, SlackAPIError, get_slack_client
from .blocks import (
    thread_parent_blocks,
    thread_parent_text,
    entry_reply_blocks,
    entry_reply_text,
)

logger = logging.getLogger(__name__)


class SlackSyncError(Exception):
    """Error during Slack synchronization."""
    pass


def is_sync_enabled() -> bool:
    """Check if Slack sync is enabled (bot token configured)."""
    return is_slack_bot_enabled()


def sync_entry_to_slack(
    repo: str,
    topic: str,
    entry_id: str,
    agent: str,
    role: str,
    entry_type: str,
    title: str,
    body: str,
    timestamp: str,
    status: str = "OPEN",
    ball_owner: str = "",
    entry_count: int = 0,
    spec: Optional[str] = None,
) -> Optional[str]:
    """Sync a watercooler entry to Slack as a threaded reply.

    If this is the first entry for the thread, creates the parent message first.
    
    Args:
        repo: Repository name (e.g., "watercooler-cloud")
        topic: Thread topic (e.g., "slack-integration")
        entry_id: Watercooler entry ID (ULID)
        agent: Entry author
        role: Author role (planner, implementer, etc.)
        entry_type: Entry type (Note, Plan, etc.)
        title: Entry title
        body: Entry body (markdown)
        timestamp: Entry timestamp (ISO or formatted)
        status: Thread status (for parent message)
        ball_owner: Current ball owner (for parent message)
        entry_count: Total entries in thread (for parent message)
        spec: Agent specialization

    Returns:
        Slack message ts if successful, None if sync not enabled or failed

    Raises:
        SlackSyncError: If sync fails due to API error
    """
    if not is_sync_enabled():
        logger.debug("Slack sync not enabled (no bot token)")
        return None

    try:
        client = get_slack_client()
        store = get_mapping_store()
        config = get_slack_config()
        prefix = config.get("channel_prefix", "wc-") if config else "wc-"

        # Get or create channel for this repo
        channel_id, channel_name, was_created = client.get_or_create_channel(
            repo, prefix
        )
        if was_created:
            logger.info(f"Created Slack channel {channel_name} for {repo}")

        # Check if we have a thread mapping
        mapping = store.get_thread(repo, topic)

        if mapping is None:
            # First sync for this thread - create parent message
            logger.info(f"Creating Slack thread for {repo}/{topic}")
            
            blocks = thread_parent_blocks(
                topic=topic,
                status=status,
                ball_owner=ball_owner,
                entry_count=entry_count,
                repo=repo,
                include_buttons=True,
            )
            text = thread_parent_text(topic, status, ball_owner, entry_count)

            result = client.post_message(channel_id, text, blocks=blocks)
            thread_ts = result.get("ts", "")

            if not thread_ts:
                raise SlackSyncError("Failed to get thread_ts from parent message")

            # Store the mapping
            mapping = SlackThreadMapping(
                topic=topic,
                repo=repo,
                slack_channel_id=channel_id,
                slack_channel_name=channel_name,
                slack_thread_ts=thread_ts,
            )
            store.set_thread(mapping)
            logger.info(f"Created thread mapping: {topic} -> {thread_ts}")

        # Check if this entry was already synced (idempotency)
        if entry_id in mapping.entry_message_map:
            logger.debug(f"Entry {entry_id} already synced to Slack")
            return mapping.entry_message_map[entry_id]

        # Post entry as threaded reply
        blocks = entry_reply_blocks(
            entry_id=entry_id,
            agent=agent,
            role=role,
            entry_type=entry_type,
            title=title,
            body=body,
            timestamp=timestamp,
            spec=spec,
        )
        text = entry_reply_text(agent, role, entry_type, title, body, timestamp)

        result = client.post_message(
            channel_id=mapping.slack_channel_id,
            text=text,
            blocks=blocks,
            thread_ts=mapping.slack_thread_ts,
        )
        message_ts = result.get("ts", "")

        if message_ts:
            # Update mapping with this entry
            store.update_thread_sync(repo, topic, entry_id, message_ts)
            logger.info(f"Synced entry {entry_id[:12]}... to Slack {message_ts}")

        return message_ts

    except SlackAPIError as e:
        logger.error(f"Slack API error syncing entry: {e}")
        raise SlackSyncError(f"Slack API error: {e.error}") from e

    except Exception as e:
        logger.error(f"Unexpected error syncing entry to Slack: {e}")
        raise SlackSyncError(str(e)) from e


def sync_status_change(
    repo: str,
    topic: str,
    old_status: str,
    new_status: str,
    changed_by: str,
) -> Optional[str]:
    """Post a status change notification to the Slack thread.

    Args:
        repo: Repository name
        topic: Thread topic
        old_status: Previous status
        new_status: New status
        changed_by: Agent who changed the status

    Returns:
        Message ts if successful, None if not enabled or thread not synced
    """
    if not is_sync_enabled():
        return None

    try:
        store = get_mapping_store()
        mapping = store.get_thread(repo, topic)

        if mapping is None:
            logger.debug(f"No Slack mapping for {repo}/{topic}, skipping status sync")
            return None

        client = get_slack_client()

        # Post status change as thread reply
        from .blocks import status_update_blocks
        blocks = status_update_blocks(topic, old_status, new_status, changed_by)
        text = f"Status changed: {old_status} → {new_status} by {changed_by}"

        result = client.post_message(
            channel_id=mapping.slack_channel_id,
            text=text,
            blocks=blocks,
            thread_ts=mapping.slack_thread_ts,
        )
        return result.get("ts")

    except Exception as e:
        logger.error(f"Error syncing status change to Slack: {e}")
        return None


def sync_ball_flip(
    repo: str,
    topic: str,
    from_agent: str,
    to_agent: str,
    note: Optional[str] = None,
) -> Optional[str]:
    """Post a ball flip notification to the Slack thread.

    Args:
        repo: Repository name
        topic: Thread topic
        from_agent: Previous ball owner
        to_agent: New ball owner
        note: Optional handoff note

    Returns:
        Message ts if successful, None if not enabled or thread not synced
    """
    if not is_sync_enabled():
        return None

    try:
        store = get_mapping_store()
        mapping = store.get_thread(repo, topic)

        if mapping is None:
            logger.debug(f"No Slack mapping for {repo}/{topic}, skipping ball flip sync")
            return None

        client = get_slack_client()

        from .blocks import ball_flip_blocks
        blocks = ball_flip_blocks(topic, from_agent, to_agent, note)
        text = f"Ball passed: {from_agent} → {to_agent}"
        if note:
            text += f" ({note})"

        result = client.post_message(
            channel_id=mapping.slack_channel_id,
            text=text,
            blocks=blocks,
            thread_ts=mapping.slack_thread_ts,
        )
        return result.get("ts")

    except Exception as e:
        logger.error(f"Error syncing ball flip to Slack: {e}")
        return None


def sync_handoff(
    repo: str,
    topic: str,
    from_agent: str,
    to_agent: str,
    note: Optional[str] = None,
) -> Optional[str]:
    """Post an explicit handoff notification to the Slack thread.

    Args:
        repo: Repository name
        topic: Thread topic
        from_agent: Agent handing off
        to_agent: Agent receiving handoff
        note: Optional handoff message

    Returns:
        Message ts if successful, None if not enabled or thread not synced
    """
    if not is_sync_enabled():
        return None

    try:
        store = get_mapping_store()
        mapping = store.get_thread(repo, topic)

        if mapping is None:
            logger.debug(f"No Slack mapping for {repo}/{topic}, skipping handoff sync")
            return None

        client = get_slack_client()

        from .blocks import handoff_blocks
        blocks = handoff_blocks(topic, from_agent, to_agent, note)
        text = f"Handoff: {from_agent} → {to_agent}"
        if note:
            text += f"\n{note}"

        result = client.post_message(
            channel_id=mapping.slack_channel_id,
            text=text,
            blocks=blocks,
            thread_ts=mapping.slack_thread_ts,
        )
        return result.get("ts")

    except Exception as e:
        logger.error(f"Error syncing handoff to Slack: {e}")
        return None


def update_thread_parent(
    repo: str,
    topic: str,
    status: str,
    ball_owner: str,
    entry_count: int,
) -> bool:
    """Update the thread parent message with current state.

    Called after status changes, ball flips, or significant updates.

    Args:
        repo: Repository name
        topic: Thread topic
        status: Current status
        ball_owner: Current ball owner
        entry_count: Current entry count

    Returns:
        True if updated successfully, False otherwise
    """
    if not is_sync_enabled():
        return False

    try:
        store = get_mapping_store()
        mapping = store.get_thread(repo, topic)

        if mapping is None:
            return False

        client = get_slack_client()

        blocks = thread_parent_blocks(
            topic=topic,
            status=status,
            ball_owner=ball_owner,
            entry_count=entry_count,
            repo=repo,
            include_buttons=True,
        )
        text = thread_parent_text(topic, status, ball_owner, entry_count)

        # Update the parent message
        client._request(
            "POST",
            "chat.update",
            {
                "channel": mapping.slack_channel_id,
                "ts": mapping.slack_thread_ts,
                "text": text,
                "blocks": blocks,
            },
        )

        logger.debug(f"Updated thread parent for {repo}/{topic}")
        return True

    except Exception as e:
        logger.error(f"Error updating thread parent: {e}")
        return False
