"""Slack integration package for watercooler MCP server.

Provides notification capabilities for thread events:
- New entries (say)
- Ball flips (handoff)
- Status changes
"""

from .notify import (
    notify_new_entry,
    notify_ball_flip,
    notify_handoff,
    notify_status_change,
    send_webhook,
)

__all__ = [
    "notify_new_entry",
    "notify_ball_flip",
    "notify_handoff",
    "notify_status_change",
    "send_webhook",
]
