"""Block Kit message templates for Slack.

Provides formatted message blocks for:
- Thread parent messages (topic header)
- Entry replies (structured content)
- Interactive buttons (Ack, Handoff, Close)

Block Kit reference: https://api.slack.com/block-kit
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _section(text: str, accessory: Optional[Dict] = None) -> Dict[str, Any]:
    """Create a section block."""
    block: Dict[str, Any] = {
        "type": "section",
        "text": {"type": "mrkdwn", "text": text},
    }
    if accessory:
        block["accessory"] = accessory
    return block


def _context(elements: List[str]) -> Dict[str, Any]:
    """Create a context block with mrkdwn elements."""
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": e} for e in elements],
    }


def _divider() -> Dict[str, Any]:
    """Create a divider block."""
    return {"type": "divider"}


def _button(
    text: str,
    action_id: str,
    value: str,
    style: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a button element.

    Args:
        text: Button label
        action_id: Unique action identifier
        value: Value passed to action handler
        style: Optional "primary" or "danger"
    """
    button: Dict[str, Any] = {
        "type": "button",
        "text": {"type": "plain_text", "text": text, "emoji": True},
        "action_id": action_id,
        "value": value,
    }
    if style:
        button["style"] = style
    return button


def _actions(elements: List[Dict]) -> Dict[str, Any]:
    """Create an actions block with buttons/menus."""
    return {"type": "actions", "elements": elements}


# Thread parent message blocks


def thread_parent_blocks(
    topic: str,
    status: str = "OPEN",
    ball_owner: str = "",
    entry_count: int = 0,
    repo: Optional[str] = None,
    branch: Optional[str] = None,
    include_buttons: bool = True,
) -> List[Dict[str, Any]]:
    """Create Block Kit blocks for thread parent message.

    Args:
        topic: Thread topic (e.g., "slack-integration")
        status: Thread status (OPEN, IN_REVIEW, CLOSED, BLOCKED)
        ball_owner: Current ball owner (agent name)
        entry_count: Number of entries in thread
        repo: Repository name for context
        branch: Git branch for this thread (enables branch-aware sync)
        include_buttons: Whether to include action buttons

    Returns:
        List of Block Kit blocks
    """
    # Status emoji mapping
    status_emoji = {
        "OPEN": "🟢",
        "IN_REVIEW": "🟡",
        "CLOSED": "⚪",
        "BLOCKED": "🔴",
    }.get(status.upper(), "⚪")

    # Header with topic
    blocks: List[Dict[str, Any]] = [
        _section(f"🧵 *{topic}*"),
    ]

    # Status line
    status_parts = [f"{status_emoji} {status}"]
    if ball_owner:
        status_parts.append(f"🎾 Ball: *{ball_owner}*")
    if entry_count:
        status_parts.append(f"📝 {entry_count} entries")

    blocks.append(_context(status_parts))

    # Parseable metadata for watercooler-site auto-discovery
    # Format: wc:repo/topic@branch - enables reverse lookup from Slack events
    # Branch suffix is optional but critical for branch-aware sync
    if repo:
        if branch:
            blocks.append(_context([f"`wc:{repo}/{topic}@{branch}`"]))
        else:
            blocks.append(_context([f"`wc:{repo}/{topic}`"]))

    # Action buttons (for interactive messages)
    if include_buttons:
        # Value encodes topic for action handlers
        action_value = topic
        if repo:
            action_value = f"{repo}:{topic}"

        buttons = [
            _button("✓ Ack", "wc_ack", action_value),
            _button("🔄 Handoff", "wc_handoff", action_value),
        ]

        if status.upper() != "CLOSED":
            buttons.append(_button("📋 Close", "wc_close", action_value, style="danger"))

        blocks.append(_actions(buttons))

    return blocks


def thread_parent_text(
    topic: str,
    status: str = "OPEN",
    ball_owner: str = "",
    entry_count: int = 0,
) -> str:
    """Create fallback text for thread parent message.

    Used when blocks can't be rendered (notifications, etc.)
    """
    parts = [f"🧵 *{topic}*", f"Status: {status}"]
    if ball_owner:
        parts.append(f"Ball: {ball_owner}")
    if entry_count:
        parts.append(f"Entries: {entry_count}")
    return " | ".join(parts)


# Entry reply blocks


def entry_reply_blocks(
    entry_id: str,
    agent: str,
    role: str,
    entry_type: str,
    title: str,
    body: str,
    timestamp: str,
    spec: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Create Block Kit blocks for an entry reply.

    Args:
        entry_id: Watercooler entry ID (ULID)
        agent: Entry author (e.g., "Claude")
        role: Author role (planner, implementer, etc.)
        entry_type: Entry type (Note, Plan, Decision, etc.)
        title: Entry title
        body: Entry body (markdown)
        timestamp: Entry timestamp (ISO or formatted)
        spec: Agent specialization if different from role

    Returns:
        List of Block Kit blocks
    """
    # Role emoji mapping
    role_emoji = {
        "planner": "📐",
        "critic": "🔍",
        "implementer": "⚙️",
        "tester": "🧪",
        "pm": "📋",
        "scribe": "📝",
    }.get(role.lower(), "💬")

    # Type badge
    type_badge = {
        "Note": "📌",
        "Plan": "🗺️",
        "Decision": "⚖️",
        "PR": "🔀",
        "Closure": "✅",
    }.get(entry_type, "📌")

    blocks: List[Dict[str, Any]] = []

    # Header line: Title by Agent (role) • Type
    header = f"*{title}*"
    meta = f"{role_emoji} {agent} ({role}) • {type_badge} {entry_type}"
    if spec and spec.lower() != role.lower():
        meta += f" [{spec}]"

    blocks.append(_section(header))
    blocks.append(_context([meta]))
    blocks.append(_divider())

    # Body - truncate if too long (Slack limit ~3000 chars)
    max_body = 2800
    display_body = body
    if len(body) > max_body:
        display_body = body[: max_body - 20] + "\n\n_...truncated_"

    blocks.append(_section(display_body))

    # Footer with timestamp and entry ID
    blocks.append(_context([f"_{timestamp}_ • `{entry_id[:12]}...`"]))

    return blocks


def entry_reply_text(
    agent: str,
    role: str,
    entry_type: str,
    title: str,
    body: str,
    timestamp: str,
) -> str:
    """Create fallback text for entry reply.

    Used when blocks can't be rendered.
    """
    # Truncate body for text fallback
    max_body = 500
    display_body = body[:max_body] + "..." if len(body) > max_body else body

    lines = [
        f"*{title}* by {agent} ({role}) • {entry_type}",
        "─" * 40,
        display_body,
        f"_{timestamp}_",
    ]
    return "\n".join(lines)


# Status update blocks


def status_update_blocks(
    topic: str,
    old_status: str,
    new_status: str,
    changed_by: str,
) -> List[Dict[str, Any]]:
    """Create blocks for status change notification."""
    emoji_map = {
        "OPEN": "🟢",
        "IN_REVIEW": "🟡",
        "CLOSED": "⚪",
        "BLOCKED": "🔴",
    }
    old_emoji = emoji_map.get(old_status.upper(), "⚪")
    new_emoji = emoji_map.get(new_status.upper(), "⚪")

    return [
        _section(
            f"🔄 Status changed: {old_emoji} {old_status} → {new_emoji} *{new_status}*"
        ),
        _context([f"Changed by {changed_by}"]),
    ]


def ball_flip_blocks(
    topic: str,
    from_agent: str,
    to_agent: str,
    note: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Create blocks for ball flip notification."""
    blocks = [
        _section(f"🎾 Ball passed: {from_agent} → *{to_agent}*"),
    ]
    if note:
        blocks.append(_context([f"_{note}_"]))
    return blocks


def handoff_blocks(
    topic: str,
    from_agent: str,
    to_agent: str,
    note: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Create blocks for explicit handoff notification."""
    blocks = [
        _section(f"🤝 Handoff: {from_agent} → *{to_agent}*"),
    ]
    if note:
        blocks.append(_section(f"_{note}_"))
    return blocks
