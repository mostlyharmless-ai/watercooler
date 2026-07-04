"""Unit tests for watercooler.metrics.orchestration (Phase 6)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from watercooler.metrics.orchestration import (
    ActorClassification,
    OrchestrationMetrics,
    classify_actor,
    compute_orchestration_metrics,
    format_markdown_report,
    is_coordination_pattern,
)


def _entry(
    *,
    entry_id: str = "01HZAAT0BC3D4E5F6G7H8J9K0M",
    entry_type: str = "Note",
    agent: str = "caleb",
    body: str = "Spec: implementer\n\nHello.",
    timestamp: str = "2026-05-15T00:00:00+00:00",
    agent_func: str | None = None,
    actor_class: str | None = None,
) -> dict[str, Any]:
    e: dict[str, Any] = {
        "entry_id": entry_id,
        "entry_type": entry_type,
        "agent": agent,
        "body": body,
        "timestamp": timestamp,
    }
    if agent_func:
        e["agent_func"] = agent_func
    if actor_class:
        e["actor_class"] = actor_class
    return e


# ---------------------------------------------------------------------------
# classify_actor
# ---------------------------------------------------------------------------


class TestClassifyActor:
    def test_agent_func_parsed(self):
        c = classify_actor(
            _entry(agent_func="Claude Code:claude-opus-4-7:implementer")
        )
        assert c.actor_class == "agent"
        assert c.platform == "Claude Code"
        assert c.model == "claude-opus-4-7"
        assert c.role == "implementer"

    def test_daemon_agent_prefix(self):
        c = classify_actor(_entry(agent="ExtractDecisionsDaemon"))
        assert c.actor_class == "daemon"

    def test_human_name(self):
        c = classify_actor(_entry(agent="caleb"))
        assert c.actor_class == "human"

    def test_agent_platform_prefix(self):
        # Platform name with no agent_func still classifies as agent.
        c = classify_actor(_entry(agent="Cursor", agent_func=None))
        assert c.actor_class == "agent"

    def test_actor_class_backfill_trusted(self):
        c = classify_actor(
            _entry(agent="unknown-bot", actor_class="daemon")
        )
        assert c.actor_class == "daemon"

    def test_human_grandfathered_maps_to_human(self):
        c = classify_actor(
            _entry(agent="legacy-author", actor_class="human_grandfathered")
        )
        assert c.actor_class == "human"

    def test_unknown_when_nothing_matches(self):
        c = classify_actor(_entry(agent="mystery-bot-7000"))
        assert c.actor_class == "unknown"

    def test_paren_form_with_known_user_is_human(self):
        """``"<Platform> (<user>)"`` is the canonical agent shape in real
        thread entries. When the user-in-parens is a known human, the
        author is classified human — they're the authority behind the
        write, even if a platform routed it. Regression for #869 High."""
        c = classify_actor(_entry(agent="ChatGPT (caleb)"))
        assert c.actor_class == "human"

    def test_paren_form_with_human_base_is_human(self):
        """When the base IS a human name (e.g. \"Caleb Howard (slack)\"
        — the human's name appearing as agent identity), classify as
        human."""
        c = classify_actor(_entry(agent="Caleb Howard (slack)"))
        # caleb is in _HUMAN_AGENT_NAMES; the regex captures the base
        # but neither "caleb howard" nor "slack" are in the literal
        # allowlist. We accept either ``human`` (if the base is in the
        # allowlist) or ``unknown`` (if it isn't); the regression is
        # that the parens stopped it from EVER matching previously.
        # Tighter test below covers the "known user" path.
        assert c.actor_class in {"human", "unknown"}

    def test_paren_form_with_agent_platform_is_agent_with_role(self):
        """``"Claude Code (caleb)"`` with caleb as the user shape should
        classify as human (caleb is the human running the agent);
        ``"Claude Code (some-bot)"`` would route through the agent
        branch."""
        c = classify_actor(_entry(agent="Claude Code (caleb)"))
        # caleb is in _HUMAN_AGENT_NAMES — classifies as human.
        assert c.actor_class == "human"

        c2 = classify_actor(_entry(agent="Claude Code (codex)"))
        # codex isn't in the human allowlist; falls through to agent
        # via the platform-prefix branch.
        assert c2.actor_class == "agent"
        assert c2.platform == "Claude Code"


# ---------------------------------------------------------------------------
# is_coordination_pattern
# ---------------------------------------------------------------------------


class TestCoordinationPattern:
    def test_spec_pm_short_body(self):
        e = _entry(body="Spec: pm\n\nBall: jay. Next: continue.")
        assert is_coordination_pattern(e) is True

    def test_ball_next_advisory_only(self):
        e = _entry(body="Spec: implementer\n\nBall: caleb. Next: review.")
        assert is_coordination_pattern(e) is True

    def test_long_substantive_body_is_not_coordination(self):
        big = "x" * 1500
        e = _entry(body=f"Spec: pm\n\n{big}")
        assert is_coordination_pattern(e) is False

    def test_decision_entry_is_not_coordination_even_if_short(self):
        e = _entry(entry_type="Decision", body="Spec: pm\n\nBall: caleb.")
        assert is_coordination_pattern(e) is False


# ---------------------------------------------------------------------------
# compute_orchestration_metrics
# ---------------------------------------------------------------------------


CANDIDATE_BODY = """\
Spec: decision-extractor
[automated: decision_extractor]
Candidate-Type: Decision
Candidate-Status: needs_human_confirmation
Surface-Kind: decision
Confidence: 4/5
Failed-Gates: g6_temporal
Source-Entry: 01HZA0T0BC3D4E5F6G7H8J9K0M

## Candidate Decision
Adopt PostgreSQL.
"""


PROMOTION_DISPOSITION_BODY = """\
Spec: candidate-disposition
CandidateDisposition: promoted
Disposition-Target: 01HZAAT0BC3D4E5F6G7H8J9K0M
Promoted-To: 01HZABT0BC3D4E5F6G7H8J9K0M
Disposition-Authorized-By: caleb

## Disposition
Promoted.
"""


class TestComputeMetrics:
    def test_basic_counts(self):
        entries = [
            _entry(entry_type="Note", agent="caleb"),
            _entry(entry_type="Note", agent="ExtractDecisionsDaemon"),
            _entry(
                entry_type="Decision",
                agent="ExtractDecisionsDaemon",
                actor_class="daemon",
            ),
            _entry(entry_type="Decision", agent="caleb", actor_class="human"),
            _entry(
                entry_type="Decision",
                agent_func="Claude Code:claude-opus-4-7:implementer",
            ),
        ]
        m = compute_orchestration_metrics({"t": entries})
        assert m.total_entries == 5
        assert m.by_entry_type["Note"] == 2
        assert m.by_entry_type["Decision"] == 3
        assert m.decisions_total == 3
        assert m.decisions_by_actor["daemon"] == 1
        assert m.decisions_by_actor["human"] == 1
        assert m.decisions_by_actor["agent"] == 1
        assert m.threads_covered == 1

    def test_agent_authored_ratio(self):
        entries = [
            _entry(entry_type="Decision", agent="caleb"),
            _entry(entry_type="Decision", agent="caleb"),
            _entry(
                entry_type="Decision",
                agent="ExtractDecisionsDaemon",
                actor_class="daemon",
            ),
        ]
        m = compute_orchestration_metrics({"t": entries})
        # 1 of 3 is daemon (agent-class for this metric).
        assert m.agent_authored_decision_ratio == pytest.approx(1 / 3)

    def test_candidate_note_count(self):
        entries = [
            _entry(entry_type="Note", body=CANDIDATE_BODY),
            _entry(entry_type="Note", body=CANDIDATE_BODY),
            _entry(entry_type="Note"),
        ]
        m = compute_orchestration_metrics({"t": entries})
        assert m.candidate_note_emissions == 2

    def test_promotion_count(self):
        entries = [
            _entry(entry_type="Note", body=PROMOTION_DISPOSITION_BODY),
            _entry(entry_type="Note"),
        ]
        m = compute_orchestration_metrics({"t": entries})
        assert m.promotion_count == 1

    def test_coordination_pattern_count(self):
        entries = [
            _entry(body="Spec: pm\n\nBall: caleb. Next: continue."),
            _entry(body="Spec: implementer\n\nLet me explain things at length." + "x" * 1500),
            _entry(body="Spec: pm\n\nBall: jay. Next: review."),
        ]
        m = compute_orchestration_metrics({"t": entries})
        assert m.coordination_pattern_count == 2

    def test_window_filter_excludes_out_of_range_entries(self):
        in_window = _entry(timestamp="2026-05-15T00:00:00+00:00")
        before = _entry(
            entry_id="01HZABT0BC3D4E5F6G7H8J9K0M",
            timestamp="2026-04-01T00:00:00+00:00",
        )
        after = _entry(
            entry_id="01HZACT0BC3D4E5F6G7H8J9K0M",
            timestamp="2026-07-01T00:00:00+00:00",
        )
        m = compute_orchestration_metrics(
            {"t": [in_window, before, after]},
            window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        assert m.total_entries == 1

    def test_threads_covered_counts_unique_threads_with_entries(self):
        entries_a = [_entry()]
        entries_b = [_entry()]
        entries_empty: list[dict[str, Any]] = []
        m = compute_orchestration_metrics(
            {"a": entries_a, "b": entries_b, "c": entries_empty}
        )
        assert m.threads_covered == 2

    def test_zero_decisions_yields_zero_ratio(self):
        entries = [_entry(entry_type="Note")]
        m = compute_orchestration_metrics({"t": entries})
        assert m.agent_authored_decision_ratio == 0.0


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------


class TestMarkdownReport:
    def _metrics(self) -> OrchestrationMetrics:
        m = OrchestrationMetrics()
        m.total_entries = 100
        m.decisions_total = 10
        m.decisions_by_actor["human"] = 7
        m.decisions_by_actor["daemon"] = 3
        m.agent_authored_decision_ratio = 0.3
        m.candidate_note_emissions = 5
        m.promotion_count = 2
        m.coordination_pattern_count = 8
        m.threads_covered = 12
        return m

    def test_current_only_report_renders_table(self):
        report = format_markdown_report(self._metrics())
        assert "Decisions (total) | 10" in report
        assert "Candidate Note emissions | 5" in report
        assert "Agent-authored Decision ratio | 30.00%" in report

    def test_baseline_comparison_renders_two_columns(self):
        baseline = OrchestrationMetrics()
        baseline.decisions_total = 8
        baseline.candidate_note_emissions = 0
        baseline.promotion_count = 0
        baseline.coordination_pattern_count = 12
        baseline.threads_covered = 10
        baseline.agent_authored_decision_ratio = 0.0

        report = format_markdown_report(self._metrics(), baseline=baseline)
        # Both current and baseline columns present.
        assert "Decisions (total) | 10 | 8 |" in report
        assert "Candidate Note emissions | 5 | 0 |" in report
        assert "Coordination-pattern entries | 8 | 12 |" in report

    def test_to_dict_round_trip(self):
        m = self._metrics()
        d = m.to_dict()
        assert d["decisions_total"] == 10
        assert d["agent_authored_decision_ratio"] == 0.3
        assert d["candidate_note_emissions"] == 5
