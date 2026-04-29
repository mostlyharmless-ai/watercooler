"""Tests for pulse_stance_lib — stance modulation data types and logic."""

from __future__ import annotations

from dataclasses import asdict

from watercooler.pulse_stance_lib import (
    AdvisoryAction,
    StanceSignals,
    _READ_ONLY_TOOLS,
    build_stance_advisories,
    extract_stance_signals,
    pulse_to_stance,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_snapshot() -> dict:
    """Snapshot with benign signals — should produce all-L0 advisories."""
    return {
        "contributors": {
            "alice": {
                "observation_counts": {"decision": 5, "insight": 3},
                "focus_areas": [],
                "open_loops": [],
            },
        },
        "stalled_threads": [],
        "corpus": {"sessions_in_window": 2},
        "risk_surface_tags": [],
        "analysis": {
            "latest_report_path": None,
            "is_fresh": False,
        },
    }


def _elevated_snapshot() -> dict:
    """Snapshot with high volatility and risk tags."""
    return {
        "contributors": {
            "alice": {
                "observation_counts": {
                    "decision": 1,
                    "insight": 0,
                    "problem": 4,
                    "risk": 3,
                    "exploration": 2,
                },
                "focus_areas": ["auth"],
                "open_loops": ["fix auth", "fix perms", "fix roles"],
            },
            "bob": {
                "observation_counts": {
                    "decision": 0,
                    "insight": 1,
                    "problem": 5,
                    "risk": 2,
                    "exploration": 3,
                },
                "focus_areas": ["auth", "db"],
                "open_loops": ["fix db", "fix cache", "fix index"],
            },
        },
        "stalled_threads": ["thread-a", "thread-b", "thread-c"],
        "corpus": {"sessions_in_window": 5},
        "risk_surface_tags": ["high-churn", "no-tests", "no-docs"],
        "analysis": {
            "latest_report_path": "/reports/analysis.md",
            "is_fresh": False,
        },
    }


# ---------------------------------------------------------------------------
# Signal extraction tests
# ---------------------------------------------------------------------------


class TestExtractStanceSignals:
    def test_none_snapshot_degraded_mode(self):
        signals = extract_stance_signals(None)
        assert signals.pulse_available is False
        assert signals.volatility_ratio == 0.0
        assert signals.stalled_thread_count == 0

    def test_empty_dict_normalized_to_none(self):
        signals = extract_stance_signals({})
        assert signals.pulse_available is False

    def test_minimal_snapshot_full_mode(self):
        signals = extract_stance_signals(_minimal_snapshot())
        assert signals.pulse_available is True
        assert signals.volatility_ratio == 0.0  # all stable, no changing
        assert signals.stalled_thread_count == 0

    def test_elevated_snapshot_computes_volatility(self):
        signals = extract_stance_signals(_elevated_snapshot())
        assert signals.pulse_available is True
        # alice: 9 changing / 10 total = 0.90
        # bob: 10 changing / 11 total ≈ 0.91
        assert signals.volatility_ratio > 0.80

    def test_all_none_volatility_ratios(self):
        """When all contributors have zero total, volatility should be 0.0."""
        snap = {
            "contributors": {
                "empty": {
                    "observation_counts": {},
                    "focus_areas": [],
                    "open_loops": [],
                },
            },
            "stalled_threads": [],
            "corpus": {},
            "risk_surface_tags": [],
            "analysis": {},
        }
        signals = extract_stance_signals(snap)
        assert signals.pulse_available is True
        assert signals.volatility_ratio == 0.0

    def test_coordinator_findings_counted(self):
        findings = [
            {"category": "stalled_open_loop"},
            {"category": "stalled_open_loop"},
            {"category": "aware_role_concentration"},
            {"category": "stalled_dropout"},
            {"category": "aware_burst"},
        ]
        signals = extract_stance_signals(None, coordinator_findings=findings)
        assert signals.coordinator_stalled_open_loop_count == 2
        assert signals.coordinator_role_concentration_count == 1
        assert signals.coordinator_dropout_count == 1
        assert signals.coordinator_burst_count == 1

    def test_empty_coordinator_findings(self):
        signals = extract_stance_signals(None, coordinator_findings=[])
        assert signals.coordinator_stalled_open_loop_count == 0

    def test_risk_tag_count(self):
        signals = extract_stance_signals(_elevated_snapshot())
        assert signals.risk_tag_count == 3

    def test_open_loop_count(self):
        signals = extract_stance_signals(_elevated_snapshot())
        assert signals.open_loop_count == 6  # 3 + 3

    def test_analysis_freshness(self):
        snap = _minimal_snapshot()
        snap["analysis"] = {
            "latest_report_path": "/report.md",
            "is_fresh": True,
        }
        signals = extract_stance_signals(snap)
        assert signals.analysis_report_available is True
        assert signals.analysis_is_fresh is True

    def test_no_analysis_report(self):
        signals = extract_stance_signals(_minimal_snapshot())
        assert signals.analysis_report_available is False
        assert signals.analysis_is_fresh is False

    def test_focus_area_overlap(self):
        signals = extract_stance_signals(_elevated_snapshot())
        assert signals.focus_area_overlap_count == 1  # "auth" shared

    def test_sessions_in_window(self):
        signals = extract_stance_signals(_elevated_snapshot())
        assert signals.sessions_in_window == 5


# ---------------------------------------------------------------------------
# pulse_to_stance tests
# ---------------------------------------------------------------------------


class TestPulseToStance:
    def test_unknown_role_raises(self):
        import pytest

        with pytest.raises(ValueError, match="Unknown stance role"):
            pulse_to_stance("unknown", StanceSignals())

    def test_minimal_all_l0(self):
        advisories = build_stance_advisories(_minimal_snapshot())
        for a in advisories:
            assert a.level == 0
            assert a.schema_version == 1

    def test_minimal_returns_three_roles(self):
        advisories = build_stance_advisories(_minimal_snapshot())
        roles = {a.role for a in advisories}
        assert roles == {"planner", "critic", "tester"}

    def test_planner_volatility_soft(self):
        """Volatility at soft threshold → L1."""
        signals = StanceSignals(
            pulse_available=True,
            volatility_ratio=0.55,
        )
        a = pulse_to_stance("planner", signals)
        assert a.level == 1
        assert a.stance.retrieval_pressure > 0.0
        assert "volatility_ratio" in a.triggered_signals

    def test_planner_volatility_hard(self):
        """Volatility at hard threshold → L2."""
        signals = StanceSignals(
            pulse_available=True,
            volatility_ratio=0.75,
        )
        a = pulse_to_stance("planner", signals)
        assert a.level == 2
        assert a.stance.decision_caution >= 0.7

    def test_planner_stalled_open_loops_soft(self):
        """Coordinator stalled loops at soft → L1."""
        signals = StanceSignals(
            coordinator_stalled_open_loop_count=3,
        )
        a = pulse_to_stance("planner", signals)
        assert a.level == 1
        assert a.stance.closure_pressure > 0.0

    def test_planner_combined_l2(self):
        """Volatility soft + stalled soft → L2."""
        signals = StanceSignals(
            pulse_available=True,
            volatility_ratio=0.55,
            coordinator_stalled_open_loop_count=3,
        )
        a = pulse_to_stance("planner", signals)
        assert a.level == 2

    def test_planner_role_concentration_hard(self):
        signals = StanceSignals(
            coordinator_role_concentration_count=4,
        )
        a = pulse_to_stance("planner", signals)
        assert a.level == 2

    def test_critic_risk_soft(self):
        signals = StanceSignals(
            pulse_available=True,
            risk_tag_count=1,
        )
        a = pulse_to_stance("critic", signals)
        assert a.level == 1
        assert a.stance.critique_intensity > 0.0

    def test_critic_risk_hard(self):
        signals = StanceSignals(
            pulse_available=True,
            risk_tag_count=4,
        )
        a = pulse_to_stance("critic", signals)
        assert a.level == 2

    def test_critic_dropout_only(self):
        signals = StanceSignals(
            coordinator_dropout_count=2,
        )
        a = pulse_to_stance("critic", signals)
        assert a.level == 1
        assert a.stance.provenance_requirement > 0.0

    def test_critic_risk_plus_dropout_l2(self):
        signals = StanceSignals(
            pulse_available=True,
            risk_tag_count=1,
            coordinator_dropout_count=1,
        )
        a = pulse_to_stance("critic", signals)
        assert a.level == 2

    def test_critic_open_loop_hard(self):
        signals = StanceSignals(
            pulse_available=True,
            open_loop_count=7,
        )
        a = pulse_to_stance("critic", signals)
        assert a.level == 2
        assert a.stance.retrieval_pressure >= 0.7

    def test_tester_stalled_soft(self):
        signals = StanceSignals(
            pulse_available=True,
            stalled_thread_count=3,
        )
        a = pulse_to_stance("tester", signals)
        assert a.level == 1
        assert a.stance.retrieval_pressure > 0.0

    def test_tester_stalled_hard(self):
        signals = StanceSignals(
            pulse_available=True,
            stalled_thread_count=5,
        )
        a = pulse_to_stance("tester", signals)
        assert a.level == 2

    def test_tester_analysis_stale(self):
        signals = StanceSignals(
            pulse_available=True,
            analysis_report_available=True,
            analysis_is_fresh=False,
        )
        a = pulse_to_stance("tester", signals)
        assert a.level == 1
        assert a.stance.provenance_requirement > 0.0

    def test_tester_stalled_plus_stale_l2(self):
        signals = StanceSignals(
            pulse_available=True,
            stalled_thread_count=2,
            analysis_report_available=True,
            analysis_is_fresh=False,
        )
        a = pulse_to_stance("tester", signals)
        assert a.level == 2

    def test_tester_burst(self):
        signals = StanceSignals(
            coordinator_burst_count=1,
        )
        a = pulse_to_stance("tester", signals)
        assert a.level == 1
        assert a.stance.handoff_bias > 0.0

    def test_planner_role_concentration_soft(self):
        """Role concentration at soft threshold → L1 with non-zero stance vector."""
        signals = StanceSignals(coordinator_role_concentration_count=1)
        a = pulse_to_stance("planner", signals)
        assert a.level == 1
        assert a.stance.retrieval_pressure > 0.0

    def test_critic_open_loop_soft(self):
        """Open loop count at soft threshold → L1 with non-zero stance vector."""
        signals = StanceSignals(pulse_available=True, open_loop_count=3)
        a = pulse_to_stance("critic", signals)
        assert a.level == 1
        assert a.stance.critique_intensity > 0.0


# ---------------------------------------------------------------------------
# Advisory properties tests
# ---------------------------------------------------------------------------


class TestAdvisoryProperties:
    def test_missing_inputs_degraded_mode(self):
        signals = StanceSignals(pulse_available=False)
        a = pulse_to_stance("planner", signals)
        assert len(a.missing_inputs) > 0
        assert "volatility_ratio" in a.missing_inputs

    def test_missing_inputs_full_mode(self):
        signals = StanceSignals(pulse_available=True)
        a = pulse_to_stance("planner", signals)
        assert a.missing_inputs == []

    def test_threshold_crossings_populated(self):
        signals = StanceSignals(
            pulse_available=True,
            volatility_ratio=0.6,
        )
        a = pulse_to_stance("planner", signals)
        assert len(a.threshold_crossings) > 0
        assert any("SOFT" in c for c in a.threshold_crossings)

    def test_l0_no_threshold_crossings(self):
        signals = StanceSignals()
        a = pulse_to_stance("planner", signals)
        assert a.level == 0
        # May still have empty crossings list — that's fine

    def test_actions_are_read_only(self):
        """All advisory actions must use read/query tools only."""
        # Test all roles at elevated levels
        for snapshot_fn, findings in [
            (_elevated_snapshot, []),
            (
                None,
                [
                    {"category": "stalled_open_loop"},
                    {"category": "stalled_open_loop"},
                    {"category": "stalled_open_loop"},
                    {"category": "aware_role_concentration"},
                    {"category": "aware_role_concentration"},
                    {"category": "stalled_dropout"},
                    {"category": "aware_burst"},
                ],
            ),
        ]:
            snap = snapshot_fn() if snapshot_fn else None
            advisories = build_stance_advisories(snap, coordinator_findings=findings)
            for a in advisories:
                for action in a.actions:
                    assert (
                        action.tool in _READ_ONLY_TOOLS
                    ), f"Non-read-only tool {action.tool!r} in {a.role} advisory"

    def test_advisory_action_invalid_tool_raises(self):
        import pytest

        with pytest.raises(ValueError, match="_READ_ONLY_TOOLS"):
            AdvisoryAction(phase="pre", tool="watercooler_say", arguments={})

    def test_advisory_serialization_roundtrip(self):
        signals = StanceSignals(
            pulse_available=True,
            volatility_ratio=0.6,
        )
        a = pulse_to_stance("planner", signals)
        d = asdict(a)
        # Verify key fields survive serialization
        assert d["schema_version"] == 1
        assert d["role"] == "planner"
        assert d["level"] == a.level
        assert isinstance(d["stance"]["retrieval_pressure"], float)
        assert isinstance(d["actions"], list)


# ---------------------------------------------------------------------------
# Advisory signature tests
# ---------------------------------------------------------------------------


class TestAdvisorySignature:
    def test_stable_when_inputs_unchanged(self):
        signals = StanceSignals(
            pulse_available=True,
            volatility_ratio=0.6,
        )
        a1 = pulse_to_stance("planner", signals)
        a2 = pulse_to_stance("planner", signals)
        assert a1.advisory_signature == a2.advisory_signature

    def test_changes_on_level_change(self):
        s1 = StanceSignals(pulse_available=True, volatility_ratio=0.55)
        s2 = StanceSignals(pulse_available=True, volatility_ratio=0.75)
        a1 = pulse_to_stance("planner", s1)
        a2 = pulse_to_stance("planner", s2)
        assert a1.level != a2.level
        assert a1.advisory_signature != a2.advisory_signature

    def test_changes_on_degraded_full_transition(self):
        """Same level/signals but degraded→full should change signature."""
        coord_findings = [
            {"category": "stalled_open_loop"},
            {"category": "stalled_open_loop"},
            {"category": "stalled_open_loop"},
        ]
        s_degraded = extract_stance_signals(
            None,
            coordinator_findings=coord_findings,
        )
        s_full = extract_stance_signals(
            _minimal_snapshot(),
            coordinator_findings=coord_findings,
        )
        a_deg = pulse_to_stance("planner", s_degraded)
        a_full = pulse_to_stance("planner", s_full)
        # Both should be L1 from coordinator signals
        assert a_deg.level >= 1
        assert a_full.level >= 1
        # Signatures differ due to pulse_available
        assert a_deg.advisory_signature != a_full.advisory_signature

    def test_different_roles_different_signatures(self):
        signals = StanceSignals(
            pulse_available=True,
            volatility_ratio=0.6,
            risk_tag_count=2,
            stalled_thread_count=3,
        )
        sigs = set()
        for role in ("planner", "critic", "tester"):
            a = pulse_to_stance(role, signals)
            sigs.add(a.advisory_signature)
        assert len(sigs) == 3

    def test_changes_when_triggered_signals_change(self):
        s1 = StanceSignals(
            coordinator_stalled_open_loop_count=3,
        )
        s2 = StanceSignals(
            coordinator_stalled_open_loop_count=3,
            coordinator_role_concentration_count=2,
        )
        a1 = pulse_to_stance("planner", s1)
        a2 = pulse_to_stance("planner", s2)
        assert a1.advisory_signature != a2.advisory_signature

    def test_cross_role_signal_does_not_change_signature(self):
        """Critic-only signals must not change planner signature (cross-role contamination fix)."""
        # risk_tag_count is a critic-only signal — planner never reads it
        s1 = StanceSignals(pulse_available=True, volatility_ratio=0.55)
        s2 = StanceSignals(
            pulse_available=True, volatility_ratio=0.55, risk_tag_count=5
        )
        a1 = pulse_to_stance("planner", s1)
        a2 = pulse_to_stance("planner", s2)
        assert a1.level == a2.level  # same planner level
        assert (
            a1.advisory_signature == a2.advisory_signature
        ), "Planner signature must not change when critic-only risk_tag_count crosses threshold"

        # volatility_ratio is a planner-only signal — critic never reads it
        s3 = StanceSignals(pulse_available=True, risk_tag_count=3)
        s4 = StanceSignals(pulse_available=True, risk_tag_count=3, volatility_ratio=0.8)
        a3 = pulse_to_stance("critic", s3)
        a4 = pulse_to_stance("critic", s4)
        assert a3.level == a4.level  # same critic level
        assert (
            a3.advisory_signature == a4.advisory_signature
        ), "Critic signature must not change when planner-only volatility_ratio crosses threshold"


# ---------------------------------------------------------------------------
# build_stance_advisories integration tests
# ---------------------------------------------------------------------------


class TestBuildStanceAdvisories:
    def test_returns_three(self):
        advisories = build_stance_advisories(None)
        assert len(advisories) == 3

    def test_degraded_mode_no_exception(self):
        advisories = build_stance_advisories(None)
        for a in advisories:
            assert a.signal_values.pulse_available is False

    def test_empty_snapshot_degraded(self):
        advisories = build_stance_advisories({})
        for a in advisories:
            assert a.signal_values.pulse_available is False

    def test_elevated_snapshot_produces_l1_or_l2(self):
        advisories = build_stance_advisories(_elevated_snapshot())
        levels = {a.role: a.level for a in advisories}
        # At least one role should be elevated with this snapshot
        assert any(level > 0 for level in levels.values())


# ---------------------------------------------------------------------------
# Severity mapping
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# P2.1 — new contributor corpus signal transport
# ---------------------------------------------------------------------------


class TestNewContributorStance:
    def test_planner_l1_on_new_contributor(self):
        """2.1.d: new contributor count >= SOFT elevates planner and adds
        an aware_new_contributor action."""
        signals = StanceSignals(coordinator_new_contributor_count=1)
        a = pulse_to_stance("planner", signals)
        assert a.level >= 1
        assert "coordinator_new_contributor_count" in a.triggered_signals
        nc_actions = [
            act
            for act in a.actions
            if act.arguments.get("category") == "aware_new_contributor"
        ]
        assert len(nc_actions) == 1
        assert nc_actions[0].tool == "watercooler_daemon_findings"
        assert nc_actions[0].phase == "pre"

    def test_new_contributor_signal_not_in_missing_inputs_degraded(self):
        """2.1.e: in degraded mode, coordinator_new_contributor_count must
        NOT appear in missing_inputs — it's coordinator-derived, not pulse."""
        signals = extract_stance_signals(
            None,
            coordinator_findings=[{"category": "aware_new_contributor"}],
        )
        assert signals.pulse_available is False
        assert signals.coordinator_new_contributor_count == 1
        a = pulse_to_stance("planner", signals)
        assert "coordinator_new_contributor_count" not in a.missing_inputs

    def test_coarsen_crossings_includes_new_contributor_bucket(self):
        """2.1.f: advisory_signature must include the SOFT bucket for
        coordinator_new_contributor_count when elevated."""
        s0 = StanceSignals()
        s1 = StanceSignals(coordinator_new_contributor_count=1)
        a0 = pulse_to_stance("planner", s0)
        a1 = pulse_to_stance("planner", s1)
        assert a0.advisory_signature != a1.advisory_signature


# ---------------------------------------------------------------------------
# P2.2 — tester action routing
# ---------------------------------------------------------------------------


class TestTesterActionRouting:
    def test_burst_only_points_to_aware_burst(self):
        """2.2.a: burst alone → single action pointing at aware_burst."""
        signals = StanceSignals(coordinator_burst_count=1)
        a = pulse_to_stance("tester", signals)
        assert a.level >= 1
        cats = [act.arguments.get("category") for act in a.actions]
        assert cats == ["aware_burst"]

    def test_stalled_only_points_to_stalled_open_loop(self):
        """2.2.b: stalled alone → single action pointing at stalled_open_loop."""
        signals = StanceSignals(
            pulse_available=True,
            stalled_thread_count=3,
        )
        a = pulse_to_stance("tester", signals)
        assert a.level >= 1
        cats = [act.arguments.get("category") for act in a.actions]
        assert cats == ["stalled_open_loop"]

    def test_analysis_stale_routes_to_stalled_open_loop(self):
        """analysis_stale is a stalled-family signal for the tester — route
        via the stalled_open_loop action."""
        signals = StanceSignals(
            pulse_available=True,
            analysis_report_available=True,
            analysis_is_fresh=False,
        )
        a = pulse_to_stance("tester", signals)
        assert a.level >= 1
        cats = [act.arguments.get("category") for act in a.actions]
        assert cats == ["stalled_open_loop"]

    def test_both_signals_emit_both_actions(self):
        """2.2.c: burst + stalled → both actions appear."""
        signals = StanceSignals(
            pulse_available=True,
            coordinator_burst_count=1,
            stalled_thread_count=3,
        )
        a = pulse_to_stance("tester", signals)
        assert a.level >= 1
        cats = {act.arguments.get("category") for act in a.actions}
        assert cats == {"aware_burst", "stalled_open_loop"}

    def test_advisory_signature_unchanged_by_action_swap(self):
        """2.2.d: two runs with identical signals produce identical signatures,
        regardless of how actions are constructed."""
        signals = StanceSignals(
            pulse_available=True,
            coordinator_burst_count=1,
            stalled_thread_count=3,
        )
        a1 = pulse_to_stance("tester", signals)
        a2 = pulse_to_stance("tester", signals)
        assert a1.advisory_signature == a2.advisory_signature
        # And: changing only action routing (by flipping which branches run)
        # changes the signature via triggered_signals — verify burst-only vs
        # stalled-only are distinct signatures.
        a_burst = pulse_to_stance("tester", StanceSignals(coordinator_burst_count=1))
        a_stalled = pulse_to_stance(
            "tester",
            StanceSignals(pulse_available=True, stalled_thread_count=3),
        )
        assert a_burst.advisory_signature != a_stalled.advisory_signature


class TestSeverityMapping:
    def test_l1_info(self):
        signals = StanceSignals(
            pulse_available=True,
            volatility_ratio=0.55,
        )
        a = pulse_to_stance("planner", signals)
        assert a.level == 1
        # Severity is set by the coordinator, not the lib, but we verify
        # the level is correct for the coordinator to map

    def test_l2_is_higher(self):
        signals = StanceSignals(
            pulse_available=True,
            volatility_ratio=0.75,
        )
        a = pulse_to_stance("planner", signals)
        assert a.level == 2


# ---------------------------------------------------------------------------
# Phase 3c-2 — source_lead_ids field shape + signal→lead map parity
# ---------------------------------------------------------------------------


class TestSourceLeadIdsField:
    def test_stance_advisory_includes_source_lead_ids(self):
        """StanceAdvisory exposes source_lead_ids as a tuple[str, ...] defaulting to ()."""
        advisory = build_stance_advisories(None)[0]
        # Default populated by pulse_to_stance() when the coordinator does
        # not pre-enrich — lib stays stdlib-only and never invents IDs.
        assert isinstance(advisory.source_lead_ids, tuple)
        assert advisory.source_lead_ids == ()

        # Frozen dataclass: dataclasses.replace() is the enrichment path.
        from dataclasses import replace

        enriched = replace(advisory, source_lead_ids=("fid-a", "fid-b"))
        assert enriched.source_lead_ids == ("fid-a", "fid-b")
        # asdict() preserves tuples (dataclasses.asdict copies tuples as tuples).
        payload = asdict(enriched)
        assert payload["source_lead_ids"] == ("fid-a", "fid-b")

    def test_stance_signal_to_lead_categories_covers_all_coordinator_signals(self):
        """The v1A category map must cover every coordinator_*_count signal on StanceSignals.

        Guards against a signal being added to StanceSignals without a
        corresponding lead-category mapping — which would silently drop
        provenance for the new signal when advisories escalate on it.
        """
        from watercooler.pulse_stance_lib import (
            _STANCE_SIGNAL_TO_LEAD_CATEGORIES,
        )

        coordinator_fields = {
            name
            for name in StanceSignals.__dataclass_fields__
            if name.startswith("coordinator_") and name.endswith("_count")
        }
        mapped = set(_STANCE_SIGNAL_TO_LEAD_CATEGORIES.keys())
        assert coordinator_fields == mapped, (
            f"signal fields {sorted(coordinator_fields - mapped)} lack a "
            f"lead-category mapping; stale entries {sorted(mapped - coordinator_fields)}"
        )
        # Every mapped value is a non-empty frozenset of category strings
        for sig, cats in _STANCE_SIGNAL_TO_LEAD_CATEGORIES.items():
            assert isinstance(
                cats, frozenset
            ), f"{sig!r} must map to a frozenset for hashability"
            assert cats, f"{sig!r} maps to an empty set"
            assert all(isinstance(c, str) and c for c in cats)

    def test_trend_supersession_rate_wired_into_stance_signals(self):
        """extract_stance_signals threads trend_supersession_rate onto StanceSignals
        in both full-pulse and degraded modes."""
        # Degraded (snapshot=None): trend_supersession_rate should still land
        degraded = extract_stance_signals(None, trend_supersession_rate=0.42)
        assert degraded.pulse_available is False
        assert degraded.trend_supersession_rate == 0.42

        # None by default (no argument)
        default = extract_stance_signals(None)
        assert default.trend_supersession_rate is None

        # Full-pulse mode also carries the field through
        full = extract_stance_signals(_minimal_snapshot(), trend_supersession_rate=0.17)
        assert full.pulse_available is True
        assert full.trend_supersession_rate == 0.17
