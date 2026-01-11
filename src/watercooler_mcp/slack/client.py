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
from .token_service import get_workspace_token, is_token_service_configured

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

    Token resolution order:
    1. Explicit bot_token parameter
    2. Token service API (if workspace_id provided and service configured)
    3. WATERCOOLER_SLACK_BOT_TOKEN env var / config file
    """

    BASE_URL = "https://slack.com/api"
    DEFAULT_TIMEOUT = 10.0

    def __init__(
        self,
        bot_token: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ):
        """Initialize Slack client.

        Args:
            bot_token: Slack bot token (xoxb-...). If None, tries other sources.
            workspace_id: Slack workspace ID (T...). Used to fetch token from
                         token service if bot_token is not provided.
        """
        self._workspace_id = workspace_id

        # Token resolution
        if bot_token is None and workspace_id and is_token_service_configured():
            # Try token service first for multi-workspace support
            bot_token = get_workspace_token(workspace_id)
            if bot_token:
                logger.info(f"Using token from token service for workspace {workspace_id}")

        if bot_token is None:
            # Fall back to config/env var
            config = get_slack_config()
            bot_token = config.get("bot_token", "") if config else ""

        if not bot_token:
            raise ValueError(
                "Slack bot token not configured. Set WATERCOOLER_SLACK_BOT_TOKEN "
                "or configure token service (WATERCOOLER_TOKEN_API_URL + WATERCOOLER_TOKEN_API_KEY)"
            )

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

    # Thread operations

    def get_thread_replies(
        self,
        channel_id: str,
        thread_ts: str,
        include_parent: bool = False,
    ) -> List[Dict[str, Any]]:
        """Get all messages in a Slack thread.

        Uses conversations.replies API with pagination to get all replies.

        Args:
            channel_id: Channel containing the thread
            thread_ts: Thread parent message timestamp
            include_parent: If True, include the parent message in results

        Returns:
            List of message objects sorted by timestamp (oldest first)
        """
        messages: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            payload: Dict[str, Any] = {
                "channel": channel_id,
                "ts": thread_ts,
                "limit": 200,
            }
            if cursor:
                payload["cursor"] = cursor

            result = self._request("POST", "conversations.replies", payload)
            batch = result.get("messages", [])

            # First message is always the parent
            if not include_parent and batch:
                batch = [m for m in batch if m.get("ts") != thread_ts]

            messages.extend(batch)

            # Check for pagination
            response_metadata = result.get("response_metadata", {})
            cursor = response_metadata.get("next_cursor")
            if not cursor:
                break

        # Sort by timestamp (oldest first)
        messages.sort(key=lambda m: float(m.get("ts", "0")))
        return messages

    def delete_message(
        self,
        channel_id: str,
        message_ts: str,
    ) -> bool:
        """Delete a single message from Slack.

        Uses chat.delete API. Requires appropriate permissions.
        Bot can only delete its own messages.

        Args:
            channel_id: Channel containing the message
            message_ts: Message timestamp to delete

        Returns:
            True if deleted successfully

        Raises:
            SlackAPIError: If deletion fails (e.g., message not found,
                          permission denied)
        """
        try:
            self._request(
                "POST",
                "chat.delete",
                {"channel": channel_id, "ts": message_ts},
            )
            return True
        except SlackAPIError as e:
            # message_not_found is ok - already deleted
            if e.error == "message_not_found":
                logger.debug(f"Message {message_ts} already deleted")
                return True
            raise

    def batch_delete_messages(
        self,
        channel_id: str,
        message_timestamps: List[str],
        batch_size: int = 20,
        delay_between_batches: float = 1.0,
    ) -> int:
        """Delete messages in batches with rate limit awareness.

        Processes messages in batches with configurable delays between
        batches to avoid Slack rate limits. Handles rate limit errors
        gracefully by respecting retry_after.

        Args:
            channel_id: Channel containing the messages
            message_timestamps: List of message timestamps to delete
            batch_size: Number of messages per batch (default: 20)
            delay_between_batches: Seconds to wait between batches (default: 1.0)

        Returns:
            Number of messages successfully deleted
        """
        import time

        deleted = 0
        total = len(message_timestamps)

        for i in range(0, total, batch_size):
            batch = message_timestamps[i : i + batch_size]

            for ts in batch:
                try:
                    if self.delete_message(channel_id, ts):
                        deleted += 1
                except SlackAPIError as e:
                    if e.error == "ratelimited":
                        # Respect Slack's retry_after header
                        retry_after = 1.0
                        if e.response:
                            retry_after = float(e.response.get("retry_after", 1))
                        logger.warning(
                            f"Rate limited, waiting {retry_after}s before retry"
                        )
                        time.sleep(retry_after)
                        # Retry this message
                        try:
                            if self.delete_message(channel_id, ts):
                                deleted += 1
                        except SlackAPIError:
                            logger.error(f"Failed to delete message {ts} after retry")
                    else:
                        logger.error(f"Failed to delete message {ts}: {e.error}")

            # Delay between batches to avoid rate limits
            if i + batch_size < total:
                time.sleep(delay_between_batches)

        logger.info(f"Batch delete complete: {deleted}/{total} messages deleted")
        return deleted

    def update_message(
        self,
        channel_id: str,
        message_ts: str,
        text: str,
        blocks: Optional[List[Dict]] = None,
    ) -> Dict[str, Any]:
        """Update an existing message.

        Uses chat.update API. Bot can only update its own messages.

        Args:
            channel_id: Channel containing the message
            message_ts: Message timestamp to update
            text: New message text (fallback for notifications)
            blocks: New Block Kit blocks (optional)

        Returns:
            Updated message object

        Raises:
            SlackAPIError: If update fails
        """
        payload: Dict[str, Any] = {
            "channel": channel_id,
            "ts": message_ts,
            "text": text,
        }
        if blocks:
            payload["blocks"] = blocks

        return self._request("POST", "chat.update", payload)

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

    # Auth operations

    def auth_test(self) -> Dict[str, Any]:
        """Test authentication and get workspace info.

        Returns:
            Auth info including team_id, team name, bot_id, user_id
        """
        return self._request("POST", "auth.test", {})

    def get_team_id(self) -> str:
        """Get the Slack workspace (team) ID.

        Returns:
            Team ID (e.g., "T07ABC123")

        Raises:
            SlackAPIError: If auth.test fails
        """
        result = self.auth_test()
        return result.get("team_id", "")


# Convenience function for one-off operations
# Cache keyed by workspace_id (None for default/single workspace)
_client_cache: Dict[Optional[str], SlackClient] = {}


def get_slack_client(
    bot_token: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> SlackClient:
    """Get or create a Slack client.

    For multi-workspace support, pass workspace_id to get a client
    with the correct token for that workspace.

    Args:
        bot_token: Optional token override (bypasses token service)
        workspace_id: Slack workspace ID for multi-workspace support

    Returns:
        SlackClient instance
    """
    # Explicit token always creates new client (no caching)
    if bot_token:
        return SlackClient(bot_token=bot_token, workspace_id=workspace_id)

    # Check cache
    if workspace_id in _client_cache:
        return _client_cache[workspace_id]

    # Create and cache new client
    client = SlackClient(workspace_id=workspace_id)
    _client_cache[workspace_id] = client
    return client


def clear_client_cache() -> None:
    """Clear the client cache. Useful for testing."""
    _client_cache.clear()
