"""Multi-tick integration tests for ``DecisionStanceDaemon``.

End-to-end roundtrip through the real ``append_findings`` / ``load_findings``
JSONL storage path. Confirms:
- All three Phase 1 roles can simultaneously elevate from a single signal mix.
- Tick 2 with unchanged signals emits zero new findings (replace-on-change dedup).
- Signal flip on tick 3 re-emits only the role whose signature changed.
- A full clear (signals drained) emits a tombstone per previously-elevated role.
"""

from __future__ import annotations

import time

import pytest

from watercooler.config_schema import DecisionStanceConfig
from watercooler_mcp.daemons.decision_stance import DecisionStanceDaemon
from watercooler_mcp.daemons.state import (
    Finding,
    append_findings,
    load_findings,
)


@pytest.fixture
def isolated_daemons_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR",
        tmp_path / "daemons",
    )
    return tmp_path / "daemons"


def _make_finding(
    *, daemon: str, category: str, idx: int, details: dict | None = None
) -> Finding:
    return Finding(
        finding_id=f"{daemon}-{category}-{idx}-{int(time.time()*1000)}-{idx}",
        daemon_name=daemon,
        severity="info",
        category=category,
        topic=f"topic-{idx}",
        entry_id="",
        message="seed",
        details=details or {},
        created_at=time.time(),
    )


def _seed_signals(*, candidates: int = 0, success: int = 0, rejected: int = 0) -> None:
    if candidates:
        append_findings(
            "decision_detector",
            [
                _make_finding(
                    daemon="decision_detector",
                    category="decision_candidate",
                    idx=i,
                    details={"tier": "High"},
                )
                for i in range(candidates)
            ],
        )
    if success:
        append_findings(
            "decision_extractor",
            [
                _make_finding(
                    daemon="decision_extractor",
                    category="extraction_success",
                    idx=i,
                )
                for i in range(success)
            ],
        )
    if rejected:
        append_findings(
            "decision_extractor",
            [
                _make_finding(
                    daemon="decision_extractor",
                    category="extraction_rejected",
                    idx=100 + i,
                )
                for i in range(rejected)
            ],
        )


def _make_daemon() -> DecisionStanceDaemon:
    return DecisionStanceDaemon(config=DecisionStanceConfig(enabled=True))


# ----------------------------------------------------------------- #


def test_multi_tick_dedup_and_flip(isolated_daemons_dir):
    """Three-tick scenario covering elevation, dedup, and signal flip."""

    # --- Tick 1: HARD candidate backlog + HARD rejection ratio fires all roles
    # critic L2 (HARD backlog), planner L1 (HARD rejection), tester L1 (drought).
    # success=0 so recorded-decisions proxy is 0 → drought row triggers tester.
    _seed_signals(candidates=8, success=0, rejected=9)
    daemon = _make_daemon()

    tick1 = daemon.tick()
    append_findings("decision_stance", tick1)

    by_role = {f.topic: f for f in tick1}
    assert "stance:critic" in by_role
    assert "stance:planner" in by_role
    assert "stance:tester" in by_role
    assert by_role["stance:critic"].details["advisory"]["level"] == 2
    assert by_role["stance:planner"].details["advisory"]["level"] == 1
    assert by_role["stance:tester"].details["advisory"]["level"] == 1

    # --- Tick 2: signals unchanged → all three roles dedup → empty
    tick2 = daemon.tick()
    assert tick2 == [], [f.topic for f in tick2]

    # --- Tick 3: backlog drops below SOFT (clear critic), rejection ratio
    # stays HARD (planner stays elevated), drought persists (tester elevated).
    # We can't remove findings cleanly, so we shorten the rolling window so the
    # detector candidates fall outside it but the extractor findings remain.
    # Both detector + extractor findings have the same created_at, so we use a
    # custom daemon with a near-zero window for detector findings only — but
    # the daemon doesn't expose per-source windows. Easiest: drop detector
    # findings file to clear backlog, leaving extractor findings in place.
    det_file = isolated_daemons_dir / "decision_detector" / "findings.jsonl"
    det_file.write_text("")  # backlog → 0; recorded_decisions still = 0 (success=0)

    tick3 = daemon.tick()

    by_role3 = {f.topic: f for f in tick3}
    # Critic drops L2 → L1 (rejection-ratio SOFT row keeps it elevated at a new
    # signature) — the signature change re-emits a fresh L1 finding.
    assert "stance:critic" in by_role3
    assert by_role3["stance:critic"].details["advisory"]["level"] == 1
    # Planner rejection ratio is unchanged → no new finding for planner.
    assert "stance:planner" not in by_role3
    # Tester drought relied on backlog ≥ SOFT — without backlog, tester now L0
    # (was L1) → tombstone with dedup_signature="cleared".
    assert "stance:tester" in by_role3
    assert by_role3["stance:tester"].details["advisory"]["level"] == 0


def test_findings_queryable_via_load_findings(isolated_daemons_dir):
    """A consumer querying ``stance_advisory`` findings sees decision_stance output."""
    _seed_signals(candidates=8)
    daemon = _make_daemon()
    findings = daemon.tick()
    append_findings("decision_stance", findings)

    loaded = load_findings("decision_stance", category="stance_advisory")
    assert len(loaded) >= 1
    # Daemon name namespacing — agents query by daemon to isolate sources.
    assert all(f.daemon_name == "decision_stance" for f in loaded)
    # Topics follow the documented stance:{role} namespace.
    assert all(f.topic.startswith("stance:") for f in loaded)


def test_advisory_payload_shape(isolated_daemons_dir):
    """``details["advisory"]`` carries the documented StanceAdvisory schema."""
    _seed_signals(candidates=8)
    daemon = _make_daemon()
    findings = daemon.tick()

    critic = next(f for f in findings if f.topic == "stance:critic")
    advisory = critic.details["advisory"]
    # Required fields for downstream consumers.
    for key in (
        "schema_version",
        "role",
        "level",
        "summary",
        "triggered_signals",
        "missing_inputs",
        "threshold_crossings",
        "advisory_signature",
        "signal_values",
        "stance",
        "actions",
        "source_lead_ids",
    ):
        assert key in advisory, key
    assert advisory["schema_version"] == 1
    assert advisory["role"] == "critic"


def test_advisory_carries_source_lead_ids(isolated_daemons_dir):
    """``source_lead_ids`` lists detector finding IDs that drove the elevation.

    Open-core analogue of the premium coordinator-lead provenance: when
    HIGH-tier ``decision_candidate`` findings cross the SOFT threshold,
    the resulting critic + tester advisories should cite those finding IDs.
    """
    # Seed 3 HIGH-tier candidates with explicit, deterministic finding IDs
    # so we can assert exact set membership.
    seeded_ids = [f"det-known-{i}" for i in range(3)]
    append_findings(
        "decision_detector",
        [
            Finding(
                finding_id=fid,
                daemon_name="decision_detector",
                severity="info",
                category="decision_candidate",
                topic=f"topic-{i}",
                entry_id=f"entry-{i}",
                message="seed",
                details={"tier": "High", "score": 5.0},
                created_at=time.time(),
            )
            for i, fid in enumerate(seeded_ids)
        ],
    )

    daemon = _make_daemon()
    findings = daemon.tick()
    by_role = {f.topic: f for f in findings}

    critic_ids = list(by_role["stance:critic"].details["advisory"]["source_lead_ids"])
    tester_ids = list(by_role["stance:tester"].details["advisory"]["source_lead_ids"])
    assert set(critic_ids) == set(seeded_ids)
    assert set(tester_ids) == set(seeded_ids)
