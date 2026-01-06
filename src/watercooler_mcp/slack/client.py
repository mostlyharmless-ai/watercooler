"""Slack API client for watercooler integration.

Provides high-level operations:
- Channel lookup/creation
- Message posting (threaded replies)
- User info lookup

Uses the bot token from config for authentication.
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..config import get_slack_config
from .mapping import (
    SlackChannelMapping,
    SlackThreadMapping,
    SlackMappingStore,
    get_mapping_store,
)

logger = logging.getLogger(__name__)


class SlackAPIError(Exception):
    """Error from Slack API."""

    def __init__(self, error: str, response: Optional[Dict] = None):
        self.error = error
        self.response = response
        super().__init__(f"Slack API error: {error}")


class SlackClient:
    """Client for Slack Web API.

    Uses bot token for authentication. All methods are synchronous
    to keep the implementation simple - async can be added later if needed.
    """

    BASE_URL = "https://slack.com/api"
    DEFAULT_TIMEOUT = 10.0

    def __init__(self, bot_token: Optional[str] = None):
        """Initialize Slack client.

        Args:
            bot_token: Slack bot token (xoxb-...). If None, reads from config.
        """
        if bot_token is None:
            config = get_slack_config()
            bot_token = config.bot_token if config else ""

        if not bot_token:
            raise ValueError("Slack bot token not configured")

        self._token = bot_token
        self._store = get_mapping_store()

    def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Dict[str, Any]:
        """Make a request to Slack API.

        Args:
            method: HTTP method (GET or POST)
            endpoint: API endpoint (e.g., "conversations.list")
            data: Request body for POST requests
            timeout: Request timeout in seconds

        Returns:
            Parsed JSON response

        Raises:
            SlackAPIError: If API returns ok=false or request fails
        """
        url = f"{self.BASE_URL}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        request_data = None
        if data:
            request_data = json.dumps(data).encode("utf-8")

        try:
            request = urllib.request.Request(
                url,
                data=request_data,
                headers=headers,
                method=method,
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))

            if not result.get("ok"):
                raise SlackAPIError(result.get("error", "unknown"), result)

            return result

        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8") if e.fp else ""
            logger.error(f"Slack API HTTP error: {e.code} - {body}")
            raise SlackAPIError(f"HTTP {e.code}: {body}")

        except urllib.error.URLError as e:
            logger.error(f"Slack API URL error: {e.reason}")
            raise SlackAPIError(f"Network error: {e.reason}")

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON from Slack: {e}")
            raise SlackAPIError(f"Invalid response: {e}")

    # Channel operations

    def list_channels(
        self,
        types: str = "public_channel",
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """List channels the bot can see.

        Args:
            types: Channel types (public_channel, private_channel)
            limit: Maximum channels to return

        Returns:
            List of channel objects
        """
        result = self._request(
            "POST",
            "conversations.list",
            {"types": types, "limit": limit, "exclude_archived": True},
        )
        return result.get("channels", [])

    def find_channel_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Find a channel by name.

        Args:
            name: Channel name (with or without #)

        Returns:
            Channel object if found, None otherwise
        """
        # Strip # prefix if present
        name = name.lstrip("#")

        channels = self.list_channels()
        for ch in channels:
            if ch.get("name") == name:
                return ch
        return None

    def create_channel(self, name: str) -> Dict[str, Any]:
        """Create a new public channel.

        Args:
            name: Channel name (without #, max 80 chars)

        Returns:
            Created channel object

        Raises:
            SlackAPIError: If creation fails (e.g., name taken)
        """
        # Normalize name: lowercase, no #, replace spaces with hyphens
        name = name.lower().lstrip("#").replace(" ", "-")[:80]

        result = self._request(
            "POST",
            "conversations.create",
            {"name": name, "is_private": False},
        )
        return result.get("channel", {})

    def join_channel(self, channel_id: str) -> bool:
        """Join a channel.

        Args:
            channel_id: Channel ID to join

        Returns:
            True if successful
        """
        self._request("POST", "conversations.join", {"channel": channel_id})
        return True

    def get_or_create_channel(
        self,
        repo: str,
        prefix: str = "wc-",
    ) -> Tuple[str, str, bool]:
        """Get or create channel for a repository.

        Checks mapping store first, then Slack, then creates if needed.

        Args:
            repo: Repository name (e.g., "watercooler-cloud")
            prefix: Channel name prefix (e.g., "wc-")

        Returns:
            Tuple of (channel_id, channel_name, was_created)
        """
        # Check mapping store first
        existing = self._store.get_channel(repo)
        if existing:
            logger.debug(f"Using cached channel {existing.slack_channel_name}")
            return existing.slack_channel_id, existing.slack_channel_name, False

        # Build expected channel name
        channel_name = f"{prefix}{repo}"

        # Search in Slack
        channel = self.find_channel_by_name(channel_name)
        if channel:
            channel_id = channel["id"]
            logger.info(f"Found existing channel #{channel_name}")

            # Join if not already a member
            if not channel.get("is_member"):
                self.join_channel(channel_id)

            # Store mapping
            mapping = SlackChannelMapping(
                repo=repo,
                slack_channel_id=channel_id,
                slack_channel_name=f"#{channel_name}",
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self._store.set_channel(mapping)

            return channel_id, f"#{channel_name}", False

        # Create new channel
        config = get_slack_config()
        if config and not config.get("auto_create_channels", True):
            raise SlackAPIError(
                f"Channel #{channel_name} not found and auto_create_channels=false"
            )

        logger.info(f"Creating channel #{channel_name}")
        channel = self.create_channel(channel_name)
        channel_id = channel["id"]

        # Store mapping
        mapping = SlackChannelMapping(
            repo=repo,
            slack_channel_id=channel_id,
            slack_channel_name=f"#{channel_name}",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._store.set_channel(mapping)

        return channel_id, f"#{channel_name}", True

    # Message operations

    def post_message(
        self,
        channel_id: str,
        text: str,
        blocks: Optional[List[Dict]] = None,
        thread_ts: Optional[str] = None,
        reply_broadcast: bool = False,
    ) -> Dict[str, Any]:
        """Post a message to a channel.

        Args:
            channel_id: Channel to post to
            text: Message text (fallback for notifications)
            blocks: Block Kit blocks (optional)
            thread_ts: Thread timestamp for replies
            reply_broadcast: Also post to channel when replying to thread

        Returns:
            Message object with ts (timestamp)
        """
        payload: Dict[str, Any] = {
            "channel": channel_id,
            "text": text,
        }

        if blocks:
            payload["blocks"] = blocks

        if thread_ts:
            payload["thread_ts"] = thread_ts
            if reply_broadcast:
                payload["reply_broadcast"] = True

        result = self._request("POST", "chat.postMessage", payload)
        return result

    def post_thread_parent(
        self,
        channel_id: str,
        topic: str,
        status: str = "OPEN",
        ball_owner: str = "",
        entry_count: int = 0,
    ) -> str:
        """Post a thread parent message for a watercooler thread.

        Args:
            channel_id: Channel to post to
            topic: Thread topic
            status: Thread status
            ball_owner: Current ball owner
            entry_count: Number of entries

        Returns:
            Message ts (to use as thread_ts for replies)
        """
        # Simple text format for now - Block Kit can be added in step 2.5
        text = f"🧵 *{topic}*\nStatus: {status}"
        if ball_owner:
            text += f" | Ball: {ball_owner}"
        if entry_count:
            text += f" | Entries: {entry_count}"

        result = self.post_message(channel_id, text)
        return result.get("ts", "")

    def post_entry_reply(
        self,
        channel_id: str,
        thread_ts: str,
        entry_id: str,
        agent: str,
        role: str,
        entry_type: str,
        title: str,
        body: str,
        timestamp: str,
    ) -> str:
        """Post a thread entry as a Slack reply.

        Args:
            channel_id: Channel ID
            thread_ts: Parent message ts
            entry_id: Watercooler entry ID
            agent: Entry author
            role: Author role
            entry_type: Entry type (Note, Plan, etc.)
            title: Entry title
            body: Entry body (truncated if too long)
            timestamp: Entry timestamp

        Returns:
            Message ts of the reply
        """
        # Truncate body if too long (Slack limit is ~3000 chars for text)
        max_body = 2500
        if len(body) > max_body:
            body = body[: max_body - 3] + "..."

        # Format entry as Slack message
        text = f"*{title}* by {agent} ({role}) • {entry_type}\n"
        text += "─" * 40 + "\n"
        text += body
        text += f"\n\n_{timestamp}_"

        result = self.post_message(channel_id, text, thread_ts=thread_ts)
        return result.get("ts", "")

    # User operations

    def get_user_info(self, user_id: str) -> Dict[str, Any]:
        """Get user info by ID.

        Args:
            user_id: Slack user ID (U...)

        Returns:
            User object with profile info
        """
        result = self._request("POST", "users.info", {"user": user_id})
        return result.get("user", {})

    def get_user_email(self, user_id: str) -> Optional[str]:
        """Get user's email address.

        Args:
            user_id: Slack user ID

        Returns:
            Email address if available, None otherwise
        """
        user = self.get_user_info(user_id)
        return user.get("profile", {}).get("email")

    def get_user_display_name(self, user_id: str) -> str:
        """Get user's display name.

        Args:
            user_id: Slack user ID

        Returns:
            Display name (falls back to real_name or user_id)
        """
        user = self.get_user_info(user_id)
        profile = user.get("profile", {})

        return (
            profile.get("display_name")
            or profile.get("real_name")
            or user.get("name")
            or user_id
        )


# Convenience function for one-off operations
_client: Optional[SlackClient] = None


def get_slack_client(bot_token: Optional[str] = None) -> SlackClient:
    """Get or create the Slack client singleton.

    Args:
        bot_token: Optional token override

    Returns:
        SlackClient instance
    """
    global _client
    if _client is None or bot_token:
        _client = SlackClient(bot_token)
    return _client
