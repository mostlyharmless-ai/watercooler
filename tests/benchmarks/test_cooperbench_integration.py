"""Integration tests that run CooperBench tasks with watercooler messaging.

These tests are expensive (minutes per task, require API keys, Docker) and
are NOT run in CI.  Use ``-m integration_cooperbench`` to opt-in::

    pytest tests/benchmarks/test_cooperbench_integration.py -v -m integration_cooperbench
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

# CooperBench install path — set via env or default
COOPERBENCH_DIR = Path(
    "/home/caleb/Work/Personal/MostlyHarmless-AI/repo/CooperBench"
)

_cooperbench_available = COOPERBENCH_DIR.is_dir() and (
    COOPERBENCH_DIR / "src" / "cooperbench"
).is_dir()


def _ensure_cooperbench_importable() -> None:
    """Add CooperBench's src/ to sys.path if not already importable."""
    src = str(COOPERBENCH_DIR / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def _has_cooperbench_module() -> bool:
    """Check if cooperbench is importable."""
    if not _cooperbench_available:
        return False
    _ensure_cooperbench_importable()
    try:
        importlib.import_module("cooperbench")
        return True
    except ImportError:
        return False


@pytest.mark.benchmark
@pytest.mark.integration_cooperbench
@pytest.mark.skipif(
    not _has_cooperbench_module(),
    reason="CooperBench not installed or not found at expected path",
)
class TestCooperBenchWatercoolerChannel:
    """Run CooperBench tasks with watercooler messaging instead of Redis.

    Prerequisites:
    - CooperBench cloned at COOPERBENCH_DIR
    - Docker running (for evaluation)
    - API key configured (for LLM agents)
    """

    def test_single_task_produces_patches(self, tmp_path):
        """Run one CooperBench task with watercooler messaging.

        Verifies:
        1. Discovers a task from the CooperBench dataset
        2. Both agents complete without error
        3. Each agent produces a non-empty patch
        4. Communication entries exist in the watercooler thread

        This is a smoke test — full evaluation (applying patches, running
        test suites in Docker) is handled by the experiment runner script.
        """
        pytest.skip(
            "Full integration requires CooperBench task runner setup. "
            "Use tests/benchmarks/scripts/run_cooperbench_experiment.py "
            "for end-to-end experiments."
        )

    def test_message_ordering_matches_redis(self, tmp_path):
        """Verify message ordering semantics.

        In Redis, messages are strictly FIFO per-inbox.  In watercooler,
        messages are ordered by graph entry index.  This test verifies
        that the watercooler connector preserves causal ordering.
        """
        from .adapters.cooperbench_adapter import WatercoolerMessagingConnector

        agents = ["alice", "bob"]
        alice = WatercoolerMessagingConnector("alice", agents, tmp_path)
        bob = WatercoolerMessagingConnector("bob", agents, tmp_path)

        # Interleaved sends
        alice.send("bob", "step1")
        bob.send("alice", "step2")
        alice.send("bob", "step3")

        # Bob sees alice's messages in order
        bob_msgs = bob.receive()
        assert [m["content"] for m in bob_msgs] == ["step1", "step3"]

        # Alice sees bob's message
        alice_msgs = alice.receive()
        assert [m["content"] for m in alice_msgs] == ["step2"]

    def test_watercooler_shared_context_advantage(self, tmp_path):
        """Demonstrate watercooler's shared visibility advantage.

        In Redis, if agent A sends to agent B, agent C never sees it.
        In watercooler, agent C sees all messages — this test quantifies
        the information asymmetry difference.
        """
        from .adapters.cooperbench_adapter import WatercoolerMessagingConnector

        agents = ["a", "b", "c"]
        conn_a = WatercoolerMessagingConnector("a", agents, tmp_path)
        conn_b = WatercoolerMessagingConnector("b", agents, tmp_path)
        conn_c = WatercoolerMessagingConnector("c", agents, tmp_path)

        # A sends to B (point-to-point in Redis, shared in watercooler)
        conn_a.send("b", "secret plan for feature X")

        # In watercooler, C sees A's message too
        c_msgs = conn_c.receive()
        assert len(c_msgs) == 1, (
            "Watercooler shared visibility: C should see A→B message"
        )
        assert c_msgs[0]["content"] == "secret plan for feature X"

        # B also sees it
        b_msgs = conn_b.receive()
        assert len(b_msgs) == 1
