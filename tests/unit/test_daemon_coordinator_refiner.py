"""Tests for CoordinatorRefinerDaemon (Phase 3d-2).

Covers the 27 test requirements from the addendum's Testing Contract:
- Refinement-shape tests (1-9)
- Cursor tests (10-16)
- Input-scope tests (17-19)
- Fail-open tests (20-24)
- Integration tests (25-27)

See ``dev_docs/plans/2026-04-21-feat-coordinator-refiner-daemon-design-addendum-plan.md``.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from watercooler.config_schema import CoordinatorRefinerConfig
from watercooler.pulse_stance_lib import AdvisoryAction, CoordinatorLead
from watercooler_mcp.daemons.coordinator_refiner import (
    CoordinatorRefinerDaemon,
    _MESSAGE_TARGET_CHARS,
    _derive_message,
    _parse_llm_response,
)
from watercooler_mcp.daemons.llm_client import DaemonLLMClient
from watercooler_mcp.daemons.state import (
    DaemonCheckpoint,
    Finding,
    acknowledge_finding,
    append_findings,
    load_findings,
)

# ----------------------------------------------------------------- #
# Fixtures / helpers
# ----------------------------------------------------------------- #


def _advisory_action(
    tool: str = "watercooler_read_thread",
    topic: str = "topic-a",
    reason: str = "review pending plan",
) -> AdvisoryAction:
    return AdvisoryAction(
        phase="pre",
        tool=tool,
        arguments={"topic": topic},
        reason=reason,
    )


def _lead_payload(
    *,
    source_category: str = "stalled_open_loop",
    source_topic: str = "topic-a",
    summary: str = "open plan without decision",
    tags: tuple[str, ...] = ("planner",),
    action: Optional[AdvisoryAction] = None,
    t2_context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build a serialized CoordinatorLead dict (as written by project_coordinator)."""
    lead = CoordinatorLead(
        schema_version=1,
        source_category=source_category,
        source_topic=source_topic,
        summary=summary,
        relevance_tags=tags,
        suggested_action=(
            action if action is not None else _advisory_action(topic=source_topic)
        ),
        t2_context=t2_context,
    )
    return asdict(lead)


def _raw_lead_finding(
    *,
    finding_id: str = "LEAD-000",
    source_category: str = "stalled_open_loop",
    source_topic: str = "topic-a",
    created_at: float = 1_000.0,
    lead_payload: Optional[dict[str, Any]] = None,
    acknowledged: bool = False,
) -> Finding:
    payload = (
        lead_payload
        if lead_payload is not None
        else _lead_payload(
            source_category=source_category,
            source_topic=source_topic,
        )
    )
    return Finding(
        finding_id=finding_id,
        daemon_name="project_coordinator",
        severity="info",
        category="coordinator_lead",
        topic=source_topic,
        message="coordinator lead",
        details={"lead": payload},
        created_at=created_at,
        acknowledged=acknowledged,
    )


def _ok_response(
    assessment: str = (
        "Thread holds an unresolved plan entry with no Decision or Closure. "
        "The stalled loop is the primary risk here."
    ),
    next_step: str = "Read the most recent plan entries and surface owner intent.",
) -> str:
    return json.dumps({"assessment": assessment, "recommended_next_step": next_step})


@pytest.fixture(autouse=True)
def _isolate_checkpoint():
    """Prevent tests from loading on-disk checkpoints or real hosted-mode state.

    Restores propagation on watercooler_mcp logger so pytest caplog can
    capture warnings (observability file handler sets propagate=False).
    """
    import logging as _logging
    ns_logger = _logging.getLogger("watercooler_mcp")
    _saved_propagate = ns_logger.propagate
    ns_logger.propagate = True
    fresh = DaemonCheckpoint(daemon_name="coordinator_refiner")
    with patch("watercooler_mcp.daemons.base.load_checkpoint", return_value=fresh), \
         patch("watercooler_mcp.daemons.hosted_data.is_daemon_hosted_mode", return_value=False):
        yield
    ns_logger.propagate = _saved_propagate


@pytest.fixture
def mock_llm():
    client = MagicMock(spec=DaemonLLMClient)
    client.is_available.return_value = True
    client.complete.return_value = _ok_response()
    return client


@pytest.fixture
def enabled_config():
    return CoordinatorRefinerConfig(enabled=True, max_leads_per_tick=5)


def _make_daemon(
    config: CoordinatorRefinerConfig,
    llm: DaemonLLMClient,
) -> CoordinatorRefinerDaemon:
    return CoordinatorRefinerDaemon(config=config, llm_client=llm)


# ----------------------------------------------------------------- #
# Helper-function sanity tests
# ----------------------------------------------------------------- #


class TestHelpers:
    def test_parse_llm_response_ok(self):
        result = _parse_llm_response(_ok_response("a", "b"))
        assert result == {"assessment": "a", "recommended_next_step": "b"}

    def test_parse_llm_response_strips_code_fence(self):
        wrapped = "```json\n" + _ok_response("x", "y") + "\n```"
        assert _parse_llm_response(wrapped) == {
            "assessment": "x",
            "recommended_next_step": "y",
        }

    def test_parse_llm_response_extracts_embedded_json(self):
        wrapped = "Prelude... " + _ok_response("p", "q") + " ...trail"
        assert _parse_llm_response(wrapped) == {
            "assessment": "p",
            "recommended_next_step": "q",
        }

    def test_parse_llm_response_none_on_bad_json(self):
        assert _parse_llm_response("not json at all") is None

    def test_parse_llm_response_none_on_missing_fields(self):
        assert _parse_llm_response(json.dumps({"assessment": "only"})) is None

    def test_parse_llm_response_none_on_empty_strings(self):
        assert (
            _parse_llm_response(
                json.dumps({"assessment": " ", "recommended_next_step": ""})
            )
            is None
        )

    def test_derive_message_truncates_long_assessment(self):
        long = "a" * 400 + ". Next sentence."
        msg = _derive_message(long)
        assert msg
        assert len(msg) <= _MESSAGE_TARGET_CHARS

    def test_derive_message_fallback_on_empty(self):
        assert _derive_message("   ") == "coordinator_lead refined"


# ----------------------------------------------------------------- #
# Refinement shape (tests 1-9)
# ----------------------------------------------------------------- #


class TestRefinementShape:
    """Covers testing-contract items 1–9."""

    def test_1_one_refined_finding_per_eligible_lead(self, enabled_config, mock_llm):
        raw = [
            _raw_lead_finding(finding_id=f"LEAD-{i:03d}", source_topic=f"t-{i}")
            for i in range(3)
        ]
        daemon = _make_daemon(enabled_config, mock_llm)
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=raw,
        ):
            findings = daemon.tick()
        assert len(findings) == 3
        assert {f.details["source_finding_id"] for f in findings} == {
            "LEAD-000",
            "LEAD-001",
            "LEAD-002",
        }

    def test_2_source_finding_id_matches(self, enabled_config, mock_llm):
        raw = _raw_lead_finding(finding_id="LEAD-XYZ")
        daemon = _make_daemon(enabled_config, mock_llm)
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=[raw],
        ):
            findings = daemon.tick()
        assert findings[0].details["source_finding_id"] == "LEAD-XYZ"

    def test_3_source_category_matches(self, enabled_config, mock_llm):
        raw = _raw_lead_finding(source_category="aware_burst")
        daemon = _make_daemon(enabled_config, mock_llm)
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=[raw],
        ):
            findings = daemon.tick()
        assert findings[0].details["source_category"] == "aware_burst"

    def test_4_topic_equals_source_topic(self, enabled_config, mock_llm):
        raw = _raw_lead_finding(source_topic="specific-thread")
        daemon = _make_daemon(enabled_config, mock_llm)
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=[raw],
        ):
            findings = daemon.tick()
        assert findings[0].topic == "specific-thread"
        assert findings[0].details["source_topic"] == "specific-thread"

    def test_5_suggested_action_passthrough_verbatim(self, enabled_config, mock_llm):
        action = _advisory_action(
            tool="watercooler_search",
            topic="topic-a",
            reason="find related decisions",
        )
        payload = _lead_payload(action=action)
        raw_action = payload["suggested_action"]
        raw = _raw_lead_finding(lead_payload=payload)
        daemon = _make_daemon(enabled_config, mock_llm)
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=[raw],
        ):
            findings = daemon.tick()
        assert findings[0].details["suggested_action"] == raw_action
        assert findings[0].details["suggested_action"] is not raw_action  # copy

    def test_6_source_t2_context_passthrough_when_present(
        self, enabled_config, mock_llm
    ):
        t2 = {
            "schema_version": 2,
            "analysis_stalled": True,
            "days_since_last": 9,
            "workflow_shape_name": "plan-without-decision",
            "recommendation_rule_ids": ["r1", "r2"],
        }
        payload = _lead_payload(t2_context=t2)
        raw = _raw_lead_finding(lead_payload=payload)
        daemon = _make_daemon(enabled_config, mock_llm)
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=[raw],
        ):
            findings = daemon.tick()
        assert findings[0].details["source_t2_context"] == t2

    def test_7_source_t2_context_key_present_when_absent(
        self, enabled_config, mock_llm
    ):
        payload = _lead_payload(t2_context=None)
        raw = _raw_lead_finding(lead_payload=payload)
        daemon = _make_daemon(enabled_config, mock_llm)
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=[raw],
        ):
            findings = daemon.tick()
        assert "source_t2_context" in findings[0].details
        assert findings[0].details["source_t2_context"] is None

    def test_8_severity_always_info(self, enabled_config, mock_llm):
        raw = _raw_lead_finding()
        daemon = _make_daemon(enabled_config, mock_llm)
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=[raw],
        ):
            findings = daemon.tick()
        assert findings[0].severity == "info"

    def test_9_message_non_empty_and_bounded(self, enabled_config):
        long_assessment = (
            "This thread is drifting for weeks with no decision entry posted. " * 12
        )
        llm = MagicMock(spec=DaemonLLMClient)
        llm.is_available.return_value = True
        llm.complete.return_value = _ok_response(
            assessment=long_assessment.strip(),
            next_step="Read recent entries.",
        )
        raw = _raw_lead_finding()
        daemon = _make_daemon(enabled_config, llm)
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=[raw],
        ):
            findings = daemon.tick()
        msg = findings[0].message
        assert msg
        assert 0 < len(msg) <= _MESSAGE_TARGET_CHARS


# ----------------------------------------------------------------- #
# Cursor lifecycle (tests 10-16)
# ----------------------------------------------------------------- #


class TestCursor:
    """Covers testing-contract items 10–16."""

    def test_10_cursor_appends_on_success(self, enabled_config, mock_llm):
        raw = _raw_lead_finding(finding_id="LEAD-AAA")
        daemon = _make_daemon(enabled_config, mock_llm)
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=[raw],
        ):
            daemon.tick()
        assert "LEAD-AAA" in daemon._get_refined_ids()

    def test_11_cursor_no_append_on_llm_unavailable(self, enabled_config):
        llm = MagicMock(spec=DaemonLLMClient)
        llm.is_available.return_value = False
        raw = _raw_lead_finding(finding_id="LEAD-BBB")
        daemon = _make_daemon(enabled_config, llm)
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=[raw],
        ):
            daemon.tick()
        assert daemon._get_refined_ids() == []

    def test_12_cursor_no_append_on_llm_raise(self, enabled_config):
        llm = MagicMock(spec=DaemonLLMClient)
        llm.is_available.return_value = True
        llm.complete.side_effect = RuntimeError("boom")
        raw = _raw_lead_finding(finding_id="LEAD-CCC")
        daemon = _make_daemon(enabled_config, llm)
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=[raw],
        ):
            findings = daemon.tick()
        assert findings == []
        assert daemon._get_refined_ids() == []

    def test_13_cursor_no_append_on_parse_failure(self, enabled_config):
        llm = MagicMock(spec=DaemonLLMClient)
        llm.is_available.return_value = True
        llm.complete.return_value = "sorry, no json here"
        raw = _raw_lead_finding(finding_id="LEAD-DDD")
        daemon = _make_daemon(enabled_config, llm)
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=[raw],
        ):
            findings = daemon.tick()
        assert findings == []
        assert daemon._get_refined_ids() == []

    def test_14_cursor_advances_on_malformed_payload(self, enabled_config, mock_llm):
        malformed = Finding(
            finding_id="LEAD-MAL",
            daemon_name="project_coordinator",
            severity="info",
            category="coordinator_lead",
            topic="topic-a",
            details={"wrong": "shape"},
            created_at=1_000.0,
        )
        daemon = _make_daemon(enabled_config, mock_llm)
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=[malformed],
        ):
            findings = daemon.tick()
        assert findings == []
        assert "LEAD-MAL" in daemon._get_refined_ids()

    def test_15_cursor_gc_prunes_stale_ids(self, mock_llm):
        cfg = CoordinatorRefinerConfig(enabled=True, cursor_gc_interval=1)
        daemon = _make_daemon(cfg, mock_llm)
        daemon._set_refined_ids(["LIVE-1", "LIVE-2", "STALE-1", "STALE-2"])
        live = [
            _raw_lead_finding(finding_id="LIVE-1"),
            _raw_lead_finding(finding_id="LIVE-2"),
        ]
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=live,
        ):
            daemon.tick()
        refined = daemon._get_refined_ids()
        assert "STALE-1" not in refined
        assert "STALE-2" not in refined
        assert "LIVE-1" in refined and "LIVE-2" in refined

    def test_16_second_tick_does_not_re_refine(self, enabled_config, mock_llm):
        raw = [
            _raw_lead_finding(finding_id="L1", source_topic="t1"),
            _raw_lead_finding(finding_id="L2", source_topic="t2"),
        ]
        daemon = _make_daemon(enabled_config, mock_llm)
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=raw,
        ):
            tick1 = daemon.tick()
            tick2 = daemon.tick()
        assert len(tick1) == 2
        assert tick2 == []


# ----------------------------------------------------------------- #
# Input scope (tests 17-19)
# ----------------------------------------------------------------- #


class TestInputScope:
    """Covers testing-contract items 17–19."""

    def test_17_all_categories_eligible(self, enabled_config, mock_llm):
        raw = [
            _raw_lead_finding(
                finding_id=f"L-{cat}",
                source_category=cat,
                source_topic=f"topic-{cat}",
            )
            for cat in (
                "stalled_open_loop",
                "aware_burst",
                "aware_role_concentration",
                "stalled_dropout",
                "connect_role_complement",
            )
        ]
        daemon = _make_daemon(enabled_config, mock_llm)
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=raw,
        ):
            findings = daemon.tick()
        assert len(findings) == 5
        assert {f.details["source_category"] for f in findings} == {
            "stalled_open_loop",
            "aware_burst",
            "aware_role_concentration",
            "stalled_dropout",
            "connect_role_complement",
        }

    def test_18_disabled_by_default_returns_empty(self, mock_llm):
        cfg = CoordinatorRefinerConfig()  # enabled=False by default
        assert cfg.enabled is False
        daemon = _make_daemon(cfg, mock_llm)
        raw = _raw_lead_finding()
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=[raw],
        ) as m_load:
            findings = daemon.tick()
        assert findings == []
        # When disabled, we must short-circuit BEFORE loading findings.
        m_load.assert_not_called()

    def test_19_max_leads_per_tick_cap(self, mock_llm):
        cfg = CoordinatorRefinerConfig(enabled=True, max_leads_per_tick=2)
        raw = [
            _raw_lead_finding(
                finding_id=f"L{i:02d}",
                source_topic=f"t-{i}",
                created_at=1_000.0 + i,
            )
            for i in range(6)
        ]
        daemon = _make_daemon(cfg, mock_llm)
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=raw,
        ):
            findings = daemon.tick()
        assert len(findings) == 2
        # Oldest two by created_at go first (deterministic ordering).
        assert [f.details["source_finding_id"] for f in findings] == ["L00", "L01"]


# ----------------------------------------------------------------- #
# Fail-open (tests 20-24)
# ----------------------------------------------------------------- #


class TestFailOpen:
    """Covers testing-contract items 20–24."""

    def test_20_llm_unavailable_returns_empty(self, enabled_config):
        llm = MagicMock(spec=DaemonLLMClient)
        llm.is_available.return_value = False
        daemon = _make_daemon(enabled_config, llm)
        raw = _raw_lead_finding()
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=[raw],
        ):
            findings = daemon.tick()  # must not raise
        assert findings == []

    def test_21_llm_raise_returns_partial_and_does_not_raise(self, enabled_config):
        llm = MagicMock(spec=DaemonLLMClient)
        llm.is_available.return_value = True
        # First call raises, second succeeds
        llm.complete.side_effect = [RuntimeError("boom"), _ok_response()]
        raw = [
            _raw_lead_finding(finding_id="L1", source_topic="t1", created_at=1_000.0),
            _raw_lead_finding(finding_id="L2", source_topic="t2", created_at=1_001.0),
        ]
        daemon = _make_daemon(enabled_config, llm)
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=raw,
        ):
            findings = daemon.tick()  # must not raise
        assert len(findings) == 1
        assert findings[0].details["source_finding_id"] == "L2"
        # Cursor only advances for the success.
        assert "L2" in daemon._get_refined_ids()
        assert "L1" not in daemon._get_refined_ids()

    def test_22_parse_failure_returns_partial_logs_warning(
        self, enabled_config, caplog
    ):
        llm = MagicMock(spec=DaemonLLMClient)
        llm.is_available.return_value = True
        llm.complete.side_effect = ["junk output", _ok_response()]
        raw = [
            _raw_lead_finding(finding_id="L1", source_topic="t1", created_at=1_000.0),
            _raw_lead_finding(finding_id="L2", source_topic="t2", created_at=1_001.0),
        ]
        daemon = _make_daemon(enabled_config, llm)
        with caplog.at_level(
            "WARNING", logger="watercooler_mcp.daemons.coordinator_refiner"
        ):
            with patch(
                "watercooler_mcp.daemons.coordinator_refiner.load_findings",
                return_value=raw,
            ):
                findings = daemon.tick()
        assert len(findings) == 1
        assert any("parse failure" in rec.message for rec in caplog.records)

    def test_23_empty_findings_returns_empty(self, enabled_config, mock_llm):
        daemon = _make_daemon(enabled_config, mock_llm)
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=[],
        ):
            findings = daemon.tick()  # must not raise
        assert findings == []

    def test_24_malformed_payload_permanent_skip(self, enabled_config, mock_llm):
        good = _raw_lead_finding(
            finding_id="GOOD", source_topic="tg", created_at=1_001.0
        )
        malformed = Finding(
            finding_id="BAD",
            daemon_name="project_coordinator",
            severity="info",
            category="coordinator_lead",
            topic="tb",
            details={"lead": "this-should-be-a-dict"},
            created_at=1_000.0,
        )
        daemon = _make_daemon(enabled_config, mock_llm)
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=[malformed, good],
        ):
            findings = daemon.tick()
        assert len(findings) == 1
        assert findings[0].details["source_finding_id"] == "GOOD"
        # Both advance: GOOD succeeded, BAD permanent-skipped.
        refined = daemon._get_refined_ids()
        assert "GOOD" in refined
        assert "BAD" in refined

    def test_24b_non_string_summary_does_not_wedge_cursor(self, enabled_config, mock_llm):
        """A list (or other non-str) in summary must not cause an infinite retry.

        CoordinatorLead.from_dict() does no type validation on summary, so a
        stored list would previously cause TypeError in _build_prompt → caught
        by tick()'s bare except → advance=False → retried forever.
        """
        non_str_summary_payload = _lead_payload(source_topic="wedge-topic")
        non_str_summary_payload["summary"] = ["not", "a", "string"]
        raw = _raw_lead_finding(
            finding_id="LIST-SUMMARY",
            source_topic="wedge-topic",
            lead_payload=non_str_summary_payload,
        )
        daemon = _make_daemon(enabled_config, mock_llm)
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=[raw],
        ):
            findings = daemon.tick()
        # LLM must have been called (no TypeError in prompt construction).
        mock_llm.complete.assert_called_once()
        # Cursor must advance so this lead isn't retried indefinitely.
        assert "LIST-SUMMARY" in daemon._get_refined_ids()
        # A finding is emitted (the str()-coerced summary is valid LLM input).
        assert len(findings) == 1


# ----------------------------------------------------------------- #
# Integration (tests 25-27)
# ----------------------------------------------------------------- #


class TestIntegration:
    """Covers testing-contract items 25–27 — end-to-end via state.py storage."""

    @pytest.fixture
    def isolated_daemons_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR",
            tmp_path / "daemons",
        )
        return tmp_path / "daemons"

    def test_25_refined_findings_queryable(
        self, enabled_config, mock_llm, isolated_daemons_dir
    ):
        raw = [
            _raw_lead_finding(finding_id="LEAD-INT-1", source_topic="int-1"),
            _raw_lead_finding(finding_id="LEAD-INT-2", source_topic="int-2"),
        ]
        # Seed project_coordinator findings on disk so the daemon can load them.
        append_findings("project_coordinator", raw)

        daemon = _make_daemon(enabled_config, mock_llm)
        findings = daemon.tick()
        assert len(findings) == 2

        # Persist the refined findings — in production BaseDaemon.run_once writes
        # these via append_findings after tick() returns.
        append_findings("coordinator_refiner", findings)

        loaded = load_findings(
            "coordinator_refiner", category="refined_coordinator_lead"
        )
        assert len(loaded) == 2
        assert {f.daemon_name for f in loaded} == {"coordinator_refiner"}
        assert {f.details["source_finding_id"] for f in loaded} == {
            "LEAD-INT-1",
            "LEAD-INT-2",
        }

    def test_26_ack_raw_lead_does_not_ack_refined(
        self, enabled_config, mock_llm, isolated_daemons_dir
    ):
        raw = _raw_lead_finding(finding_id="LEAD-ACK", source_topic="ack-topic")
        append_findings("project_coordinator", [raw])

        daemon = _make_daemon(enabled_config, mock_llm)
        findings = daemon.tick()
        append_findings("coordinator_refiner", findings)

        # Acknowledge the raw lead.
        assert acknowledge_finding("project_coordinator", "LEAD-ACK") is True

        # Raw lead is now ack'd; refined finding must remain unacked.
        refined_before = load_findings(
            "coordinator_refiner",
            unacknowledged_only=True,
        )
        assert len(refined_before) == 1
        assert refined_before[0].acknowledged is False
        assert refined_before[0].details["source_finding_id"] == "LEAD-ACK"

        # And of course acking the refined finding directly still works.
        refined_id = refined_before[0].finding_id
        assert acknowledge_finding("coordinator_refiner", refined_id) is True
        refined_after = load_findings(
            "coordinator_refiner",
            unacknowledged_only=True,
        )
        assert refined_after == []

    def test_27_query_by_daemon_name_returns_refined(
        self, enabled_config, mock_llm, isolated_daemons_dir
    ):
        raw = _raw_lead_finding(finding_id="LEAD-QRY", source_topic="q-topic")
        append_findings("project_coordinator", [raw])

        daemon = _make_daemon(enabled_config, mock_llm)
        findings = daemon.tick()
        append_findings("coordinator_refiner", findings)

        # Queries targeting project_coordinator must NOT see the refined output.
        pc = load_findings("project_coordinator")
        assert all(f.category == "coordinator_lead" for f in pc)
        # Queries targeting coordinator_refiner must return the refined finding.
        cr = load_findings("coordinator_refiner")
        assert len(cr) == 1
        assert cr[0].daemon_name == "coordinator_refiner"
        assert cr[0].category == "refined_coordinator_lead"


# ----------------------------------------------------------------- #
# Config schema sanity
# ----------------------------------------------------------------- #


class TestLoadScope:
    """Verify the daemon uses limit=None + order='oldest' to avoid backlog starvation."""

    def test_load_findings_called_with_no_limit_and_order_oldest(
        self, enabled_config, mock_llm
    ):
        """Daemon must pass limit=None so cursor filter runs on the full set."""
        daemon = _make_daemon(enabled_config, mock_llm)
        raw = _raw_lead_finding()
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=[raw],
        ) as m_load:
            daemon.tick()
        kwargs = m_load.call_args.kwargs
        assert kwargs.get("order") == "oldest", "must load oldest-first"
        assert kwargs.get("limit") is None, (
            "limit must be None — a hard ceiling applied before cursor filtering "
            "would permanently hide leads beyond the limit once earlier ones are refined."
        )

    def test_multi_tick_convergence(self, mock_llm, tmp_path, monkeypatch):
        """Regression: every lead must be reachable across multiple ticks.

        With a per-tick cap of 3 and 8 seeded leads, ticks 1-2 each refine 3
        and tick 3 refines the remaining 2. None should be permanently skipped.
        """
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR",
            tmp_path / "daemons",
        )
        raw = [
            _raw_lead_finding(
                finding_id=f"L{i:04d}",
                source_topic=f"t-{i}",
                created_at=1_000.0 + i,
            )
            for i in range(8)
        ]
        append_findings("project_coordinator", raw)

        cfg = CoordinatorRefinerConfig(enabled=True, max_leads_per_tick=3)
        daemon = _make_daemon(cfg, mock_llm)

        tick1 = daemon.tick()
        tick2 = daemon.tick()
        tick3 = daemon.tick()
        tick4 = daemon.tick()  # backlog exhausted — must return []

        all_refined = [f.details["source_finding_id"] for f in tick1 + tick2 + tick3]
        assert sorted(all_refined) == [f"L{i:04d}" for i in range(8)]
        assert len(tick4) == 0, "tick after full drain must return empty"


class TestLLMParams:
    """Verify llm_temperature and llm_timeout_seconds are wired through."""

    def test_complete_called_with_configured_temperature_and_timeout(
        self, mock_llm
    ):
        cfg = CoordinatorRefinerConfig(
            enabled=True,
            max_leads_per_tick=1,
            llm_temperature=0.17,
            llm_timeout_seconds=42,
        )
        raw = _raw_lead_finding()
        daemon = _make_daemon(cfg, mock_llm)
        with patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=[raw],
        ):
            daemon.tick()
        mock_llm.complete.assert_called_once()
        kwargs = mock_llm.complete.call_args.kwargs
        assert kwargs["temperature"] == 0.17
        assert kwargs["timeout"] == pytest.approx(42.0)
        assert kwargs["max_tokens"] == cfg.llm_max_tokens


class TestHostedModeRuns:
    """Refiner runs in hosted scopes now that ``HostedDaemonCoordinator``
    registers it alongside ``project_coordinator`` (PR 1 of the daemon
    routing remediation plan).  The previous ``is_daemon_hosted_mode``
    tick guard was removed because the hosted coordinator wires
    ``state_namespace`` correctly per scope.
    """

    def test_runs_when_hosted(self, enabled_config, mock_llm):
        raw = _raw_lead_finding()
        daemon = _make_daemon(enabled_config, mock_llm)
        with patch(
            "watercooler_mcp.daemons.hosted_data.is_daemon_hosted_mode", return_value=True
        ), patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=[raw],
        ):
            findings = daemon.tick()
        assert len(findings) == 1
        mock_llm.complete.assert_called_once()

    def test_runs_when_not_hosted(self, enabled_config, mock_llm):
        raw = _raw_lead_finding()
        daemon = _make_daemon(enabled_config, mock_llm)
        with patch(
            "watercooler_mcp.daemons.hosted_data.is_daemon_hosted_mode", return_value=False
        ), patch(
            "watercooler_mcp.daemons.coordinator_refiner.load_findings",
            return_value=[raw],
        ):
            findings = daemon.tick()
        assert len(findings) == 1


class TestConfigSchema:
    def test_defaults(self):
        cfg = CoordinatorRefinerConfig()
        assert cfg.enabled is False
        assert cfg.interval == 600.0
        assert cfg.max_leads_per_tick == 5
        assert cfg.cursor_gc_interval == 24
        assert cfg.llm_max_tokens == 512
        assert cfg.llm_temperature == 0.3
        assert cfg.llm_timeout_seconds == pytest.approx(30.0)

    def test_interval_bound(self):
        with pytest.raises(Exception):
            CoordinatorRefinerConfig(interval=30.0)

    def test_frozen(self):
        cfg = CoordinatorRefinerConfig()
        with pytest.raises(Exception):
            cfg.enabled = True  # type: ignore[misc]
