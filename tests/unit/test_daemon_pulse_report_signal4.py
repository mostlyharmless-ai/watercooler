"""Tests for PulseReportDaemon._load_coordinator_leads — Signal 4."""

from __future__ import annotations

import pytest

pytest.importorskip(
    "watercooler_mcp.daemons.pulse_report",
    reason="private daemon — not in open-core build",
)

from unittest.mock import MagicMock, patch

import pytest

from watercooler.config_schema import PulseReportConfig
from watercooler_mcp.daemons.pulse_report import (
    PulseReportDaemon,
    _COORD_LEADS_LOAD_CAP,
    _COORD_LEADS_REPORT_CAP,
)


@pytest.fixture(autouse=True)
def _isolate_daemon_state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path / "daemons"
    )


def _make_daemon() -> PulseReportDaemon:
    return PulseReportDaemon(config=PulseReportConfig(enabled=True))


def _make_finding_mock(
    topic: str = "topic-a",
    source_category: str = "stalled_open_loop",
) -> MagicMock:
    """Return a mock Finding whose to_dict() returns a serialised coordinator_lead dict."""
    f = MagicMock()
    f.to_dict.return_value = {
        "finding_id": f"fid-{topic}",
        "daemon_name": "project_coordinator",
        "category": "coordinator_lead",
        "topic": topic,
        "severity": "warning",
        "details": {
            "lead": {
                "source_category": source_category,
                "source_topic": topic,
                "summary": f"summary for {topic}",
                "relevance_tags": ["pm"],
                "t2_context": None,
            }
        },
    }
    return f


# ---------------------------------------------------------------------------
# Tests 9-11
# ---------------------------------------------------------------------------


def test_load_coordinator_leads_returns_none_in_hosted_mode(tmp_path):
    """Test 9: hosted mode → None returned immediately, no checkpoint or findings load."""
    daemon = _make_daemon()

    with (
        patch(
            "watercooler_mcp.daemons.pulse_report.load_checkpoint"
        ) as mock_cp,
        patch(
            "watercooler_mcp.daemons.pulse_report.load_findings"
        ) as mock_lf,
        patch(
            "watercooler_mcp.daemons.pulse_report.is_daemon_hosted_mode",
            return_value=True,
        ),
    ):
        result = daemon._load_coordinator_leads()

    assert result is None
    mock_cp.assert_not_called()
    mock_lf.assert_not_called()


def test_load_coordinator_leads_filters_to_active_signals(tmp_path):
    """Test 9b: active_signals checkpoint present → only matching topic+category pass."""
    from watercooler_mcp.daemons.state import DaemonCheckpoint

    daemon = _make_daemon()

    coord_cp = DaemonCheckpoint(
        daemon_name="project_coordinator",
        extras={
            "active_signals": {
                "topic-a": {
                    "categories": ["stalled_open_loop"],
                    "last_evaluated_at": 1000.0,
                },
                "topic-b": {
                    "categories": ["aware_burst"],
                    "last_evaluated_at": 1000.0,
                },
            }
        },
    )

    findings = [
        _make_finding_mock("topic-a", "stalled_open_loop"),  # active
        _make_finding_mock("topic-b", "aware_burst"),  # active
        _make_finding_mock("topic-stale", "stalled_open_loop"),  # topic not in active_signals
    ]

    with (
        patch(
            "watercooler_mcp.daemons.pulse_report.is_daemon_hosted_mode",
            return_value=False,
        ),
        patch(
            "watercooler_mcp.daemons.pulse_report.load_checkpoint",
            return_value=coord_cp,
        ),
        patch(
            "watercooler_mcp.daemons.pulse_report.load_findings",
            return_value=findings,
        ),
    ):
        result = daemon._load_coordinator_leads()

    assert result is not None
    assert len(result) == 2
    topics = {r["topic"] for r in result}
    assert topics == {"topic-a", "topic-b"}


def test_load_coordinator_leads_returns_empty_when_active_signals_key_absent(tmp_path):
    """Test 10: coordinator checkpoint has no active_signals key → return []."""
    from watercooler_mcp.daemons.state import DaemonCheckpoint

    daemon = _make_daemon()

    # Fresh checkpoint with empty extras — no "active_signals" key
    coord_cp = DaemonCheckpoint(daemon_name="project_coordinator", extras={})

    with (
        patch(
            "watercooler_mcp.daemons.pulse_report.is_daemon_hosted_mode",
            return_value=False,
        ),
        patch(
            "watercooler_mcp.daemons.pulse_report.load_checkpoint",
            return_value=coord_cp,
        ),
        patch(
            "watercooler_mcp.daemons.pulse_report.load_findings",
        ) as mock_lf,
    ):
        result = daemon._load_coordinator_leads()

    assert result == []
    mock_lf.assert_not_called()


def test_load_coordinator_leads_falls_back_unfiltered_when_checkpoint_raises(tmp_path):
    """Test 10b: checkpoint raises → active_filter=None → unfiltered fallback (conservative)."""
    daemon = _make_daemon()

    findings = [
        _make_finding_mock("topic-a", "stalled_open_loop"),
        _make_finding_mock("topic-b", "aware_burst"),
    ]

    with (
        patch(
            "watercooler_mcp.daemons.pulse_report.is_daemon_hosted_mode",
            return_value=False,
        ),
        patch(
            "watercooler_mcp.daemons.pulse_report.load_checkpoint",
            side_effect=OSError("disk failure"),
        ),
        patch(
            "watercooler_mcp.daemons.pulse_report.load_findings",
            return_value=findings,
        ),
    ):
        result = daemon._load_coordinator_leads()

    assert result is not None
    assert len(result) == 2  # both returned (unfiltered)


def test_load_coordinator_leads_category_aware_filter(tmp_path):
    """Test 10c: topic present but source_category not in active set → excluded.

    Note on 9b vs 10c split:
    9b exercises topic-not-in-active_signals (topic itself absent).
    10c exercises category-not-in-topic's-set (topic present, wrong category).
    Both branches must be tested — do not consolidate.
    """
    from watercooler_mcp.daemons.state import DaemonCheckpoint

    daemon = _make_daemon()

    coord_cp = DaemonCheckpoint(
        daemon_name="project_coordinator",
        extras={
            "active_signals": {
                "topic-a": {
                    "categories": ["stalled_open_loop"],
                    "last_evaluated_at": 1000.0,
                },
            }
        },
    )

    findings = [
        _make_finding_mock("topic-a", "stalled_open_loop"),  # active
        _make_finding_mock("topic-a", "aware_burst"),  # topic present, category inactive
        _make_finding_mock("topic-b", "stalled_open_loop"),  # topic absent
    ]

    with (
        patch(
            "watercooler_mcp.daemons.pulse_report.is_daemon_hosted_mode",
            return_value=False,
        ),
        patch(
            "watercooler_mcp.daemons.pulse_report.load_checkpoint",
            return_value=coord_cp,
        ),
        patch(
            "watercooler_mcp.daemons.pulse_report.load_findings",
            return_value=findings,
        ),
    ):
        result = daemon._load_coordinator_leads()

    assert result is not None
    assert len(result) == 1
    assert result[0]["details"]["lead"]["source_category"] == "stalled_open_loop"
    assert result[0]["topic"] == "topic-a"


def test_load_coordinator_leads_graceful_degradation(tmp_path):
    """Test 11: load_findings raises → method returns None (not re-raises)."""
    from watercooler_mcp.daemons.state import DaemonCheckpoint

    daemon = _make_daemon()

    coord_cp = DaemonCheckpoint(
        daemon_name="project_coordinator",
        extras={
            "active_signals": {
                "topic-a": {"categories": ["stalled_open_loop"], "last_evaluated_at": 1.0}
            }
        },
    )

    with (
        patch(
            "watercooler_mcp.daemons.pulse_report.is_daemon_hosted_mode",
            return_value=False,
        ),
        patch(
            "watercooler_mcp.daemons.pulse_report.load_checkpoint",
            return_value=coord_cp,
        ),
        patch(
            "watercooler_mcp.daemons.pulse_report.load_findings",
            side_effect=RuntimeError("JSONL corrupted"),
        ),
    ):
        result = daemon._load_coordinator_leads()

    assert result is None


def test_load_coordinator_leads_active_signals_empty_dict():
    """Test 12 — #324: active_signals={} (key present, empty dict) → all leads filtered → []."""
    from watercooler_mcp.daemons.state import DaemonCheckpoint

    daemon = _make_daemon()

    coord_cp = DaemonCheckpoint(
        daemon_name="project_coordinator",
        extras={"active_signals": {}},
    )

    with (
        patch(
            "watercooler_mcp.daemons.pulse_report.is_daemon_hosted_mode",
            return_value=False,
        ),
        patch(
            "watercooler_mcp.daemons.pulse_report.load_checkpoint",
            return_value=coord_cp,
        ),
        patch(
            "watercooler_mcp.daemons.pulse_report.load_findings",
            return_value=[],
        ),
    ):
        result = daemon._load_coordinator_leads()

    assert result == []
