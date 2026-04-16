"""Tests for ProjectCoordinatorDaemon — coordination intelligence scanning."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from watercooler.baseline_graph import storage
from watercooler.config_schema import ProjectCoordinatorConfig
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
        findings1 = daemon.tick()
        stance1 = self._stance_findings(findings1)

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
        graph_dir = storage.get_graph_dir(threads_dir)
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
        findings1 = daemon.tick()
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
        topics = {f.topic for f in stance}
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
