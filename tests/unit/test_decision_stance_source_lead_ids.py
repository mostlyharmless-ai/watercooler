"""Unit tests for ``DecisionStanceDaemon`` source_lead_ids enrichment.

Covers the open-core back-pointer plumbing:
- ``resolve_decision_source_ids`` filtering, dedup, and cap behavior in isolation
- End-to-end through ``DecisionStanceDaemon.tick()`` so emitted advisories
  carry detector / extractor finding IDs that match each role's
  ``triggered_signals``.
"""

from __future__ import annotations

import time

import pytest

from watercooler.config_schema import DecisionStanceConfig
from watercooler.pulse_stance_lib import (
    _SOURCE_LEAD_IDS_CAP,
    resolve_decision_source_ids,
)
from watercooler_mcp.daemons.decision_stance import DecisionStanceDaemon
from watercooler_mcp.daemons.state import Finding, append_findings

# ----------------------------------------------------------------- #
# Fixtures (mirror tests/unit/test_decision_stance_daemon.py)
# ----------------------------------------------------------------- #


@pytest.fixture
def isolated_daemons_dir(tmp_path, monkeypatch):
    """Isolate daemon state under tmp_path so tests don't pollute disk."""
    monkeypatch.setattr(
        "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR",
        tmp_path / "daemons",
    )
    return tmp_path / "daemons"


def _seed_detector(tier: str, n: int = 1, *, prefix: str = "det") -> list[str]:
    """Append ``n`` detector candidate findings with ``details.tier``.

    Returns the list of synthetic finding_ids for assertions.
    """
    now = time.time()
    ids: list[str] = []
    findings: list[Finding] = []
    for i in range(n):
        fid = f"{prefix}-{tier}-{i}-{int(now * 1000)}"
        ids.append(fid)
        findings.append(
            Finding(
                finding_id=fid,
                daemon_name="decision_detector",
                severity="info",
                category="decision_candidate",
                topic=f"topic-{i}",
                entry_id=f"entry-{i}",
                message="seed",
                details={"tier": tier, "score": 4.0 if tier == "High" else 1.0},
                created_at=now,
            )
        )
    append_findings("decision_detector", findings)
    return ids


def _seed_extractor(category: str, n: int = 1, *, prefix: str = "ext") -> list[str]:
    """Append ``n`` extractor findings with the given category."""
    now = time.time()
    ids: list[str] = []
    findings: list[Finding] = []
    for i in range(n):
        fid = f"{prefix}-{category}-{i}-{int(now * 1000)}"
        ids.append(fid)
        findings.append(
            Finding(
                finding_id=fid,
                daemon_name="decision_extractor",
                severity="info",
                category=category,
                topic=f"topic-{i}",
                entry_id=f"entry-{i}",
                message="seed",
                details={},
                created_at=now,
            )
        )
    append_findings("decision_extractor", findings)
    return ids


def _make_daemon(**cfg_kwargs) -> DecisionStanceDaemon:
    cfg = DecisionStanceConfig(enabled=True, **cfg_kwargs)
    return DecisionStanceDaemon(
        interval=cfg.interval,
        config=cfg,
        threads_dir=None,
    )


def _detector_dict(
    fid: str, *, tier: str = "High", category: str = "decision_candidate"
) -> dict:
    """Build the dict shape ``_collect_signals`` produces for the detector."""
    return {
        "finding_id": fid,
        "topic": "topic",
        "entry_id": "entry",
        "category": category,
        "details": {"tier": tier},
        "created_at": time.time(),
    }


def _extractor_dict(fid: str, *, category: str) -> dict:
    """Build the dict shape ``_collect_signals`` produces for the extractor."""
    return {
        "finding_id": fid,
        "topic": "topic",
        "entry_id": "entry",
        "category": category,
        "details": {},
        "created_at": time.time(),
    }


# ----------------------------------------------------------------- #
# resolve_decision_source_ids — direct helper tests
# ----------------------------------------------------------------- #


class TestResolveHelper:
    """``resolve_decision_source_ids`` in isolation — no daemon, no disk I/O."""

    def test_returns_empty_when_no_signals_triggered(self) -> None:
        ids, truncated = resolve_decision_source_ids(
            triggered_signals=[],
            detector_findings=[_detector_dict("a")],
            extractor_findings=[_extractor_dict("b", category="extraction_success")],
        )
        assert ids == ()
        assert truncated is False

    def test_filters_detector_to_high_tier_only(self) -> None:
        det = [
            _detector_dict("h1", tier="High"),
            _detector_dict("h2", tier="High"),
            _detector_dict("m1", tier="Medium"),
            _detector_dict("l1", tier="Low"),
        ]
        ids, truncated = resolve_decision_source_ids(
            triggered_signals=["decision_candidate_high_count"],
            detector_findings=det,
            extractor_findings=[],
        )
        assert ids == ("h1", "h2")
        assert truncated is False

    def test_filters_by_triggered_signals(self) -> None:
        # Critic triggered by rejection ratio only — should pull from
        # extractor rejection categories, not detector at all.
        det = [_detector_dict("h1", tier="High")]
        ext = [
            _extractor_dict("r1", category="extraction_rejected"),
            _extractor_dict("r2", category="extraction_failed"),
            _extractor_dict("s1", category="extraction_success"),
        ]
        ids, _ = resolve_decision_source_ids(
            triggered_signals=["decision_extraction_rejected_count"],
            detector_findings=det,
            extractor_findings=ext,
        )
        assert set(ids) == {"r1", "r2"}
        assert "h1" not in ids
        assert "s1" not in ids

    def test_caps_at_source_lead_ids_cap(self) -> None:
        det = [
            _detector_dict(f"h{i}", tier="High")
            for i in range(_SOURCE_LEAD_IDS_CAP + 5)
        ]
        ids, truncated = resolve_decision_source_ids(
            triggered_signals=["decision_candidate_high_count"],
            detector_findings=det,
            extractor_findings=[],
        )
        assert len(ids) == _SOURCE_LEAD_IDS_CAP
        assert truncated is True

    def test_dedups_across_signals(self) -> None:
        # extraction_success findings match BOTH ``decision_extraction_success_count``
        # AND ``decisions_recorded_recent_count`` in the mapping — the helper must
        # report each finding_id once.
        ext = [
            _extractor_dict("s1", category="extraction_success"),
            _extractor_dict("s2", category="extraction_success"),
        ]
        ids, _ = resolve_decision_source_ids(
            triggered_signals=[
                "decision_extraction_success_count",
                "decisions_recorded_recent_count",
            ],
            detector_findings=[],
            extractor_findings=ext,
        )
        assert ids == ("s1", "s2")

    def test_skips_unknown_signal_names(self) -> None:
        det = [_detector_dict("h1", tier="High")]
        ids, _ = resolve_decision_source_ids(
            triggered_signals=["nonexistent_signal", "decision_candidate_high_count"],
            detector_findings=det,
            extractor_findings=[],
        )
        assert ids == ("h1",)


# ----------------------------------------------------------------- #
# End-to-end through DecisionStanceDaemon.tick()
# ----------------------------------------------------------------- #


class TestDaemonEnrichment:
    """``DecisionStanceDaemon.tick()`` populates ``source_lead_ids`` end-to-end."""

    def test_critic_advisory_carries_high_tier_detector_ids(
        self, isolated_daemons_dir
    ) -> None:
        seeded = _seed_detector("High", n=3)  # SOFT backlog → critic L1
        d = _make_daemon()
        findings = d.tick()

        critic = [f for f in findings if f.topic == "stance:critic"]
        assert len(critic) == 1
        adv = critic[0].details["advisory"]
        assert adv["level"] == 1
        # asdict serializes the tuple — both list (JSON) and tuple are acceptable.
        ids = list(adv["source_lead_ids"])
        assert set(ids) == set(seeded)

    def test_medium_tier_detector_findings_excluded(self, isolated_daemons_dir) -> None:
        seeded_high = _seed_detector("High", n=3, prefix="hi")
        seeded_med = _seed_detector("Medium", n=2, prefix="med")
        d = _make_daemon()
        findings = d.tick()

        critic = [f for f in findings if f.topic == "stance:critic"]
        adv = critic[0].details["advisory"]
        ids = set(adv["source_lead_ids"])
        assert ids == set(seeded_high)
        assert ids.isdisjoint(seeded_med)

    def test_planner_rejection_ratio_pulls_extractor_ids_not_detector(
        self, isolated_daemons_dir
    ) -> None:
        # 1 success + 9 rejections → ratio 0.9 → planner L1 on rejection signal.
        seeded_succ = _seed_extractor("extraction_success", n=1, prefix="s")
        seeded_rej = _seed_extractor("extraction_rejected", n=9, prefix="r")
        # Add a detector finding in the window — must NOT appear in planner IDs
        # because the rejection-ratio signal does not map to detector findings.
        seeded_det = _seed_detector("High", n=1, prefix="d")

        d = _make_daemon()
        findings = d.tick()

        planner = [f for f in findings if f.topic == "stance:planner"]
        assert len(planner) == 1
        adv = planner[0].details["advisory"]
        ids = set(adv["source_lead_ids"])
        # Planner triggered_signals contains decision_extraction_rejected_count;
        # the rejection IDs must be present and the lone detector ID absent.
        assert set(seeded_rej).issubset(ids)
        assert ids.isdisjoint(seeded_det)
        # Success is not in the rejection mapping — must not be cited.
        assert ids.isdisjoint(seeded_succ)

    def test_truncation_tracked_in_status_summary(self, isolated_daemons_dir) -> None:
        # Seed more than the cap so the helper reports truncation.
        _seed_detector("High", n=_SOURCE_LEAD_IDS_CAP + 5)
        d = _make_daemon()
        d.tick()
        status = d.status_summary()
        # Critic + tester both triggered on the high-tier signal — both
        # should report truncation.
        assert status["last_source_ids_truncated"]["critic"] is True
        assert status["last_source_ids_truncated"]["tester"] is True
        # Planner did not trigger on a detector signal — no truncation.
        assert status["last_source_ids_truncated"]["planner"] is False

    def test_quiet_signals_means_no_findings_no_truncation(
        self, isolated_daemons_dir
    ) -> None:
        d = _make_daemon()
        findings = d.tick()
        assert findings == []
        status = d.status_summary()
        assert status["last_source_ids_truncated"] == {
            "planner": False,
            "critic": False,
            "tester": False,
        }


# ----------------------------------------------------------------- #
# Provenance staleness — re-emit when source_ids rotate even at
# unchanged advisory_signature (Codex P2 from the source-leads review).
# ----------------------------------------------------------------- #


class TestProvenanceStaleness:
    """Re-emit when source_lead_ids rotate at steady-state SOFT/HARD."""

    def test_reemits_when_source_ids_rotate_at_same_signature(
        self, isolated_daemons_dir
    ) -> None:
        # Tick 1: 3 HIGH-tier candidates A1..A3 → critic L1 SOFT.
        seeded_a = _seed_detector("High", n=3, prefix="A")
        d = _make_daemon()
        tick1 = d.tick()
        critic1 = [f for f in tick1 if f.topic == "stance:critic"]
        assert len(critic1) == 1
        adv1 = critic1[0].details["advisory"]
        assert adv1["level"] == 1
        sig1 = adv1["advisory_signature"]
        assert set(adv1["source_lead_ids"]) == set(seeded_a)

        # Sanity: same state, second tick → deduped (no new emission).
        tick1b = d.tick()
        assert [f for f in tick1b if f.topic == "stance:critic"] == []

        # Tick 2: rotate the source pool. The original three findings age
        # out (we drop them by truncating the detector findings file) and
        # three new HIGH-tier findings B1..B3 enter. Bucket stays at SOFT,
        # so the rule-driven advisory_signature is identical to tick 1.
        det_file = isolated_daemons_dir / "decision_detector" / "findings.jsonl"
        det_file.write_text("")
        seeded_b = _seed_detector("High", n=3, prefix="B")
        # Fresh daemon picks up tick1's state from disk via the bootstrap
        # path — exercises the emission-signature reconstruction.
        d2 = _make_daemon()
        tick2 = d2.tick()

        critic2 = [f for f in tick2 if f.topic == "stance:critic"]
        # The fix: provenance changed → re-emit must fire.
        assert len(critic2) == 1, (
            "expected a re-emission when source_ids rotated; "
            "without source_ids in the dedup signature, this would silently "
            "leave the persisted advisory citing aged-out IDs"
        )
        adv2 = critic2[0].details["advisory"]
        assert adv2["level"] == 1
        # The advisory's public signature stays identity-stable across the
        # rotation (same level, same triggered_signals, same SOFT bucket).
        assert adv2["advisory_signature"] == sig1
        # ...but the persisted advisory now cites the fresh source IDs.
        assert set(adv2["source_lead_ids"]) == set(seeded_b)
        assert set(adv2["source_lead_ids"]).isdisjoint(seeded_a)
        # The two emissions land at different finding IDs because the
        # emission signature folds in the source-IDs hash.
        assert critic1[0].finding_id != critic2[0].finding_id

    def test_no_reemit_when_signature_and_source_ids_unchanged(
        self, isolated_daemons_dir
    ) -> None:
        # Steady state — same findings, two ticks → exactly one critic finding.
        _seed_detector("High", n=3, prefix="C")
        d = _make_daemon()
        tick1 = d.tick()
        tick2 = d.tick()

        critic1 = [f for f in tick1 if f.topic == "stance:critic"]
        critic2 = [f for f in tick2 if f.topic == "stance:critic"]
        assert len(critic1) == 1
        assert critic2 == []  # deduped — same signature, same source IDs


# ----------------------------------------------------------------- #
# Truncation persistence — capped provenance must be flagged on the
# emitted Finding so consumers don't read a partial list as complete.
# ----------------------------------------------------------------- #


class TestTruncationPersistence:
    """``details["source_lead_ids_truncated"]`` is set on capped advisories."""

    def test_truncation_flag_persisted_on_finding_when_cap_hit(
        self, isolated_daemons_dir
    ) -> None:
        # Seed enough HIGH-tier candidates to overflow the cap.
        _seed_detector("High", n=_SOURCE_LEAD_IDS_CAP + 5)
        d = _make_daemon()
        findings = d.tick()

        critic = [f for f in findings if f.topic == "stance:critic"]
        assert len(critic) == 1
        # Persisted Finding-wrapper details carry the truncation bit so
        # downstream consumers reading via ``watercooler_daemon_findings``
        # know the source_lead_ids list is partial.
        assert critic[0].details.get("source_lead_ids_truncated") is True
        # And the advisory's source_lead_ids tuple is capped.
        assert (
            len(critic[0].details["advisory"]["source_lead_ids"])
            == _SOURCE_LEAD_IDS_CAP
        )

    def test_truncation_flag_omitted_when_below_cap(self, isolated_daemons_dir) -> None:
        # Cap is 10; SOFT triggers at 3. Use 5 (< cap) — no truncation.
        _seed_detector("High", n=5)
        d = _make_daemon()
        findings = d.tick()

        critic = [f for f in findings if f.topic == "stance:critic"]
        assert len(critic) == 1
        # Bit is omitted from details (not set False) to keep payload tight,
        # matching the premium coordinator's convention.
        assert "source_lead_ids_truncated" not in critic[0].details

    def test_reemit_when_truncation_flag_flips_with_same_top_ids(
        self, isolated_daemons_dir
    ) -> None:
        # Tick 1: exactly cap-many findings (no truncation).
        _seed_detector("High", n=_SOURCE_LEAD_IDS_CAP, prefix="A")
        d = _make_daemon()
        tick1 = d.tick()
        critic1 = [f for f in tick1 if f.topic == "stance:critic"]
        assert len(critic1) == 1
        assert "source_lead_ids_truncated" not in critic1[0].details

        # Tick 2: add 5 more findings. The first cap-many sorted IDs are
        # the *same* set as tick 1 (lexicographic order is stable for the
        # original prefix), but truncation flag flips True. Without folding
        # the flag into emission_sig, the same source_lead_ids tuple would
        # suppress re-emission and persist a stale "no truncation" claim.
        _seed_detector("High", n=5, prefix="Z")  # later prefix sorts after
        tick2 = d.tick()
        critic2 = [f for f in tick2 if f.topic == "stance:critic"]
        assert len(critic2) == 1, (
            "expected re-emit when truncation status changed, even at the "
            "same top-N sorted source IDs"
        )
        assert critic2[0].details.get("source_lead_ids_truncated") is True

    def test_bootstrap_rehydrates_truncation_flag(self, isolated_daemons_dir) -> None:
        # Tick 1 on daemon d1 — cap hit, truncation persisted.
        _seed_detector("High", n=_SOURCE_LEAD_IDS_CAP + 3)
        d1 = _make_daemon()
        tick1 = d1.tick()
        append_findings("decision_stance", tick1)

        # Fresh daemon (simulates restart) ticks against the same signals.
        # Bootstrap must read the persisted truncation bit so the rehydrated
        # emission signature matches the live tick's emission signature —
        # otherwise the fresh daemon would re-emit a redundant advisory.
        d2 = _make_daemon()
        tick2 = d2.tick()
        critic2 = [f for f in tick2 if f.topic == "stance:critic"]
        assert critic2 == [], (
            "fresh daemon re-emitted; bootstrap likely missed the "
            "source_lead_ids_truncated bit on the persisted finding"
        )
