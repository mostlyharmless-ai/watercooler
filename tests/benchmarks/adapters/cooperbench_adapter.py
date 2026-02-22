"""WatercoolerMessagingConnector — drop-in replacement for CooperBench's
Redis-based MessagingConnector that routes inter-agent communication through
watercooler threads.

This enables A/B experiments: same CooperBench tasks, same agents, Redis
channel vs watercooler channel.

Key difference from Redis:
  - Redis uses per-agent private inboxes (point-to-point).
  - Watercooler uses a shared thread visible to all agents (pub/sub).
  All agents see all messages, enabling better coordination.

Usage::

    from tests.benchmarks.adapters.cooperbench_adapter import (
        WatercoolerMessagingConnector,
    )

    conn = WatercoolerMessagingConnector(
        agent_id="agent1",
        agents=["agent1", "agent2"],
        threads_dir=Path("/tmp/threads"),
    )
    conn.send("agent2", "Hello!")
    messages = conn.receive()
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from ulid import ULID

from watercooler.baseline_graph.reader import read_thread_from_graph
from watercooler.commands_graph import say


class WatercoolerMessagingConnector:
    """Watercooler-backed messaging that replaces CooperBench's MessagingConnector.

    Implements the same interface as ``cooperbench.agents.mini_swe_agent_v2
    .connectors.messaging.MessagingConnector``:

    - ``setup(env)``
    - ``send(recipient, content)``
    - ``receive() -> list[dict]``
    - ``broadcast(content)``
    - ``peek() -> int``

    Instead of Redis RPUSH/LPOP on private inboxes, all messages are
    watercooler thread entries on a shared topic.  Every agent sees every
    message — this is the experimental variable.
    """

    def __init__(
        self,
        agent_id: str,
        agents: list[str],
        threads_dir: Path,
        topic: str = "cooperbench-collab",
    ) -> None:
        self.agent_id = agent_id
        self.agents = agents
        self.threads_dir = Path(threads_dir)
        self.topic = topic
        self._cursor: int = 0  # entry index read cursor
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Agent name helpers
    # ------------------------------------------------------------------

    # Watercooler's say() canonicalizes agents to "name (user_tag)".
    # We need to match against the canonical form and map back to the
    # raw agent_id that CooperBench expects.
    _TAG_RE = re.compile(r"^(.+?)\s+\(.*\)$")

    def _is_own_entry(self, entry_agent: str) -> bool:
        """True if *entry_agent* (possibly canonicalized) is this agent."""
        return self._extract_base(entry_agent) == self.agent_id

    def _extract_base(self, canonical: str) -> str:
        """Strip the ``(user_tag)`` suffix added by watercooler canonicalization."""
        m = self._TAG_RE.match(canonical)
        return m.group(1) if m else canonical

    def _to_raw_agent_id(self, canonical: str) -> str:
        """Map a canonical agent name back to the raw agent_id.

        If the base matches one of the known agents, return that.
        Otherwise return the canonical form as-is.
        """
        base = self._extract_base(canonical)
        # Prefer exact match from the known agents list
        for a in self.agents:
            if a == base:
                return a
        return base

    # ------------------------------------------------------------------
    # CooperBench interface
    # ------------------------------------------------------------------

    def setup(self, env: Any = None) -> None:
        """No sandbox setup needed for watercooler."""

    def send(self, recipient: str, content: str) -> None:
        """Send a message to *recipient* via a watercooler thread entry."""
        say(
            self.topic,
            threads_dir=self.threads_dir,
            agent=self.agent_id,
            role="implementer",
            title=f"To {recipient}",
            entry_type="Note",
            body=content,
            entry_id=str(ULID()),
        )

    def receive(self) -> list[dict[str, str]]:
        """Return new messages from other agents since the last read.

        Advances the internal cursor so the same entries are not returned
        twice.  Returns dicts with keys ``from``, ``to``, ``content``,
        ``timestamp`` — matching CooperBench's message format.
        """
        result = read_thread_from_graph(self.threads_dir, self.topic)
        if result is None:
            return []

        _, entries = result
        with self._lock:
            new_entries = [
                e for e in entries[self._cursor:]
                if not self._is_own_entry(e.agent)
            ]
            self._cursor = len(entries)

        return [
            {
                "from": self._to_raw_agent_id(e.agent),
                "to": self.agent_id,
                "content": e.body or "",
                "timestamp": e.timestamp,
            }
            for e in new_entries
        ]

    def broadcast(self, content: str) -> None:
        """Broadcast a message visible to all agents."""
        say(
            self.topic,
            threads_dir=self.threads_dir,
            agent=self.agent_id,
            role="implementer",
            title="Broadcast",
            entry_type="Note",
            body=content,
            entry_id=str(ULID()),
        )

    def peek(self) -> int:
        """Count unread messages from other agents without consuming them."""
        result = read_thread_from_graph(self.threads_dir, self.topic)
        if result is None:
            return 0

        _, entries = result
        return sum(
            1 for e in entries[self._cursor:]
            if not self._is_own_entry(e.agent)
        )
