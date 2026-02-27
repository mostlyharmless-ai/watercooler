"""Integration tests for CooperBench + watercooler messaging pipeline.

Simulates CooperBench's ``execute_coop()`` threading model: two agents
run concurrently, communicate through the watercooler adapter, and
return results.  No Docker, API keys, or Modal needed — the agents are
lightweight stubs that exercise the real messaging path.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from watercooler.baseline_graph.reader import read_thread_from_graph

from .adapters.cooperbench_adapter import WatercoolerMessagingConnector


def _mock_agent(
    agent_id: str,
    feature_task: str,
    conn: WatercoolerMessagingConnector,
    results: dict,
    other_agent: str,
) -> None:
    """Simulate a CooperBench agent that sends and receives messages.

    Mirrors the structure of CooperBench's ``_spawn_agent()`` → adapter
    ``run()`` → agent loop, but without LLM calls or sandbox execution.
    """
    sent: list[dict] = []

    # Phase 1: announce feature assignment
    conn.send(other_agent, f"Starting work on: {feature_task}")
    sent.append({"to": other_agent, "content": f"Starting work on: {feature_task}"})

    # Phase 2: wait for the other agent's announcement (poll with timeout)
    deadline = time.monotonic() + 5.0
    while conn.peek() == 0 and time.monotonic() < deadline:
        time.sleep(0.01)

    msgs = conn.receive()

    # Phase 3: coordinate based on what we learned
    if msgs:
        conn.send(
            other_agent,
            f"Acknowledged your task. I'll avoid conflicts with: {msgs[0]['content']}",
        )
        sent.append({"to": other_agent, "content": "coordination ack"})

    # Phase 4: broadcast completion
    conn.broadcast(f"{agent_id} finished feature: {feature_task}")
    sent.append({"to": "all", "content": f"finished {feature_task}"})

    results[agent_id] = {
        "agent_id": agent_id,
        "feature_task": feature_task,
        "status": "Submitted",
        "patch": f"--- mock patch for {feature_task} ---",
        "messages_received": msgs,
        "messages_sent": sent,
        "steps": 3,
    }


@pytest.mark.benchmark
class TestCooperBenchPipeline:
    """Simulate CooperBench's execute_coop() with watercooler messaging."""

    def test_coop_pipeline_two_agents(self, tmp_path):
        """Two agents complete tasks and communicate through watercooler.

        This mirrors the full CooperBench cooperative execution flow:
        1. Two agents are assigned separate features
        2. They communicate via shared watercooler thread
        3. Both complete and produce results
        4. Communication log is extractable from the thread
        """
        agents = ["agent1", "agent2"]
        features = {
            "agent1": "Implement user authentication with OAuth2",
            "agent2": "Add rate limiting to API endpoints",
        }
        conns = {
            a: WatercoolerMessagingConnector(a, agents, tmp_path)
            for a in agents
        }
        results: dict = {}

        # Spawn agents in threads (same as execute_coop)
        threads = []
        for agent_id in agents:
            other = [a for a in agents if a != agent_id][0]
            t = threading.Thread(
                target=_mock_agent,
                args=(agent_id, features[agent_id], conns[agent_id], results, other),
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=10)

        # Both agents completed
        assert len(results) == 2
        for agent_id in agents:
            assert results[agent_id]["status"] == "Submitted"
            assert results[agent_id]["patch"]

        # Both agents received messages from the other
        for agent_id in agents:
            assert len(results[agent_id]["messages_received"]) >= 1, (
                f"{agent_id} received no messages"
            )

        # Thread has entries from both agents
        result = read_thread_from_graph(tmp_path, "cooperbench-collab")
        assert result is not None
        _, entries = result
        entry_agents = {e.agent.split(" ")[0] for e in entries}
        assert "agent1" in entry_agents
        assert "agent2" in entry_agents

        # At least 4 entries: 2 announcements + 2 coordination acks
        # (broadcasts add more)
        assert len(entries) >= 4

    def test_coop_pipeline_message_ordering(self, tmp_path):
        """Messages maintain causal ordering across concurrent agents."""
        agents = ["agent1", "agent2"]
        conns = {
            a: WatercoolerMessagingConnector(a, agents, tmp_path)
            for a in agents
        }

        # Sequential exchange (like real agent negotiation)
        conns["agent1"].send("agent2", "I'll handle the database schema")
        conns["agent2"].send("agent1", "OK, I'll do the API layer")
        conns["agent1"].send("agent2", "Schema is ready, you can integrate")
        conns["agent2"].send("agent1", "Integration complete")

        # Read the full thread — entries should be in send order
        _, entries = read_thread_from_graph(tmp_path, "cooperbench-collab")
        bodies = [e.body for e in entries]
        assert bodies == [
            "I'll handle the database schema",
            "OK, I'll do the API layer",
            "Schema is ready, you can integrate",
            "Integration complete",
        ]

    def test_coop_pipeline_extracts_conversation(self, tmp_path):
        """The watercooler thread serves as the conversation log.

        In CooperBench, ``_extract_conversation()`` parses agent messages
        from result dicts.  With watercooler, the thread IS the
        conversation log — entries map directly to messages.
        """
        agents = ["agent1", "agent2"]
        conns = {
            a: WatercoolerMessagingConnector(a, agents, tmp_path)
            for a in agents
        }

        conns["agent1"].send("agent2", "What API endpoints do you need?")
        conns["agent2"].send("agent1", "POST /users and GET /users/:id")
        conns["agent1"].broadcast("Both features are compatible, no conflicts")

        # Extract conversation from thread (watercooler equivalent of
        # CooperBench's _extract_conversation)
        _, entries = read_thread_from_graph(tmp_path, "cooperbench-collab")

        conversation = []
        for e in entries:
            base_agent = e.agent.split(" ")[0]
            conversation.append({
                "from": base_agent,
                "title": e.title,
                "content": e.body,
                "timestamp": e.timestamp,
            })

        assert len(conversation) == 3
        assert conversation[0]["from"] == "agent1"
        assert conversation[0]["title"] == "To agent2"
        assert conversation[1]["from"] == "agent2"
        assert conversation[2]["title"] == "Broadcast"
