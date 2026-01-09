"""Slack synchronization for watercooler threads.

Handles bidirectional sync between Watercooler threads and Slack:
- Outbound: Entry → Slack threaded reply
- Inbound: Slack reply → Entry (via Events API, handled by watercooler-site)

This module provides the outbound sync logic, called after thread writes.

Git-Native Mapping:
When a new Slack thread is created, the mapping is stored in:
  .watercooler/slack-mappings/{topic}.json
This allows watercooler-site to read mappings directly from GitHub.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
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


def write_git_native_mapping(
    threads_dir: Path,
    topic: str,
    slack_team_id: str,
    slack_channel_id: str,
    slack_channel_name: str,
    slack_thread_ts: str,
    branch: Optional[str] = None,
) -> Path:
    """Write Slack thread mapping to the repo for git-native sync.

    Creates .watercooler/slack-mappings/{topic}.json with the mapping data.
    This file is committed to the threads repo so watercooler-site can
    read it via GitHub API.

    Args:
        threads_dir: Path to threads directory
        topic: Thread topic (used as filename)
        slack_team_id: Slack workspace ID
        slack_channel_id: Slack channel ID
        slack_channel_name: Slack channel name (for reference)
        slack_thread_ts: Slack thread timestamp (parent message ts)
        branch: Git branch for this thread (enables branch-aware sync)

    Returns:
        Path to the created mapping file
    """
    mappings_dir = threads_dir / ".watercooler" / "slack-mappings"
    mappings_dir.mkdir(parents=True, exist_ok=True)

    mapping_file = mappings_dir / f"{topic}.json"
    mapping_data: Dict[str, Any] = {
        "slackTeamId": slack_team_id,
        "slackChannelId": slack_channel_id,
        "slackChannelName": slack_channel_name,
        "slackThreadTs": slack_thread_ts,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    # Include branch if provided - critical for branch-aware inbound sync
    if branch:
        mapping_data["branch"] = branch

    with open(mapping_file, "w") as f:
        json.dump(mapping_data, f, indent=2)

    logger.info(f"Wrote git-native Slack mapping to {mapping_file}")
    return mapping_file


def read_git_native_mapping(
    threads_dir: Path,
    topic: str,
) -> Optional[Dict[str, str]]:
    """Read Slack thread mapping from the repo.

    Args:
        threads_dir: Path to threads directory
        topic: Thread topic

    Returns:
        Mapping dict or None if not found
    """
    mapping_file = threads_dir / ".watercooler" / "slack-mappings" / f"{topic}.json"
    if not mapping_file.exists():
        return None

    try:
        with open(mapping_file, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to read git-native mapping for {topic}: {e}")
        return None


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
    threads_dir: Optional[Path] = None,
    branch: Optional[str] = None,
) -> Optional[str]:
    """Sync a watercooler entry to Slack as a threaded reply.

    If this is the first entry for the thread, creates the parent message first
    and writes a git-native mapping to .watercooler/slack-mappings/{topic}.json.

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
        threads_dir: Path to threads directory (for git-native mapping)
        branch: Git branch for this thread (enables branch-aware inbound sync)

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

            # Get team_id for git-native mapping
            team_id = client.get_team_id()

            blocks = thread_parent_blocks(
                topic=topic,
                status=status,
                ball_owner=ball_owner,
                entry_count=entry_count,
                repo=repo,
                branch=branch,
                include_buttons=True,
            )
            text = thread_parent_text(topic, status, ball_owner, entry_count)

            result = client.post_message(channel_id, text, blocks=blocks)
            thread_ts = result.get("ts", "")

            if not thread_ts:
                raise SlackSyncError("Failed to get thread_ts from parent message")

            # Write git-native mapping for watercooler-site discovery
            if threads_dir:
                write_git_native_mapping(
                    threads_dir=threads_dir,
                    topic=topic,
                    slack_team_id=team_id,
                    slack_channel_id=channel_id,
                    slack_channel_name=channel_name.lstrip("#"),
                    slack_thread_ts=thread_ts,
                    branch=branch,
                )
                logger.info(f"Wrote git-native Slack mapping for {topic}")

            # Store the local mapping
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


def teardown_slack_thread(repo: str, topic: str) -> int:
    """Delete all entry reply messages in a Slack thread.

    Preserves the parent message but clears all replies.
    Clears entry_message_map in mapping store.

    Args:
        repo: Repository name
        topic: Thread topic

    Returns:
        Number of messages deleted

    Raises:
        SlackSyncError: If teardown fails due to API error
    """
    if not is_sync_enabled():
        return 0

    try:
        store = get_mapping_store()
        mapping = store.get_thread(repo, topic)

        if mapping is None:
            logger.debug(f"No Slack mapping for {repo}/{topic}, nothing to tear down")
            return 0

        client = get_slack_client()

        # Get all messages in the thread (excluding parent)
        replies = client.get_thread_replies(
            mapping.slack_channel_id,
            mapping.slack_thread_ts,
            include_parent=False,
        )

        deleted_count = 0
        for message in replies:
            message_ts = message.get("ts")
            if message_ts:
                try:
                    if client.delete_message(mapping.slack_channel_id, message_ts):
                        deleted_count += 1
                except SlackAPIError as e:
                    # Log but continue - some messages may not be deletable
                    logger.warning(f"Could not delete message {message_ts}: {e.error}")

        # Clear the entry_message_map in the mapping
        if mapping.entry_message_map:
            mapping.entry_message_map.clear()
            store.set_thread(mapping)

        logger.info(f"Tore down {deleted_count} messages from {repo}/{topic}")
        return deleted_count

    except SlackAPIError as e:
        logger.error(f"Slack API error during teardown: {e}")
        raise SlackSyncError(f"Slack API error: {e.error}") from e

    except Exception as e:
        logger.error(f"Unexpected error during teardown: {e}")
        raise SlackSyncError(str(e)) from e


def close_thread_slack_representation(
    repo: str,
    topic: str,
    closure_summary: Optional[str] = None,
    entry_count: int = 0,
) -> bool:
    """Update Slack representation for closed thread.

    Updates the parent message to:
    - Show CLOSED status with ⚪ emoji
    - Remove all action buttons
    - Optionally display closure summary

    Args:
        repo: Repository name
        topic: Thread topic
        closure_summary: Optional summary text for the closure
        entry_count: Number of entries in thread

    Returns:
        True if updated successfully, False otherwise
    """
    if not is_sync_enabled():
        return False

    try:
        store = get_mapping_store()
        mapping = store.get_thread(repo, topic)

        if mapping is None:
            logger.debug(f"No Slack mapping for {repo}/{topic}, cannot close representation")
            return False

        client = get_slack_client()

        # Build closed variant of parent message
        blocks = thread_parent_blocks(
            topic=topic,
            status="CLOSED",
            ball_owner="",
            entry_count=entry_count,
            repo=repo,
            include_buttons=False,
            is_closed=True,
            closure_summary=closure_summary,
        )
        text = thread_parent_text(topic, "CLOSED", "", entry_count)

        # Update the parent message
        client.update_message(
            mapping.slack_channel_id,
            mapping.slack_thread_ts,
            text,
            blocks,
        )

        logger.info(f"Closed Slack representation for {repo}/{topic}")
        return True

    except SlackAPIError as e:
        logger.error(f"Slack API error closing thread representation: {e}")
        return False

    except Exception as e:
        logger.error(f"Error closing thread representation: {e}")
        return False


def rebuild_slack_thread(
    repo: str,
    topic: str,
    entries: list,
    status: str,
    ball_owner: str,
    is_closed: bool = False,
    closure_summary: Optional[str] = None,
    branch: Optional[str] = None,
) -> Dict[str, str]:
    """Rebuild Slack thread from watercooler source.

    Tears down existing messages and reposts entries in chronological order.
    Useful for:
    - Recovery from out-of-sequence sync
    - Fixing corrupted mappings
    - Full thread refresh

    Args:
        repo: Repository name
        topic: Thread topic
        entries: List of entry dicts with keys:
            entry_id, agent, role, entry_type, title, body, timestamp
        status: Thread status
        ball_owner: Current ball owner
        is_closed: If True, render closed variant
        closure_summary: Optional summary for closed threads
        branch: Git branch for metadata

    Returns:
        New entry_message_map (entry_id -> message_ts)

    Raises:
        SlackSyncError: If rebuild fails
    """
    if not is_sync_enabled():
        return {}

    try:
        store = get_mapping_store()
        mapping = store.get_thread(repo, topic)

        if mapping is None:
            logger.warning(f"No Slack mapping for {repo}/{topic}, cannot rebuild")
            return {}

        client = get_slack_client()

        # Step 1: Tear down existing messages
        teardown_slack_thread(repo, topic)

        # Step 2: Update parent message
        blocks = thread_parent_blocks(
            topic=topic,
            status=status if not is_closed else "CLOSED",
            ball_owner=ball_owner if not is_closed else "",
            entry_count=len(entries),
            repo=repo,
            branch=branch,
            include_buttons=not is_closed,
            is_closed=is_closed,
            closure_summary=closure_summary,
        )
        text = thread_parent_text(
            topic,
            status if not is_closed else "CLOSED",
            ball_owner if not is_closed else "",
            len(entries),
        )

        client.update_message(
            mapping.slack_channel_id,
            mapping.slack_thread_ts,
            text,
            blocks,
        )

        # Step 3: Repost entries in chronological order
        new_entry_map: Dict[str, str] = {}

        for entry in entries:
            entry_blocks = entry_reply_blocks(
                entry_id=entry.get("entry_id", ""),
                agent=entry.get("agent", "Unknown"),
                role=entry.get("role", "implementer"),
                entry_type=entry.get("entry_type", "Note"),
                title=entry.get("title", ""),
                body=entry.get("body", ""),
                timestamp=entry.get("timestamp", ""),
                spec=entry.get("spec"),
            )
            entry_text = entry_reply_text(
                entry.get("agent", "Unknown"),
                entry.get("role", "implementer"),
                entry.get("entry_type", "Note"),
                entry.get("title", ""),
                entry.get("body", ""),
                entry.get("timestamp", ""),
            )

            result = client.post_message(
                channel_id=mapping.slack_channel_id,
                text=entry_text,
                blocks=entry_blocks,
                thread_ts=mapping.slack_thread_ts,
            )
            message_ts = result.get("ts", "")

            if message_ts and entry.get("entry_id"):
                new_entry_map[entry["entry_id"]] = message_ts

        # Step 4: Update mapping with new entry_message_map
        mapping.entry_message_map = new_entry_map
        store.set_thread(mapping)

        logger.info(f"Rebuilt Slack thread {repo}/{topic} with {len(entries)} entries")
        return new_entry_map

    except SlackAPIError as e:
        logger.error(f"Slack API error during rebuild: {e}")
        raise SlackSyncError(f"Slack API error: {e.error}") from e

    except Exception as e:
        logger.error(f"Unexpected error during rebuild: {e}")
        raise SlackSyncError(str(e)) from e
