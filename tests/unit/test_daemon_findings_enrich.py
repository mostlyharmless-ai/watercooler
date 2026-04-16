"""Tests for _daemon_findings_impl enrichment_stats emission.

Covers:
- #295: enrich=True with no coordinator_lead findings still emits enrichment_stats
- Hosted no-leads case: skipped=3 (not 0) in hosted mode
- Invalid code_path with lead findings: enrichment_stats emitted with error=True
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from watercooler_mcp.tools.daemon import _daemon_findings_impl


def _make_ctx() -> MagicMock:
    return MagicMock()


def _make_runtime(findings: list) -> MagicMock:
    """Return a minimal DaemonManager mock whose get_all_findings returns findings."""
    runtime = MagicMock()
    # Not a HostedDaemonCoordinator — isinstance check must return False.
    runtime.__class__.__name__ = "DaemonManager"
    runtime.get_all_findings.return_value = findings
    runtime.get_daemon.return_value = None
    return runtime


class TestEnrichStatsWhenNoCoordinatorLeads:
    """enrichment_stats is emitted even when no coordinator_lead findings exist."""

    def test_enrich_true_no_coordinator_leads_emits_stats(self, tmp_path):
        """enrich=True with only non-lead findings → enrichment_stats present, attempted=0."""
        from watercooler_mcp.daemons.state import Finding

        hygiene = Finding(
            finding_id="fid-1",
            daemon_name="thread_auditor",
            severity="info",
            category="stale_thread",
            topic="some-topic",
        )
        runtime = _make_runtime([hygiene])

        with (
            patch(
                "watercooler_mcp.daemons.get_daemon_runtime",
                return_value=runtime,
            ),
            patch("watercooler_mcp.daemons.ensure_hosted_scope_for_current_context"),
        ):
            result = json.loads(
                _daemon_findings_impl(
                    _make_ctx(),
                    daemon="thread_auditor",
                    enrich=True,
                    code_path=str(tmp_path),
                )
            )

        assert "enrichment_stats" in result, (
            "enrichment_stats must be present when enrich=True even if no "
            "coordinator_lead findings exist"
        )
        stats = result["enrichment_stats"]
        assert stats["attempted"] == 0
        assert stats["succeeded"] == 0
        assert stats["skipped"] == 0
        assert stats["mode"] == "local"

    def test_enrich_false_no_stats(self, tmp_path):
        """enrich=False → enrichment_stats absent regardless of findings."""
        from watercooler_mcp.daemons.state import Finding

        hygiene = Finding(
            finding_id="fid-2",
            daemon_name="thread_auditor",
            severity="info",
            category="stale_thread",
            topic="some-topic",
        )
        runtime = _make_runtime([hygiene])

        with (
            patch(
                "watercooler_mcp.daemons.get_daemon_runtime",
                return_value=runtime,
            ),
            patch("watercooler_mcp.daemons.ensure_hosted_scope_for_current_context"),
        ):
            result = json.loads(
                _daemon_findings_impl(
                    _make_ctx(),
                    daemon="thread_auditor",
                    enrich=False,
                    code_path=str(tmp_path),
                )
            )

        assert "enrichment_stats" not in result

    def test_hosted_no_coordinator_leads_skipped_is_3(self, tmp_path):
        """Hosted + no coordinator_lead findings → skipped=3 (not 0)."""
        from watercooler_mcp.daemons.state import Finding
        from watercooler_mcp.daemons.hosted_coordinator import HostedDaemonCoordinator

        hygiene = Finding(
            finding_id="fid-3",
            daemon_name="thread_auditor",
            severity="info",
            category="stale_thread",
            topic="some-topic",
        )
        hosted = MagicMock(spec=HostedDaemonCoordinator)
        hosted.get_findings.return_value = [hygiene]

        with (
            patch(
                "watercooler_mcp.daemons.get_daemon_runtime",
                return_value=hosted,
            ),
            patch("watercooler_mcp.daemons.ensure_hosted_scope_for_current_context"),
        ):
            result = json.loads(
                _daemon_findings_impl(
                    _make_ctx(),
                    enrich=True,
                    code_path=str(tmp_path),
                )
            )

        assert "enrichment_stats" in result
        stats = result["enrichment_stats"]
        assert stats["attempted"] == 0
        assert stats["succeeded"] == 0
        assert stats["skipped"] == 3
        assert stats["mode"] == "hosted"


class TestEnrichStatsInvalidCodePath:
    """enrichment_stats is always emitted when enrich=True, even for invalid code_path."""

    def _make_lead_finding(self):
        from watercooler_mcp.daemons.state import Finding

        f = MagicMock(spec=Finding)
        f.to_dict.return_value = {
            "finding_id": "lead-1",
            "daemon_name": "project_coordinator",
            "category": "coordinator_lead",
            "topic": "t",
            "severity": "info",
        }
        return f

    def test_invalid_code_path_emits_error_stats(self):
        """code_path not a dir + coordinator_lead findings → enrichment_stats with error=True."""
        runtime = _make_runtime([])
        runtime.get_all_findings.return_value = [self._make_lead_finding()]

        with (
            patch(
                "watercooler_mcp.daemons.get_daemon_runtime",
                return_value=runtime,
            ),
            patch("watercooler_mcp.daemons.ensure_hosted_scope_for_current_context"),
        ):
            result = json.loads(
                _daemon_findings_impl(
                    _make_ctx(),
                    enrich=True,
                    code_path="/definitely/not/a/real/dir",
                )
            )

        assert (
            "enrichment_stats" in result
        ), "enrichment_stats must be present even when code_path is invalid"
        stats = result["enrichment_stats"]
        assert stats["attempted"] == 0
        assert stats["succeeded"] == 0
        assert stats["skipped"] == 0
        assert stats["mode"] == "local"
        assert stats.get("error") is True
