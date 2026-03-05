from __future__ import annotations

import contextvars
import getpass
import json
import os
import re
from pathlib import Path
from typing import Tuple

# Context variable for user tag from HTTP request (Remote MCP)
_user_tag_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar('user_tag', default=None)


def _load_agents_registry(path: str | None) -> dict:
    """Load agents registry from JSON file, merging with defaults.

    The registry structure is:
        {
            "canonical": {"claude": "Claude", "codex": "Codex", ...},
            "counterpart": {"Codex": "Claude", "Claude": "Codex", ...},
            "default_ball": "Team"
        }
    """
    default_registry = {
        "canonical": {"claude": "Claude", "codex": "Codex"},
        "counterpart": {"Codex": "Claude", "Claude": "Codex"},
        "default_ball": "Team",
    }
    if not path:
        return default_registry
    p = Path(path)
    if not p.exists():
        return default_registry
    try:
        file_registry = json.loads(p.read_text(encoding="utf-8"))
        # Merge loaded registry into default registry for any missing keys:
        merged = default_registry.copy()
        merged.update(file_registry)
        # For nested dict "canonical" and "counterpart", merge keys:
        for key in ["canonical", "counterpart"]:
            if key in file_registry and isinstance(file_registry[key], dict):
                merged[key] = {**default_registry.get(key, {}), **file_registry[key]}
        return merged
    except Exception:
        return default_registry


def _split_agent_and_tag(agent: str) -> Tuple[str, str | None]:
    """Parse agent strings in the format 'Agent (user)'.

    Returns a tuple: (agent, tag) where tag is None if not present.
    """
    match = re.match(r"^(.*?)(?:\s*\(([^)]+)\))\s*$", agent)
    if match:
        base = match.group(1).strip()
        tag = match.group(2).strip()
        return base, tag or None
    return agent.strip(), None


def set_user_tag(user_tag: str | None) -> None:
    """Set the user tag for the current context (Remote MCP requests)."""
    _user_tag_ctx.set(user_tag)


def _get_git_user() -> str | None:
    """Get user tag, preferring context variable (Remote MCP) over OS username.

    For Remote MCP: Returns user tag from HTTP request context (GitHub username).
    For Local MCP: Returns OS username via getpass.getuser().
    """
    # Check context variable first (Remote MCP)
    ctx_user = _user_tag_ctx.get()
    if ctx_user:
        return ctx_user

    # Fallback to OS user (Local MCP)
    try:
        return getpass.getuser()
    except Exception:
        return None


def _canonical_agent(agent: str, registry: dict | None = None, user_tag: str | None = None) -> str:
    """Return the canonical agent name with user tag.

    Looks up the base in registry["canonical"] using a lower-case key.
    If no tag is provided, appends the user tag (if available) in the form " (user)".
    
    Args:
        agent: Agent string, optionally with tag in format "Agent (tag)"
        registry: Optional agent registry
        user_tag: Optional explicit user tag (takes precedence over context variable and agent string)
    """
    a, tag = _split_agent_and_tag(agent.strip())
    base_key = a.lower()
    # Default canonical mapping for common agents
    default_canonical = {"codex": "Codex", "claude": "Claude", "team": "Team"}
    canonical_map = (registry or {}).get("canonical", default_canonical)
    canonical = canonical_map.get(base_key, a)
    # Priority: explicit user_tag > tag from agent string > context variable/OS user
    if user_tag:
        tag = user_tag
    elif not tag:
        tag = _get_git_user()
    return f"{canonical} ({tag})" if tag else canonical


def _counterpart_of(agent: str, registry: dict | None = None) -> str:
    """
    Return the counterpart agent after resolving multi-agent chains.

    Uses the registry["counterpart"] mapping to follow a chain of counterparts.
    For simple 2-agent flips (A→B, B→A), returns B when given A.
    For multi-agent chains (A→B→C), follows the chain until end or cycle.
    The user tag, if any, is then reattached in the form " (tag)".

    NOTE: In multi-agent scenarios, any agent can manually set the ball to any other
    agent using handoff() or explicit ball= parameter. This could lead to collision
    situations if multiple agents respond simultaneously, but this is expected to be
    rare and non-critical. Future enhancement: Add optimistic locking or conflict
    resolution if collision issues arise.
    """
    # Default counterpart mapping uses canonical (capitalized) keys to match _canonical_agent output
    counterpart_map = (registry or {}).get("counterpart", {"Codex": "Claude", "Claude": "Codex"})
    # Get the canonical base and separate tag.
    canon_with_tag = _canonical_agent(agent, registry)
    base, tag = _split_agent_and_tag(canon_with_tag)

    # Simple case: just one hop to the counterpart
    if base in counterpart_map:
        current = counterpart_map[base]
    else:
        current = base  # No counterpart defined, return self

    return f"{current} ({tag})" if tag else current


def _default_agent_and_role(registry: dict | None = None) -> Tuple[str, str]:
    """Returns a tuple of default agent from registry and current user name."""
    user = _get_git_user() or "user"
    default = (registry or {}).get("default_ball", "Team")
    return default, user
