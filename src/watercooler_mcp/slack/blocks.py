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


def _overflow(
    action_id: str,
    options: List[Dict[str, str]],
) -> Dict[str, Any]:
    """Create an overflow menu element.

    Args:
        action_id: Unique action identifier
        options: List of {text, value} dicts for menu items
    """
    return {
        "type": "overflow",
        "action_id": action_id,
        "options": [
            {
                "text": {"type": "plain_text", "text": opt["text"], "emoji": True},
                "value": opt["value"],
            }
            for opt in options
        ],
    }


# Thread parent message blocks


def thread_parent_blocks(
    topic: str,
    status: str = "OPEN",
    ball_owner: str = "",
    entry_count: int = 0,
    repo: Optional[str] = None,
    branch: Optional[str] = None,
    include_buttons: bool = True,
    is_closed: bool = False,
    closure_summary: Optional[str] = None,
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
        is_closed: If True, render closed variant (no buttons, CLOSED status)
        closure_summary: Optional summary text to display when closed

    Returns:
        List of Block Kit blocks
    """
    # Branch indicator - show prominently for non-main branches
    branch_indicator = ""
    if branch and branch != "main":
        branch_indicator = f" `[{branch}]`"

    # Handle closed threads with distinct visual styling (early return, no buttons)
    if is_closed:
        blocks: List[Dict[str, Any]] = [
            # Strikethrough topic + checkmark for visual distinction
            _section(f"~🧵 *{topic}*~ ✅{branch_indicator}"),
            _context([f"⚪ CLOSED • {entry_count} entries archived"]),
        ]
        # Closure summary shown prominently
        if closure_summary:
            blocks.append(_divider())
            blocks.append(_section(f"📋 *Resolution*\n{closure_summary}"))
        # Parseable metadata for watercooler-site auto-discovery
        if repo:
            if branch:
                blocks.append(_context([f"`wc:{repo}/{topic}@{branch}`"]))
            else:
                blocks.append(_context([f"`wc:{repo}/{topic}`"]))
        return blocks  # No action buttons for closed threads

    # Status emoji mapping (for open threads)
    status_emoji = {
        "OPEN": "🟢",
        "IN_REVIEW": "🟡",
        "CLOSED": "⚪",
        "BLOCKED": "🔴",
    }.get(status.upper(), "⚪")

    # Header with topic and branch badge
    blocks: List[Dict[str, Any]] = [
        _section(f"🧵 *{topic}*{branch_indicator}"),
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
        # Format: owner/repo:topic:branch (branch optional)
        action_value = topic
        if repo:
            if branch:
                action_value = f"{repo}:{topic}:{branch}"
            else:
                action_value = f"{repo}:{topic}"

        # Primary actions row
        buttons: List[Dict[str, Any]] = [
            _button("✓ Ack", "wc_ack", action_value),
            _button("🔄 Handoff", "wc_handoff", action_value),
            _button("📝 Add Entry", "wc_add_entry", action_value),
        ]

        # Overflow menu with secondary actions
        overflow_options = [
            {"text": "📊 Change Status", "value": f"status:{action_value}"},
            {"text": "🔗 View in Dashboard", "value": f"dashboard:{action_value}"},
            {"text": "📋 Copy Thread Link", "value": f"copy_link:{action_value}"},
        ]
        buttons.append(_overflow("wc_overflow", overflow_options))

        # Close button only if not already closed
        if status.upper() != "CLOSED":
            buttons.append(_button("Close", "wc_close", action_value, style="danger"))

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
