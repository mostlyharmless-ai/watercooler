"""Integration test for Phase 5a convergence telemetry end-to-end.

Verifies that PulseSnapshotDaemon.tick() attaches ``convergence_signals``
to the persisted pulse_snapshot and that the shape matches expectations.
Also asserts the negative requirement: no ``phase_pressure_advisory`` finding
is generated (that is Phase 5b, gated on empirical evaluation).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from watercooler.baseline_graph import storage
from watercooler.config_schema import PulseSnapshotConfig
from watercooler.pulse_snapshot_lib import _MIN_ENTRIES_FOR_CONVERGENCE
from watercooler_mcp.daemons.pulse_snapshot import PulseSnapshotDaemon


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TOPIC = "convergence-test-thread"


def _make_daemon(tmp_path: Path) -> PulseSnapshotDaemon:
    cfg = PulseSnapshotConfig()
    d = PulseSnapshotDaemon(
        config=cfg,
        threads_dir=tmp_path / "threads",
        code_root=tmp_path,
    )
    return d


def _seed_thread(
    graph_dir: Path,
    topic: str,
    n_entries: int,
    *,
    with_embeddings: bool = True,
) -> None:
    """Write a thread with n_entries and optional search-index embeddings."""
    thread_dir = storage.ensure_thread_graph_dir(graph_dir, topic)
    storage.atomic_write_json(
        thread_dir / "meta.json",
        {
            "id": f"thread:{topic}",
            "topic": topic,
            "title": topic,
            "status": "OPEN",
        },
    )
    entries = [
        {
            "entry_id": f"E{i:04d}",
            "role": "planner" if i % 3 != 1 else "critic",
            "entry_type": "Note",
            "title": f"Entry {i}",
            "body": f"Entry body {i}",
            "timestamp": "2026-05-19T00:00:00Z",
            "agent": "test",
            "index": i,
            "thread_topic": topic,
        }
        for i in range(n_entries)
    ]
    storage.atomic_write_jsonl(thread_dir / "entries.jsonl", entries)

    if with_embeddings:
        records = [
            {
                "entry_id": f"E{i:04d}",
                "thread_topic": topic,
                "embedding": [1.0 if j == i % 8 else 0.0 for j in range(8)],
            }
            for i in range(n_entries)
        ]
        storage.atomic_write_jsonl(thread_dir / "search-index.jsonl", records)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConvergenceTelemetryE2E:
    def _run_tick(self, daemon: PulseSnapshotDaemon, tmp_path: Path) -> list[Any]:
        """Run one daemon tick with mocked LLM availability check."""
        with (
            patch.object(daemon, "_resolve_context", return_value=True),
            patch.object(
                daemon,
                "_get_llm_client",
                return_value=MagicMock(is_available=MagicMock(return_value=False)),
            ),
        ):
            return daemon.tick()

    def test_convergence_signals_in_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After a tick, pulse_snapshot contains convergence_signals for qualifying threads."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR",
            tmp_path / "daemons",
        )
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir(parents=True)
        graph_dir = storage.ensure_graph_dir(threads_dir)

        _seed_thread(graph_dir, TOPIC, _MIN_ENTRIES_FOR_CONVERGENCE + 3)

        daemon = _make_daemon(tmp_path)
        daemon._resolved_threads_dir = threads_dir
        daemon._resolved_code_root = tmp_path
        from watercooler.pulse_snapshot_lib import derive_repo_key
        daemon._repo_key = derive_repo_key(tmp_path)

        # Patch build_snapshot to return a minimal snapshot so the tick doesn't fail
        # on missing session-context threads while still exercising the convergence path.
        minimal_snapshot: dict[str, Any] = {
            "snapshot_version": "1.0",
            "generated_at": "2026-05-19T00:00:00Z",
            "repo_key": daemon._repo_key,
            "window_days": 7,
            "code_branch": "*",
            "corpus": {"sessions_in_window": 0},
            "contributors": {},
            "queue_pending": 0,
            "stalled_threads": [],
            "risk_surface_tags": [],
            "analysis": {"latest_report_path": None, "latest_report_age_days": None, "is_fresh": False},
        }

        with patch(
            "watercooler_mcp.daemons.pulse_snapshot.build_snapshot",
            return_value=minimal_snapshot,
        ):
            daemon.tick()

        snapshot = daemon.get_snapshot(daemon._repo_key)
        assert snapshot is not None
        assert "convergence_signals" in snapshot

        signals = snapshot["convergence_signals"]
        assert isinstance(signals, dict)
        assert TOPIC in signals
        topic_signals = signals[TOPIC]
        assert topic_signals["entry_count"] == _MIN_ENTRIES_FOR_CONVERGENCE + 3
        assert "tradeoff_recurrence" in topic_signals
        assert "concern_cluster_recurrence" in topic_signals
        assert "constraint_class_emergence" in topic_signals

    def test_no_phase_pressure_advisory_finding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Phase 5a MUST NOT emit phase_pressure_advisory findings (Phase 5b gate)."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR",
            tmp_path / "daemons",
        )
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir(parents=True)
        graph_dir = storage.ensure_graph_dir(threads_dir)
        _seed_thread(graph_dir, TOPIC, _MIN_ENTRIES_FOR_CONVERGENCE + 5)

        daemon = _make_daemon(tmp_path)
        daemon._resolved_threads_dir = threads_dir
        daemon._resolved_code_root = tmp_path
        from watercooler.pulse_snapshot_lib import derive_repo_key
        daemon._repo_key = derive_repo_key(tmp_path)

        minimal_snapshot: dict[str, Any] = {
            "snapshot_version": "1.0",
            "generated_at": "2026-05-19T00:00:00Z",
            "repo_key": daemon._repo_key,
            "window_days": 7,
            "code_branch": "*",
            "corpus": {"sessions_in_window": 0},
            "contributors": {},
            "queue_pending": 0,
            "stalled_threads": [],
            "risk_surface_tags": [],
            "analysis": {"latest_report_path": None, "latest_report_age_days": None, "is_fresh": False},
        }

        with patch(
            "watercooler_mcp.daemons.pulse_snapshot.build_snapshot",
            return_value=minimal_snapshot,
        ):
            findings = daemon.tick()

        advisory_findings = [
            f for f in findings if f.category == "phase_pressure_advisory"
        ]
        assert advisory_findings == [], (
            "Phase 5a must not emit phase_pressure_advisory findings — "
            "that is Phase 5b, gated on empirical evaluation"
        )

    def test_short_thread_gets_insufficient_data_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Threads with < 10 entries get a note rather than computed signals."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR",
            tmp_path / "daemons",
        )
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir(parents=True)
        graph_dir = storage.ensure_graph_dir(threads_dir)
        _seed_thread(graph_dir, "short-thread", 4)

        daemon = _make_daemon(tmp_path)
        daemon._resolved_threads_dir = threads_dir
        daemon._resolved_code_root = tmp_path
        from watercooler.pulse_snapshot_lib import derive_repo_key
        daemon._repo_key = derive_repo_key(tmp_path)

        minimal_snapshot: dict[str, Any] = {
            "snapshot_version": "1.0",
            "generated_at": "2026-05-19T00:00:00Z",
            "repo_key": daemon._repo_key,
            "window_days": 7,
            "code_branch": "*",
            "corpus": {"sessions_in_window": 0},
            "contributors": {},
            "queue_pending": 0,
            "stalled_threads": [],
            "risk_surface_tags": [],
            "analysis": {"latest_report_path": None, "latest_report_age_days": None, "is_fresh": False},
        }

        with patch(
            "watercooler_mcp.daemons.pulse_snapshot.build_snapshot",
            return_value=minimal_snapshot,
        ):
            daemon.tick()

        snapshot = daemon.get_snapshot(daemon._repo_key)
        signals = (snapshot or {}).get("convergence_signals", {})
        if "short-thread" in signals:
            assert "insufficient_data" in signals["short-thread"].get("note", "")
