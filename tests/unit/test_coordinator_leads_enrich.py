"""Tests for coordinator_leads.enrich_leads — S1/S2/S3 enrichment overlays."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from watercooler_mcp.daemons.state import DaemonCheckpoint, Finding
from watercooler_mcp.tools.coordinator_leads import enrich_leads

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_lead(topic: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a serialized coordinator_lead Finding dict."""
    return Finding(
        finding_id="fid-test",
        daemon_name="project_coordinator",
        severity="info",
        category="coordinator_lead",
        topic=topic,
        details=details or {"lead": {"summary": f"Lead for {topic}"}},
    ).to_dict()


def _make_finding(
    daemon: str,
    category: str,
    topic: str,
    details: dict[str, Any] | None = None,
) -> Finding:
    return Finding(
        finding_id=f"fid-{topic}",
        daemon_name=daemon,
        severity="info",
        category=category,
        topic=topic,
        details=details or {},
    )


# ---------------------------------------------------------------------------
# S1 — ThreadAuditor hygiene overlay
# ---------------------------------------------------------------------------


class TestEnrichLeadsS1:
    def test_hygiene_tags_overlaid_on_coordinator_lead(self):
        lead = _make_lead("alpha")
        hygiene_findings = [
            _make_finding("thread_auditor", "missing_status", "alpha"),
            _make_finding("thread_auditor", "missing_entry_id", "alpha"),
        ]
        with (
            patch(
                "watercooler_mcp.tools.coordinator_leads.load_findings",
                side_effect=lambda daemon, **kw: (
                    hygiene_findings if daemon == "thread_auditor" else []
                ),
            ),
            patch(
                "watercooler_mcp.tools.coordinator_leads._load_dimension_scores",
                return_value=None,
            ),
        ):
            result, _stats = enrich_leads(
                [lead],
                namespace="",
                repo_key="test-key",
            )

        assert result[0]["hygiene_tags"] == ["missing_entry_id", "missing_status"]

    def test_hygiene_absent_when_no_auditor_findings(self):
        lead = _make_lead("beta")
        with (
            patch(
                "watercooler_mcp.tools.coordinator_leads.load_findings",
                return_value=[],
            ),
            patch(
                "watercooler_mcp.tools.coordinator_leads._load_dimension_scores",
                return_value=None,
            ),
        ):
            result, _stats = enrich_leads(
                [lead],
                namespace="",
                repo_key="test-key",
            )

        assert "hygiene_tags" not in result[0]

    def test_hygiene_absent_for_different_topic(self):
        lead = _make_lead("gamma")
        hygiene_findings = [
            _make_finding("thread_auditor", "missing_status", "other-topic"),
        ]
        with (
            patch(
                "watercooler_mcp.tools.coordinator_leads.load_findings",
                side_effect=lambda daemon, **kw: (
                    hygiene_findings if daemon == "thread_auditor" else []
                ),
            ),
            patch(
                "watercooler_mcp.tools.coordinator_leads._load_dimension_scores",
                return_value=None,
            ),
        ):
            result, _stats = enrich_leads(
                [lead],
                namespace="",
                repo_key="test-key",
            )

        assert "hygiene_tags" not in result[0]

    def test_s1_skipped_in_hosted_mode(self):
        from watercooler_mcp.daemons.hosted_coordinator import HostedDaemonCoordinator

        lead = _make_lead("delta")
        mock_hosted = MagicMock(spec=HostedDaemonCoordinator)
        with patch(
            "watercooler_mcp.tools.coordinator_leads.load_findings",
            return_value=[_make_finding("thread_auditor", "missing_status", "delta")],
        ) as mock_load:
            result, _stats = enrich_leads(
                [lead],
                namespace="u:repo",
                repo_key="test-key",
                runtime=mock_hosted,
            )

        # load_findings should NOT have been called for thread_auditor in hosted mode
        for call in mock_load.call_args_list:
            assert (
                call.args[0] != "thread_auditor"
            ), "S1 load_findings should be skipped in hosted mode"
        assert "hygiene_tags" not in result[0]

    def test_hygiene_uses_single_global_read(self):
        """load_findings is called once regardless of lead count."""
        leads = [_make_lead(f"topic-{i}") for i in range(5)]
        call_count: list[int] = [0]

        def _load(daemon, **kw):
            if daemon == "thread_auditor":
                call_count[0] += 1
            return []

        with (
            patch(
                "watercooler_mcp.tools.coordinator_leads.load_findings",
                side_effect=_load,
            ),
            patch(
                "watercooler_mcp.tools.coordinator_leads._load_dimension_scores",
                return_value=None,
            ),
        ):
            enrich_leads(
                leads,
                namespace="",
                repo_key="test-key",
            )

        assert call_count[0] == 1, "Expected exactly one global read for thread_auditor"


# ---------------------------------------------------------------------------
# S2 — Decision-candidate booster
# ---------------------------------------------------------------------------


class TestEnrichLeadsS2:
    def test_pending_decision_candidates_count(self):
        lead = _make_lead("proj-x")
        dec_findings = [
            _make_finding("decision_detector", "decision_candidate", "proj-x"),
            _make_finding("decision_detector", "decision_candidate", "proj-x"),
        ]
        with (
            patch(
                "watercooler_mcp.tools.coordinator_leads.load_findings",
                side_effect=lambda daemon, **kw: (
                    dec_findings if daemon == "decision_detector" else []
                ),
            ),
            patch(
                "watercooler_mcp.tools.coordinator_leads._load_dimension_scores",
                return_value=None,
            ),
        ):
            result, _stats = enrich_leads(
                [lead],
                namespace="",
                repo_key="test-key",
            )

        assert result[0]["pending_decision_candidates"] == 2

    def test_suggested_action_swapped_when_candidates_present(self):
        lead = _make_lead("proj-y")
        dec_findings = [
            _make_finding("decision_detector", "decision_candidate", "proj-y"),
        ]
        with (
            patch(
                "watercooler_mcp.tools.coordinator_leads.load_findings",
                side_effect=lambda daemon, **kw: (
                    dec_findings if daemon == "decision_detector" else []
                ),
            ),
            patch(
                "watercooler_mcp.tools.coordinator_leads._load_dimension_scores",
                return_value=None,
            ),
        ):
            result, _stats = enrich_leads(
                [lead],
                namespace="",
                repo_key="test-key",
            )

        override = result[0].get("suggested_action_override")
        assert override is not None
        assert override["tool"] == "watercooler_daemon_findings"
        # Must use AdvisoryAction-compatible schema (arguments, not params)
        assert "arguments" in override
        assert "params" not in override
        assert override["arguments"]["daemon"] == "decision_detector"
        assert override["arguments"]["topic"] == "proj-y"
        assert "phase" in override
        assert "reason" in override

    def test_no_override_when_no_candidates(self):
        lead = _make_lead("proj-z")
        with (
            patch(
                "watercooler_mcp.tools.coordinator_leads.load_findings",
                return_value=[],
            ),
            patch(
                "watercooler_mcp.tools.coordinator_leads._load_dimension_scores",
                return_value=None,
            ),
        ):
            result, _stats = enrich_leads(
                [lead],
                namespace="",
                repo_key="test-key",
            )

        assert "pending_decision_candidates" not in result[0]
        assert "suggested_action_override" not in result[0]

    def test_s2_skipped_in_hosted_mode(self):
        from watercooler_mcp.daemons.hosted_coordinator import HostedDaemonCoordinator

        lead = _make_lead("proj-hosted")
        mock_hosted = MagicMock(spec=HostedDaemonCoordinator)
        with patch(
            "watercooler_mcp.tools.coordinator_leads.load_findings",
            return_value=[
                _make_finding("decision_detector", "decision_candidate", "proj-hosted")
            ],
        ) as mock_load:
            result, _stats = enrich_leads(
                [lead],
                namespace="u:repo",
                repo_key="test-key",
                runtime=mock_hosted,
            )

        for call in mock_load.call_args_list:
            assert call.args[0] != "decision_detector"
        assert "pending_decision_candidates" not in result[0]

    def test_s2_uses_single_global_read(self):
        leads = [_make_lead(f"t{i}") for i in range(4)]
        dec_call_count: list[int] = [0]

        def _load(daemon, **kw):
            if daemon == "decision_detector":
                dec_call_count[0] += 1
            return []

        with (
            patch(
                "watercooler_mcp.tools.coordinator_leads.load_findings",
                side_effect=_load,
            ),
            patch(
                "watercooler_mcp.tools.coordinator_leads._load_dimension_scores",
                return_value=None,
            ),
        ):
            enrich_leads(
                leads,
                namespace="",
                repo_key="test-key",
            )

        assert dec_call_count[0] == 1


# ---------------------------------------------------------------------------
# S3 — PulseSnapshot dimension scores
# ---------------------------------------------------------------------------


class TestEnrichLeadsS3:
    _SCORES = {
        "goal_clarity": 0.8,
        "constraint_pressure": 0.4,
        "evidence_quality": 0.6,
        "execution_momentum": 0.7,
    }

    def test_pulse_context_overlaid_with_four_dimensions(self):
        lead = _make_lead("thread-a")
        with (
            patch(
                "watercooler_mcp.tools.coordinator_leads.load_findings",
                return_value=[],
            ),
            patch(
                "watercooler_mcp.tools.coordinator_leads._load_dimension_scores",
                return_value=self._SCORES,
            ),
        ):
            result, _stats = enrich_leads(
                [lead],
                namespace="",
                repo_key="test-repo-key",
            )

        ctx = result[0].get("pulse_context")
        assert ctx is not None
        for key in (
            "goal_clarity",
            "constraint_pressure",
            "evidence_quality",
            "execution_momentum",
        ):
            assert key in ctx

    def test_s3_absent_when_no_snapshot(self):
        lead = _make_lead("thread-b")
        with (
            patch(
                "watercooler_mcp.tools.coordinator_leads.load_findings",
                return_value=[],
            ),
            patch(
                "watercooler_mcp.tools.coordinator_leads._load_dimension_scores",
                return_value=None,
            ),
        ):
            result, _stats = enrich_leads(
                [lead],
                namespace="",
                repo_key="test-repo-key",
            )

        assert "pulse_context" not in result[0]

    def test_s3_checkpoint_fallback(self):
        """_load_dimension_scores path-2: reads from DaemonCheckpoint extras."""
        from watercooler_mcp.tools.coordinator_leads import _load_dimension_scores

        cp = DaemonCheckpoint(daemon_name="pulse_snapshot")
        cp.extras = {
            "projects": {
                "test-repo-key": {
                    "dimension_scores": self._SCORES,
                }
            }
        }

        # Pass runtime=None to force Path 2 (cross-process checkpoint fallback).
        # Patch at the resolve site in pulse_snapshot (where load_checkpoint is imported).
        with patch(
            "watercooler_mcp.daemons.pulse_snapshot.load_checkpoint",
            return_value=cp,
        ):
            scores = _load_dimension_scores(None, "", "test-repo-key")

        assert scores is not None
        assert scores["goal_clarity"] == 0.8

    def test_s3_path1_falls_through_to_path2_when_none(self):
        """Path 1 None return should fall through to Path 2 checkpoint."""
        from watercooler_mcp.tools.coordinator_leads import _load_dimension_scores
        from watercooler_mcp.daemons.pulse_snapshot import PulseSnapshotDaemon

        cp = DaemonCheckpoint(daemon_name="pulse_snapshot")
        cp.extras = {"projects": {"test-key": {"dimension_scores": self._SCORES}}}

        mock_ps = MagicMock(spec=PulseSnapshotDaemon)
        mock_ps.get_dimension_scores.return_value = None  # daemon hasn't ticked yet

        mock_manager = MagicMock()
        mock_manager.get_daemon.return_value = mock_ps

        # Patch at the resolve site in pulse_snapshot (where load_checkpoint is imported).
        with patch(
            "watercooler_mcp.daemons.pulse_snapshot.load_checkpoint",
            return_value=cp,
        ):
            scores = _load_dimension_scores(mock_manager, "", "test-key")

        # Must fall through to checkpoint even though daemon exists
        assert scores is not None
        assert scores["goal_clarity"] == 0.8

    def test_pulse_context_omits_none_dimension_values(self):
        """Keys missing from dimension_scores are absent from pulse_context."""
        lead = _make_lead("thread-partial")
        partial_scores = {"goal_clarity": 0.9}  # only one dimension present
        with (
            patch(
                "watercooler_mcp.tools.coordinator_leads.load_findings",
                return_value=[],
            ),
            patch(
                "watercooler_mcp.tools.coordinator_leads._load_dimension_scores",
                return_value=partial_scores,
            ),
        ):
            result, _stats = enrich_leads(
                [lead],
                namespace="",
                repo_key="test-key",
            )

        ctx = result[0].get("pulse_context")
        assert ctx is not None
        assert "goal_clarity" in ctx
        # Keys not in the scores dict must be absent (not present as None)
        assert "constraint_pressure" not in ctx
        assert "evidence_quality" not in ctx
        assert "execution_momentum" not in ctx

    def test_s3_skipped_in_hosted_mode(self):
        from watercooler_mcp.daemons.hosted_coordinator import HostedDaemonCoordinator

        lead = _make_lead("thread-d")
        mock_hosted = MagicMock(spec=HostedDaemonCoordinator)

        with (
            patch(
                "watercooler_mcp.tools.coordinator_leads.load_findings",
                return_value=[],
            ),
            patch(
                "watercooler_mcp.tools.coordinator_leads._load_dimension_scores",
            ) as mock_s3,
        ):
            result, _stats = enrich_leads(
                [lead],
                namespace="u:repo",
                repo_key="test-key",
                runtime=mock_hosted,
            )

        # _load_dimension_scores should NOT have been called in hosted mode
        mock_s3.assert_not_called()
        assert "pulse_context" not in result[0]


# ---------------------------------------------------------------------------
# Fail-open / isolation
# ---------------------------------------------------------------------------


class TestEnrichLeadsFailOpen:
    def test_s1_failure_does_not_abort_s3(self):
        """An S1 load error leaves the lead intact; S3 can still overlay."""
        lead = _make_lead("thread-e")

        def _load(daemon, **kw):
            if daemon == "thread_auditor":
                raise OSError("read error")
            return []

        with (
            patch(
                "watercooler_mcp.tools.coordinator_leads.load_findings",
                side_effect=_load,
            ),
            patch(
                "watercooler_mcp.tools.coordinator_leads._load_dimension_scores",
                return_value={"execution_momentum": 0.5},
            ),
        ):
            result, _stats = enrich_leads(
                [lead],
                namespace="",
                repo_key="rk",
            )

        assert "hygiene_tags" not in result[0]
        # S3 still ran
        assert result[0]["pulse_context"]["execution_momentum"] == 0.5

    def test_non_coordinator_lead_findings_passed_through_unchanged(self):
        other = Finding(
            finding_id="fid-other",
            daemon_name="thread_auditor",
            severity="warning",
            category="missing_status",
            topic="some-thread",
        ).to_dict()
        lead = _make_lead("some-thread")

        with (
            patch(
                "watercooler_mcp.tools.coordinator_leads.load_findings",
                return_value=[],
            ),
            patch(
                "watercooler_mcp.tools.coordinator_leads._load_dimension_scores",
                return_value=None,
            ),
        ):
            result, _stats = enrich_leads(
                [other, lead],
                namespace="",
                repo_key="rk",
            )

        # Both findings present, non-lead is unchanged
        categories = [r["category"] for r in result]
        assert "missing_status" in categories
        assert "coordinator_lead" in categories
        other_result = next(r for r in result if r["category"] == "missing_status")
        assert other_result == other

    def test_original_ordering_preserved_for_mixed_results(self):
        """coordinator_lead findings must not be moved to the end of the list."""
        lead = _make_lead("the-topic")
        other = Finding(
            finding_id="fid-other",
            daemon_name="thread_auditor",
            severity="info",
            category="stale_thread",
            topic="the-topic",
        ).to_dict()

        # Input order: [lead, other]
        with (
            patch(
                "watercooler_mcp.tools.coordinator_leads.load_findings",
                return_value=[],
            ),
            patch(
                "watercooler_mcp.tools.coordinator_leads._load_dimension_scores",
                return_value=None,
            ),
        ):
            result, _stats = enrich_leads(
                [lead, other],
                namespace="",
                repo_key="rk",
            )

        # Order must be preserved: coordinator_lead first, then stale_thread
        assert result[0]["category"] == "coordinator_lead"
        assert result[1]["category"] == "stale_thread"

    def test_no_coordinator_leads_returns_input_unchanged(self):
        """When no coordinator_lead findings are present, returns input unchanged."""
        other = Finding(
            finding_id="fid-x",
            daemon_name="thread_auditor",
            severity="info",
            category="stale_thread",
            topic="t",
        ).to_dict()

        result, _stats = enrich_leads(
            [other],
            namespace="",
            repo_key="rk",
        )

        # No coordinator_leads present — result unchanged
        assert result == [other]

    def test_empty_input_returns_empty(self):
        result, _stats = enrich_leads([], namespace="", repo_key="rk")
        assert result == []


# ---------------------------------------------------------------------------
# enrichment_stats (#292)
# ---------------------------------------------------------------------------


class TestEnrichLeadsStats:
    def test_stats_s1_only_has_data(self):
        """S1 has data, S2/S3 absent → attempted=3, succeeded=1, skipped=0, mode=local."""
        lead = _make_lead("topic-stats")
        hygiene_findings = [
            _make_finding("thread_auditor", "missing_status", "topic-stats")
        ]
        with (
            patch(
                "watercooler_mcp.tools.coordinator_leads.load_findings",
                side_effect=lambda daemon, **kw: (
                    hygiene_findings if daemon == "thread_auditor" else []
                ),
            ),
            patch(
                "watercooler_mcp.tools.coordinator_leads._load_dimension_scores",
                return_value=None,
            ),
        ):
            _, stats = enrich_leads([lead], namespace="", repo_key="rk")

        assert stats["attempted"] == 3
        assert stats["succeeded"] == 1  # only S1 had data
        assert stats["skipped"] == 0
        assert stats["mode"] == "local"

    def test_stats_all_signals_unavailable(self):
        """No signals return data → attempted=3, succeeded=0, skipped=0, mode=local."""
        lead = _make_lead("topic-empty")
        with (
            patch(
                "watercooler_mcp.tools.coordinator_leads.load_findings",
                return_value=[],
            ),
            patch(
                "watercooler_mcp.tools.coordinator_leads._load_dimension_scores",
                return_value=None,
            ),
        ):
            _, stats = enrich_leads([lead], namespace="", repo_key="rk")

        assert stats["attempted"] == 3
        assert stats["succeeded"] == 0
        assert stats["skipped"] == 0
        assert stats["mode"] == "local"

    def test_stats_hosted_mode_skips_all(self):
        """Hosted mode → skipped=3, attempted=0, succeeded=0, mode=hosted."""
        from watercooler_mcp.daemons.hosted_coordinator import HostedDaemonCoordinator

        lead = _make_lead("topic-hosted")
        mock_hosted = MagicMock(spec=HostedDaemonCoordinator)
        with patch(
            "watercooler_mcp.tools.coordinator_leads.load_findings",
            return_value=[],
        ):
            _, stats = enrich_leads(
                [lead],
                namespace="u:repo",
                repo_key="rk",
                runtime=mock_hosted,
            )

        assert stats["attempted"] == 0
        assert stats["succeeded"] == 0
        assert stats["skipped"] == 3
        assert stats["mode"] == "hosted"

    def test_stats_no_coordinator_leads_in_input(self):
        """No coordinator_leads → attempted=0, skipped=0, mode=local (early return)."""
        other = Finding(
            finding_id="fid-x",
            daemon_name="thread_auditor",
            severity="info",
            category="stale_thread",
            topic="t",
        ).to_dict()

        _, stats = enrich_leads([other], namespace="", repo_key="rk")

        assert stats["attempted"] == 0
        assert stats["succeeded"] == 0
        assert stats["skipped"] == 0
        assert stats["mode"] == "local"
