"""Unit tests for WatercoolerMessagingConnector.

Tests the connector against watercooler's baseline graph — no CooperBench
dependency or external services needed.  Marked ``benchmark`` so they are
auto-skipped unless the benchmark directory is explicitly targeted.
"""

from __future__ import annotations

import concurrent.futures

import pytest

from watercooler.baseline_graph.reader import read_thread_from_graph

from .adapters.cooperbench_adapter import WatercoolerMessagingConnector


@pytest.mark.benchmark
class TestWatercoolerMessagingConnector:
    """Drop-in replacement for CooperBench's Redis MessagingConnector."""

    def test_send_creates_entry(self, tmp_path):
        """send() writes an entry to the watercooler thread."""
        conn = WatercoolerMessagingConnector(
            "agent1", ["agent1", "agent2"], tmp_path,
        )
        conn.send("agent2", "Hello from agent1")

        result = read_thread_from_graph(tmp_path, "cooperbench-collab")
        assert result is not None
        _, entries = result
        assert len(entries) >= 1
        assert "Hello from agent1" in entries[-1].body

    def test_send_entry_metadata(self, tmp_path):
        """send() populates agent, role, title, and entry_type correctly."""
        conn = WatercoolerMessagingConnector(
            "agent1", ["agent1", "agent2"], tmp_path,
        )
        conn.send("agent2", "metadata test")

        _, entries = read_thread_from_graph(tmp_path, "cooperbench-collab")
        entry = entries[-1]
        # Agent is canonicalized with user tag, e.g. "agent1 (caleb)"
        assert entry.agent.startswith("agent1")
        assert entry.role == "implementer"
        assert entry.title == "To agent2"
        assert entry.entry_type == "Note"

    def test_receive_returns_other_agent_messages(self, tmp_path):
        """receive() only returns messages from other agents."""
        conn1 = WatercoolerMessagingConnector(
            "agent1", ["agent1", "agent2"], tmp_path,
        )
        conn2 = WatercoolerMessagingConnector(
            "agent2", ["agent1", "agent2"], tmp_path,
        )
        conn1.send("agent2", "Hello agent2")

        messages = conn2.receive()
        assert len(messages) == 1
        assert messages[0]["from"] == "agent1"
        assert messages[0]["content"] == "Hello agent2"
        assert messages[0]["to"] == "agent2"
        assert "timestamp" in messages[0]

    def test_receive_skips_own_messages(self, tmp_path):
        """receive() filters out messages the agent itself sent."""
        conn1 = WatercoolerMessagingConnector(
            "agent1", ["agent1", "agent2"], tmp_path,
        )
        conn1.send("agent2", "outbound msg")

        messages = conn1.receive()
        assert len(messages) == 0

    def test_receive_advances_cursor(self, tmp_path):
        """Calling receive() twice does not return the same messages."""
        conn1 = WatercoolerMessagingConnector(
            "agent1", ["agent1", "agent2"], tmp_path,
        )
        conn2 = WatercoolerMessagingConnector(
            "agent2", ["agent1", "agent2"], tmp_path,
        )
        conn1.send("agent2", "msg1")
        conn2.receive()  # advances cursor past msg1

        conn1.send("agent2", "msg2")
        messages = conn2.receive()
        assert len(messages) == 1
        assert messages[0]["content"] == "msg2"

    def test_broadcast_visible_to_all(self, tmp_path):
        """broadcast() creates an entry visible to all other agents."""
        agents = ["agent1", "agent2", "agent3"]
        conn1 = WatercoolerMessagingConnector("agent1", agents, tmp_path)
        conn2 = WatercoolerMessagingConnector("agent2", agents, tmp_path)
        conn3 = WatercoolerMessagingConnector("agent3", agents, tmp_path)

        conn1.broadcast("Hello everyone")
        assert len(conn2.receive()) == 1
        assert len(conn3.receive()) == 1

    def test_broadcast_entry_title(self, tmp_path):
        """broadcast() sets title to 'Broadcast'."""
        conn = WatercoolerMessagingConnector(
            "agent1", ["agent1", "agent2"], tmp_path,
        )
        conn.broadcast("broadcast body")

        _, entries = read_thread_from_graph(tmp_path, "cooperbench-collab")
        assert entries[-1].title == "Broadcast"

    def test_peek_counts_unread(self, tmp_path):
        """peek() returns count of unread messages without consuming."""
        conn1 = WatercoolerMessagingConnector(
            "agent1", ["agent1", "agent2"], tmp_path,
        )
        conn2 = WatercoolerMessagingConnector(
            "agent2", ["agent1", "agent2"], tmp_path,
        )
        conn1.send("agent2", "msg1")
        conn1.send("agent2", "msg2")

        assert conn2.peek() == 2
        assert conn2.peek() == 2  # idempotent — does not consume

        conn2.receive()
        assert conn2.peek() == 0

    def test_shared_visibility(self, tmp_path):
        """All agents see all messages — watercooler's key difference from Redis."""
        conn1 = WatercoolerMessagingConnector(
            "agent1", ["agent1", "agent2"], tmp_path,
        )
        conn2 = WatercoolerMessagingConnector(
            "agent2", ["agent1", "agent2"], tmp_path,
        )
        # agent1 sends TO agent2, agent2 sends TO agent1
        conn1.send("agent2", "This is for agent2")
        conn2.send("agent1", "This is for agent1")

        # Each agent sees the other's message
        msgs1 = conn1.receive()
        assert len(msgs1) == 1
        assert msgs1[0]["from"] == "agent2"

        msgs2 = conn2.receive()
        assert len(msgs2) == 1
        assert msgs2[0]["from"] == "agent1"

    def test_setup_is_noop(self, tmp_path):
        """setup() accepts an env arg and does nothing."""
        conn = WatercoolerMessagingConnector(
            "agent1", ["agent1"], tmp_path,
        )
        conn.setup(None)  # should not raise
        conn.setup({"some": "env"})

    def test_receive_on_empty_thread(self, tmp_path):
        """receive() returns [] when no thread exists yet."""
        conn = WatercoolerMessagingConnector(
            "agent1", ["agent1", "agent2"], tmp_path,
        )
        assert conn.receive() == []

    def test_peek_on_empty_thread(self, tmp_path):
        """peek() returns 0 when no thread exists yet."""
        conn = WatercoolerMessagingConnector(
            "agent1", ["agent1", "agent2"], tmp_path,
        )
        assert conn.peek() == 0

    def test_thread_safe_concurrent_send(self, tmp_path):
        """Multiple agents can send concurrently without data loss."""
        agents = ["agent1", "agent2", "agent3"]
        conns = {
            a: WatercoolerMessagingConnector(a, agents, tmp_path)
            for a in agents
        }

        def send_messages(agent_id: str) -> None:
            for i in range(5):
                conns[agent_id].broadcast(f"{agent_id}-msg-{i}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            pool.map(send_messages, agents)

        # Each agent should see 10 messages (5 from each of the other 2)
        for agent_id in agents:
            msgs = conns[agent_id].receive()
            assert len(msgs) == 10, (
                f"{agent_id} got {len(msgs)} messages, expected 10"
            )
