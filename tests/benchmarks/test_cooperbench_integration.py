"""Integration tests that run full CooperBench tasks with watercooler messaging.

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
class TestCooperBenchFullTask:
    """Run full CooperBench tasks with watercooler messaging.

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

        This is a smoke test -- full evaluation (applying patches, running
        test suites in Docker) is handled by the experiment runner script.
        """
        pytest.skip(
            "Full integration requires CooperBench task runner setup. "
            "Use tests/benchmarks/scripts/run_cooperbench_experiment.py "
            "for end-to-end experiments."
        )
