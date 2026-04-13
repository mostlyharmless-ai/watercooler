"""Slack thread mapping storage.

Maps Watercooler threads to their Slack representations:
- Repository <-> Slack channel
- Thread (topic) <-> Slack thread (parent message ts)
- Entry <-> Slack reply

Storage: JSON file in ~/.watercooler/slack_mappings.json
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SlackThreadMapping:
    """Maps a Watercooler thread to its Slack representation."""

    # Watercooler identifiers
    topic: str                          # Thread topic (e.g., "slack-integration")
    repo: str                           # Repository name (e.g., "watercooler-cloud")

    # Slack identifiers
    slack_channel_id: str               # Channel ID (e.g., "C07ABC123")
    slack_channel_name: str             # Channel name (e.g., "#wc-watercooler-cloud")
    slack_thread_ts: str                # Parent message timestamp (thread identifier)

    # Sync state
    last_synced_entry_id: str = ""      # Last entry ID synced to Slack
    last_synced_at: str = ""            # ISO timestamp of last sync

    # Entry ID -> Slack message ts mapping for replies
    entry_message_map: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "SlackThreadMapping":
        """Create from dictionary."""
        return cls(
            topic=data["topic"],
            repo=data["repo"],
            slack_channel_id=data["slack_channel_id"],
            slack_channel_name=data["slack_channel_name"],
            slack_thread_ts=data["slack_thread_ts"],
            last_synced_entry_id=data.get("last_synced_entry_id", ""),
            last_synced_at=data.get("last_synced_at", ""),
            entry_message_map=data.get("entry_message_map", {}),
        )

    def update_sync(self, entry_id: str, message_ts: str) -> None:
        """Record that an entry was synced to Slack.

        Args:
            entry_id: The watercooler entry ID (ULID)
            message_ts: The Slack message timestamp
        """
        self.last_synced_entry_id = entry_id
        self.last_synced_at = datetime.now(timezone.utc).isoformat()
        self.entry_message_map[entry_id] = message_ts


@dataclass
class SlackChannelMapping:
    """Maps a repository to its Slack channel."""

    repo: str                           # Repository name
    slack_channel_id: str               # Channel ID
    slack_channel_name: str             # Channel name (e.g., "#wc-watercooler-cloud")
    created_at: str = ""                # When channel was created/linked
    thread_count: int = 0               # Number of threads in this channel

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "SlackChannelMapping":
        """Create from dictionary."""
        return cls(
            repo=data["repo"],
            slack_channel_id=data["slack_channel_id"],
            slack_channel_name=data["slack_channel_name"],
            created_at=data.get("created_at", ""),
            thread_count=data.get("thread_count", 0),
        )


class SlackMappingStore:
    """Persistent storage for Slack mappings.

    Stores mappings in ~/.watercooler/slack_mappings.json with structure:
    {
        "version": 1,
        "channels": {
            "<repo>": { channel mapping }
        },
        "threads": {
            "<repo>/<topic>": { thread mapping }
        }
    }
    """

    VERSION = 1

    def __init__(self, path: Optional[Path] = None):
        """Initialize mapping store.

        Args:
            path: Path to mappings file. Defaults to ~/.watercooler/slack_mappings.json
        """
        if path is None:
            path = Path.home() / ".watercooler" / "slack_mappings.json"
        self.path = path
        self._channels: Dict[str, SlackChannelMapping] = {}
        self._threads: Dict[str, SlackThreadMapping] = {}
        self._load()

    def _load(self) -> None:
        """Load mappings from disk."""
        if not self.path.exists():
            logger.debug(f"No mappings file at {self.path}, starting fresh")
            return

        try:
            with open(self.path, "r") as f:
                data = json.load(f)

            version = data.get("version", 1)
            if version > self.VERSION:
                logger.warning(
                    f"Mappings file version {version} > supported {self.VERSION}"
                )

            # Load channel mappings
            for repo, channel_data in data.get("channels", {}).items():
                self._channels[repo] = SlackChannelMapping.from_dict(channel_data)

            # Load thread mappings
            for key, thread_data in data.get("threads", {}).items():
                self._threads[key] = SlackThreadMapping.from_dict(thread_data)

            logger.debug(
                f"Loaded {len(self._channels)} channels, {len(self._threads)} threads"
            )

        except Exception as e:
            logger.error(f"Failed to load mappings from {self.path}: {e}")
            # Don't crash - start with empty mappings
            self._channels = {}
            self._threads = {}

    def _save(self) -> None:
        """Persist mappings to disk."""
        try:
            # Ensure directory exists
            self.path.parent.mkdir(parents=True, exist_ok=True)

            data = {
                "version": self.VERSION,
                "channels": {
                    repo: ch.to_dict() for repo, ch in self._channels.items()
                },
                "threads": {
                    key: th.to_dict() for key, th in self._threads.items()
                },
            }

            with open(self.path, "w") as f:
                json.dump(data, f, indent=2)

            logger.debug(f"Saved mappings to {self.path}")

        except Exception as e:
            logger.error(f"Failed to save mappings to {self.path}: {e}")
            raise

    @staticmethod
    def _thread_key(repo: str, topic: str) -> str:
        """Generate key for thread mapping lookup."""
        return f"{repo}/{topic}"

    # Channel operations

    def get_channel(self, repo: str) -> Optional[SlackChannelMapping]:
        """Get channel mapping for a repository."""
        return self._channels.get(repo)

    def set_channel(self, mapping: SlackChannelMapping) -> None:
        """Store channel mapping."""
        self._channels[mapping.repo] = mapping
        self._save()

    def list_channels(self) -> List[SlackChannelMapping]:
        """List all channel mappings."""
        return list(self._channels.values())

    # Thread operations

    def get_thread(self, repo: str, topic: str) -> Optional[SlackThreadMapping]:
        """Get thread mapping for a topic."""
        key = self._thread_key(repo, topic)
        return self._threads.get(key)

    def set_thread(self, mapping: SlackThreadMapping) -> None:
        """Store thread mapping."""
        key = self._thread_key(mapping.repo, mapping.topic)
        self._threads[key] = mapping
        self._save()

    def list_threads(self, repo: Optional[str] = None) -> List[SlackThreadMapping]:
        """List thread mappings, optionally filtered by repo."""
        threads = list(self._threads.values())
        if repo:
            threads = [t for t in threads if t.repo == repo]
        return threads

    def get_thread_by_ts(
        self, channel_id: str, thread_ts: str
    ) -> Optional[SlackThreadMapping]:
        """Find thread mapping by Slack thread_ts.

        Used for reverse lookup when receiving Slack events.

        Args:
            channel_id: Slack channel ID
            thread_ts: Slack thread timestamp

        Returns:
            Thread mapping if found, None otherwise
        """
        for mapping in self._threads.values():
            if (
                mapping.slack_channel_id == channel_id
                and mapping.slack_thread_ts == thread_ts
            ):
                return mapping
        return None

    def update_thread_sync(
        self, repo: str, topic: str, entry_id: str, message_ts: str
    ) -> None:
        """Record that an entry was synced to Slack.

        Args:
            repo: Repository name
            topic: Thread topic
            entry_id: Watercooler entry ID
            message_ts: Slack message timestamp
        """
        mapping = self.get_thread(repo, topic)
        if mapping:
            mapping.update_sync(entry_id, message_ts)
            self._save()
        else:
            logger.warning(f"No mapping found for {repo}/{topic}")


# Module-level singleton for convenience
_store: Optional[SlackMappingStore] = None


def get_mapping_store(path: Optional[Path] = None) -> SlackMappingStore:
    """Get or create the mapping store singleton.

    Args:
        path: Optional custom path. If provided and different from
              existing store's path, creates a new store.

    Returns:
        SlackMappingStore instance
    """
    global _store
    if _store is None or (path is not None and path != _store.path):
        _store = SlackMappingStore(path)
    return _store
