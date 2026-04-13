"""Slack webhook notifications for watercooler thread events.

This module provides fire-and-forget notifications to Slack via webhooks.
Notifications are non-blocking and failures don't affect thread operations.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
import urllib.error
from typing import Any, Dict, Optional

from ..config import get_slack_config, is_slack_enabled
from ..observability import log_debug, log_warning


# Rate limiting state
_last_notification_time: float = 0.0
_notification_lock = threading.Lock()


def _rate_limit_ok(min_interval: float) -> bool:
    """Check if enough time has passed since last notification."""
    global _last_notification_time
    with _notification_lock:
        now = time.time()
        if now - _last_notification_time < min_interval:
            return False
        _last_notification_time = now
        return True


def send_webhook(
    webhook_url: str,
    payload: Dict[str, Any],
    timeout: float = 5.0,
) -> bool:
    """Send a payload to a Slack webhook URL.

    Args:
        webhook_url: Slack Incoming Webhook URL
        payload: Message payload (will be JSON-encoded)
        timeout: Request timeout in seconds

    Returns:
        True if successful, False otherwise
    """
    if not webhook_url:
        return False

    try:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except urllib.error.URLError as e:
        log_warning(f"[SLACK] Webhook failed: {e}")
        return False
    except Exception as e:
        log_warning(f"[SLACK] Webhook error: {e}")
        return False


def _send_async(webhook_url: str, payload: Dict[str, Any]) -> None:
    """Send webhook notification in background thread (fire-and-forget)."""
    def _worker():
        send_webhook(webhook_url, payload)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


def _format_thread_link(topic: str, code_repo: Optional[str] = None) -> str:
    """Format a link to the thread (for future dashboard integration)."""
    # For now, just return the topic name
    # Future: link to watercooler-site dashboard
    return f"`{topic}`"


def notify_new_entry(
    topic: str,
    agent: str,
    title: str,
    role: str,
    entry_type: str,
    code_repo: Optional[str] = None,
    ball: Optional[str] = None,
) -> bool:
    """Notify Slack when a new entry is added to a thread.

    Args:
        topic: Thread topic identifier
        agent: Agent who created the entry
        title: Entry title
        role: Agent role (planner, implementer, etc.)
        entry_type: Entry type (Note, Plan, Decision, etc.)
        code_repo: Optional code repository name
        ball: Current ball owner after the entry

    Returns:
        True if notification was sent (or queued), False if skipped
    """
    if not is_slack_enabled():
        return False

    config = get_slack_config()
    if not config.get("notify_on_say"):
        log_debug("[SLACK] notify_on_say disabled, skipping")
        return False

    if not _rate_limit_ok(config.get("min_notification_interval", 1.0)):
        log_debug("[SLACK] Rate limited, skipping notification")
        return False

    thread_link = _format_thread_link(topic, code_repo)

    # Build Slack message with Block Kit formatting
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":speech_balloon: *New Entry* in {thread_link}",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Title:*\n{title}"},
                {"type": "mrkdwn", "text": f"*Agent:*\n{agent}"},
                {"type": "mrkdwn", "text": f"*Role:*\n{role}"},
                {"type": "mrkdwn", "text": f"*Type:*\n{entry_type}"},
            ],
        },
    ]

    if ball:
        blocks.append({
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f":tennis: Ball is with *{ball}*"},
            ],
        })

    payload = {
        "text": f"New entry in {topic}: {title}",  # Fallback for notifications
        "blocks": blocks,
    }

    webhook_url = config.get("webhook_url", "")
    log_debug(f"[SLACK] Sending new entry notification for {topic}")
    _send_async(webhook_url, payload)
    return True


def notify_ball_flip(
    topic: str,
    from_agent: str,
    to_agent: str,
    title: Optional[str] = None,
    code_repo: Optional[str] = None,
) -> bool:
    """Notify Slack when the ball is flipped to another agent.

    Args:
        topic: Thread topic identifier
        from_agent: Agent passing the ball
        to_agent: Agent receiving the ball
        title: Optional entry title that caused the flip
        code_repo: Optional code repository name

    Returns:
        True if notification was sent (or queued), False if skipped
    """
    if not is_slack_enabled():
        return False

    config = get_slack_config()
    if not config.get("notify_on_ball_flip"):
        log_debug("[SLACK] notify_on_ball_flip disabled, skipping")
        return False

    if not _rate_limit_ok(config.get("min_notification_interval", 1.0)):
        log_debug("[SLACK] Rate limited, skipping notification")
        return False

    thread_link = _format_thread_link(topic, code_repo)

    text = f":tennis: Ball flipped in {thread_link}"
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": text,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*From:*\n{from_agent}"},
                {"type": "mrkdwn", "text": f"*To:*\n{to_agent}"},
            ],
        },
    ]

    if title:
        blocks.append({
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"_\"{title}\"_"},
            ],
        })

    payload = {
        "text": f"Ball passed from {from_agent} to {to_agent} in {topic}",
        "blocks": blocks,
    }

    webhook_url = config.get("webhook_url", "")
    log_debug(f"[SLACK] Sending ball flip notification for {topic}")
    _send_async(webhook_url, payload)
    return True


def notify_handoff(
    topic: str,
    from_agent: str,
    to_agent: str,
    note: Optional[str] = None,
    code_repo: Optional[str] = None,
) -> bool:
    """Notify Slack when an explicit handoff occurs.

    Args:
        topic: Thread topic identifier
        from_agent: Agent initiating handoff
        to_agent: Agent receiving handoff
        note: Optional handoff note
        code_repo: Optional code repository name

    Returns:
        True if notification was sent (or queued), False if skipped
    """
    if not is_slack_enabled():
        return False

    config = get_slack_config()
    if not config.get("notify_on_handoff"):
        log_debug("[SLACK] notify_on_handoff disabled, skipping")
        return False

    if not _rate_limit_ok(config.get("min_notification_interval", 1.0)):
        log_debug("[SLACK] Rate limited, skipping notification")
        return False

    thread_link = _format_thread_link(topic, code_repo)

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":handshake: *Handoff* in {thread_link}",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*From:*\n{from_agent}"},
                {"type": "mrkdwn", "text": f"*To:*\n{to_agent}"},
            ],
        },
    ]

    if note:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Note:*\n{note}",
            },
        })

    payload = {
        "text": f"Handoff from {from_agent} to {to_agent} in {topic}",
        "blocks": blocks,
    }

    webhook_url = config.get("webhook_url", "")
    log_debug(f"[SLACK] Sending handoff notification for {topic}")
    _send_async(webhook_url, payload)
    return True


def notify_status_change(
    topic: str,
    old_status: Optional[str],
    new_status: str,
    agent: Optional[str] = None,
    code_repo: Optional[str] = None,
) -> bool:
    """Notify Slack when a thread's status changes.

    Args:
        topic: Thread topic identifier
        old_status: Previous status (or None if unknown)
        new_status: New status
        agent: Agent who changed the status
        code_repo: Optional code repository name

    Returns:
        True if notification was sent (or queued), False if skipped
    """
    if not is_slack_enabled():
        return False

    config = get_slack_config()
    if not config.get("notify_on_status_change"):
        log_debug("[SLACK] notify_on_status_change disabled, skipping")
        return False

    if not _rate_limit_ok(config.get("min_notification_interval", 1.0)):
        log_debug("[SLACK] Rate limited, skipping notification")
        return False

    thread_link = _format_thread_link(topic, code_repo)

    # Choose emoji based on status
    status_emoji = {
        "OPEN": ":green_circle:",
        "IN_REVIEW": ":large_yellow_circle:",
        "BLOCKED": ":red_circle:",
        "CLOSED": ":white_check_mark:",
    }.get(new_status.upper(), ":large_blue_circle:")

    status_text = f"{old_status} → {new_status}" if old_status else new_status

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{status_emoji} *Status Changed* in {thread_link}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{status_text}*",
            },
        },
    ]

    if agent:
        blocks.append({
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"Changed by {agent}"},
            ],
        })

    payload = {
        "text": f"Status changed to {new_status} in {topic}",
        "blocks": blocks,
    }

    webhook_url = config.get("webhook_url", "")
    log_debug(f"[SLACK] Sending status change notification for {topic}")
    _send_async(webhook_url, payload)
    return True
