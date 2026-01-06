"""Slack integration for Watercooler.

This package provides:
- Webhook notifications (Phase 1)
- Channel <-> Repository mapping (Phase 2+)
- Thread <-> Slack thread (thread_ts) mapping (Phase 2+)
- Entry <-> Slack reply synchronization (Phase 2+)
- Block Kit message formatting (Phase 2+)
"""

# Phase 1: Webhook notifications
from watercooler_mcp.slack.notify import (
    send_webhook,
    notify_new_entry,
    notify_ball_flip,
    notify_handoff,
    notify_status_change,
)

# Phase 2+: Bidirectional sync
from watercooler_mcp.slack.mapping import (
    SlackThreadMapping,
    SlackChannelMapping,
    SlackMappingStore,
    get_mapping_store,
)
from watercooler_mcp.slack.client import (
    SlackClient,
    SlackAPIError,
    get_slack_client,
)
from watercooler_mcp.slack.blocks import (
    thread_parent_blocks,
    thread_parent_text,
    entry_reply_blocks,
    entry_reply_text,
    status_update_blocks,
    ball_flip_blocks,
    handoff_blocks,
)
from watercooler_mcp.slack.sync import (
    SlackSyncError,
    is_sync_enabled,
    sync_entry_to_slack,
    sync_status_change,
    sync_ball_flip,
    sync_handoff,
    update_thread_parent,
)

__all__ = [
    # Notifications (Phase 1)
    "send_webhook",
    "notify_new_entry",
    "notify_ball_flip",
    "notify_handoff",
    "notify_status_change",
    # Mapping (Phase 2+)
    "SlackThreadMapping",
    "SlackChannelMapping",
    "SlackMappingStore",
    "get_mapping_store",
    # Client (Phase 2+)
    "SlackClient",
    "SlackAPIError",
    "get_slack_client",
    # Block Kit templates (Phase 2+)
    "thread_parent_blocks",
    "thread_parent_text",
    "entry_reply_blocks",
    "entry_reply_text",
    "status_update_blocks",
    "ball_flip_blocks",
    "handoff_blocks",
    # Sync (Phase 2+)
    "SlackSyncError",
    "is_sync_enabled",
    "sync_entry_to_slack",
    "sync_status_change",
    "sync_ball_flip",
    "sync_handoff",
    "update_thread_parent",
]
