"""Unit tests for ``DecisionStanceDaemon``.

Covers the three guarantees the proposal commits to:
1. Decision-pipeline findings → expected per-role advisory shape and level.
2. Replace-on-change dedup — the same signature does not re-emit on tick 2.
3. Tombstone emission with ``dedup_signature="cleared"`` on a L1+ → L0 transition.
"""

from __future__ import annotations

import time

import pytest

from watercooler.config_schema import DecisionStanceConfig
from watercooler.pulse_stance_lib import STANCE_ROLES, _ROLE_FNS
from watercooler.role_loader import load_roles
from watercooler_mcp.daemons.decision_stance import (
    DecisionStanceDaemon,
    _build_emission_signature,
)
from watercooler_mcp.daemons.state import (
    Finding,
    append_findings,
)

# ----------------------------------------------------------------- #
# Fixtures
# ----------------------------------------------------------------- #


@pytest.fixture
def isolated_daemons_dir(tmp_path, monkeypatch):
    """Isolate daemon state under tmp_path so tests don't pollute disk."""
    monkeypatch.setattr(
        "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR",
        tmp_path / "daemons",
    )
    return tmp_path / "daemons"


def _seed_extractor(category: str, n: int = 1, *, age_seconds: float = 0.0) -> None:
    """Append ``n`` extractor findings with the given category."""
    now = time.time() - age_seconds
    findings = [
        Finding(
            finding_id=f"ext-{category}-{i}-{int(now*1000)}",
            daemon_name="decision_extractor",
            severity="info",
            category=category,
            topic=f"topic-{i}",
            entry_id="",
            message="seed",
            details={},
            created_at=now,
        )
        for i in range(n)
    ]
    append_findings("decision_extractor", findings)


def _seed_detector(tier: str, n: int = 1, *, age_seconds: float = 0.0) -> None:
    """Append ``n`` detector candidate findings with ``details.tier``."""
    now = time.time() - age_seconds
    findings = [
        Finding(
            finding_id=f"det-{tier}-{i}-{int(now*1000)}",
            daemon_name="decision_detector",
            severity="info",
            category="decision_candidate",
            topic=f"topic-{i}",
            entry_id="",
            message="seed",
            details={"tier": tier, "score": 4.0 if tier == "High" else 1.0},
            created_at=now,
        )
        for i in range(n)
    ]
    append_findings("decision_detector", findings)


def _make_daemon(threads_dir=None, **cfg_kwargs) -> DecisionStanceDaemon:
    cfg = DecisionStanceConfig(enabled=True, **cfg_kwargs)
    # threads_dir override is optional — daemon uses it for scope_id only.
    return DecisionStanceDaemon(
        interval=cfg.interval,
        config=cfg,
        threads_dir=threads_dir,
    )


# ----------------------------------------------------------------- #
# Mapping — produce the expected advisory shape from synthetic findings
# ----------------------------------------------------------------- #


class TestMapping:
    """Synthetic findings → expected ``stance_advisory`` shape."""

    def test_critic_l1_on_soft_backlog(self, isolated_daemons_dir):
        _seed_detector("High", n=3)  # SOFT backlog
        d = _make_daemon()
        findings = d.tick()

        critic = [f for f in findings if f.topic == "stance:critic"]
        assert len(critic) == 1
        adv = critic[0].details["advisory"]
        assert adv["level"] == 1
        assert "decision_candidate_high_count" in adv["triggered_signals"]

    def test_critic_l2_on_hard_backlog(self, isolated_daemons_dir):
        _seed_detector("High", n=8)  # HARD backlog
        d = _make_daemon()
        findings = d.tick()

        critic = [f for f in findings if f.topic == "stance:critic"]
        assert len(critic) == 1
        adv = critic[0].details["advisory"]
        assert adv["level"] == 2
        assert critic[0].severity == "warning"

    def test_planner_l1_on_hard_rejection_ratio(self, isolated_daemons_dir):
        # 1 success vs 9 rejections → ratio 0.9 (HARD)
        _seed_extractor("extraction_success", n=1)
        _seed_extractor("extraction_rejected", n=9)
        d = _make_daemon()
        findings = d.tick()

        planner = [f for f in findings if f.topic == "stance:planner"]
        assert len(planner) == 1
        adv = planner[0].details["advisory"]
        assert adv["level"] == 1
        assert "decision_extraction_rejected_count" in adv["triggered_signals"]

    def test_tester_l1_on_drought_with_backlog(self, isolated_daemons_dir):
        # No successes → drought; backlog at SOFT.
        _seed_detector("High", n=3)
        # Note: we do NOT seed any extraction_success findings — the daemon
        # uses the success count as a proxy for "decisions recorded recently".
        d = _make_daemon()
        findings = d.tick()

        tester = [f for f in findings if f.topic == "stance:tester"]
        assert len(tester) == 1
        adv = tester[0].details["advisory"]
        assert adv["level"] == 1
        assert "decisions_recorded_recent_count" in adv["triggered_signals"]

    def test_no_findings_when_signals_quiet(self, isolated_daemons_dir):
        d = _make_daemon()
        findings = d.tick()
        # No prior elevation, all roles at L0 → no findings emitted.
        assert findings == []

    def test_window_excludes_old_findings(self, isolated_daemons_dir):
        # Default window is 24h; seed findings older than that.
        _seed_detector("High", n=8, age_seconds=2 * 86400.0)
        d = _make_daemon()
        findings = d.tick()
        # All signals out of window → no advisory emitted.
        assert findings == []


# ----------------------------------------------------------------- #
# Replace-on-change dedup
# ----------------------------------------------------------------- #


class TestDedup:
    def test_second_tick_emits_nothing_when_signals_unchanged(
        self, isolated_daemons_dir
    ):
        _seed_detector("High", n=8)
        d = _make_daemon()

        first = d.tick()
        assert len(first) >= 1
        # Persist the first batch — production wiring writes this via append_findings.
        append_findings("decision_stance", first)

        # Tick 2: dedup pulls in the just-persisted finding ids; same signals →
        # zero new findings.
        second = d.tick()
        assert second == []

    def test_signal_flip_emits_new_finding_on_next_tick(self, isolated_daemons_dir):
        _seed_detector("High", n=3)  # SOFT backlog → critic L1
        d = _make_daemon()
        first = d.tick()
        append_findings("decision_stance", first)

        # Push backlog over HARD by adding more candidates.
        _seed_detector("High", n=6)
        third = d.tick()
        # Signature changed (SOFT → HARD), so critic re-emits.
        critic_new = [f for f in third if f.topic == "stance:critic"]
        assert len(critic_new) == 1
        assert critic_new[0].details["advisory"]["level"] == 2


# ----------------------------------------------------------------- #
# Tombstone emission on L1+ → L0
# ----------------------------------------------------------------- #


class TestTombstone:
    def test_clears_with_tombstone_when_signals_drop(
        self, isolated_daemons_dir, tmp_path
    ):
        _seed_detector("High", n=8)
        d = _make_daemon()
        first = d.tick()
        append_findings("decision_stance", first)

        # Compaction-substitute: clear the source findings file so the next tick
        # sees zero detector candidates and the critic stance falls back to L0.
        det_file = tmp_path / "daemons" / "decision_detector" / "findings.jsonl"
        if det_file.exists():
            det_file.write_text("")

        second = d.tick()
        cleared = [
            f
            for f in second
            if f.topic == "stance:critic" and f.details["advisory"]["level"] == 0
        ]
        assert len(cleared) == 1, [f.topic for f in second]
        assert "cleared" in cleared[0].message.lower()


# ----------------------------------------------------------------- #
# Sanity — daemon name + premium gating
# ----------------------------------------------------------------- #


def test_daemon_has_correct_name_and_protocol_roles():
    d = DecisionStanceDaemon(config=DecisionStanceConfig(enabled=True))
    assert d.name == "decision_stance"
    assert STANCE_ROLES == ("planner", "critic", "tester")


def test_stance_roles_match_dispatcher_keys():
    """STANCE_ROLES and _ROLE_FNS keys cannot drift apart."""
    assert set(STANCE_ROLES) == set(_ROLE_FNS.keys())


def test_stance_roles_present_in_bundled_defaults(tmp_path):
    """The three stance-bearing roles ship in bundled .watercooler/roles.toml.

    Guards against a future PR accidentally removing planner/critic/tester
    from src/watercooler/data/roles.toml — which would not break the daemon
    (it iterates STANCE_ROLES, not the catalog) but would break write-time
    role validation for those roles.
    """
    roles = load_roles(tmp_path)  # no project file → bundled defaults
    assert set(STANCE_ROLES) <= set(roles.keys())


def test_status_summary_includes_window():
    d = DecisionStanceDaemon(config=DecisionStanceConfig(enabled=True))
    info = d.status_summary()
    assert info["window_seconds"] == 86400.0


# ----------------------------------------------------------------- #
# Regression — re-emit after dedup resync (codex P1)
# ----------------------------------------------------------------- #


class TestResyncRegression:
    """When ``_resync_dedup`` reloads the on-disk dedup set, only the latest
    fid per topic is restored. A signal reverting to a previously-seen
    signature must re-emit, even after the in-memory ``discard`` has been
    "forgotten" by a resync.
    """

    def test_revert_signature_after_resync_re_emits(self, isolated_daemons_dir):
        # Tick 1: SOFT backlog → critic L1 with signature_A.
        _seed_detector("High", n=3)
        d = _make_daemon()
        tick1 = d.tick()
        append_findings("decision_stance", tick1)
        critic_a = next(
            f.details["advisory"] for f in tick1 if f.topic == "stance:critic"
        )
        sig_a = critic_a["advisory_signature"]
        emission_a = _build_emission_signature(sig_a, critic_a["source_lead_ids"])

        # Tick 2: HARD backlog → critic L2 with signature_B (different from A).
        _seed_detector("High", n=6)
        tick2 = d.tick()
        append_findings("decision_stance", tick2)
        critic_b = next(
            f.details["advisory"] for f in tick2 if f.topic == "stance:critic"
        )
        sig_b = critic_b["advisory_signature"]
        emission_b = _build_emission_signature(sig_b, critic_b["source_lead_ids"])
        assert sig_a != sig_b

        # Force a resync: drop the in-memory dedup state and rebuild from disk
        # (simulates the every-N-ticks resync OR a daemon restart).
        d._existing_keys = set()
        d._ticks_since_resync = 0
        d._resync_dedup()

        # The latest-per-topic resync should retain only sig_B's fid for
        # stance:critic — sig_A's fid must NOT be re-cached, otherwise the
        # next revert to A is suppressed. Finding IDs are derived from the
        # *emission* signature (advisory_signature plus provenance hash).
        from watercooler_mcp.daemons.state import build_finding_id

        fid_a = build_finding_id(
            scope_id=d._scope_id,
            daemon_name=d.name,
            topic="stance:critic",
            category="stance_advisory",
            entry_id="",
            dedup_signature=emission_a,
        )
        fid_b = build_finding_id(
            scope_id=d._scope_id,
            daemon_name=d.name,
            topic="stance:critic",
            category="stance_advisory",
            entry_id="",
            dedup_signature=emission_b,
        )
        assert fid_a not in d._existing_keys, "stale fid leaked across resync"
        assert fid_b in d._existing_keys, "current head fid missing after resync"

        # Drop the extra detector findings so backlog falls back to SOFT — the
        # critic signature reverts to sig_A. Daemon must re-emit despite
        # having seen sig_A in the past.
        det_file = isolated_daemons_dir / "decision_detector" / "findings.jsonl"
        # Keep only the original 3 findings (drop the 6 added on tick 2).
        kept = det_file.read_text().splitlines()[:3]
        det_file.write_text("\n".join(kept) + ("\n" if kept else ""))
        # last_stance_signatures still says sig_B from tick 2 — that's the
        # in-memory transition source for the discard-fid_B branch in
        # _emit_for_role. Make sure the emission signature (advisory_signature
        # plus provenance hash) is set so the path is the realistic one.
        assert d._last_stance_signatures["critic"] == emission_b

        tick3 = d.tick()
        critic_re_emit = [
            f
            for f in tick3
            if f.topic == "stance:critic"
            and f.details["advisory"]["advisory_signature"] == sig_a
        ]
        assert len(critic_re_emit) == 1, (
            "revert to a previously-seen signature must re-emit after resync; "
            "got: " + str([(f.topic, f.details["advisory"]["level"]) for f in tick3])
        )


class TestBootstrapFromDisk:
    """A fresh daemon must hydrate per-role signatures from disk on tick 1
    so a restart doesn't re-emit advisories that are already current.
    Also locks in: bootstrap is one-shot (idle ticks don't re-bootstrap).
    """

    def test_fresh_daemon_does_not_reemit_active_advisory(self, isolated_daemons_dir):
        # Tick 1 on a primary daemon → critic L1 advisory written to disk.
        _seed_detector("High", n=3)
        d1 = _make_daemon()
        tick1 = d1.tick()
        append_findings("decision_stance", tick1)
        critic_advisory = next(
            f.details["advisory"] for f in tick1 if f.topic == "stance:critic"
        )
        critic_sig = critic_advisory["advisory_signature"]
        critic_ids = critic_advisory["source_lead_ids"]
        assert critic_sig
        expected_emission_sig = _build_emission_signature(critic_sig, critic_ids)

        # Construct a fresh daemon against the same daemons_dir — simulates a
        # restart with prior advisory findings on disk.
        d2 = _make_daemon()
        assert d2._last_stance_signatures["critic"] == "", "pre-tick state is fresh"

        tick_a = d2.tick()
        # Bootstrap must rehydrate critic's emission signature so the
        # unchanged signal + unchanged source IDs produce zero new findings.
        critic_emits = [f for f in tick_a if f.topic == "stance:critic"]
        assert (
            critic_emits == []
        ), "fresh daemon re-emitted active advisory; bootstrap-from-disk failed"
        assert d2._last_stance_signatures["critic"] == expected_emission_sig
        assert d2._bootstrapped is True

    def test_bootstrap_is_one_shot_when_idle(self, isolated_daemons_dir):
        # No findings on disk; daemon stays idle (no advisories ever).
        d = _make_daemon()

        d.tick()  # tick 1: bootstraps
        assert d._bootstrapped is True
        ticks_after_first = d._ticks_since_resync

        # Tick 2 on an idle daemon must NOT re-trigger bootstrap. The flag
        # protects against the prior `not self._existing_keys` regression
        # where idle ticks looped back into the bootstrap branch.
        d.tick()
        assert d._bootstrapped is True
        # If bootstrap had re-fired, _resync_dedup would have reset
        # _ticks_since_resync to 0 again (same value as after tick 1).
        # Without re-fire, it should be ticks_after_first + 1.
        assert d._ticks_since_resync == ticks_after_first + 1

    def test_tombstone_on_disk_keeps_role_signature_empty(self, isolated_daemons_dir):
        # Seed a cleared/tombstone advisory directly on disk for planner.
        tombstone = Finding(
            finding_id="tomb-planner",
            daemon_name="decision_stance",
            severity="info",
            category="stance_advisory",
            topic="stance:planner",
            entry_id="",
            message="Planner: cleared",
            details={
                "advisory": {
                    "schema_version": 1,
                    "role": "planner",
                    "level": 0,
                    "advisory_signature": "",
                    "summary": "Planner: cleared",
                }
            },
            created_at=time.time(),
        )
        append_findings("decision_stance", [tombstone])

        d = _make_daemon()
        # No live signals — tick should leave planner at L0 silently (no
        # tombstone re-emit), with the in-memory signature staying empty.
        tick1 = d.tick()
        assert all(f.topic != "stance:planner" for f in tick1)
        assert d._last_stance_signatures["planner"] == ""


class TestProjectSalience:
    """project_salience decoration via .watercooler/roles.toml (Role Salience Compiler)."""

    def test_salience_decorates_elevated_advisory(self, isolated_daemons_dir, tmp_path):
        wc_dir = tmp_path / ".watercooler"
        wc_dir.mkdir()
        (wc_dir / "roles.toml").write_text(
            '[roles.critic]\n'
            'description = "Critic"\n'
            'canonical_role = "critic"\n'
            'project_salience = ["watch for hidden authority expansion"]\n'
        )
        _seed_detector("High", n=3)  # elevates critic to L1
        d = _make_daemon()
        d._code_root = tmp_path
        d._resolved_threads_dir = isolated_daemons_dir
        findings = d.tick()

        critic = next(f for f in findings if f.topic == "stance:critic")
        assert critic.details["advisory"]["project_salience"] == (
            "watch for hidden authority expansion",
        )
        assert (
            critic.details["advisory"]["authority_basis"]
            == "human_promoted_lesson_projected"
        )

        planner = next(f for f in findings if f.topic == "stance:tester")
        assert planner.details["advisory"]["project_salience"] == ()

    def test_malformed_roles_toml_falls_back_with_deduped_diagnostic(
        self, isolated_daemons_dir, tmp_path
    ):
        wc_dir = tmp_path / ".watercooler"
        wc_dir.mkdir()
        (wc_dir / "roles.toml").write_text("not valid toml [[[")

        _seed_detector("High", n=3)
        d = _make_daemon()
        d._code_root = tmp_path
        d._resolved_threads_dir = isolated_daemons_dir
        findings = d.tick()

        diagnostics = [f for f in findings if f.category == "role_salience_diagnostic"]
        assert len(diagnostics) == 1
        assert diagnostics[0].details["effect"] == "stance_salience_disabled"
        assert diagnostics[0].repo == str(tmp_path)  # scoped, not repo-leaking

        critic = next(f for f in findings if f.topic == "stance:critic")
        assert critic.details["advisory"]["project_salience"] == ()

        # Second tick with the same parse error must not re-emit the diagnostic.
        _seed_detector("High", n=1)
        findings2 = d.tick()
        assert not [
            f for f in findings2 if f.category == "role_salience_diagnostic"
        ]

    def test_salience_reloads_on_roles_toml_mtime_change(
        self, isolated_daemons_dir, tmp_path
    ):
        wc_dir = tmp_path / ".watercooler"
        wc_dir.mkdir()
        roles_path = wc_dir / "roles.toml"
        roles_path.write_text(
            '[roles.critic]\n'
            'description = "Critic"\n'
            'canonical_role = "critic"\n'
            'project_salience = ["first bullet"]\n'
        )
        _seed_detector("High", n=3)
        d = _make_daemon()
        d._code_root = tmp_path
        d._resolved_threads_dir = isolated_daemons_dir
        findings = d.tick()
        critic = next(f for f in findings if f.topic == "stance:critic")
        assert critic.details["advisory"]["project_salience"] == ("first bullet",)

        # Edit the bullet and force a tick boundary (signature must change so
        # the role re-emits, and the cache must pick up the new mtime).
        time.sleep(0.01)
        roles_path.write_text(
            '[roles.critic]\n'
            'description = "Critic"\n'
            'canonical_role = "critic"\n'
            'project_salience = ["second bullet"]\n'
        )
        _seed_detector("High", n=1)
        findings2 = d.tick()
        critic2 = next(f for f in findings2 if f.topic == "stance:critic")
        assert critic2.details["advisory"]["project_salience"] == ("second bullet",)
