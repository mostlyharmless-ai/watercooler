"""Token service client for multi-workspace Slack support.

Fetches bot tokens from the watercooler-site token service API,
enabling MCP to post to multiple Slack workspaces without
requiring a single WATERCOOLER_SLACK_BOT_TOKEN env var.

Environment variables:
- WATERCOOLER_TOKEN_API_URL: Base URL of the token service (e.g., https://watercoolerdev.com)
- WATERCOOLER_TOKEN_API_KEY: API key for authentication

Usage:
    from watercooler_mcp.slack.token_service import get_workspace_token

    token = get_workspace_token("T12345ABC")
    if token:
        client = SlackClient(bot_token=token)
        # Use client for this workspace
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# TTL for cached tokens (default 1 hour, configurable)
SLACK_TOKEN_CACHE_TTL = int(os.getenv("WATERCOOLER_SLACK_TOKEN_CACHE_TTL", "3600"))


@dataclass
class CachedSlackToken:
    """Cached Slack token with expiration tracking."""

    token: str
    cached_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        """Check if the cached token has exceeded the TTL."""
        return (time.time() - self.cached_at) > SLACK_TOKEN_CACHE_TTL


# Cache for workspace tokens (workspace_id -> CachedSlackToken)
# Tokens are cached with TTL to avoid unbounded memory growth
_token_cache: Dict[str, CachedSlackToken] = {}


def get_token_service_config() -> Dict[str, str]:
    """Get token service configuration from environment.

    Returns:
        Dict with token_api_url and token_api_key
    """
    return {
        "token_api_url": os.getenv("WATERCOOLER_TOKEN_API_URL", ""),
        "token_api_key": os.getenv("WATERCOOLER_TOKEN_API_KEY", ""),
    }


def is_token_service_configured() -> bool:
    """Check if the token service is configured.

    Returns:
        True if both API URL and key are set
    """
    config = get_token_service_config()
    return bool(config["token_api_url"]) and bool(config["token_api_key"])


def get_workspace_token(workspace_id: str, use_cache: bool = True) -> Optional[str]:
    """Fetch bot token for a Slack workspace from the token service.

    Args:
        workspace_id: Slack team/workspace ID (e.g., "T12345ABC")
        use_cache: Whether to use cached tokens (default: True)

    Returns:
        Bot token (xoxb-...) if found, None otherwise

    Note:
        If the token service is not configured (missing URL or API key),
        this function returns None without making any API calls.
    """
    if not workspace_id:
        logger.warning("get_workspace_token called with empty workspace_id")
        return None

    # Check cache first
    if use_cache and workspace_id in _token_cache:
        cached = _token_cache[workspace_id]
        if not cached.is_expired():
            logger.debug(f"Using cached token for workspace {workspace_id}")
            return cached.token
        else:
            # Remove expired token
            logger.debug(f"Cached token expired for workspace {workspace_id}, refreshing")
            del _token_cache[workspace_id]

    config = get_token_service_config()
    api_url = config["token_api_url"]
    api_key = config["token_api_key"]

    if not api_url or not api_key:
        logger.debug("Token service not configured, skipping API call")
        return None

    # Build request URL
    url = f"{api_url.rstrip('/')}/api/slack/token?workspace={workspace_id}"

    try:
        request = urllib.request.Request(
            url,
            headers={
                "x-api-key": api_key,
                "Accept": "application/json",
            },
            method="GET",
        )

        with urllib.request.urlopen(request, timeout=10.0) as response:
            data = json.loads(response.read().decode("utf-8"))

        token = data.get("token")
        if token:
            # Cache the token with TTL
            _token_cache[workspace_id] = CachedSlackToken(token=token)
            logger.info(
                f"Retrieved token for workspace {workspace_id} "
                f"(team: {data.get('teamName', 'unknown')})"
            )
            return token

        logger.warning(f"Token service returned no token for workspace {workspace_id}")
        return None

    except urllib.error.HTTPError as e:
        if e.code == 404:
            logger.info(f"Workspace {workspace_id} not found in token service")
        elif e.code == 401:
            logger.error("Token service authentication failed - check WATERCOOLER_TOKEN_API_KEY")
        else:
            body = e.read().decode("utf-8") if e.fp else ""
            logger.error(f"Token service HTTP error {e.code}: {body}")
        return None

    except urllib.error.URLError as e:
        logger.error(f"Token service connection error: {e.reason}")
        return None

    except json.JSONDecodeError as e:
        logger.error(f"Token service returned invalid JSON: {e}")
        return None

    except Exception as e:
        logger.error(f"Unexpected error fetching workspace token: {e}")
        return None


def clear_token_cache() -> None:
    """Clear the token cache.

    Useful for testing or when tokens may have been rotated.
    """
    _token_cache.clear()
    logger.debug("Token cache cleared")


def invalidate_workspace_token(workspace_id: str) -> None:
    """Remove a specific workspace token from cache.

    Call this if a token is rejected by Slack API to force a refresh.

    Args:
        workspace_id: Slack workspace ID to invalidate
    """
    if workspace_id in _token_cache:
        del _token_cache[workspace_id]
        logger.debug(f"Invalidated cached token for workspace {workspace_id}")
