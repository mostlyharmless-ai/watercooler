"""Tests for ProjectCoordinatorDaemon — coordination intelligence scanning."""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path
from typing import Any

import pytest

from watercooler.baseline_graph import storage
from watercooler.config_schema import ProjectCoordinatorConfig
from watercooler.project_coordinator_lib import ActiveSignalEntry
from watercooler_mcp.daemons.project_coordinator import ProjectCoordinatorDaemon

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_graph_thread(
    threads_dir: Path,
    topic: str,
    *,
    status: str = "OPEN",
    title: str = "Test Thread",
    entries: list[dict[str, Any]] | None = None,
    last_updated: str = "2024-04-01T00:00:00Z",
) -> None:
    """Write graph data for a thread."""
    threads_dir.mkdir(parents=True, exist_ok=True)

    # Write .md file (projection)
    p = threads_dir / f"{topic}.md"
    p.write_text(f"# {title}\nStatus: {status}\n", encoding="utf-8")

    # Write graph data
    graph_dir = storage.ensure_graph_dir(threads_dir)
    thread_dir = storage.ensure_thread_graph_dir(graph_dir, topic)

    meta = {
        "id": f"thread:{topic}",
        "topic": topic,
        "title": title,
        "status": status,
        "last_updated": last_updated,
    }
    storage.atomic_write_json(thread_dir / "meta.json", meta)

    entry_list = entries if entries is not None else []
    storage.atomic_write_jsonl(thread_dir / "entries.jsonl", entry_list)


def _entry(
    *,
    entry_id: str = "E01",
    agent: str = "Alice",
    role: str = "implementer",
    entry_type: str = "Note",
    timestamp: str = "2024-04-01T12:00:00Z",
    index: int = 0,
    title: str = "Update",
    summary: str = "",
    body: str = "",
) -> dict[str, Any]:
    return {
        "id": f"entry:{entry_id}",
        "entry_id": entry_id,
        "agent": agent,
        "role": role,
        "entry_type": entry_type,
        "timestamp": timestamp,
        "title": title,
        "summary": summary,
        "body": body,
        "index": index,
    }


def _make_daemon(
    tmp_path: Path,
    threads_dir: Path | None = None,
    **config_overrides: Any,
) -> ProjectCoordinatorDaemon:
    """Create a daemon with test config."""
    cfg = ProjectCoordinatorConfig(**config_overrides)
    return ProjectCoordinatorDaemon(
        config=cfg,
        threads_dir=threads_dir or tmp_path / "threads",
    )


def _seed_known_contributors(daemon: ProjectCoordinatorDaemon, *agents: str) -> None:
    """Pre-seed seen_contributors so tests that pre-date P2.1 new-contributor
    stance wiring are not perturbed by first-tick new-contributor advisories.

    Use this when a test asserts stance at L0 on the first tick and its
    fixture contributors would otherwise trigger aware_new_contributor →
    planner L1 advisories via the P2.1 corpus signal transport.
    """
    import time

    daemon._load_extras()
    now = time.time()
    for agent in agents:
        daemon._extras.seen_contributors[agent] = now
    daemon._save_extras()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestProjectCoordinatorDaemon:
    def test_creation(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        daemon = _make_daemon(tmp_path)
        assert daemon.name == "project_coordinator"
        assert daemon.enabled is True

    def test_tick_empty_graph(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings = daemon.tick()
        assert findings == []

    def test_tick_detects_open_loop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir,
            "stalled-topic",
            entries=[
                _entry(entry_type="Note", index=0),
                _entry(entry_type="Plan", index=1, entry_id="E02"),
                _entry(entry_type="Note", index=2, entry_id="E03"),
            ],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings = daemon.tick()
        open_loop = [f for f in findings if f.category == "stalled_open_loop"]
        assert len(open_loop) == 1
        assert open_loop[0].severity == "warning"

    def test_tick_detects_dropout(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir,
            "dropout-topic",
            entries=[
                _entry(agent="Alice", index=0, entry_id="E01"),
                _entry(agent="Alice", index=1, entry_id="E02"),
                _entry(agent="Alice", index=2, entry_id="E03"),
                _entry(agent="Bob", index=3, entry_id="E04"),
                _entry(agent="Bob", index=4, entry_id="E05"),
                _entry(agent="Bob", index=5, entry_id="E06"),
            ],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings = daemon.tick()
        dropout = [f for f in findings if f.category == "stalled_dropout"]
        assert len(dropout) == 1
        assert "alice" in dropout[0].message.lower() or "Alice" in dropout[0].message

    def test_tick_detects_role_concentration(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir,
            "mono-role-topic",
            entries=[
                _entry(role="implementer", index=i, entry_id=f"E{i:02d}")
                for i in range(5)
            ],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings = daemon.tick()
        rc = [f for f in findings if f.category == "aware_role_concentration"]
        assert len(rc) == 1
        assert rc[0].details["dominant_role"] == "implementer"

    def test_tick_incremental_skips_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir,
            "stalled-topic",
            entries=[
                _entry(entry_type="Plan", index=0),
                _entry(entry_type="Note", index=1, entry_id="E02"),
                _entry(entry_type="Note", index=2, entry_id="E03"),
            ],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)

        # First tick: should find open loop
        findings1 = daemon.tick()
        assert len(findings1) > 0

        # Second tick without changes: no new v1A findings (incremental skip + dedup).
        # Stance advisories may re-fire on legitimate de-escalation (e.g., the
        # new-contributor signal going from 1 to 0), so exclude them from this
        # assertion — that lifecycle is orthogonal to content-change detection.
        findings2 = daemon.tick()
        v1a2 = [f for f in findings2 if f.category != "stance_advisory"]
        assert len(v1a2) == 0

    def test_tick_dedup_prevents_duplicates(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir,
            "stalled-topic",
            entries=[
                _entry(entry_type="Plan", index=0),
                _entry(entry_type="Note", index=1, entry_id="E02"),
                _entry(entry_type="Note", index=2, entry_id="E03"),
            ],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)

        findings1 = daemon.tick()
        count1 = len(findings1)
        assert count1 > 0

        # Touch meta.json to force rescan
        graph_dir = storage.get_graph_dir(threads_dir)
        meta = storage.get_thread_graph_dir(graph_dir, "stalled-topic") / "meta.json"
        meta.write_text(meta.read_text())  # triggers mtime change

        findings2 = daemon.tick()
        # Deterministic IDs: same conditions → same finding_id → deduped.
        # Stance advisories may re-fire on legitimate de-escalation (P2.1
        # new-contributor signal going from 1 to 0); filter them out as they
        # are orthogonal to v1A dedup behavior.
        v1a2 = [f for f in findings2 if f.category != "stance_advisory"]
        assert len(v1a2) == 0

    def test_tick_max_findings_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"

        # Create multiple threads that will each produce findings
        for i in range(5):
            _write_graph_thread(
                threads_dir,
                f"topic-{i}",
                entries=[
                    _entry(entry_type="Plan", index=0, entry_id=f"E{i}0"),
                    _entry(entry_type="Note", index=1, entry_id=f"E{i}1"),
                    _entry(entry_type="Note", index=2, entry_id=f"E{i}2"),
                ],
            )

        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, max_findings_per_run=2)
        findings = daemon.tick()
        # v1A findings are capped; stance_advisory findings are exempt
        v1a_findings = [f for f in findings if f.category != "stance_advisory"]
        assert len(v1a_findings) <= 2

    def test_capped_burst_preserves_baseline(self, tmp_path, monkeypatch):
        """Regression: burst baseline must not advance when cap prevents emission.

        If max_findings_per_run is hit before the burst finding is materialized,
        the burst baseline must be preserved (not advanced), so the burst can
        re-fire on the next tick when the cap is lifted.
        """
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        from datetime import datetime, timedelta, timezone

        threads_dir = tmp_path / "threads"
        now = datetime.now(tz=timezone.utc)
        # All timestamps must be older than OPEN_LOOP_MIN_STALE_DAYS (7)
        # so that stalled_open_loop fires and consumes the cap slot.
        seed_ts = (now - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
        old_ts = (now - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        burst_ts = (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Thread with a Plan (triggers open_loop) and seed entries for baseline
        _write_graph_thread(
            threads_dir,
            "busy-topic",
            entries=[
                _entry(entry_type="Plan", index=0, entry_id="E00", timestamp=old_ts),
                _entry(entry_type="Note", index=1, entry_id="E01", timestamp=seed_ts),
                _entry(entry_type="Note", index=2, entry_id="E02", timestamp=seed_ts),
            ],
        )
        daemon = _make_daemon(
            tmp_path, threads_dir=threads_dir, max_findings_per_run=200
        )

        # Tick 1: seed burst baseline
        daemon.tick()
        baseline_after_seed = daemon._extras.burst_baselines.get("busy-topic")
        assert baseline_after_seed is not None

        # Add more entries to trigger burst detection
        graph_dir = storage.get_graph_dir(threads_dir)
        thread_dir = storage.get_thread_graph_dir(graph_dir, "busy-topic")
        storage.atomic_write_jsonl(
            thread_dir / "entries.jsonl",
            [
                _entry(entry_type="Plan", index=0, entry_id="E00", timestamp=old_ts),
                _entry(entry_type="Note", index=1, entry_id="E01", timestamp=seed_ts),
                _entry(entry_type="Note", index=2, entry_id="E02", timestamp=seed_ts),
                _entry(entry_type="Note", index=3, entry_id="E03", timestamp=burst_ts),
                _entry(entry_type="Note", index=4, entry_id="E04", timestamp=burst_ts),
                _entry(entry_type="Note", index=5, entry_id="E05", timestamp=burst_ts),
            ],
        )
        meta = thread_dir / "meta.json"
        meta.write_text(meta.read_text())

        # Tick 2 with cap=1: open_loop consumes the cap slot.
        # Burst detector runs but its finding can't be materialized.
        # Disable stance to isolate v1A cap behavior.
        daemon._config = ProjectCoordinatorConfig(
            max_findings_per_run=1,
            stance_enabled=False,
        )
        daemon._existing_keys.clear()
        daemon._ticks_since_resync = 0
        findings2 = daemon.tick()
        assert len(findings2) == 1
        assert findings2[0].category == "stalled_open_loop"

        # Key assertion: baseline must NOT have advanced past the seed
        baseline_after_cap = daemon._extras.burst_baselines.get("busy-topic")
        assert (
            baseline_after_cap == baseline_after_seed
        ), "Burst baseline must not advance when cap prevents finding emission"

    def test_capped_reappearance_refires_next_tick(self, tmp_path, monkeypatch):
        """Regression: reappearance findings dropped permanently when cap hit.

        If max_findings_per_run prevents a reappearance finding from being
        emitted, the seen-set must NOT advance for that contributor — so the
        reappearance re-fires on the next tick.
        """
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        from datetime import datetime, timezone

        threads_dir = tmp_path / "threads"
        now = datetime.now(tz=timezone.utc)
        now_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Two threads with two different new contributors
        _write_graph_thread(
            threads_dir,
            "topic-a",
            entries=[
                _entry(agent="AliceNew", index=0, entry_id="EA0", timestamp=now_ts),
            ],
        )
        _write_graph_thread(
            threads_dir,
            "topic-b",
            entries=[
                _entry(agent="BobNew", index=0, entry_id="EB0", timestamp=now_ts),
            ],
        )

        # Cap at 1 — only one new_contributor finding can be emitted
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, max_findings_per_run=1)
        findings1 = daemon.tick()

        # Exactly 1 new_contributor finding emitted (cap)
        nc1 = [f for f in findings1 if f.category == "aware_new_contributor"]
        assert len(nc1) == 1
        emitted_contributor = nc1[0].details["contributor"]

        # Determine which contributor was dropped (names are normalized by
        # normalize_agent — which strips platform prefixes but doesn't lowercase)
        all_expected = {"AliceNew", "BobNew"}
        emitted_set = {emitted_contributor}
        dropped_set = all_expected - emitted_set
        assert len(dropped_set) == 1
        dropped_contributor = dropped_set.pop()

        # The dropped contributor must NOT be in seen_contributors
        assert (
            dropped_contributor not in daemon._extras.seen_contributors
        ), f"Dropped contributor '{dropped_contributor}' must not be in seen-set"
        # The emitted contributor SHOULD be in seen_contributors
        assert emitted_contributor in daemon._extras.seen_contributors

        # Tick 2 with higher cap: dropped contributor's finding should re-fire
        daemon._config = ProjectCoordinatorConfig(max_findings_per_run=200)
        # Force rescan by touching meta files
        graph_dir = storage.get_graph_dir(threads_dir)
        for topic in ["topic-a", "topic-b"]:
            meta = storage.get_thread_graph_dir(graph_dir, topic) / "meta.json"
            meta.write_text(meta.read_text())
        findings2 = daemon.tick()
        nc2 = [f for f in findings2 if f.category == "aware_new_contributor"]
        nc2_contributors = {f.details["contributor"] for f in nc2}
        assert (
            dropped_contributor in nc2_contributors
        ), f"Dropped contributor '{dropped_contributor}' must re-fire on next tick"

    def test_tick_hosted_mode_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        monkeypatch.setattr(
            "watercooler_mcp.daemons.project_coordinator.is_daemon_hosted_mode",
            lambda: True,
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir,
            "some-topic",
            entries=[
                _entry(entry_type="Plan", index=0),
                _entry(entry_type="Note", index=1, entry_id="E02"),
                _entry(entry_type="Note", index=2, entry_id="E03"),
            ],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings = daemon.tick()
        assert findings == []

    def test_tick_no_threads_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        nonexistent = tmp_path / "does-not-exist"
        daemon = _make_daemon(tmp_path, threads_dir=nonexistent)
        findings = daemon.tick()
        assert findings == []

    def test_status_summary(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        monkeypatch.setattr(
            "watercooler_mcp.daemons.project_coordinator.is_daemon_hosted_mode",
            lambda: False,
        )
        daemon = _make_daemon(tmp_path)
        summary = daemon.status_summary()
        assert summary["name"] == "project_coordinator"
        assert "last_tick_threads" in summary
        assert "last_tick_findings" in summary
        assert "last_tick_skipped" in summary
        assert summary["suppression_tags"] == ["parked", "wontfix", "deferred"]
        assert summary["hosted_mode"] is False

    def test_extras_persist_across_ticks(self, tmp_path, monkeypatch):
        """Verify burst baselines and seen-contributors survive across ticks."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        # Use recent timestamps so entries aren't pruned by NEW_CONTRIBUTOR_PRUNE_DAYS
        from datetime import datetime, timezone

        now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_graph_thread(
            threads_dir,
            "active-topic",
            entries=[
                _entry(agent="NewPerson", index=0, timestamp=now_iso),
                _entry(agent="NewPerson", index=1, entry_id="E02", timestamp=now_iso),
            ],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        daemon.tick()

        # Burst baselines should be populated for the scanned thread
        assert "active-topic" in daemon._extras.burst_baselines

    def test_closed_thread_skips_stalled_detectors(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir,
            "closed-topic",
            status="CLOSED",
            entries=[
                _entry(entry_type="Plan", index=0),
                _entry(entry_type="Note", index=1, entry_id="E02"),
                _entry(entry_type="Note", index=2, entry_id="E03"),
            ],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings = daemon.tick()
        # stalled_open_loop and stalled_dropout skip closed threads
        stalled = [f for f in findings if f.category.startswith("stalled_")]
        assert stalled == []

    def test_recent_open_loop_does_not_emit(self, tmp_path, monkeypatch):
        """Daemon-boundary test: threads with recent Plans don't fire stalled_open_loop."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        from datetime import datetime, timezone

        now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir,
            "fresh-plan-topic",
            entries=[
                _entry(entry_type="Note", index=0, timestamp=now_iso),
                _entry(entry_type="Plan", index=1, entry_id="E02", timestamp=now_iso),
                _entry(entry_type="Note", index=2, entry_id="E03", timestamp=now_iso),
            ],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings = daemon.tick()
        open_loop = [f for f in findings if f.category == "stalled_open_loop"]
        assert open_loop == [], "Recent threads should not trigger stalled_open_loop"

    def test_xref_decision_emits_info_suppression_finding(self, tmp_path, monkeypatch):
        """Daemon-boundary test (#347): when a stalled open loop is resolved
        by a cross-thread Decision xref, the daemon emits a
        ``coordinator_xref_suppression`` info finding and no
        ``stalled_open_loop`` finding for that topic."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )

        from watercooler.baseline_graph.annotations import (
            AnnotationEvent,
            append_annotation,
        )
        from watercooler.baseline_graph.storage import (
            get_graph_dir,
            get_thread_graph_dir,
        )
        from watercooler.baseline_graph.writer import EntryData, upsert_entry_node

        threads_dir = tmp_path / "threads"

        # Seed the stalled thread (Plan present, no Decision/Closure).
        _write_stalled_open_loop_thread(threads_dir, topic="stalled-topic")

        # Write a Decision entry in a different thread.
        decision_id = "DEC-XREF-001"
        upsert_entry_node(
            threads_dir,
            EntryData(
                entry_id=decision_id,
                thread_topic="decisions-thread",
                index=0,
                agent="Alice",
                role="implementer",
                entry_type="Decision",
                title="Cross-thread decision",
                body="Decided elsewhere.",
                summary="",
            ),
        )

        # Add an xref annotation from the stalled thread to that Decision.
        stalled_dir = get_thread_graph_dir(get_graph_dir(threads_dir), "stalled-topic")
        append_annotation(
            stalled_dir,
            AnnotationEvent(
                id="evt-xref-001",
                target_id="stalled-topic-E02",
                target_type="entry",
                kind="xref",
                value=decision_id,
                actor="Alice",
                timestamp="2024-04-01T12:00:00+00:00",
            ),
        )

        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings = daemon.tick()

        suppression = [
            f for f in findings if f.category == "coordinator_xref_suppression"
        ]
        stalled = [f for f in findings if f.category == "stalled_open_loop"]
        assert len(suppression) == 1, (
            f"expected exactly one coordinator_xref_suppression finding, got "
            f"{[f.category for f in findings]}"
        )
        assert suppression[0].severity == "info"
        assert suppression[0].topic == "stalled-topic"
        assert suppression[0].details["xref_resolves_to"] == decision_id
        assert stalled == [], "xref suppression must replace stalled_open_loop"

    def test_entry_topic_index_not_built_for_healthy_repo(self, tmp_path, monkeypatch):
        """Regression (Codex #2): ``build_entry_topic_index`` does a full
        reverse-index scan over every thread and must NOT run when no
        thread trips the staleness gate. Seeds a fresh (non-stalled)
        thread and verifies the lazy builder is never invoked — keeping
        tick cost off the hot path for healthy repos."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        from datetime import datetime, timezone

        threads_dir = tmp_path / "threads"
        now_ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # A fresh thread with a Plan but recent activity — the staleness
        # gate in detect_stalled_open_loops should reject it before ever
        # reaching the xref branch.
        _write_graph_thread(
            threads_dir,
            "fresh-topic",
            entries=[
                _entry(
                    agent="Alice",
                    index=0,
                    entry_id="F00",
                    entry_type="Plan",
                    timestamp=now_ts,
                ),
                _entry(agent="Alice", index=1, entry_id="F01", timestamp=now_ts),
                _entry(agent="Alice", index=2, entry_id="F02", timestamp=now_ts),
            ],
        )

        call_count = {"value": 0}

        import watercooler.baseline_graph.writer as writer_mod

        real_build = writer_mod.build_entry_topic_index

        def _counting_build(path):
            call_count["value"] += 1
            return real_build(path)

        monkeypatch.setattr(writer_mod, "build_entry_topic_index", _counting_build)

        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        daemon.tick()

        assert call_count["value"] == 0, (
            f"build_entry_topic_index ran {call_count['value']}× on a healthy "
            f"repo — whole-repo scan must stay off the hot path (Codex #2)"
        )

    def test_entry_topic_index_not_built_for_stale_thread_without_xrefs(
        self, tmp_path, monkeypatch
    ):
        """Regression (Codex #3, 2026-04-18): a *stalled* thread with no xref
        annotations must not trigger the whole-repo reverse-index scan. The
        Phase 3b-2 cost contract says traversal cost scales with xrefs-per-
        thread, not graph size, so the detector must gate the index build on
        the source thread's own annotation state before paying graph-wide
        cost."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        from datetime import datetime, timedelta, timezone

        threads_dir = tmp_path / "threads"
        stale_ts = (datetime.now(tz=timezone.utc) - timedelta(days=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        # Stalled thread: 3+ entries, Plan present, no Decision/Closure,
        # timestamps > OPEN_LOOP_MIN_STALE_DAYS old. Crucially: no
        # annotation state with xrefs.
        _write_graph_thread(
            threads_dir,
            "stale-no-xrefs",
            entries=[
                _entry(
                    agent="Alice",
                    index=0,
                    entry_id="S00",
                    entry_type="Plan",
                    timestamp=stale_ts,
                ),
                _entry(agent="Alice", index=1, entry_id="S01", timestamp=stale_ts),
                _entry(agent="Alice", index=2, entry_id="S02", timestamp=stale_ts),
            ],
        )

        call_count = {"value": 0}

        import watercooler.baseline_graph.writer as writer_mod

        real_build = writer_mod.build_entry_topic_index

        def _counting_build(path):
            call_count["value"] += 1
            return real_build(path)

        monkeypatch.setattr(writer_mod, "build_entry_topic_index", _counting_build)

        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings = daemon.tick()

        assert call_count["value"] == 0, (
            f"build_entry_topic_index ran {call_count['value']}× for a stalled "
            f"thread with no xrefs — whole-repo scan must be gated on actual "
            f"xref presence (Codex #3)"
        )
        # And the detector should still emit a normal stalled_open_loop finding;
        # gating the index build must not silence the detector itself.
        stalled = [
            f
            for f in findings
            if f.category == "stalled_open_loop" and f.topic == "stale-no-xrefs"
        ]
        assert len(stalled) == 1, (
            f"stalled_open_loop must still fire when gate skips xref traversal; "
            f"got: {[(f.category, f.topic) for f in findings]}"
        )

    def test_malformed_annotation_cache_does_not_abort_tick(
        self, tmp_path, monkeypatch
    ):
        """Regression (Codex #4, 2026-04-19): the Phase 3b-2 fail-open
        contract says any read/parse/shape error on annotation state must
        degrade the xref traversal to a no-op — it must NOT abort the
        tick. A malformed cache like ``{"entry-1": 123}`` previously raised
        ``AttributeError`` from ``AnnotationState.from_dict`` because the
        loader caught only ``(OSError, ValueError, JSONDecodeError)``. The
        helper now catches ``Exception`` with ``exc_info`` logging, so a
        corrupt cache degrades gracefully and the detector still emits its
        normal ``stalled_open_loop`` finding."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        import json as _json
        from datetime import datetime, timedelta, timezone

        threads_dir = tmp_path / "threads"
        stale_ts = (datetime.now(tz=timezone.utc) - timedelta(days=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        _write_graph_thread(
            threads_dir,
            "malformed-cache",
            entries=[
                _entry(
                    agent="Alice",
                    index=0,
                    entry_id="M00",
                    entry_type="Plan",
                    timestamp=stale_ts,
                ),
                _entry(agent="Alice", index=1, entry_id="M01", timestamp=stale_ts),
                _entry(agent="Alice", index=2, entry_id="M02", timestamp=stale_ts),
            ],
        )

        # Plant a malformed annotation_state.json that will trip
        # AnnotationState.from_dict with AttributeError: the int 123
        # has no ``.get()`` method. Crucially we do NOT create an
        # annotations.jsonl, which routes the loader through the
        # no-event-log branch where the _ann_size gate is not enforced.
        from watercooler.baseline_graph import storage as _storage

        graph_dir = _storage.ensure_graph_dir(threads_dir)
        thread_dir = _storage.ensure_thread_graph_dir(graph_dir, "malformed-cache")
        (thread_dir / "annotation_state.json").write_text(
            _json.dumps({"entry-1": 123}),
            encoding="utf-8",
        )

        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings = daemon.tick()  # must not raise

        stalled = [
            f
            for f in findings
            if f.category == "stalled_open_loop" and f.topic == "malformed-cache"
        ]
        assert len(stalled) == 1, (
            "fail-open contract: malformed annotation cache must not silence "
            "the stalled_open_loop detector; "
            f"got: {[(f.category, f.topic) for f in findings]}"
        )

    def test_xref_suppression_does_not_consume_finding_cap(self, tmp_path, monkeypatch):
        """Regression (Codex #1): ``coordinator_xref_suppression`` info
        findings must not count against ``max_findings_per_run``. If they
        did, a suppressed thread could crowd out a real actionable finding
        in a later topic. Seeds a stalled thread resolved by xref
        *and* a second stalled thread with no xref, with the cap set so
        both can fit only if suppression is exempt from the count.
        """
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )

        from watercooler.baseline_graph.annotations import (
            AnnotationEvent,
            append_annotation,
        )
        from watercooler.baseline_graph.storage import (
            get_graph_dir,
            get_thread_graph_dir,
        )
        from watercooler.baseline_graph.writer import EntryData, upsert_entry_node

        threads_dir = tmp_path / "threads"

        # Topics are iterated in alphabetical order. Name them so the
        # suppression topic is visited FIRST — this reproduces Codex's
        # original scenario where a suppression finding, if it counted
        # toward the cap, would crowd out a real finding in a later topic.
        _write_stalled_open_loop_thread(threads_dir, topic="a-suppressed-topic")
        decision_id = "DEC-XREF-CAP"
        upsert_entry_node(
            threads_dir,
            EntryData(
                entry_id=decision_id,
                thread_topic="a-decisions",
                index=0,
                agent="Alice",
                role="implementer",
                entry_type="Decision",
                title="Cross-thread decision",
                body="Decided elsewhere.",
                summary="",
            ),
        )
        suppressed_dir = get_thread_graph_dir(
            get_graph_dir(threads_dir), "a-suppressed-topic"
        )
        append_annotation(
            suppressed_dir,
            AnnotationEvent(
                id="evt-xref-cap",
                target_id="a-suppressed-topic-E02",
                target_type="entry",
                kind="xref",
                value=decision_id,
                actor="Alice",
                timestamp="2024-04-01T12:00:00+00:00",
            ),
        )

        # Second stalled thread, sorted AFTER the suppressed one, with no
        # xref → real stalled_open_loop finding. If suppression counted
        # toward max_findings_per_run=1, this finding would be dropped.
        _write_stalled_open_loop_thread(threads_dir, topic="b-real-stalled-topic")

        # Force topic iteration order — list_thread_topics returns
        # filesystem order (non-deterministic). Processing the suppression
        # topic FIRST is the scenario that exposes the bug: if the
        # suppression finding counted toward the cap, the later
        # actionable topic would be dropped.
        from watercooler.baseline_graph import storage as _storage

        real_list = _storage.list_thread_topics
        monkeypatch.setattr(
            _storage,
            "list_thread_topics",
            lambda gd: sorted(real_list(gd)),
        )

        # Cap at 1 — if suppression counted, only one of the two findings
        # would emit. With suppression exempt, both emit.
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, max_findings_per_run=1)
        findings = daemon.tick()

        suppression = [
            f for f in findings if f.category == "coordinator_xref_suppression"
        ]
        stalled = [f for f in findings if f.category == "stalled_open_loop"]

        assert len(suppression) == 1, (
            f"expected suppression finding despite cap=1, got categories "
            f"{[f.category for f in findings]}"
        )
        assert len(stalled) == 1, (
            f"expected stalled_open_loop in addition to suppression (cap=1 "
            f"reserves slots for actionable findings only), got categories "
            f"{[f.category for f in findings]}"
        )
        assert stalled[0].topic == "b-real-stalled-topic"

    def test_xref_suppression_preserved_when_actionable_precedes_exempt_in_batch(
        self, tmp_path, monkeypatch
    ):
        """Regression: when a single thread's ``thread_findings`` batch is
        ordered [actionable, exempt] and the actionable item pushes the run
        past ``max_findings_per_run``, the exempt ``coordinator_xref_suppression``
        finding that follows must still be materialized.

        Today's detector ordering (stalled_open_loop first) happens to put
        any xref_suppression at index 0 of ``thread_findings``, so this
        invariant is not observably violated in production. The fix
        (``continue`` instead of ``break`` in the materialization loop, with
        ``_CAP_EXEMPT_CATEGORIES`` awareness) is contractual: if a future
        detector reordering ever puts an actionable finding ahead of an
        exempt one, the observability guarantee must still hold.

        We demonstrate the guarantee by monkey-patching
        ``detect_stalled_dropout`` to return ``[actionable, exempt]`` in a
        single thread's batch, with ``max_findings_per_run=1``.
        """
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )

        threads_dir = tmp_path / "threads"

        # Single thread — three implementer entries keep things simple; we
        # do not rely on the real detectors firing.
        _write_graph_thread(
            threads_dir,
            "only-topic",
            entries=[
                _entry(role="implementer", index=0, entry_id="only-topic-E00"),
                _entry(role="implementer", index=1, entry_id="only-topic-E01"),
                _entry(role="implementer", index=2, entry_id="only-topic-E02"),
            ],
        )

        from watercooler.project_coordinator_lib import CoordinatorFinding
        from watercooler_mcp.daemons import project_coordinator as pc_module

        actionable = CoordinatorFinding(
            category="aware_role_concentration",
            topic="only-topic",
            severity="info",
            message="stub actionable",
            dedup_signature="stub-actionable",
        )
        exempt = CoordinatorFinding(
            category="coordinator_xref_suppression",
            topic="only-topic",
            severity="info",
            message="stub exempt — must not be dropped by cap",
            dedup_signature="stub-exempt",
            details={"cross_thread_topic": "elsewhere"},
        )

        # Return [actionable, exempt] so the actionable consumes the cap
        # before the loop reaches the exempt item. This is the ordering
        # that the ``break``→``continue`` fix protects against.
        monkeypatch.setattr(
            pc_module,
            "detect_stalled_dropout",
            lambda *_a, **_kw: [actionable, exempt],
        )
        # Silence the other per-thread detectors so ``thread_findings`` is
        # exactly ``[actionable, exempt]``.
        monkeypatch.setattr(
            pc_module,
            "detect_stalled_open_loops",
            lambda *_a, **_kw: None,
        )
        monkeypatch.setattr(
            pc_module,
            "detect_aware_burst",
            lambda entries, topic, baseline, tick_time, **_kw: (None, baseline),
        )
        monkeypatch.setattr(
            pc_module,
            "detect_role_concentration",
            lambda *_a, **_kw: None,
        )

        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, max_findings_per_run=1)
        findings = daemon.tick()

        actionable_findings = [
            f for f in findings if f.category == "aware_role_concentration"
        ]
        exempt_findings = [
            f for f in findings if f.category == "coordinator_xref_suppression"
        ]

        assert len(actionable_findings) == 1, (
            f"stub actionable should emit (fills the cap at count=1), got "
            f"{[(f.topic, f.category) for f in findings]}"
        )
        assert len(exempt_findings) == 1, (
            "exempt xref_suppression must still emit even though an actionable "
            "earlier in the same thread_findings batch already filled the cap — "
            "this is the invariant the break→continue fix preserves. Got "
            f"{[(f.topic, f.category) for f in findings]}"
        )

    def test_system_tagged_entries_normalize_through_coordinator(
        self, tmp_path, monkeypatch
    ):
        """Daemon-boundary test: entries with agent 'Daemon (system)' normalize cleanly."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        # Simulate the fixed daemon_write agent format: "Daemon (system)"
        # and the legacy double-wrapped format: "Daemon ((system))"
        _write_graph_thread(
            threads_dir,
            "daemon-thread",
            entries=[
                _entry(agent="Daemon (system)", index=0, entry_id="E01"),
                _entry(agent="Daemon (system)", index=1, entry_id="E02"),
                _entry(agent="Daemon (system)", index=2, entry_id="E03"),
                _entry(agent="Daemon ((system))", index=3, entry_id="E04"),
                _entry(agent="Daemon ((system))", index=4, entry_id="E05"),
                _entry(agent="Daemon ((system))", index=5, entry_id="E06"),
                _entry(agent="Alice", index=6, entry_id="E07"),
                _entry(agent="Alice", index=7, entry_id="E08"),
                _entry(agent="Alice", index=8, entry_id="E09"),
            ],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings = daemon.tick()
        # Both "Daemon (system)" and "Daemon ((system))" should normalize
        # to "system", so no dropout between them.
        dropout = [f for f in findings if f.category == "stalled_dropout"]
        for f in dropout:
            assert "(system" not in f.details.get(
                "contributor", ""
            ), f"Malformed contributor name in dropout finding: {f.details}"
        # New contributor findings should not contain "(system"
        new_contrib = [f for f in findings if f.category == "aware_new_contributor"]
        for f in new_contrib:
            assert (
                f.details.get("contributor") != "(system"
            ), f"Malformed contributor in new_contributor finding: {f.details}"


# ---------------------------------------------------------------------------
# v1B: Stance advisory emission tests
# ---------------------------------------------------------------------------


class TestStanceAdvisoryEmission:
    """Tests for stance modulation in the coordinator daemon."""

    def _stance_findings(self, findings):
        return [f for f in findings if f.category == "stance_advisory"]

    def test_stance_emitted_with_elevated_signals(self, tmp_path, monkeypatch):
        """Coordinator emits stance findings when signals cross thresholds."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        # Create multiple threads with stalled open loops to cross thresholds
        for i in range(3):
            _write_graph_thread(
                threads_dir,
                f"stalled-{i}",
                entries=[
                    _entry(entry_type="Plan", index=0, entry_id=f"E{i}0"),
                    _entry(entry_type="Note", index=1, entry_id=f"E{i}1"),
                    _entry(entry_type="Note", index=2, entry_id=f"E{i}2"),
                ],
            )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings = daemon.tick()
        stance = self._stance_findings(findings)
        # Should have planner stance at L1+ (stalled open loops >= 2)
        planner = [f for f in stance if f.topic == "stance:planner"]
        assert len(planner) >= 1
        assert planner[0].details["advisory"]["level"] >= 1

    def test_stance_emitted_degraded_mode(self, tmp_path, monkeypatch):
        """Stance works without pulse snapshot (degraded coordinator-only)."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        for i in range(3):
            _write_graph_thread(
                threads_dir,
                f"stalled-{i}",
                entries=[
                    _entry(entry_type="Plan", index=0, entry_id=f"E{i}0"),
                    _entry(entry_type="Note", index=1, entry_id=f"E{i}1"),
                    _entry(entry_type="Note", index=2, entry_id=f"E{i}2"),
                ],
            )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings = daemon.tick()
        stance = self._stance_findings(findings)
        # Degraded mode should still produce findings from coordinator signals
        assert len(stance) >= 1, "Expected stance advisory in degraded mode"
        advisory = stance[0].details["advisory"]
        assert advisory["signal_values"]["pulse_available"] is False
        assert len(advisory["missing_inputs"]) > 0

    def test_stance_disabled(self, tmp_path, monkeypatch):
        """stance_enabled=False produces no stance findings."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        for i in range(3):
            _write_graph_thread(
                threads_dir,
                f"stalled-{i}",
                entries=[
                    _entry(entry_type="Plan", index=0, entry_id=f"E{i}0"),
                    _entry(entry_type="Note", index=1, entry_id=f"E{i}1"),
                    _entry(entry_type="Note", index=2, entry_id=f"E{i}2"),
                ],
            )
        daemon = _make_daemon(
            tmp_path,
            threads_dir=threads_dir,
            stance_enabled=False,
        )
        findings = daemon.tick()
        stance = self._stance_findings(findings)
        assert stance == []

    def test_stance_dedup_unchanged(self, tmp_path, monkeypatch):
        """Unchanged advisory between ticks → no duplicate emitted."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        for i in range(3):
            _write_graph_thread(
                threads_dir,
                f"stalled-{i}",
                entries=[
                    _entry(entry_type="Plan", index=0, entry_id=f"E{i}0"),
                    _entry(entry_type="Note", index=1, entry_id=f"E{i}1"),
                    _entry(entry_type="Note", index=2, entry_id=f"E{i}2"),
                ],
            )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        # Pre-seed Alice so aware_new_contributor doesn't perturb stance signature
        _seed_known_contributors(daemon, "Alice")
        daemon.tick()

        # Tick 2 with same state — stance should be deduped
        findings2 = daemon.tick()
        stance2 = self._stance_findings(findings2)
        assert len(stance2) == 0, "Unchanged stance should be deduped"
        assert (
            daemon._last_stance_outcome != "emitted"
        ), f"Deduped tick must not report 'emitted', got {daemon._last_stance_outcome!r}"

    def test_stance_replace_on_change(self, tmp_path, monkeypatch):
        """When advisory signature changes, new finding emitted."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        # Start with 2 stalled loops (soft threshold)
        for i in range(2):
            _write_graph_thread(
                threads_dir,
                f"stalled-{i}",
                entries=[
                    _entry(entry_type="Plan", index=0, entry_id=f"E{i}0"),
                    _entry(entry_type="Note", index=1, entry_id=f"E{i}1"),
                    _entry(entry_type="Note", index=2, entry_id=f"E{i}2"),
                ],
            )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings1 = daemon.tick()
        stance1 = self._stance_findings(findings1)
        planner1 = [f for f in stance1 if f.topic == "stance:planner"]

        # Add more stalled threads to push from SOFT to HARD
        for i in range(2, 6):
            _write_graph_thread(
                threads_dir,
                f"stalled-{i}",
                entries=[
                    _entry(entry_type="Plan", index=0, entry_id=f"E{i}0"),
                    _entry(entry_type="Note", index=1, entry_id=f"E{i}1"),
                    _entry(entry_type="Note", index=2, entry_id=f"E{i}2"),
                ],
            )

        findings2 = daemon.tick()
        stance2 = self._stance_findings(findings2)
        planner2 = [f for f in stance2 if f.topic == "stance:planner"]

        assert len(planner1) >= 1, "tick 1 should produce planner advisory"
        assert len(planner2) >= 1, "tick 2 should produce planner advisory"
        # Different finding IDs means replace-on-change worked
        assert planner1[0].finding_id != planner2[0].finding_id

    def test_stance_replace_on_change_aba_cycle(self, tmp_path, monkeypatch):
        """A→B→A cycle: returning to original signature must re-emit (not suppressed).

        When signature changes from A to B (A→B), the old fid_A must be cleared
        from _existing_keys so that a subsequent return to signature A (B→A) can
        re-emit fid_A rather than being suppressed by the stale dedup key.
        """
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        graph_dir = storage.get_graph_dir(threads_dir)

        # Tick 1: soft threshold (signature A)
        for i in range(2):
            _write_graph_thread(
                threads_dir,
                f"stalled-{i}",
                entries=[
                    _entry(entry_type="Plan", index=0, entry_id=f"E{i}0"),
                    _entry(entry_type="Note", index=1, entry_id=f"E{i}1"),
                    _entry(entry_type="Note", index=2, entry_id=f"E{i}2"),
                ],
            )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings1 = daemon.tick()
        planner1 = [
            f for f in self._stance_findings(findings1) if f.topic == "stance:planner"
        ]
        assert len(planner1) >= 1, "tick 1 should emit L1 planner advisory (sig A)"
        fid_a = planner1[0].finding_id

        # Tick 2: hard threshold (signature B — level/crossings differ)
        for i in range(2, 6):
            _write_graph_thread(
                threads_dir,
                f"stalled-{i}",
                entries=[
                    _entry(entry_type="Plan", index=0, entry_id=f"E{i}0"),
                    _entry(entry_type="Note", index=1, entry_id=f"E{i}1"),
                    _entry(entry_type="Note", index=2, entry_id=f"E{i}2"),
                ],
            )
        findings2 = daemon.tick()
        planner2 = [
            f for f in self._stance_findings(findings2) if f.topic == "stance:planner"
        ]
        assert len(planner2) >= 1, "tick 2 should emit L2 planner advisory (sig B)"
        fid_b = planner2[0].finding_id
        assert fid_a != fid_b, "sig A → sig B should produce a different finding ID"

        # Tick 3: back to soft threshold (signature A again — remove extra stalled threads)
        for i in range(2, 6):
            thread_dir = storage.get_thread_graph_dir(graph_dir, f"stalled-{i}")
            storage.atomic_write_jsonl(
                thread_dir / "entries.jsonl",
                [
                    _entry(
                        entry_type="Note",
                        index=0,
                        entry_id=f"E{i}0",
                    )
                ],
            )
            (thread_dir / "meta.json").write_text(
                (thread_dir / "meta.json").read_text()
            )
        findings3 = daemon.tick()
        planner3 = [
            f for f in self._stance_findings(findings3) if f.topic == "stance:planner"
        ]
        assert (
            len(planner3) >= 1
        ), "tick 3 (sig A again) must re-emit — fid_A must not be suppressed by stale dedup key"

    def test_stance_removed_topic_not_in_same_tick(self, tmp_path, monkeypatch):
        """Removed topics must not affect stance computation in the same tick they are pruned.

        When threads are removed from the graph, their active_signals entries must
        be pruned BEFORE stance computation so the advisory reflects the current
        signal count, not the stale one.
        """
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        graph_dir = storage.get_graph_dir(threads_dir)

        # Tick 1: enough stalled threads to trigger elevated stance
        for i in range(3):
            _write_graph_thread(
                threads_dir,
                f"stalled-{i}",
                entries=[
                    _entry(entry_type="Plan", index=0, entry_id=f"E{i}0"),
                    _entry(entry_type="Note", index=1, entry_id=f"E{i}1"),
                    _entry(entry_type="Note", index=2, entry_id=f"E{i}2"),
                ],
            )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings1 = daemon.tick()
        stance1 = [
            f for f in self._stance_findings(findings1) if f.topic == "stance:planner"
        ]
        assert len(stance1) >= 1, "tick 1 should produce planner advisory"

        # Remove all stalled threads from graph — signals should clear this tick
        for i in range(3):
            thread_dir = storage.get_thread_graph_dir(graph_dir, f"stalled-{i}")
            import shutil

            shutil.rmtree(str(thread_dir))

        findings2 = daemon.tick()
        stance2_planner = [
            f for f in self._stance_findings(findings2) if f.topic == "stance:planner"
        ]
        # If active_signals is pruned before stance computation, the planner should
        # emit a tombstone (L0) this tick, not stay elevated
        if stance2_planner:
            advisory = stance2_planner[0].details.get("advisory", {})
            assert advisory.get("level") == 0, (
                "Planner advisory should be L0 (tombstone) in the tick threads are removed, "
                f"not still elevated; got level={advisory.get('level')}"
            )

    def test_stance_tombstone_on_clearance(self, tmp_path, monkeypatch):
        """L1/L2 → L0 emits tombstone with level=0."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        # Create threads that produce stalled open loops → elevated stance
        for i in range(3):
            _write_graph_thread(
                threads_dir,
                f"stalled-{i}",
                entries=[
                    _entry(entry_type="Plan", index=0, entry_id=f"E{i}0"),
                    _entry(entry_type="Note", index=1, entry_id=f"E{i}1"),
                    _entry(entry_type="Note", index=2, entry_id=f"E{i}2"),
                ],
            )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings1 = daemon.tick()
        stance1 = self._stance_findings(findings1)
        # Verify we got elevated stance
        planner1 = [f for f in stance1 if f.topic == "stance:planner"]
        assert len(planner1) >= 1

        # Now clear all stalled conditions by adding Decision entries
        graph_dir = storage.get_graph_dir(threads_dir)
        from datetime import datetime, timezone

        now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for i in range(3):
            thread_dir = storage.get_thread_graph_dir(graph_dir, f"stalled-{i}")
            storage.atomic_write_jsonl(
                thread_dir / "entries.jsonl",
                [
                    _entry(
                        entry_type="Decision",
                        index=0,
                        entry_id=f"E{i}0",
                        timestamp=now_iso,
                    ),
                ],
            )
            # Touch meta to force rescan
            meta = thread_dir / "meta.json"
            meta.write_text(meta.read_text())

        findings2 = daemon.tick()
        stance2 = self._stance_findings(findings2)
        # Should have tombstone for planner (level=0)
        tombstones = [
            f
            for f in stance2
            if f.topic == "stance:planner"
            and f.details.get("advisory", {}).get("level") == 0
        ]
        assert len(tombstones) >= 1, "Should emit tombstone on L1→L0 clearance"

    def test_stance_l0_no_tombstone_when_already_clear(self, tmp_path, monkeypatch):
        """L0 → L0 should not emit tombstone."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        from datetime import datetime, timezone

        now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        threads_dir = tmp_path / "threads"
        # Create benign thread — no stalled loops
        _write_graph_thread(
            threads_dir,
            "benign",
            entries=[
                _entry(
                    entry_type="Note",
                    index=0,
                    entry_id="E00",
                    timestamp=now_iso,
                ),
            ],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        # Pre-seed Alice so aware_new_contributor doesn't elevate planner to L1
        _seed_known_contributors(daemon, "Alice")
        findings1 = daemon.tick()
        stance1 = self._stance_findings(findings1)
        # Should be all-L0 → no stance findings emitted
        assert stance1 == [], "L0 advisories should not emit findings"

        findings2 = daemon.tick()
        stance2 = self._stance_findings(findings2)
        assert stance2 == [], "L0→L0 should not emit tombstones"

    def test_stance_exempt_from_cap(self, tmp_path, monkeypatch):
        """Stance findings emitted even when v1A findings hit the cap."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        # Create enough stalled threads to cross soft threshold
        for i in range(3):
            _write_graph_thread(
                threads_dir,
                f"stalled-{i}",
                entries=[
                    _entry(entry_type="Plan", index=0, entry_id=f"E{i}0"),
                    _entry(entry_type="Note", index=1, entry_id=f"E{i}1"),
                    _entry(entry_type="Note", index=2, entry_id=f"E{i}2"),
                ],
            )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        # Tick 1 with high cap: populate active_signals for all threads
        daemon.tick()
        assert len(daemon._extras.active_signals) >= 2

        # Tick 2 with cap=1: v1A is capped but stance should still emit
        # because active_signals persists from tick 1
        graph_dir = storage.get_graph_dir(threads_dir)
        for i in range(3):
            meta = storage.get_thread_graph_dir(graph_dir, f"stalled-{i}") / "meta.json"
            meta.write_text(meta.read_text())
        daemon._config = ProjectCoordinatorConfig(max_findings_per_run=1)
        daemon._existing_keys.clear()
        daemon._ticks_since_resync = 0
        findings2 = daemon.tick()
        v1a = [f for f in findings2 if f.category != "stance_advisory"]
        stance = self._stance_findings(findings2)
        assert len(v1a) <= 1, "v1A capped at 1"
        assert len(stance) >= 1, "Stance should emit even when v1A cap hit"

    def test_stance_severity_mapping(self, tmp_path, monkeypatch):
        """L1 → info, L2 → warning."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        for i in range(3):
            _write_graph_thread(
                threads_dir,
                f"stalled-{i}",
                entries=[
                    _entry(entry_type="Plan", index=0, entry_id=f"E{i}0"),
                    _entry(entry_type="Note", index=1, entry_id=f"E{i}1"),
                    _entry(entry_type="Note", index=2, entry_id=f"E{i}2"),
                ],
            )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings = daemon.tick()
        for f in self._stance_findings(findings):
            level = f.details["advisory"]["level"]
            if level == 1:
                assert f.severity == "info"
            elif level >= 2:
                assert f.severity == "warning"

    def test_stance_per_role_dedup_isolation(self, tmp_path, monkeypatch):
        """Different roles have independent dedup buckets."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        for i in range(3):
            _write_graph_thread(
                threads_dir,
                f"stalled-{i}",
                entries=[
                    _entry(entry_type="Plan", index=0, entry_id=f"E{i}0"),
                    _entry(entry_type="Note", index=1, entry_id=f"E{i}1"),
                    _entry(entry_type="Note", index=2, entry_id=f"E{i}2"),
                ],
            )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings = daemon.tick()
        stance = self._stance_findings(findings)
        # Each emitted finding should have a unique topic per role
        for f in stance:
            assert f.topic.startswith("stance:")

    def test_active_signals_persisted(self, tmp_path, monkeypatch):
        """active_signals map persists across ticks for unchanged threads."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir,
            "stalled-topic",
            entries=[
                _entry(entry_type="Plan", index=0, entry_id="E00"),
                _entry(entry_type="Note", index=1, entry_id="E01"),
                _entry(entry_type="Note", index=2, entry_id="E02"),
            ],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        daemon.tick()
        assert "stalled-topic" in daemon._extras.active_signals
        cats = daemon._extras.active_signals["stalled-topic"].categories
        assert len(cats) > 0

        # Tick 2: unchanged thread → active_signals should persist
        daemon.tick()
        assert "stalled-topic" in daemon._extras.active_signals

    def test_active_signals_cleared_on_topic_removal(self, tmp_path, monkeypatch):
        """active_signals pruned when thread removed from graph."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        from datetime import datetime, timezone

        now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir,
            "temp-topic",
            entries=[
                _entry(entry_type="Plan", index=0, entry_id="E00"),
                _entry(entry_type="Note", index=1, entry_id="E01"),
                _entry(entry_type="Note", index=2, entry_id="E02"),
            ],
        )
        # Keep another thread so tick doesn't short-circuit on empty topics
        _write_graph_thread(
            threads_dir,
            "keeper",
            entries=[
                _entry(entry_type="Note", index=0, entry_id="K00", timestamp=now_iso),
            ],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        daemon.tick()
        assert "temp-topic" in daemon._extras.active_signals

        # Remove thread from graph
        import shutil

        graph_dir = storage.get_graph_dir(threads_dir)
        thread_dir = storage.get_thread_graph_dir(graph_dir, "temp-topic")
        shutil.rmtree(thread_dir)

        daemon.tick()
        assert "temp-topic" not in daemon._extras.active_signals

    def test_observability_fields(self, tmp_path, monkeypatch):
        """status_summary includes stance observability fields."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        monkeypatch.setattr(
            "watercooler_mcp.daemons.project_coordinator.is_daemon_hosted_mode",
            lambda: False,
        )
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        summary = daemon.status_summary()
        assert "stance_enabled" in summary
        assert "stance_snapshot_available" in summary
        assert "stance_last_outcome" in summary
        assert "stance_last_levels" in summary

    def test_full_cycle_l0_l1_l0_l1(self, tmp_path, monkeypatch):
        """Full cycle: L0→L1→L0→L1 — tombstone emits, re-escalation works."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        from datetime import datetime, timezone

        now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        threads_dir = tmp_path / "threads"

        # Tick 1: benign → L0, no findings
        _write_graph_thread(
            threads_dir,
            "cycle-topic",
            entries=[
                _entry(entry_type="Note", index=0, entry_id="E00", timestamp=now_iso),
            ],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        # Pre-seed Alice so aware_new_contributor doesn't elevate planner to L1
        _seed_known_contributors(daemon, "Alice")
        findings1 = daemon.tick()
        stance1 = self._stance_findings(findings1)
        assert stance1 == []

        # Tick 2: add stalled loops → L1+
        graph_dir = storage.get_graph_dir(threads_dir)
        for i in range(3):
            _write_graph_thread(
                threads_dir,
                f"stalled-{i}",
                entries=[
                    _entry(entry_type="Plan", index=0, entry_id=f"E{i}0"),
                    _entry(entry_type="Note", index=1, entry_id=f"E{i}1"),
                    _entry(entry_type="Note", index=2, entry_id=f"E{i}2"),
                ],
            )
        findings2 = daemon.tick()
        stance2 = self._stance_findings(findings2)
        planner2 = [f for f in stance2 if f.topic == "stance:planner"]
        assert len(planner2) >= 1

        # Tick 3: clear all stalled → L0, tombstone emitted
        for i in range(3):
            thread_dir = storage.get_thread_graph_dir(graph_dir, f"stalled-{i}")
            storage.atomic_write_jsonl(
                thread_dir / "entries.jsonl",
                [
                    _entry(
                        entry_type="Decision",
                        index=0,
                        entry_id=f"E{i}0",
                        timestamp=now_iso,
                    )
                ],
            )
            meta = thread_dir / "meta.json"
            meta.write_text(meta.read_text())
        findings3 = daemon.tick()
        tombstones = [
            f
            for f in self._stance_findings(findings3)
            if f.topic == "stance:planner"
            and f.details.get("advisory", {}).get("level") == 0
        ]
        assert len(tombstones) >= 1

        # Tick 4: re-add stalled loops → L1+ again (re-escalation)
        for i in range(3):
            thread_dir = storage.get_thread_graph_dir(graph_dir, f"stalled-{i}")
            storage.atomic_write_jsonl(
                thread_dir / "entries.jsonl",
                [
                    _entry(entry_type="Plan", index=0, entry_id=f"E{i}0"),
                    _entry(entry_type="Note", index=1, entry_id=f"E{i}1"),
                    _entry(entry_type="Note", index=2, entry_id=f"E{i}2"),
                ],
            )
            meta = thread_dir / "meta.json"
            meta.write_text(meta.read_text())
        findings4 = daemon.tick()
        stance4 = self._stance_findings(findings4)
        planner4 = [f for f in stance4 if f.topic == "stance:planner"]
        assert len(planner4) >= 1, "Re-escalation should emit new finding"

    def test_full_cycle_with_resync(self, tmp_path, monkeypatch):
        """L1→L0→(resync re-adds old fid via disk)→L1 — re-escalation must not be blocked.

        Simulates the real bug: _existing_keys.discard(prev_fid) is in-memory
        only; load_findings() on resync reloads prev_fid from disk and re-adds
        it to _existing_keys, blocking re-escalation.  cleared_stance_fids in
        CoordinatorExtras must subtract it back out after resync.
        """
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        from datetime import datetime, timezone

        now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        threads_dir = tmp_path / "threads"
        graph_dir = storage.get_graph_dir(threads_dir)

        # Tick 1: benign → L0
        _write_graph_thread(
            threads_dir,
            "cycle-topic",
            entries=[
                _entry(entry_type="Note", index=0, entry_id="E00", timestamp=now_iso),
            ],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        _seed_known_contributors(daemon, "Alice")
        findings1 = daemon.tick()
        assert self._stance_findings(findings1) == []

        # Tick 2: add stalled loops → L1+
        for i in range(3):
            _write_graph_thread(
                threads_dir,
                f"stalled-{i}",
                entries=[
                    _entry(entry_type="Plan", index=0, entry_id=f"E{i}0"),
                    _entry(entry_type="Note", index=1, entry_id=f"E{i}1"),
                    _entry(entry_type="Note", index=2, entry_id=f"E{i}2"),
                ],
            )
        findings2 = daemon.tick()
        planner2 = [
            f for f in self._stance_findings(findings2) if f.topic == "stance:planner"
        ]
        assert len(planner2) >= 1
        prior_fid = planner2[0].finding_id

        # Tick 3: clear all stalled → L0, tombstone emitted.
        # After this tick, prior_fid must be in cleared_stance_fids.
        for i in range(3):
            thread_dir = storage.get_thread_graph_dir(graph_dir, f"stalled-{i}")
            storage.atomic_write_jsonl(
                thread_dir / "entries.jsonl",
                [
                    _entry(
                        entry_type="Decision",
                        index=0,
                        entry_id=f"E{i}0",
                        timestamp=now_iso,
                    )
                ],
            )
            (thread_dir / "meta.json").write_text(
                (thread_dir / "meta.json").read_text()
            )
        findings3 = daemon.tick()
        tombstones = [
            f
            for f in self._stance_findings(findings3)
            if f.topic == "stance:planner"
            and f.details.get("advisory", {}).get("level") == 0
        ]
        assert len(tombstones) >= 1
        assert (
            prior_fid in daemon._extras.cleared_stance_fids
        ), "prior_fid must be tracked in cleared_stance_fids after tombstone"

        # Simulate disk-based resync re-adding prior_fid to _existing_keys.
        # This is what happens when load_findings() reloads unacknowledged
        # findings from disk (prior_fid was persisted, never acknowledged).
        # Force the resync to happen on the next tick by reaching the interval
        # threshold, and mock load_findings to return prior_fid as if it were
        # still on disk and unacknowledged.
        from unittest.mock import patch
        from watercooler_mcp.daemons.state import Finding as StateFinding

        mock_disk_finding = StateFinding(
            finding_id=prior_fid,
            daemon_name=daemon.name,
            severity="info",
            category="stance_advisory",
            topic="stance:planner",
            entry_id="",
            message="mock-persisted",
        )
        daemon._ticks_since_resync = 9  # will become 10 on next tick → triggers resync

        # Re-add stalled loops for re-escalation
        for i in range(3):
            thread_dir = storage.get_thread_graph_dir(graph_dir, f"stalled-{i}")
            storage.atomic_write_jsonl(
                thread_dir / "entries.jsonl",
                [
                    _entry(entry_type="Plan", index=0, entry_id=f"E{i}0"),
                    _entry(entry_type="Note", index=1, entry_id=f"E{i}1"),
                    _entry(entry_type="Note", index=2, entry_id=f"E{i}2"),
                ],
            )
            (thread_dir / "meta.json").write_text(
                (thread_dir / "meta.json").read_text()
            )

        with patch(
            "watercooler_mcp.daemons.project_coordinator.load_findings",
            return_value=[mock_disk_finding],
        ):
            findings4 = daemon.tick()

        planner4 = [
            f for f in self._stance_findings(findings4) if f.topic == "stance:planner"
        ]
        assert (
            len(planner4) >= 1
        ), "Re-escalation must work even after resync reloads prior_fid from disk"

    def test_empty_graph_clears_stance_state(self, tmp_path, monkeypatch):
        """When all thread topics disappear, stance state must be cleaned up.

        Previously, tick() returned early before _load_extras(), pruning, or
        stance emission when topics was empty — leaving _last_stance_outcome,
        _last_stance_levels, and active_signals stale from the prior tick.
        """
        import shutil

        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        graph_dir = storage.get_graph_dir(threads_dir)

        # Tick 1: create threads with stalled loops → elevated planner stance
        for i in range(3):
            _write_graph_thread(
                threads_dir,
                f"stalled-{i}",
                entries=[
                    _entry(entry_type="Plan", index=0, entry_id=f"E{i}0"),
                    _entry(entry_type="Note", index=1, entry_id=f"E{i}1"),
                    _entry(entry_type="Note", index=2, entry_id=f"E{i}2"),
                ],
            )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings1 = daemon.tick()
        planner1 = [
            f for f in self._stance_findings(findings1) if f.topic == "stance:planner"
        ]
        assert len(planner1) >= 1, "tick 1 should produce planner advisory"
        assert (
            daemon._extras.active_signals
        ), "active_signals should be populated after tick 1"

        # Remove all thread directories from the graph — topics list will be empty
        for i in range(3):
            thread_dir = storage.get_thread_graph_dir(graph_dir, f"stalled-{i}")
            shutil.rmtree(str(thread_dir))

        # Tick 2: no topics — must still prune state and emit tombstones
        findings2 = daemon.tick()

        # active_signals must be cleared (pruned for all removed topics)
        assert daemon._extras.active_signals == {}, (
            "active_signals must be empty after all topics removed, "
            f"got: {daemon._extras.active_signals}"
        )

        # _last_stance_levels must show L0 for all roles after the empty-graph tick.
        # (The previous code returned before _emit_stance_advisories, leaving stale
        # levels from tick 1 in _last_stance_levels.)
        assert (
            daemon._last_stance_levels
        ), "_last_stance_levels must be populated after empty-graph tick"
        for role, level in daemon._last_stance_levels.items():
            assert (
                level == 0
            ), f"_last_stance_levels[{role!r}] must be 0 after all topics removed, got {level}"

        # A tombstone (L0 advisory) should have been emitted for planner
        tombstones = [
            f
            for f in self._stance_findings(findings2)
            if f.topic == "stance:planner"
            and f.details.get("advisory", {}).get("level") == 0
        ]
        assert (
            len(tombstones) >= 1
        ), "Empty-graph tick must emit tombstone for previously-elevated planner stance"

    def test_tombstone_resync_does_not_suppress_second_tombstone(
        self, tmp_path, monkeypatch
    ):
        """L1→L0→L1→(resync re-adds tombstone fid)→L0 — second tombstone must still emit.

        The tombstone fid (dedup_signature="cleared") is discarded from _existing_keys
        on L0→L1 escalation. If it is NOT added to cleared_stance_fids at that point,
        the dedup resync will reload it from disk and re-add it to _existing_keys,
        blocking the second L1→L0 tombstone emission.
        """
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        from datetime import datetime, timezone
        from unittest.mock import patch
        from watercooler_mcp.daemons.state import Finding as StateFinding

        now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        threads_dir = tmp_path / "threads"
        graph_dir = storage.get_graph_dir(threads_dir)

        # Tick 1: L0 (no stalled threads)
        _write_graph_thread(
            threads_dir,
            "base-topic",
            entries=[
                _entry(entry_type="Note", index=0, entry_id="E00", timestamp=now_iso)
            ],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        daemon.tick()

        # Tick 2: L1+ — add stalled threads
        for i in range(3):
            _write_graph_thread(
                threads_dir,
                f"stalled-{i}",
                entries=[
                    _entry(entry_type="Plan", index=0, entry_id=f"E{i}0"),
                    _entry(entry_type="Note", index=1, entry_id=f"E{i}1"),
                    _entry(entry_type="Note", index=2, entry_id=f"E{i}2"),
                ],
            )
        findings2 = daemon.tick()
        planner2 = [
            f for f in self._stance_findings(findings2) if f.topic == "stance:planner"
        ]
        assert len(planner2) >= 1, "tick 2 must produce planner L1 advisory"

        # Capture tombstone fid (dedup_signature="cleared") for the planner role
        from watercooler_mcp.daemons.project_coordinator import build_finding_id

        tombstone_fid = build_finding_id(
            scope_id=daemon._scope_id,
            daemon_name=daemon.name,
            topic="stance:planner",
            category="stance_advisory",
            entry_id="",
            dedup_signature="cleared",
        )

        # Tick 3: L0 — clear stalled threads → tombstone emitted
        for i in range(3):
            thread_dir = storage.get_thread_graph_dir(graph_dir, f"stalled-{i}")
            storage.atomic_write_jsonl(
                thread_dir / "entries.jsonl",
                [
                    _entry(
                        entry_type="Decision",
                        index=0,
                        entry_id=f"E{i}0",
                        timestamp=now_iso,
                    )
                ],
            )
            (thread_dir / "meta.json").write_text(
                (thread_dir / "meta.json").read_text()
            )
        findings3 = daemon.tick()
        tombstones3 = [
            f
            for f in self._stance_findings(findings3)
            if f.topic == "stance:planner"
            and f.details.get("advisory", {}).get("level") == 0
        ]
        assert len(tombstones3) >= 1, "tick 3 must emit planner tombstone (L0)"

        # Tick 4: L1+ again — re-escalate; tombstone_fid must land in cleared_stance_fids
        for i in range(3):
            thread_dir = storage.get_thread_graph_dir(graph_dir, f"stalled-{i}")
            storage.atomic_write_jsonl(
                thread_dir / "entries.jsonl",
                [
                    _entry(entry_type="Plan", index=0, entry_id=f"E{i}0"),
                    _entry(entry_type="Note", index=1, entry_id=f"E{i}1"),
                    _entry(entry_type="Note", index=2, entry_id=f"E{i}2"),
                ],
            )
            (thread_dir / "meta.json").write_text(
                (thread_dir / "meta.json").read_text()
            )
        findings4 = daemon.tick()
        planner4 = [
            f for f in self._stance_findings(findings4) if f.topic == "stance:planner"
        ]
        assert len(planner4) >= 1, "tick 4 must re-escalate planner advisory"
        assert (
            tombstone_fid in daemon._extras.cleared_stance_fids
        ), "tombstone_fid must be in cleared_stance_fids after L0→L1 escalation"

        # Tick 5: L0 again — simulate resync re-adding tombstone_fid from disk,
        # then clear stalled threads. Second tombstone must still emit.
        mock_tombstone_on_disk = StateFinding(
            finding_id=tombstone_fid,
            daemon_name=daemon.name,
            severity="info",
            category="stance_advisory",
            topic="stance:planner",
            entry_id="",
            message="mock-persisted-tombstone",
        )
        daemon._ticks_since_resync = 9  # triggers resync on next tick

        for i in range(3):
            thread_dir = storage.get_thread_graph_dir(graph_dir, f"stalled-{i}")
            storage.atomic_write_jsonl(
                thread_dir / "entries.jsonl",
                [
                    _entry(
                        entry_type="Decision",
                        index=0,
                        entry_id=f"E{i}0",
                        timestamp=now_iso,
                    )
                ],
            )
            (thread_dir / "meta.json").write_text(
                (thread_dir / "meta.json").read_text()
            )

        with patch(
            "watercooler_mcp.daemons.project_coordinator.load_findings",
            return_value=[mock_tombstone_on_disk],
        ):
            findings5 = daemon.tick()

        tombstones5 = [
            f
            for f in self._stance_findings(findings5)
            if f.topic == "stance:planner"
            and f.details.get("advisory", {}).get("level") == 0
        ]
        assert (
            len(tombstones5) >= 1
        ), "Second tombstone must emit even after resync re-adds tombstone_fid from disk"


# ---------------------------------------------------------------------------
# P2.1: corpus signal transport (coordinator → stance)
# ---------------------------------------------------------------------------


class TestCorpusSignalTransport:
    """P2.1: aware_new_contributor propagates from coordinator detection
    through CoordinatorExtras.corpus_signal_inputs into stance coord_counts."""

    def test_corpus_signal_inputs_populated_by_new_contributor_branch(
        self,
        tmp_path,
        monkeypatch,
    ):
        """2.1.a: two distinct new contributors → count == 2."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir,
            "topic-a",
            entries=[_entry(agent="AliceNew", index=0, entry_id="EA0")],
        )
        _write_graph_thread(
            threads_dir,
            "topic-b",
            entries=[_entry(agent="BobNew", index=0, entry_id="EB0")],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        daemon.tick()
        assert daemon._extras.corpus_signal_inputs.get("aware_new_contributor") == 2

    def test_corpus_signal_inputs_cleared_each_tick(self, tmp_path, monkeypatch):
        """2.1.b: tick 1 has new contributors, tick 2 has none → empty dict."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir,
            "topic-a",
            entries=[_entry(agent="AliceNew", index=0, entry_id="EA0")],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        daemon.tick()
        assert daemon._extras.corpus_signal_inputs.get("aware_new_contributor") == 1
        # Tick 2: no new threads, no new contributors
        daemon.tick()
        assert "aware_new_contributor" not in daemon._extras.corpus_signal_inputs

    def test_emit_stance_advisories_merges_corpus_counts(
        self,
        tmp_path,
        monkeypatch,
    ):
        """2.1.c: corpus counts propagate into stance signals
        (coordinator_new_contributor_count) and elevate planner."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir,
            "topic-a",
            entries=[_entry(agent="AliceNew", index=0, entry_id="EA0")],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings = daemon.tick()
        stance = [f for f in findings if f.category == "stance_advisory"]
        planner = [f for f in stance if f.topic == "stance:planner"]
        assert len(planner) >= 1
        adv = planner[0].details["advisory"]
        assert adv["signal_values"]["coordinator_new_contributor_count"] >= 1
        assert "coordinator_new_contributor_count" in adv["triggered_signals"]
        assert adv["level"] >= 1
        # Action routed to aware_new_contributor category
        nc_actions = [
            a
            for a in adv["actions"]
            if a.get("arguments", {}).get("category") == "aware_new_contributor"
        ]
        assert len(nc_actions) == 1


# ---------------------------------------------------------------------------
# v1B follow-on: coordinator_lead tests
# ---------------------------------------------------------------------------


def _write_stalled_open_loop_thread(
    threads_dir: Path, topic: str = "stalled-topic"
) -> None:
    """Write a thread that produces exactly one stalled_open_loop finding.

    Uses the same default (2024-04-01) timestamps as _entry() — well past
    OPEN_LOOP_MIN_STALE_DAYS when the test suite runs in 2026+. No burst,
    no role concentration, no dropout.
    """
    _write_graph_thread(
        threads_dir,
        topic,
        entries=[
            _entry(entry_type="Note", index=0, entry_id=f"{topic}-E01"),
            _entry(entry_type="Plan", index=1, entry_id=f"{topic}-E02"),
            _entry(entry_type="Note", index=2, entry_id=f"{topic}-E03"),
        ],
    )


class TestCoordinatorLeads:
    """v1B follow-on: coordinator_lead findings layered onto v1A detections."""

    def test_coordinator_lead_not_in_active_signals(self, tmp_path, monkeypatch):
        """coordinator_lead must never appear in active_signals (pollutes stance)."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_stalled_open_loop_thread(threads_dir)
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        daemon.tick()
        for topic, sig_entry in daemon._extras.active_signals.items():
            assert (
                "coordinator_lead" not in sig_entry.categories
            ), f"coordinator_lead leaked into active_signals for topic '{topic}'"

    def test_coordinator_lead_emitted_for_stalled_open_loop(
        self, tmp_path, monkeypatch
    ):
        """Base case: stalled_open_loop triggers exactly one coordinator_lead."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_stalled_open_loop_thread(threads_dir)
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings = daemon.tick()
        leads = [f for f in findings if f.category == "coordinator_lead"]
        assert len(leads) == 1
        lead = leads[0]
        assert lead.topic == "stalled-topic"
        # Lead body carries the nested CoordinatorLead as a dict
        payload = lead.details["lead"]
        assert payload["source_category"] == "stalled_open_loop"
        assert payload["source_topic"] == "stalled-topic"
        assert payload["suggested_action"]["tool"] == "watercooler_read_thread"
        # Dedup signature is per-finding, prefixed with coordinator_lead|
        assert lead.finding_id  # deterministic, built from signature
        assert daemon._last_tick_leads == 1

    def test_coordinator_lead_skipped_when_thread_unchanged(
        self, tmp_path, monkeypatch
    ):
        """Unchanged thread short-circuits at is_thread_changed()."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_stalled_open_loop_thread(threads_dir)
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)

        tick1 = daemon.tick()
        assert len([f for f in tick1 if f.category == "coordinator_lead"]) == 1

        tick2 = daemon.tick()  # same data, no thread change → detectors skipped
        assert [f for f in tick2 if f.category == "coordinator_lead"] == []

    def test_coordinator_lead_deduped_when_thread_mutates(self, tmp_path, monkeypatch):
        """Mutated thread re-runs detectors; dedup silences the re-minted lead."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_stalled_open_loop_thread(threads_dir)
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)

        tick1 = daemon.tick()
        assert len([f for f in tick1 if f.category == "coordinator_lead"]) == 1

        # Append a Note entry — flips is_thread_changed() to True, but keeps
        # stalled_open_loop's dedup_signature stable (it depends on topic only).
        graph_dir = storage.get_graph_dir(threads_dir)
        thread_dir = storage.get_thread_graph_dir(graph_dir, "stalled-topic")
        existing = [
            _entry(entry_type="Note", index=0, entry_id="stalled-topic-E01"),
            _entry(entry_type="Plan", index=1, entry_id="stalled-topic-E02"),
            _entry(entry_type="Note", index=2, entry_id="stalled-topic-E03"),
            _entry(entry_type="Note", index=3, entry_id="stalled-topic-E04"),
        ]
        storage.atomic_write_jsonl(thread_dir / "entries.jsonl", existing)
        meta_file = thread_dir / "meta.json"
        meta_file.write_text(meta_file.read_text())  # bump mtime

        tick2 = daemon.tick()
        assert [f for f in tick2 if f.category == "coordinator_lead"] == []

    def test_leads_do_not_starve_later_thread_v1a_findings(self, tmp_path, monkeypatch):
        """Two-phase cap ordering: v1A from all threads emits before any lead."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        for i in range(3):
            _write_stalled_open_loop_thread(threads_dir, topic=f"thread-{i}")
        daemon = _make_daemon(
            tmp_path,
            threads_dir=threads_dir,
            max_findings_per_run=3,
            leads_enabled=True,
            stance_enabled=False,
        )
        findings = daemon.tick()
        stalled = [f for f in findings if f.category == "stalled_open_loop"]
        leads = [f for f in findings if f.category == "coordinator_lead"]
        assert len(stalled) == 3, (
            "expected 3 stalled_open_loop findings; fixture may have "
            "triggered additional v1A categories"
        )
        assert leads == [], "leads starved v1A findings — two-phase cap ordering broken"

    def test_leads_drain_after_corpus_findings(self, tmp_path, monkeypatch):
        """Corpus findings (aware_new_contributor) take priority over leads."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_stalled_open_loop_thread(threads_dir, topic="stalled-a")
        # A fresh contributor on a separate thread fires aware_new_contributor
        _write_graph_thread(
            threads_dir,
            "newbie-thread",
            entries=[_entry(agent="Eve", index=0, entry_id="newbie-E01")],
        )
        daemon = _make_daemon(
            tmp_path,
            threads_dir=threads_dir,
            max_findings_per_run=2,
            leads_enabled=True,
            stance_enabled=False,
        )
        findings = daemon.tick()
        categories = {f.category for f in findings}
        assert "stalled_open_loop" in categories
        assert "aware_new_contributor" in categories
        assert "coordinator_lead" not in categories

    def test_leads_disabled_via_config(self, tmp_path, monkeypatch):
        """leads_enabled=False suppresses all coordinator_lead findings."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_stalled_open_loop_thread(threads_dir)
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, leads_enabled=False)
        findings = daemon.tick()
        assert all(f.category != "coordinator_lead" for f in findings)
        assert daemon._last_tick_leads == 0

    def test_status_summary_exposes_leads_fields(self, tmp_path, monkeypatch):
        """status_summary() includes leads_enabled + last_tick_leads."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_stalled_open_loop_thread(threads_dir)
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        daemon.tick()
        summary = daemon.status_summary()
        assert summary["leads_enabled"] is True
        assert summary["last_tick_leads"] == 1

    def test_lead_remints_after_cap_lift(self, tmp_path, monkeypatch):
        """Dropped leads re-mint on the next tick when the cap is lifted.

        Regression for a bug where threads whose leads were dropped in Phase C
        had their checkpoint advanced anyway, so the next tick's
        is_thread_changed() gate skipped them and the lead was lost forever
        unless the thread physically mutated. Fix: defer checkpoint commit
        until after Phase C and hold back any topic whose lead didn't land.
        """
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_stalled_open_loop_thread(threads_dir, topic="stalled-a")
        _write_stalled_open_loop_thread(threads_dir, topic="stalled-b")
        daemon = _make_daemon(
            tmp_path,
            threads_dir=threads_dir,
            max_findings_per_run=2,
            leads_enabled=True,
            stance_enabled=False,
        )

        # Tick 1: cap=2 fits both v1A findings; leads dropped in Phase C.
        tick1 = daemon.tick()
        stalled1 = [f for f in tick1 if f.category == "stalled_open_loop"]
        leads1 = [f for f in tick1 if f.category == "coordinator_lead"]
        assert len(stalled1) == 2
        assert leads1 == [], "expected Phase C to drop both leads at cap=2"
        assert daemon._last_tick_leads == 0

        # Tick 2: lift the cap without mutating threads. The checkpoint for
        # each affected topic must have been held back so is_thread_changed()
        # still returns True on the rescan — without that, the unchanged-thread
        # gate would skip both threads and the leads would be lost forever.
        daemon._config = ProjectCoordinatorConfig(
            max_findings_per_run=10,
            leads_enabled=True,
            stance_enabled=False,
        )
        tick2 = daemon.tick()
        stalled2 = [f for f in tick2 if f.category == "stalled_open_loop"]
        leads2 = [f for f in tick2 if f.category == "coordinator_lead"]
        # v1A stalled is dedup-blocked (fids already in _existing_keys from tick 1).
        assert stalled2 == []
        # Both leads re-mint and land on the rescan.
        lead_topics = {f.topic for f in leads2}
        assert lead_topics == {
            "stalled-a",
            "stalled-b",
        }, f"leads failed to re-mint after cap lift; got {lead_topics}"
        assert daemon._last_tick_leads == 2


# ---------------------------------------------------------------------------
# Phase 3d-1 — connect_role_complement lead wrapping (tests 20, 21)
# ---------------------------------------------------------------------------


class TestRoleComplementLeadWrapping:
    """Tests 20-21: generate_leads_for_thread wraps connect_role_complement correctly."""

    def _make_rc_finding(
        self,
        *,
        topic: str = "thread-a",
        missing_role: str = "tester",
        related_topic: str = "thread-b",
        role_count: int = 3,
        relation_evidence: list[dict] | None = None,
    ):
        from watercooler.project_coordinator_lib import CoordinatorFinding

        details: dict = {
            "missing_role": missing_role,
            "related_thread_topic": related_topic,
            "related_thread_role_entry_count": role_count,
        }
        if relation_evidence is not None:
            details["relation_evidence"] = relation_evidence

        return CoordinatorFinding(
            category="connect_role_complement",
            topic=topic,
            severity="info",
            message=f"Thread '{topic}' missing {missing_role}",
            details=details,
            dedup_signature=f"{topic}|{missing_role}|{related_topic}",
        )

    def test_20_generate_leads_wraps_connect_role_complement_with_relation_evidence(self):
        """Test 20: coordinator_lead inherits relation_evidence from connect_role_complement."""
        from watercooler.project_coordinator_lib import generate_leads_for_thread

        evidence = [{"tier": "xref", "source_topic": "thread-a", "target_topic": "thread-b"}]
        finding = self._make_rc_finding(relation_evidence=evidence)

        leads = generate_leads_for_thread([finding])

        assert len(leads) == 1
        lead_finding = leads[0]
        assert lead_finding.category == "coordinator_lead"
        assert lead_finding.topic == "thread-a"
        # relation_evidence propagated at the details level
        assert lead_finding.details.get("relation_evidence") == evidence
        # nested lead payload has the right source_category
        payload = lead_finding.details["lead"]
        assert payload["source_category"] == "connect_role_complement"
        assert payload["source_topic"] == "thread-a"
        # dedup signature prefixed correctly
        assert lead_finding.dedup_signature == "coordinator_lead|thread-a|tester|thread-b"

    def test_21_suggested_action_is_read_only(self):
        """Test 21: connect_role_complement lead's suggested_action uses watercooler_read_thread."""
        from watercooler.project_coordinator_lib import generate_leads_for_thread

        finding = self._make_rc_finding(
            missing_role="critic",
            related_topic="thread-b",
            relation_evidence=[{"tier": "pair_tag", "tag": "pair:feature-x"}],
        )

        leads = generate_leads_for_thread([finding])
        assert len(leads) == 1

        suggested = leads[0].details["lead"]["suggested_action"]
        assert suggested["tool"] == "watercooler_read_thread"
        assert suggested["phase"] == "pre"
        assert suggested["arguments"]["topic"] == "thread-b"
        assert suggested["arguments"]["summary_only"] is True


# ---------------------------------------------------------------------------
# Fix 1 regression: Phase B2 Tier 3 requires fresh pulse_block
# ---------------------------------------------------------------------------


class TestRoleComplementTier3PulseBlockGate:
    """Fix 1 regression: rc_risk_clusters built from pulse_block, not raw recommendations."""

    def _setup_rc_daemon(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_graph_thread(
            threads_dir,
            "thread-alpha",
            status="OPEN",
            entries=[
                _entry(entry_id="alpha-E01", role="planner", index=0),
                _entry(entry_id="alpha-E02", role="planner", index=1),
            ],
        )
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir, stance_enabled=False)
        daemon._config = daemon._config.__class__(
            role_complement_enabled=True,
            role_complement_monitored_roles=["tester"],
            leads_enabled=False,
            stance_enabled=False,
        )
        return daemon

    def test_risk_clusters_none_when_pulse_block_absent(self, tmp_path, monkeypatch):
        """When analysis_result has no pulse_block key, rc_risk_clusters must be None.

        Fix 1 repro: before the fix, raw recommendations were used to build
        rc_risk_clusters even when pulse_block was absent, enabling false Tier 3 pairs.
        """
        from watercooler_mcp.daemons import project_coordinator as pc_module

        daemon = self._setup_rc_daemon(tmp_path, monkeypatch)

        captured: dict = {}

        # risk_clusters is the 6th positional arg (index 5) in detect_role_complement
        def fake_detect(all_active_entries, all_active_tags, threads_dir,
                        entry_topic_index, analysis_by_topic, risk_clusters,
                        **kwargs):
            captured["risk_clusters"] = risk_clusters
            return []

        monkeypatch.setattr(pc_module, "detect_role_complement", fake_detect)
        monkeypatch.setattr(daemon, "_load_analysis_context", lambda: {
            "generated_at": "2099-01-01T00:00:00Z",
            "window_threads": [],
            "recommendations": [
                {
                    "rule_id": "R01",
                    "text": "pair",
                    "priority": "actionable",
                    "confidence": 0.9,
                    "affected_threads": ["thread-alpha", "thread-beta"],
                    "affected_contributors": [],
                }
            ],
            # pulse_block intentionally absent
        })

        daemon.tick()
        assert captured.get("risk_clusters") is None, (
            "rc_risk_clusters must be None when pulse_block key is absent; "
            f"got {captured.get('risk_clusters')!r}"
        )

    def test_risk_clusters_none_when_pulse_block_degraded(self, tmp_path, monkeypatch):
        """When pulse_block has no pulse_block_version, rc_risk_clusters must be None."""
        from watercooler_mcp.daemons import project_coordinator as pc_module

        daemon = self._setup_rc_daemon(tmp_path, monkeypatch)

        captured: dict = {}

        def fake_detect(all_active_entries, all_active_tags, threads_dir,
                        entry_topic_index, analysis_by_topic, risk_clusters,
                        **kwargs):
            captured["risk_clusters"] = risk_clusters
            return []

        monkeypatch.setattr(pc_module, "detect_role_complement", fake_detect)
        monkeypatch.setattr(daemon, "_load_analysis_context", lambda: {
            "generated_at": "2099-01-01T00:00:00Z",
            "window_threads": [],
            "recommendations": [],
            "pulse_block": {
                # pulse_block_version intentionally absent → degraded/schema-incompatible
                "coordination_risks": [
                    {"rule_id": "R01", "text": "pair",
                     "affected_threads": ["thread-alpha", "thread-beta"]}
                ]
            },
        })

        daemon.tick()
        assert captured.get("risk_clusters") is None, (
            "rc_risk_clusters must be None when pulse_block lacks pulse_block_version; "
            f"got {captured.get('risk_clusters')!r}"
        )

    def test_risk_clusters_none_when_pulse_block_incompatible_version(
        self, tmp_path, monkeypatch
    ):
        """When pulse_block_version is non-1.x (e.g. '2.0'), rc_risk_clusters must be None.

        Mirrors pulse_report_lib.py which rejects non-1.* versions as degraded.
        """
        from watercooler_mcp.daemons import project_coordinator as pc_module

        daemon = self._setup_rc_daemon(tmp_path, monkeypatch)

        captured: dict = {}

        def fake_detect(all_active_entries, all_active_tags, threads_dir,
                        entry_topic_index, analysis_by_topic, risk_clusters,
                        **kwargs):
            captured["risk_clusters"] = risk_clusters
            return []

        monkeypatch.setattr(pc_module, "detect_role_complement", fake_detect)
        monkeypatch.setattr(daemon, "_load_analysis_context", lambda: {
            "generated_at": "2099-01-01T00:00:00Z",
            "window_threads": [],
            "recommendations": [],
            "pulse_block": {
                "pulse_block_version": "2.0",  # incompatible — not a 1.x version
                "coordination_risks": [
                    {"rule_id": "R01", "text": "pair",
                     "affected_threads": ["thread-alpha", "thread-beta"]}
                ],
            },
        })

        daemon.tick()
        assert captured.get("risk_clusters") is None, (
            "rc_risk_clusters must be None when pulse_block_version is non-1.x; "
            f"got {captured.get('risk_clusters')!r}"
        )

    def test_risk_clusters_populated_from_pulse_block_coordination_risks(
        self, tmp_path, monkeypatch
    ):
        """When pulse_block is valid, rc_risk_clusters reflects coordination_risks."""
        from watercooler_mcp.daemons import project_coordinator as pc_module

        daemon = self._setup_rc_daemon(tmp_path, monkeypatch)

        captured: dict = {}

        def fake_detect(all_active_entries, all_active_tags, threads_dir,
                        entry_topic_index, analysis_by_topic, risk_clusters,
                        **kwargs):
            captured["risk_clusters"] = risk_clusters
            return []

        monkeypatch.setattr(pc_module, "detect_role_complement", fake_detect)
        monkeypatch.setattr(daemon, "_load_analysis_context", lambda: {
            "generated_at": "2099-01-01T00:00:00Z",
            "window_threads": [],
            "recommendations": [],
            "pulse_block": {
                "pulse_block_version": "1.0",
                "coordination_risks": [
                    {
                        "rule_id": "R01",
                        "text": "co-affected pair",
                        "affected_threads": ["thread-alpha", "thread-beta"],
                    }
                ],
            },
        })

        daemon.tick()
        clusters = captured.get("risk_clusters")
        assert clusters is not None, "rc_risk_clusters should be populated from coordination_risks"
        assert len(clusters) == 1
        rule_id, risk_text, topics = clusters[0]
        assert rule_id == "R01"
        assert risk_text == "co-affected pair"
        assert topics == frozenset({"thread-alpha", "thread-beta"})

    def test_risk_clusters_include_all_topics_when_affected_threads_exceeds_50(
        self, tmp_path, monkeypatch
    ):
        """Oversized affected_threads list must not be silently truncated.

        Regression: a [:50] cap on affected_threads caused Tier 3 pairs where
        both topics fell past position 50 to be silently dropped.
        """
        from watercooler_mcp.daemons import project_coordinator as pc_module

        daemon = self._setup_rc_daemon(tmp_path, monkeypatch)

        captured: dict = {}

        def fake_detect(all_active_entries, all_active_tags, threads_dir,
                        entry_topic_index, analysis_by_topic, risk_clusters,
                        **kwargs):
            captured["risk_clusters"] = risk_clusters
            return []

        monkeypatch.setattr(pc_module, "detect_role_complement", fake_detect)

        # Build a risk with 60 affected threads; topics at index 58 and 59 must survive
        affected = [f"t{i:02d}" for i in range(60)]
        monkeypatch.setattr(daemon, "_load_analysis_context", lambda: {
            "generated_at": "2099-01-01T00:00:00Z",
            "window_threads": [],
            "recommendations": [],
            "pulse_block": {
                "pulse_block_version": "1.0",
                "coordination_risks": [
                    {
                        "rule_id": "R-LARGE",
                        "text": "large cluster",
                        "affected_threads": affected,
                    }
                ],
            },
        })

        daemon.tick()
        clusters = captured.get("risk_clusters")
        assert clusters is not None
        assert len(clusters) == 1
        _, _, topics = clusters[0]
        assert "t58" in topics, "topic at index 58 must not be truncated"
        assert "t59" in topics, "topic at index 59 must not be truncated"
        assert len(topics) == 60


# ---------------------------------------------------------------------------
# Phase 2 — t2_context enrichment (tests 6, 7, 8, 8b)
# ---------------------------------------------------------------------------


class TestCoordinatorLeadsT2Context:
    """Phase 2 — _load_analysis_context wired into tick(); t2_context metric."""

    def test_tick_loads_analysis_context(self, tmp_path, monkeypatch):
        """Test 6: _load_analysis_context returns result → lead has non-None t2_context."""
        from unittest.mock import MagicMock

        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_stalled_open_loop_thread(threads_dir, topic="stalled-topic")
        daemon = _make_daemon(
            tmp_path, threads_dir=threads_dir, leads_enabled=True, stance_enabled=False
        )

        analysis_result = {
            "window_threads": [
                {
                    "topic": "stalled-topic",
                    "days_since_last": 20,
                    "workflow_shape": {"id": "w1", "name": "linear", "confidence": 0.8},
                    "has_decision": False,
                    "has_closure": False,
                    "stalled": True,
                    "entry_count_total": 10,
                }
            ],
            "recommendations": [],
        }
        # Patch _load_analysis_context to return the result without needing real daemons.
        daemon._load_analysis_context = MagicMock(return_value=analysis_result)

        findings = daemon.tick()
        leads = [f for f in findings if f.category == "coordinator_lead"]
        assert len(leads) == 1
        t2 = leads[0].details["lead"].get("t2_context")
        assert t2 is not None
        assert t2["schema_version"] == 2
        assert t2["days_since_last"] == 20
        assert t2["analysis_stalled"] is True
        assert "stalled" not in t2

    def test_tick_t2_context_graceful_degradation(self, tmp_path, monkeypatch):
        """Test 7: _load_analysis_context returns None → tick completes, t2_context=None."""
        from unittest.mock import MagicMock

        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_stalled_open_loop_thread(threads_dir, topic="stalled-topic")
        daemon = _make_daemon(
            tmp_path, threads_dir=threads_dir, leads_enabled=True, stance_enabled=False
        )
        daemon._load_analysis_context = MagicMock(return_value=None)

        findings = daemon.tick()
        leads = [f for f in findings if f.category == "coordinator_lead"]
        assert len(leads) == 1
        t2 = leads[0].details["lead"].get("t2_context")
        assert t2 is None
        assert daemon._last_tick_leads == 1

    def test_status_summary_exposes_t2_enriched(self, tmp_path, monkeypatch):
        """Test 8: last_tick_t2_enriched counts leads with non-None t2_context only."""
        from unittest.mock import MagicMock

        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        # topic-a → in analysis_by_topic → will have t2_context
        _write_stalled_open_loop_thread(threads_dir, topic="topic-a")
        # topic-b → NOT in analysis_by_topic → t2_context=None
        _write_stalled_open_loop_thread(threads_dir, topic="topic-b")

        daemon = _make_daemon(
            tmp_path, threads_dir=threads_dir, leads_enabled=True, stance_enabled=False
        )
        analysis_result = {
            "window_threads": [
                {
                    "topic": "topic-a",
                    "days_since_last": 15,
                    "workflow_shape": None,
                    "has_decision": False,
                    "has_closure": False,
                    "stalled": False,
                    "entry_count_total": 5,
                }
            ],
            "recommendations": [],
        }
        daemon._load_analysis_context = MagicMock(return_value=analysis_result)

        findings = daemon.tick()
        leads = [f for f in findings if f.category == "coordinator_lead"]
        assert len(leads) == 2, f"expected 2 leads, got {len(leads)}"

        summary = daemon.status_summary()
        assert summary["last_tick_leads"] == 2
        assert summary["last_tick_t2_enriched"] == 1  # only topic-a has t2_context

    def test_last_tick_t2_enriched_resets_between_ticks(self, tmp_path, monkeypatch):
        """Test 8b: _last_tick_t2_enriched resets to 0 at tick start (current-tick metric)."""
        from unittest.mock import MagicMock

        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        # Write 3 threads for tick 1
        for i in range(1, 4):
            _write_stalled_open_loop_thread(threads_dir, topic=f"topic-{i}")

        daemon = _make_daemon(
            tmp_path, threads_dir=threads_dir, leads_enabled=True, stance_enabled=False
        )

        # Tick 1: all 3 topics in analysis context → 3 enriched
        analysis_result_tick1 = {
            "window_threads": [
                {"topic": f"topic-{i}", "days_since_last": 15, "entry_count_total": 5}
                for i in range(1, 4)
            ],
            "recommendations": [],
        }
        daemon._load_analysis_context = MagicMock(return_value=analysis_result_tick1)
        daemon.tick()
        assert daemon._last_tick_t2_enriched == 3

        # Tick 2: only 1 topic in analysis context (topics 2 and 3 will be deduped anyway).
        # Because all leads were emitted on tick 1, tick 2's topics are deduped by
        # _existing_keys and no new leads land → _last_tick_t2_enriched resets to 0.
        analysis_result_tick2 = {
            "window_threads": [
                {"topic": "topic-1", "days_since_last": 16, "entry_count_total": 5}
            ],
            "recommendations": [],
        }
        daemon._load_analysis_context = MagicMock(return_value=analysis_result_tick2)
        daemon.tick()
        # All leads are deduped → 0 new leads emitted → counter is 0 (reset, not 3)
        assert daemon._last_tick_t2_enriched == 0
        assert daemon.status_summary()["last_tick_t2_enriched"] == 0


class TestRescanCoherence:
    """Phase 3a-2: rescan loop emits leads and defers state commits until after drain."""

    def _backdate_active_signals(
        self,
        daemon: "ProjectCoordinatorDaemon",
        topic: str,
        age_seconds: float = 86401.0,
    ) -> float:
        """Backdate a topic's last_evaluated_at so the rescan loop picks it up."""
        daemon._load_extras()
        past = time.time() - age_seconds
        entry = daemon._extras.active_signals.get(topic)
        if entry is not None:
            daemon._extras.active_signals[topic] = ActiveSignalEntry(
                categories=entry.categories,
                last_evaluated_at=past,
            )
        daemon._save_extras()
        return past

    def test_rescan_generates_leads_for_newly_stale_threads(
        self, tmp_path, monkeypatch
    ):
        """Rescan loop emits coordinator_lead for threads with stale time-sensitive signals.

        Scenario: tick 1 scans the thread with leads disabled (no lead in _existing_keys),
        tick 2 triggers rescan path and should generate + emit the lead.
        """
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_stalled_open_loop_thread(threads_dir)

        # Tick 1: scan without leads — populates checkpoint + active_signals but no lead fid
        daemon = _make_daemon(
            tmp_path,
            threads_dir=threads_dir,
            leads_enabled=False,
            stance_enabled=False,
        )
        daemon.tick()
        assert "stalled-topic" in daemon._extras.active_signals

        # Backdate active_signals so the rescan loop triggers on tick 2
        self._backdate_active_signals(daemon, "stalled-topic")

        # Enable leads for tick 2
        daemon._config = ProjectCoordinatorConfig(
            leads_enabled=True, stance_enabled=False
        )

        # Tick 2: main loop skips (unchanged), rescan picks it up, lead is emitted
        findings2 = daemon.tick()
        leads = [f for f in findings2 if f.category == "coordinator_lead"]
        assert len(leads) == 1
        assert leads[0].topic == "stalled-topic"

    def test_rescan_leads_are_drained_after_rescan_loop(self, tmp_path, monkeypatch):
        """Rescan leads land in findings and advance active_signals when emitted.

        After a successful rescan drain, the topic's active_signals entry must
        be updated to tick_time — confirming the buffered state was committed.
        """
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_stalled_open_loop_thread(threads_dir)

        daemon = _make_daemon(
            tmp_path,
            threads_dir=threads_dir,
            leads_enabled=False,
            stance_enabled=False,
        )
        daemon.tick()

        self._backdate_active_signals(daemon, "stalled-topic")
        daemon._config = ProjectCoordinatorConfig(
            leads_enabled=True, stance_enabled=False
        )

        before = time.time()
        findings2 = daemon.tick()
        after = time.time()

        leads = [f for f in findings2 if f.category == "coordinator_lead"]
        assert len(leads) == 1

        # Signal commit: last_evaluated_at must be updated to this tick's timestamp
        updated_ts = daemon._extras.active_signals["stalled-topic"].last_evaluated_at
        assert before <= updated_ts <= after, (
            f"active_signals.last_evaluated_at ({updated_ts}) not updated to tick_time "
            f"[{before}, {after}]"
        )
        assert daemon._last_tick_leads == 1

    def test_rescan_dropped_lead_does_not_advance_signal_timestamp(
        self, tmp_path, monkeypatch
    ):
        """When the cap is full, a dropped rescan lead must NOT advance active_signals.

        If active_signals.last_evaluated_at were updated despite the cap drop,
        the thread would not be re-evaluated for another 24 hours — silently
        losing the coordination signal.
        """
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"

        # Thread A fills the cap in the main loop (will always detect open loop)
        _write_stalled_open_loop_thread(threads_dir, topic="cap-filler")
        # Thread B is the rescan candidate (unchanged for main loop)
        _write_stalled_open_loop_thread(threads_dir, topic="rescan-topic")

        # Tick 1: both threads scanned; leads disabled so no fids in _existing_keys
        daemon = _make_daemon(
            tmp_path,
            threads_dir=threads_dir,
            leads_enabled=False,
            stance_enabled=False,
            max_findings_per_run=200,
        )
        daemon.tick()

        # Backdate only "rescan-topic" so it hits the rescan path
        old_ts = self._backdate_active_signals(daemon, "rescan-topic")

        # cap-filler is also unchanged → must NOT re-scan or it'd be skipped too.
        # Set cap=1 and ensure cap-filler re-scans (touch meta so is_thread_changed=True).
        graph_dir = storage.get_graph_dir(threads_dir)
        meta = storage.get_thread_graph_dir(graph_dir, "cap-filler") / "meta.json"
        meta.touch()  # bump mtime so is_thread_changed() forces re-scan of cap-filler

        daemon._config = ProjectCoordinatorConfig(
            leads_enabled=True,
            stance_enabled=False,
            max_findings_per_run=1,  # cap=1: cap-filler's v1A finding fills it
        )

        findings2 = daemon.tick()

        # The cap is 1; cap-filler's Phase C lead fills it before the rescan drain.
        # rescan-topic's lead is therefore dropped by the rescan second drain.
        rescan_topic_leads = [
            f
            for f in findings2
            if f.category == "coordinator_lead" and f.topic == "rescan-topic"
        ]
        assert (
            rescan_topic_leads == []
        ), "rescan-topic lead must be dropped when cap is full"

        # active_signals for rescan-topic must NOT have advanced — lead was dropped
        updated_ts = daemon._extras.active_signals["rescan-topic"].last_evaluated_at
        assert updated_ts == old_ts, (
            f"active_signals.last_evaluated_at was advanced ({updated_ts}) despite "
            f"rescan lead being dropped by cap — thread will skip rescan for 24h"
        )

    def test_rescan_lead_deduped_if_already_emitted(self, tmp_path, monkeypatch):
        """When a rescan lead's fid is already in _existing_keys, it is skipped.

        The topic's active_signals must still be updated (dedup ≠ cap-drop):
        the finding already exists, so the thread state should advance.
        """
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_stalled_open_loop_thread(threads_dir)

        # Tick 1 with leads enabled → lead emitted, fid in _existing_keys
        daemon = _make_daemon(
            tmp_path,
            threads_dir=threads_dir,
            leads_enabled=True,
            stance_enabled=False,
        )
        tick1 = daemon.tick()
        assert len([f for f in tick1 if f.category == "coordinator_lead"]) == 1
        # lead fid (a hex hash) is recorded in the dedup cache
        assert len(daemon._existing_keys) >= 1

        # Backdate active_signals to trigger rescan on tick 2
        self._backdate_active_signals(daemon, "stalled-topic")

        before = time.time()
        tick2 = daemon.tick()
        after = time.time()

        # Rescan runs, generates same lead, but fid is in _existing_keys → deduped
        leads2 = [f for f in tick2 if f.category == "coordinator_lead"]
        assert leads2 == [], "deduped lead must not appear in tick 2 findings"

        # active_signals IS updated (dedup ≠ cap-drop)
        updated_ts = daemon._extras.active_signals["stalled-topic"].last_evaluated_at
        assert (
            before <= updated_ts <= after
        ), "active_signals.last_evaluated_at must be updated even when lead is deduped"
        assert (
            daemon._last_tick_leads == 0
        ), "deduped lead must not increment leads counter"

    def test_rescan_state_committed_when_leads_disabled(self, tmp_path, monkeypatch):
        """When leads_enabled=False, rescan state still advances — no drain gate to block it.

        rescan_topics_with_dropped_leads stays empty (the drain never runs),
        so the commit loop advances active_signals for every rescan topic.
        """
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        _write_stalled_open_loop_thread(threads_dir)

        # Tick 1: populate active_signals with leads disabled
        daemon = _make_daemon(
            tmp_path,
            threads_dir=threads_dir,
            leads_enabled=False,
            stance_enabled=False,
        )
        daemon.tick()
        assert "stalled-topic" in daemon._extras.active_signals

        self._backdate_active_signals(daemon, "stalled-topic")

        before = time.time()
        findings2 = daemon.tick()
        after = time.time()

        # No coordinator_lead emitted (leads disabled)
        leads = [f for f in findings2 if f.category == "coordinator_lead"]
        assert leads == []

        # active_signals IS updated — leads_enabled=False never populates
        # rescan_topics_with_dropped_leads, so state always commits.
        updated_ts = daemon._extras.active_signals["stalled-topic"].last_evaluated_at
        assert before <= updated_ts <= after, (
            f"active_signals.last_evaluated_at ({updated_ts}) not advanced when "
            f"leads_enabled=False — rescan state should always commit in this mode"
        )

    def test_rescan_re_emits_stalled_open_loop_when_target_decision_removed(
        self, tmp_path, monkeypatch
    ):
        """Gap 1 / Codex High: xref suppression must lift on 24h rescan when
        the cross-thread referent (target Decision) is mutated out-of-band.

        Scenario: source thread carries an xref to a Decision in another
        thread. Tick 1 emits ``coordinator_xref_suppression`` and caches it
        in ``active_signals``. The Decision is then deleted without touching
        the source thread's mtime/entry_count, so ``is_thread_changed(source)``
        returns False on tick 2 — the only path that can re-evaluate the
        source is the rescan loop. Before the fix, that loop gated on
        ``_TIME_SENSITIVE_CATEGORIES`` which excluded
        ``coordinator_xref_suppression``, leaving the suppression stuck.
        After the rename to ``_RESCAN_TRIGGER_CATEGORIES`` (which now
        includes the suppression category), the rescan picks it up,
        re-runs the detector, and re-emits ``stalled_open_loop`` —
        materialized via ``coordinator_lead`` on the output path.
        """
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )

        from watercooler.baseline_graph.annotations import (
            AnnotationEvent,
            append_annotation,
        )
        from watercooler.baseline_graph.storage import (
            get_graph_dir,
            get_thread_graph_dir,
        )
        from watercooler.baseline_graph.writer import (
            EntryData,
            delete_entry_node,
            upsert_entry_node,
        )

        threads_dir = tmp_path / "threads"
        _write_stalled_open_loop_thread(threads_dir, topic="source-topic")

        decision_id = "DEC-RESCAN-001"
        upsert_entry_node(
            threads_dir,
            EntryData(
                entry_id=decision_id,
                thread_topic="target-topic",
                index=0,
                agent="Alice",
                role="implementer",
                entry_type="Decision",
                title="Target decision",
                body="Decided.",
                summary="",
            ),
        )

        source_dir = get_thread_graph_dir(get_graph_dir(threads_dir), "source-topic")
        append_annotation(
            source_dir,
            AnnotationEvent(
                id="evt-xref-rescan-001",
                target_id="source-topic-E02",
                target_type="entry",
                kind="xref",
                value=decision_id,
                actor="Alice",
                timestamp="2024-04-01T12:00:00+00:00",
            ),
        )

        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)

        # Tick 1: suppression fires, no stalled_open_loop / coordinator_lead.
        tick1 = daemon.tick()
        suppression = [f for f in tick1 if f.category == "coordinator_xref_suppression"]
        stalled = [f for f in tick1 if f.category == "stalled_open_loop"]
        leads1 = [f for f in tick1 if f.category == "coordinator_lead"]
        assert len(suppression) == 1, (
            f"expected one coordinator_xref_suppression on tick 1, got "
            f"{[f.category for f in tick1]}"
        )
        assert stalled == []
        assert leads1 == []
        sig_entry = daemon._extras.active_signals["source-topic"]
        assert "coordinator_xref_suppression" in sig_entry.categories, (
            "suppression category must be cached in active_signals so the "
            "rescan loop can re-evaluate later"
        )

        # Target Decision is removed out-of-band; source thread mtime /
        # entry_count untouched, so is_thread_changed(source) stays False.
        assert delete_entry_node(threads_dir, "target-topic", decision_id)

        # Force rescan eligibility without wall-clock manipulation.
        self._backdate_active_signals(daemon, "source-topic")

        # Tick 2: main loop skips source-topic (unchanged), but the rescan
        # loop now includes coordinator_xref_suppression in its trigger set
        # and re-runs the detector. With the target Decision gone, the
        # detector returns stalled_open_loop, which generates a lead.
        tick2 = daemon.tick()
        leads2 = [
            f
            for f in tick2
            if f.category == "coordinator_lead" and f.topic == "source-topic"
        ]
        assert len(leads2) == 1, (
            f"expected stalled_open_loop lead to re-emerge on rescan, got "
            f"{[f.category for f in tick2]}"
        )
        assert leads2[0].details["lead"]["source_category"] == "stalled_open_loop"

        # active_signals must be rebuilt: suppression category gone,
        # stalled_open_loop present.
        updated = daemon._extras.active_signals["source-topic"]
        assert "coordinator_xref_suppression" not in updated.categories, (
            "stale suppression category must be dropped once the target "
            "Decision is gone"
        )
        assert "stalled_open_loop" in updated.categories


# ---------------------------------------------------------------------------
# Phase 3c-1 — graph-backed decision evidence contract
# ---------------------------------------------------------------------------


class TestGraphBackedDecisionEvidence:
    """Guardrail for the Phase 3c-1 correction.

    The coordinator must rely on graph-backed ``Decision`` entries plus xref
    traversal — not on a public per-topic accessor exposed by
    ``ExtractDecisionsDaemon``. This test fails if the coordinator grows
    a direct import from ``decision_extractor``, which would recreate the
    state-mirroring anti-pattern §3c-1 explicitly rejects.
    """

    def test_graph_backed_decision_evidence_path_does_not_require_extractor_accessor(
        self,
    ):
        """project_coordinator must not import from decision_extractor.

        Scope: the coordinator module itself. ``decision_extractor`` remains
        importable elsewhere (MCP tools, CLI, tests) — this guardrail only
        blocks the coordinator's hot path from taking a dependency on
        extractor internals.
        """
        import ast
        from pathlib import Path

        coordinator_src = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "watercooler_mcp"
            / "daemons"
            / "project_coordinator.py"
        )
        tree = ast.parse(coordinator_src.read_text(encoding="utf-8"))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if "decision_extractor" in node.module:
                    offenders.append(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if "decision_extractor" in alias.name:
                        offenders.append(alias.name)
        assert offenders == [], (
            f"project_coordinator.py imported {offenders!r}; §3c-1 requires "
            "decision evidence to come from graph-backed Decision entries, "
            "not extractor internals"
        )


# ---------------------------------------------------------------------------
# Phase 3c-2 — stance source_lead_ids provenance
# ---------------------------------------------------------------------------


def _coord_lead_finding(
    *,
    finding_id: str,
    topic: str,
    category: str,
    daemon_name: str = "project_coordinator",
):
    """Construct a coordinator_lead Finding for direct index-building tests."""
    from watercooler_mcp.daemons.state import Finding

    return Finding(
        finding_id=finding_id,
        daemon_name=daemon_name,
        severity="info",
        category="coordinator_lead",
        topic=topic,
        entry_id="",
        message=f"lead for {topic}",
        details={"lead": {"source_topic": topic, "source_category": category}},
    )


class TestSourceLeadIdsProvenance:
    """Direct unit tests for _build_active_leads_index and
    _source_lead_ids_for_advisory (Phase 3c-2 provenance)."""

    def _seed_active_signal(
        self, daemon: ProjectCoordinatorDaemon, topic: str, *categories: str
    ) -> None:
        # NOTE: do NOT call daemon._load_extras() here — it rebuilds _extras
        # from the checkpoint, wiping prior seeds. Callers must _load_extras()
        # once before seeding multiple topics.
        daemon._extras.active_signals[topic] = ActiveSignalEntry(
            categories=set(categories), last_evaluated_at=time.time()
        )

    def test_stance_advisory_source_lead_ids_empty_when_no_matching_leads(
        self, tmp_path, monkeypatch
    ):
        """Advisory triggered only by non-coordinator signals yields empty
        source_lead_ids — _STANCE_SIGNAL_TO_LEAD_CATEGORIES does not map
        volatility_ratio / risk_tag_count to any lead category."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        daemon = _make_daemon(tmp_path, threads_dir=tmp_path / "threads")
        daemon._load_extras()
        # Populate an index with a stalled_open_loop lead that IS in active_signals
        self._seed_active_signal(daemon, "any-topic", "stalled_open_loop")
        lead_index = [("fid-1", "any-topic", "stalled_open_loop")]

        # Non-coordinator signals map to nothing → empty output, no truncation
        ids, truncated = daemon._source_lead_ids_for_advisory(
            ["volatility_ratio", "risk_tag_count"], lead_index
        )
        assert ids == ()
        assert truncated is False

        # Empty triggered_signals → empty output
        ids2, truncated2 = daemon._source_lead_ids_for_advisory([], lead_index)
        assert ids2 == ()
        assert truncated2 is False

    def test_stance_advisory_source_lead_ids_capped_and_deduplicated(
        self, tmp_path, monkeypatch
    ):
        """>10 matching leads → capped at _SOURCE_LEAD_IDS_CAP with truncated=True.
        Duplicates (same finding_id across current-tick + persisted) collapse to one."""
        from watercooler.pulse_stance_lib import _SOURCE_LEAD_IDS_CAP

        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        daemon = _make_daemon(tmp_path, threads_dir=tmp_path / "threads")
        daemon._load_extras()

        # Build a lead_index with 15 unique stalled_open_loop lead ids — more
        # than the cap. Per-advisory filter keeps the first 10 in order.
        lead_index = [
            (f"fid-{i:02d}", f"topic-{i}", "stalled_open_loop") for i in range(15)
        ]
        ids, truncated = daemon._source_lead_ids_for_advisory(
            ["coordinator_stalled_open_loop_count"], lead_index
        )
        assert len(ids) == _SOURCE_LEAD_IDS_CAP
        assert truncated is True
        assert ids == tuple(f"fid-{i:02d}" for i in range(_SOURCE_LEAD_IDS_CAP))

        # Dedup path: _build_active_leads_index collapses same finding_id from
        # both current-tick and persisted sources.
        for t in ("topic-a", "topic-b"):
            self._seed_active_signal(daemon, t, "stalled_open_loop")
        current_tick = [
            _coord_lead_finding(
                finding_id="fid-a", topic="topic-a", category="stalled_open_loop"
            ),
            _coord_lead_finding(
                finding_id="fid-b", topic="topic-b", category="stalled_open_loop"
            ),
        ]
        # Persisted store returns the SAME fid-a — must not duplicate.
        mock_persisted = [
            _coord_lead_finding(
                finding_id="fid-a", topic="topic-a", category="stalled_open_loop"
            ),
            _coord_lead_finding(
                finding_id="fid-c", topic="topic-a", category="stalled_open_loop"
            ),
        ]
        from unittest.mock import patch

        with patch(
            "watercooler_mcp.daemons.project_coordinator.load_findings",
            return_value=mock_persisted,
        ):
            index = daemon._build_active_leads_index(current_tick)
        fids = [fid for fid, _t, _c in index]
        assert fids == [
            "fid-a",
            "fid-b",
            "fid-c",
        ], f"expected current-tick first, then persisted, deduped by fid; got {fids}"

    def test_stance_advisory_source_lead_ids_truncation_flag_recorded(
        self, tmp_path, monkeypatch
    ):
        """End-to-end: >10 stalled-loop topics → emitted Finding records
        ``source_lead_ids_truncated: True`` in ``details`` (not on the frozen
        StanceAdvisory)."""
        from watercooler.pulse_stance_lib import _SOURCE_LEAD_IDS_CAP

        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        threads_dir = tmp_path / "threads"
        lead_count = _SOURCE_LEAD_IDS_CAP + 3  # 13 > cap of 10
        for i in range(lead_count):
            _write_stalled_open_loop_thread(threads_dir, topic=f"stalled-{i}")
        daemon = _make_daemon(tmp_path, threads_dir=threads_dir)
        findings = daemon.tick()

        planner = [
            f
            for f in findings
            if f.category == "stance_advisory" and f.topic == "stance:planner"
        ]
        assert len(planner) == 1
        advisory = planner[0].details["advisory"]
        # StanceAdvisory is frozen — the flag lives on the Finding's details,
        # not on the advisory itself.
        assert "source_lead_ids_truncated" not in advisory
        assert planner[0].details.get("source_lead_ids_truncated") is True
        assert len(advisory["source_lead_ids"]) == _SOURCE_LEAD_IDS_CAP

    def test_source_lead_ids_excludes_topics_absent_from_active_signals(
        self, tmp_path, monkeypatch
    ):
        """A persisted lead whose source_topic is not in active_signals
        must not contribute to source_lead_ids."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        daemon = _make_daemon(tmp_path, threads_dir=tmp_path / "threads")
        daemon._load_extras()

        # Only "live-topic" is in active_signals with the matching category.
        self._seed_active_signal(daemon, "live-topic", "stalled_open_loop")
        # "stale-topic" is absent — persisted lead for it must be dropped.
        # "other-topic" is in active_signals but with a different category.
        self._seed_active_signal(daemon, "other-topic", "aware_burst")

        persisted = [
            _coord_lead_finding(
                finding_id="fid-live",
                topic="live-topic",
                category="stalled_open_loop",
            ),
            _coord_lead_finding(
                finding_id="fid-stale",
                topic="stale-topic",
                category="stalled_open_loop",
            ),
            _coord_lead_finding(
                finding_id="fid-mismatch",
                topic="other-topic",
                category="stalled_open_loop",
            ),
        ]
        from unittest.mock import patch

        with patch(
            "watercooler_mcp.daemons.project_coordinator.load_findings",
            return_value=persisted,
        ):
            index = daemon._build_active_leads_index(current_tick_findings=[])

        fids = {fid for fid, _t, _c in index}
        assert fids == {"fid-live"}, (
            f"only leads whose (topic, category) is live in active_signals "
            f"should appear; got {fids}"
        )

    def test_source_lead_ids_filtered_per_advisory_triggered_signals(
        self, tmp_path, monkeypatch
    ):
        """Each advisory's source_lead_ids is scoped to its own triggered_signals
        via _STANCE_SIGNAL_TO_LEAD_CATEGORIES — no cross-role provenance bleed."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        daemon = _make_daemon(tmp_path, threads_dir=tmp_path / "threads")
        daemon._load_extras()

        # Mixed-category index: one lead per coordinator category.
        lead_index = [
            ("fid-stalled", "t-stalled", "stalled_open_loop"),
            ("fid-rc", "t-rc", "aware_role_concentration"),
            ("fid-burst", "t-burst", "aware_burst"),
            ("fid-drop", "t-drop", "stalled_dropout"),
            ("fid-new", "t-new", "aware_new_contributor"),
        ]

        # Planner triggered by stalled-loops only → stalled_open_loop lead only
        ids, _ = daemon._source_lead_ids_for_advisory(
            ["coordinator_stalled_open_loop_count"], lead_index
        )
        assert ids == ("fid-stalled",)

        # Tester triggered by burst only → burst lead only (NOT stalled)
        ids, _ = daemon._source_lead_ids_for_advisory(
            ["coordinator_burst_count"], lead_index
        )
        assert ids == ("fid-burst",)

        # Planner triggered by stalled + role_concentration → those two only
        ids, _ = daemon._source_lead_ids_for_advisory(
            [
                "coordinator_stalled_open_loop_count",
                "coordinator_role_concentration_count",
            ],
            lead_index,
        )
        assert set(ids) == {"fid-stalled", "fid-rc"}
        # Burst/dropout/new-contributor leads must not bleed into a planner advisory
        assert "fid-burst" not in ids
        assert "fid-drop" not in ids
        assert "fid-new" not in ids

    def test_source_lead_ids_cap_through_active_leads_index_end_to_end(
        self, tmp_path, monkeypatch
    ):
        """End-to-end: _build_active_leads_index assembles > cap entries from
        a mix of current-tick + persisted leads (with overlapping fids), then
        _source_lead_ids_for_advisory truncates to the cap and flags it.

        Covers todo 362: existing cap tests either synthesise lead_index
        directly (bypassing assembly) or only exercise small dedup scenarios.
        This test drives the full pipeline so that any future drift in
        _build_active_leads_index ordering, dedup, or source-merge behaviour
        trips a dedicated assertion.
        """
        from unittest.mock import patch

        from watercooler.pulse_stance_lib import _SOURCE_LEAD_IDS_CAP

        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        daemon = _make_daemon(tmp_path, threads_dir=tmp_path / "threads")
        daemon._load_extras()

        # Seed active_signals for 13 topics in the stalled_open_loop category so
        # persisted leads referencing those topics survive the active-signal
        # filter inside _build_active_leads_index.
        total_topics = _SOURCE_LEAD_IDS_CAP + 3
        for i in range(total_topics):
            self._seed_active_signal(daemon, f"topic-{i:02d}", "stalled_open_loop")

        # 5 leads from the current tick (first 5 topics).
        current_tick = [
            _coord_lead_finding(
                finding_id=f"fid-{i:02d}",
                topic=f"topic-{i:02d}",
                category="stalled_open_loop",
            )
            for i in range(5)
        ]
        # 10 persisted leads: topics 3..12. Overlap with current-tick on fids
        # fid-03, fid-04 — dedup must collapse these. Total unique = 13 (> cap).
        persisted = [
            _coord_lead_finding(
                finding_id=f"fid-{i:02d}",
                topic=f"topic-{i:02d}",
                category="stalled_open_loop",
            )
            for i in range(3, total_topics)
        ]

        with patch(
            "watercooler_mcp.daemons.project_coordinator.load_findings",
            return_value=persisted,
        ):
            lead_index = daemon._build_active_leads_index(current_tick)

        # Sanity: dedup produced 13 unique fids (> cap). If this assertion
        # breaks in the future, the assembly behaviour changed and downstream
        # truncation semantics need re-verification.
        fids = [fid for fid, _t, _c in lead_index]
        assert len(fids) == total_topics
        assert len(set(fids)) == total_topics, (
            f"dedup failed: expected {total_topics} unique fids, "
            f"got {len(set(fids))} ({fids})"
        )

        # Truncate through _source_lead_ids_for_advisory for a planner-
        # triggered advisory. Assert cap + truncated flag + no duplicates in
        # the capped output.
        ids, truncated = daemon._source_lead_ids_for_advisory(
            ["coordinator_stalled_open_loop_count"], lead_index
        )
        assert len(ids) == _SOURCE_LEAD_IDS_CAP
        assert truncated is True
        assert len(set(ids)) == len(ids), f"capped list has duplicates: {ids}"
        # Ordering: current-tick leads come first (fid-00..04), then persisted
        # by fid ascending (fid-05..09). The cap takes the first 10.
        assert ids == tuple(f"fid-{i:02d}" for i in range(_SOURCE_LEAD_IDS_CAP))

# ---------------------------------------------------------------------------
# Phase 3c-2 — trend snapshot loading
# ---------------------------------------------------------------------------


class TestTrendSnapshotLoading:
    """Freshness + fail-open behaviour for _load_trend_snapshot()."""

    # pulse_report is Copybara-excluded in the open-core build. Scope the skip
    # to this class only — pytest.importorskip() at class body raises during
    # module import and would skip the entire file.
    pytestmark = pytest.mark.skipif(
        importlib.util.find_spec("watercooler_mcp.daemons.pulse_report") is None,
        reason="pulse_report is private; skipped in open-core builds",
    )

    def test_load_trend_snapshot_fail_open(self, tmp_path, monkeypatch):
        """Both the in-process manager lookup and the on-disk checkpoint
        load are wrapped in try/except — either failing must yield None
        (degraded mode), never raise into the tick."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        daemon = _make_daemon(tmp_path, threads_dir=tmp_path / "threads")

        # No daemon manager registered (get_daemon_manager returns None by default
        # in tests) and no checkpoint file on disk — both paths must silently
        # return None rather than raise.
        assert daemon._load_trend_snapshot() is None

        # Force the in-process path to raise — must still return None.
        def _boom(*_args, **_kwargs):
            raise RuntimeError("forced failure")

        monkeypatch.setattr("watercooler_mcp.daemons.get_daemon_manager", _boom)
        assert daemon._load_trend_snapshot() is None

    def test_load_trend_snapshot_skips_stale_snapshot(self, tmp_path, monkeypatch):
        """An on-disk checkpoint older than _TREND_SNAPSHOT_MAX_AGE_HOURS
        must return None so downstream stance code does not thread a stale
        supersession_rate through to advisories."""
        from datetime import datetime, timedelta, timezone

        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
        )
        daemon = _make_daemon(tmp_path, threads_dir=tmp_path / "threads")

        # Write a stale on-disk checkpoint: generated_at 24h in the past, far
        # beyond the 4h freshness window.
        stale_ts = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        fresh_ts = datetime.now(timezone.utc).isoformat()

        from watercooler_mcp.daemons.state import DaemonCheckpoint, save_checkpoint

        repo_key = daemon._scope_id or ""

        def _write_trend_checkpoint(result: dict) -> None:
            cp = DaemonCheckpoint(daemon_name="trend_snapshot")
            cp.extras = {"projects": {repo_key: {"trend_snapshot": result}}}
            save_checkpoint(cp, namespace=daemon.state_namespace)

        # Stale snapshot → rejected by is_trend_snapshot_fresh.
        _write_trend_checkpoint({"supersession_rate": 0.42, "generated_at": stale_ts})
        assert daemon._load_trend_snapshot() is None

        # Replace with a fresh snapshot — same shape, recent timestamp.
        _write_trend_checkpoint({"supersession_rate": 0.33, "generated_at": fresh_ts})
        signals = daemon._load_trend_snapshot()
        assert signals is not None
        assert signals.supersession_rate == 0.33
