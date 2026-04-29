"""Unit tests for pulse_report_lib.py."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


from watercooler.pulse_report_lib import (
    AnalysisFeed,
    ContributorSummary,
    CorpusSummary,
    DecisionPipelineStatus,
    PulseBlock,
    PulseReport,
    PulseReportInputs,
    StalledThreadInfo,
    SignalStatus,
    TrendSignals,
    _INSIGHTS_DISPLAY_CAP,
    _parse_pulse_block,
    _render_coordination_insights,
    _render_signal3_section,
    assemble_report,
    load_analysis_feed_from_dict,
    load_analysis_feed_from_file,
    load_decision_pipeline_status,
    stalled_thread_from_snapshot,
    synthesize_executive_summary,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 4, 4, 12, 0, 0, tzinfo=timezone.utc)


def _make_pulse_block_dict(
    version: str = "1.3",
    *,
    risks: list[dict] | None = None,
    stalled: list[dict] | None = None,
    pairings: list[dict] | None = None,
    actions: list[dict] | None = None,
) -> dict:
    _risks = (
        risks
        if risks is not None
        else [
            {
                "rule_id": "R01",
                "text": "Contributor working in isolation",
                "confidence": 0.8,
                "affected_threads": ["feature-auth"],
            }
        ]
    )
    _stalled = (
        stalled
        if stalled is not None
        else [
            {
                "topic": "old-feature",
                "days_since_last": 20,
                "last_entry_timestamp": None,
            }
        ]
    )
    _pairings = (
        pairings
        if pairings is not None
        else [
            {
                "contributor": "alice",
                "recommended_partner": "bob",
                "reason": "Shared domain knowledge",
                "rule_id": "R04",
            }
        ]
    )
    _actions = (
        actions
        if actions is not None
        else [
            {
                "rule_id": "R01",
                "text": "Sync alice with bob on feature-auth",
                "confidence": 0.8,
                "priority": "high",
            }
        ]
    )
    return {
        "pulse_block_version": version,
        "coordination_risks": _risks,
        "stalled_threads": _stalled,
        "recommended_pairings": _pairings,
        "top_actions": _actions,
        "workflow_shape_distribution": {},
    }


def _make_analysis_json(tmp_path: Path, *, version: str = "1.3") -> Path:
    p = tmp_path / "analysis.json"
    p.write_text(json.dumps({"pulse_block": _make_pulse_block_dict(version=version)}))
    return p


def _make_contributor(name: str = "alice") -> ContributorSummary:
    return ContributorSummary(
        name=name,
        session_count=3,
        last_active="2026-04-04T10:00:00+00:00",
        focus_areas=["daemon design", "testing"],
        recent_observations=[
            {"kind": "decision", "text": "chose approach A"},
            {"kind": "insight", "text": "caching helps here"},
        ],
        open_loops=["flaky test in CI"],
    )


def _make_minimal_inputs(
    *,
    contributors: dict | None = None,
    analysis_feed: AnalysisFeed | None = None,
    trend_signals: TrendSignals | None = None,
) -> PulseReportInputs:
    if contributors is None:
        contributors = {"alice": _make_contributor()}
    return PulseReportInputs(
        contributors=contributors,
        corpus=CorpusSummary(
            session_context_threads=1,
            total_entries_scanned=10,
            sessions_in_window=3,
        ),
        queue_pending=0,
        window_days=7,
        branch="main",
        stalled_threads=[],
        analysis_feed=analysis_feed,
        decision_pipeline=None,
        trend_signals=trend_signals,
        report_date="2026-04-04",
        generated_at="2026-04-04T12:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# load_analysis_feed_from_file
# ---------------------------------------------------------------------------


def test_load_analysis_feed_valid_json(tmp_path):
    p = _make_analysis_json(tmp_path)
    feed = load_analysis_feed_from_file(p, now=NOW)
    assert not feed.degraded
    assert feed.pulse_block is not None
    assert feed.pulse_block.pulse_block_version == "1.3"
    assert len(feed.pulse_block.coordination_risks) == 1
    assert feed.is_fresh


def test_load_analysis_feed_missing_pulse_block(tmp_path):
    p = tmp_path / "analysis.json"
    p.write_text(json.dumps({"other_key": "value"}))
    feed = load_analysis_feed_from_file(p, now=NOW)
    assert feed.degraded
    assert "pulse_block" in feed.degraded_reason
    assert feed.pulse_block is None


def test_load_analysis_feed_incompatible_version(tmp_path):
    p = _make_analysis_json(tmp_path, version="2.0")
    feed = load_analysis_feed_from_file(p, now=NOW)
    assert feed.degraded
    assert "2.0" in feed.degraded_reason
    assert feed.pulse_block is None


def test_load_analysis_feed_stale(tmp_path):
    p = _make_analysis_json(tmp_path)
    old_time = NOW - timedelta(days=90)
    os.utime(p, (old_time.timestamp(), old_time.timestamp()))
    feed = load_analysis_feed_from_file(p, freshness_days=7, now=NOW)
    assert not feed.degraded  # still parseable
    assert not feed.is_fresh
    assert feed.report_age_days is not None
    assert feed.report_age_days > 80


def test_load_analysis_feed_file_not_found(tmp_path):
    p = tmp_path / "nonexistent.json"
    feed = load_analysis_feed_from_file(p, now=NOW)
    assert feed.degraded
    assert "not found" in feed.degraded_reason


def test_load_analysis_feed_invalid_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json {{{")
    feed = load_analysis_feed_from_file(p, now=NOW)
    assert feed.degraded
    assert "failed to parse" in feed.degraded_reason


def test_load_analysis_feed_report_age_days_override(tmp_path):
    """report_age_days overrides mtime so freshness reflects the persisted .md artifact age."""
    p = _make_analysis_json(tmp_path)
    # File was just written (mtime = now) — without override would be fresh
    # Caller passes md_age_days=30 to represent a stale persisted report
    feed = load_analysis_feed_from_file(
        p, freshness_days=7, now=NOW, report_age_days=30.0
    )
    assert not feed.degraded
    assert not feed.is_fresh
    assert feed.report_age_days == 30.0


def test_load_analysis_feed_report_age_days_fresh_override(tmp_path):
    """report_age_days=1 marks a file as fresh regardless of mtime."""
    p = _make_analysis_json(tmp_path)
    # Make the file appear old by mtime
    old_time = NOW - timedelta(days=90)
    os.utime(p, (old_time.timestamp(), old_time.timestamp()))
    # Caller supplies fresh age — overrides mtime
    feed = load_analysis_feed_from_file(
        p, freshness_days=7, now=NOW, report_age_days=1.0
    )
    assert not feed.degraded
    assert feed.is_fresh
    assert feed.report_age_days == 1.0


def test_load_analysis_feed_empty_lists(tmp_path):
    """pulse_block with all-empty lists should parse without error."""
    raw = {
        "pulse_block": {
            "pulse_block_version": "1.0",
            "coordination_risks": [],
            "stalled_threads": [],
            "recommended_pairings": [],
            "top_actions": [],
            "workflow_shape_distribution": {},
        }
    }
    p = tmp_path / "empty.json"
    p.write_text(json.dumps(raw))
    feed = load_analysis_feed_from_file(p, now=NOW)
    assert not feed.degraded
    assert feed.pulse_block is not None
    assert feed.pulse_block.coordination_risks == []


# ---------------------------------------------------------------------------
# load_decision_pipeline_status
# ---------------------------------------------------------------------------


def test_load_decision_pipeline_no_reports(tmp_path):
    status = load_decision_pipeline_status(tmp_path, now=NOW)
    assert status.detection_report_path is None
    assert status.detection_report_age_days is None
    assert status.recent_decision_count == 0
    assert not status.is_detection_fresh


def test_load_decision_pipeline_with_report(tmp_path):
    report = tmp_path / "2026-04-04-decision-candidates.md"
    report.write_text("# Candidates\n- item")
    status = load_decision_pipeline_status(tmp_path, freshness_days=7, now=NOW)
    assert status.detection_report_path is not None
    assert status.detection_report_age_days is not None
    assert status.detection_report_age_days < 1
    assert status.is_detection_fresh


def test_load_decision_pipeline_stale_report(tmp_path):
    report = tmp_path / "2026-03-01-decision-candidates.md"
    report.write_text("# Candidates\n- item")
    old_time = NOW - timedelta(days=40)
    os.utime(report, (old_time.timestamp(), old_time.timestamp()))
    status = load_decision_pipeline_status(tmp_path, freshness_days=7, now=NOW)
    assert status.detection_report_path is not None
    assert not status.is_detection_fresh


def test_load_decision_pipeline_with_recent_decisions(tmp_path):
    ts_recent = (NOW - timedelta(days=2)).isoformat()
    ts_old = (NOW - timedelta(days=30)).isoformat()
    decisions = [
        {"title": "Use Redis for caching", "timestamp": ts_recent},
        {"title": "Old decision", "timestamp": ts_old},
        {"title": "Use PostgreSQL", "timestamp": ts_recent},
        {"title": "Another recent one", "timestamp": ts_recent},
    ]
    status = load_decision_pipeline_status(
        tmp_path, now=NOW, recent_decisions=decisions, window_days=7
    )
    assert status.recent_decision_count == 3
    assert len(status.recent_decision_titles) == 3
    assert "Use Redis for caching" in status.recent_decision_titles


def test_load_decision_pipeline_missing_dir(tmp_path):
    """Non-existent reports_dir should return empty status, not crash."""
    status = load_decision_pipeline_status(tmp_path / "nonexistent", now=NOW)
    assert status.detection_report_path is None
    assert status.recent_decision_count == 0


def test_load_decision_pipeline_real_repo_layout(tmp_path):
    """Caller passes dev_docs/reports/decision-candidates/ directly — files are at root."""
    subdir = tmp_path / "decision-candidates"
    subdir.mkdir()
    report = subdir / "2026-03-18-decision-candidates.md"
    report.write_text("# Candidates\n- item")
    # Caller passes the subdir, not the parent
    status = load_decision_pipeline_status(subdir, freshness_days=7, now=NOW)
    assert status.detection_report_path is not None
    assert "2026-03-18-decision-candidates.md" in status.detection_report_path


# ---------------------------------------------------------------------------
# assemble_report — degraded / missing signal scenarios
# ---------------------------------------------------------------------------


def test_assemble_report_no_signal2():
    inputs = _make_minimal_inputs(analysis_feed=None)
    report = assemble_report(inputs)
    assert report.run_status.signal2 == SignalStatus.NO_DATA
    assert "## Project Health (Signal 2)" in report.markdown
    assert "No Signal 2 data available" in report.markdown


def test_assemble_report_no_signal3():
    inputs = _make_minimal_inputs(trend_signals=None)
    report = assemble_report(inputs)
    assert report.run_status.signal3 == SignalStatus.NO_DATA
    assert "## Trend Signals" in report.markdown


def test_assemble_report_empty_contributors():
    inputs = _make_minimal_inputs(contributors={})
    report = assemble_report(inputs)
    assert report.run_status.signal1 == SignalStatus.NO_DATA
    # corpus has session_context_threads=1, so the "threads exist but no
    # sessions in window" wording is used
    assert "No session activity in the last" in report.markdown


def test_assemble_report_degraded_analysis_feed(tmp_path):
    feed = AnalysisFeed(
        pulse_block=None,
        report_path="/tmp/fake.json",
        report_age_days=10.0,
        is_fresh=False,
        degraded=True,
        degraded_reason="pulse_block key absent",
    )
    inputs = _make_minimal_inputs(analysis_feed=feed)
    report = assemble_report(inputs)
    assert "Degraded mode" in report.markdown or "degraded" in report.markdown.lower()


# ---------------------------------------------------------------------------
# assemble_report — markdown structure
# ---------------------------------------------------------------------------


def test_assemble_report_all_sections_present():
    inputs = _make_minimal_inputs()
    report = assemble_report(inputs)
    assert "## Executive Summary" in report.markdown
    assert "## Session Activity (Signal 1)" in report.markdown
    assert "## Project Health (Signal 2)" in report.markdown
    assert "## Trend Signals (Signal 3)" in report.markdown
    assert "Run stats:" in report.markdown


def test_assemble_report_header_contains_date():
    inputs = _make_minimal_inputs()
    report = assemble_report(inputs)
    assert "# Project Pulse — 2026-04-04" in report.markdown


def test_assemble_report_returns_pulse_report():
    inputs = _make_minimal_inputs()
    report = assemble_report(inputs)
    assert isinstance(report, PulseReport)
    assert report.report_date == "2026-04-04"
    assert report.generated_at == "2026-04-04T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Run footer
# ---------------------------------------------------------------------------


def test_render_run_footer_all_ok():
    from watercooler.pulse_report_lib import PulseBlock

    pulse_block = PulseBlock(
        pulse_block_version="1.3",
        coordination_risks=[],
        stalled_threads=[],
        recommended_pairings=[],
        top_actions=[],
        workflow_shape_distribution={},
    )
    inputs = _make_minimal_inputs(
        analysis_feed=AnalysisFeed(
            pulse_block=pulse_block,
            report_path=None,
            report_age_days=1.0,
            is_fresh=True,
            degraded=False,
            degraded_reason="",
        ),
        trend_signals=TrendSignals(
            supersession_rate=0.1,
            active_fact_count=90,
            superseded_fact_count=10,
            sample_size=100,
            top_volatile_topics=["auth"],
            top_stable_topics=["core"],
            trend_direction="stable",
        ),
    )
    report = assemble_report(inputs)
    assert "Signal 1 ok" in report.markdown
    assert "Signal 2 ok" in report.markdown
    assert "Signal 3 ok" in report.markdown


def test_render_run_footer_mixed():
    """Stale T2 (valid pulse_block, not fresh) + unavailable T3 produces correct footer."""
    from watercooler.pulse_report_lib import PulseBlock

    pulse_block = PulseBlock(
        pulse_block_version="1.3",
        coordination_risks=[],
        stalled_threads=[],
        recommended_pairings=[],
        top_actions=[],
        workflow_shape_distribution={},
    )
    feed = AnalysisFeed(
        pulse_block=pulse_block,
        report_path=None,
        report_age_days=30.0,
        is_fresh=False,
        degraded=False,
        degraded_reason="",
    )
    inputs = _make_minimal_inputs(analysis_feed=feed, trend_signals=None)
    report = assemble_report(inputs)
    assert "Signal 2 stale" in report.markdown
    assert "Signal 3 no data" in report.markdown


def test_render_run_footer_contains_window_and_branch():
    inputs = _make_minimal_inputs()
    report = assemble_report(inputs)
    assert "Window: last 7 days" in report.markdown
    assert "Branch: main" in report.markdown
    assert "Generated: 2026-04-04T12:00:00+00:00" in report.markdown


# ---------------------------------------------------------------------------
# Contributor block
# ---------------------------------------------------------------------------


def test_render_contributor_no_open_loops():
    contributor = ContributorSummary(
        name="bob",
        session_count=1,
        last_active="2026-04-03T08:00:00+00:00",
        focus_areas=["refactoring"],
        recent_observations=[],
        open_loops=[],
    )
    inputs = _make_minimal_inputs(contributors={"bob": contributor})
    report = assemble_report(inputs)
    assert "### bob" in report.markdown
    assert "Open loops" not in report.markdown


def test_render_contributor_with_observations():
    obs = [
        {"kind": "decision", "text": "chose Redis over Memcached"},
        {"kind": "problem", "text": "hit deadlock in pipeline"},
    ]
    contributor = ContributorSummary(
        name="alice",
        session_count=2,
        last_active="2026-04-04T09:00:00+00:00",
        focus_areas=["caching"],
        recent_observations=obs,
        open_loops=["unresolved CI failure"],
    )
    inputs = _make_minimal_inputs(contributors={"alice": contributor})
    report = assemble_report(inputs)
    assert "[decision] chose Redis over Memcached" in report.markdown
    assert "[problem] hit deadlock in pipeline" in report.markdown
    assert "unresolved CI failure" in report.markdown


# ---------------------------------------------------------------------------
# Signal 2 rendering
# ---------------------------------------------------------------------------


def test_render_signal2_stale_warning(tmp_path):
    p = tmp_path / "analysis.json"
    p.write_text(json.dumps({"pulse_block": _make_pulse_block_dict()}))
    feed = load_analysis_feed_from_file(p, now=NOW, report_age_days=15.0)
    assert feed.report_age_days == 15.0
    inputs = _make_minimal_inputs(analysis_feed=feed)
    report = assemble_report(inputs)
    assert "15.0 days old" in report.markdown or "stale" in report.markdown.lower()


def test_render_signal2_full_pulse_block(tmp_path):
    p = _make_analysis_json(tmp_path)
    feed = load_analysis_feed_from_file(p, now=NOW)
    inputs = _make_minimal_inputs(analysis_feed=feed)
    report = assemble_report(inputs)
    assert "### Coordination" in report.markdown
    assert "R01" in report.markdown
    assert "alice" in report.markdown or "Coordination risks" in report.markdown
    assert "Top actions" in report.markdown
    assert "Recommended pairings" in report.markdown


def test_render_signal2_no_coordination_risks(tmp_path):
    raw = _make_pulse_block_dict(risks=[], pairings=[], actions=[])
    p = tmp_path / "analysis.json"
    p.write_text(json.dumps({"pulse_block": raw}))
    feed = load_analysis_feed_from_file(p, now=NOW)
    inputs = _make_minimal_inputs(analysis_feed=feed)
    report = assemble_report(inputs)
    assert "none detected" in report.markdown


# ---------------------------------------------------------------------------
# Signal 3 rendering
# ---------------------------------------------------------------------------


def test_trend_signals_zero_supersession():
    signals = TrendSignals(
        supersession_rate=0.0,
        active_fact_count=100,
        superseded_fact_count=0,
        sample_size=100,
        top_volatile_topics=[],
        top_stable_topics=["core"],
        trend_direction="stable",
    )
    inputs = _make_minimal_inputs(trend_signals=signals)
    report = assemble_report(inputs)
    assert "0%" in report.markdown
    assert "stable" in report.markdown


def test_trend_signals_improving():
    signals = TrendSignals(
        supersession_rate=0.15,
        active_fact_count=85,
        superseded_fact_count=15,
        sample_size=100,
        top_volatile_topics=["auth", "caching"],
        top_stable_topics=["core"],
        trend_direction="improving",
    )
    inputs = _make_minimal_inputs(trend_signals=signals)
    report = assemble_report(inputs)
    assert "improving" in report.markdown
    assert "auth" in report.markdown
    # Rendered labels use "relations", not "areas"
    assert "Volatile relations" in report.markdown
    assert "Stable relations" in report.markdown
    assert "Volatile areas" not in report.markdown
    assert "Stable areas" not in report.markdown
    # Sample size rendered dynamically, not hardcoded "100"
    assert "sample of up to 100 facts" in report.markdown
    assert "top-100 fact sample" not in report.markdown


def test_dimension_scores_render_without_trend_signals():
    """Dimension scores must render even when TrendSnapshot is disabled (T1-only path)."""
    dimension_scores = {
        "goal_clarity": {
            "level_score": 0.55,
            "level_label": "mixed",
            "baseline_score": 0.55,
            "trend_delta": 0.0,
            "trend_label": "stable",
            "confidence": 0.65,
            "watch": False,
            "notes": [],
        },
        "constraint_pressure": {
            "level_score": 0.1,
            "level_label": "low",
            "baseline_score": 0.1,
            "trend_delta": 0.0,
            "trend_label": "stable",
            "confidence": 0.65,
            "watch": False,
            "notes": [],
        },
        "evidence_quality": {
            "level_score": 0.75,
            "level_label": "high",
            "baseline_score": 0.75,
            "trend_delta": 0.0,
            "trend_label": "stable",
            "confidence": 0.65,
            "watch": False,
            "notes": [],
        },
        "execution_momentum": {
            "level_score": 0.5,
            "level_label": "mixed",
            "baseline_score": 0.5,
            "trend_delta": 0.0,
            "trend_label": "stable",
            "confidence": 0.65,
            "watch": False,
            "notes": ["Momentum in degraded mode — observation mix proxy"],
        },
    }
    result = _render_signal3_section(
        trend_signals=None,
        status=SignalStatus.NO_DATA,
        dimension_scores=dimension_scores,
    )
    assert "Project configuration" in result
    assert "forming" in result  # goal_clarity mixed
    assert "loose" in result  # constraint_pressure low
    assert "strong" in result  # evidence_quality high
    assert "probing" in result  # execution_momentum mixed
    assert "Goal clarity" in result
    # Internal field names must NOT appear in rendered output
    assert "goal_clarity" not in result
    assert "level_score" not in result


def test_dimension_scores_suppress_band_hedges_strong_labels():
    """Confidence < 0.40 (suppress band) must hedge strong labels and add caveat."""
    dimension_scores = {
        "goal_clarity": {
            "level_score": 0.80,
            "level_label": "high",
            "baseline_score": 0.80,
            "trend_delta": 0.0,
            "trend_label": "stable",
            "confidence": 0.35,  # suppress band
            "watch": False,
            "notes": [],
        },
        "constraint_pressure": {
            "level_score": 0.80,
            "level_label": "high",
            "baseline_score": 0.80,
            "trend_delta": 0.0,
            "trend_label": "stable",
            "confidence": 0.35,
            "watch": False,
            "notes": [],
        },
        "evidence_quality": {
            "level_score": 0.80,
            "level_label": "high",
            "baseline_score": 0.80,
            "trend_delta": 0.0,
            "trend_label": "stable",
            "confidence": 0.35,
            "watch": False,
            "notes": [],
        },
        "execution_momentum": {
            "level_score": 0.80,
            "level_label": "high",
            "baseline_score": 0.80,
            "trend_delta": 0.0,
            "trend_label": "stable",
            "confidence": 0.35,
            "watch": False,
            "notes": [],
        },
    }
    result = _render_signal3_section(
        trend_signals=None,
        status=SignalStatus.NO_DATA,
        dimension_scores=dimension_scores,
    )
    # Suppress band: strong labels should be hedged
    assert "appears crisp" in result  # goal_clarity high → hedged
    assert "seems tight" in result  # constraint_pressure high → hedged
    assert "appears strong" in result  # evidence_quality high → hedged
    # execution_momentum high prose ("driving") must also be hedged
    assert "may be driving" in result
    # Caveat must appear for all suppress-band items
    assert result.count("Low confidence") == 4


def test_dimension_scores_watch_band_adds_caveat_without_hedging():
    """Confidence 0.40–0.49 (watch band) must add caveat but keep original labels."""
    dimension_scores = {
        "goal_clarity": {
            "level_score": 0.80,
            "level_label": "high",
            "baseline_score": 0.80,
            "trend_delta": 0.0,
            "trend_label": "stable",
            "confidence": 0.45,  # watch band
            "watch": False,
            "notes": [],
        },
    }
    result = _render_signal3_section(
        trend_signals=None,
        status=SignalStatus.NO_DATA,
        dimension_scores=dimension_scores,
    )
    # Watch band: original label retained (not hedged)
    assert "crisp" in result
    assert "appears crisp" not in result
    # Caveat must appear
    assert "Low confidence" in result


# ---------------------------------------------------------------------------
# LLM synthesis
# ---------------------------------------------------------------------------


def test_synthesize_executive_summary_success():
    class FakeLLM:
        def complete(self, prompt, system, max_tokens):
            return "- All good\n- Things are moving"

    inputs = _make_minimal_inputs()
    result = synthesize_executive_summary(inputs, FakeLLM())
    assert result is not None
    assert "All good" in result


def test_synthesize_executive_summary_returns_none_on_failure():
    class FailingLLM:
        def complete(self, prompt, system, max_tokens):
            raise RuntimeError("LLM exploded")

    inputs = _make_minimal_inputs()
    result = synthesize_executive_summary(inputs, FailingLLM())
    assert result is None


def test_synthesize_executive_summary_none_on_empty_response():
    class EmptyLLM:
        def complete(self, prompt, system, max_tokens):
            return None

    inputs = _make_minimal_inputs()
    result = synthesize_executive_summary(inputs, EmptyLLM())
    assert result is None


def test_assemble_report_uses_llm_summary():
    """When llm_executive_summary is provided, it appears in the output."""
    inputs = _make_minimal_inputs()
    report = assemble_report(inputs, llm_executive_summary="- Custom LLM bullet")
    assert "Custom LLM bullet" in report.markdown


# ---------------------------------------------------------------------------
# Queue pending warning
# ---------------------------------------------------------------------------


def test_queue_pending_appears_in_executive_summary():
    inputs = _make_minimal_inputs()
    inputs.queue_pending = 3
    report = assemble_report(inputs)
    assert "3" in report.markdown
    assert "queue" in report.markdown.lower() or "pending" in report.markdown.lower()


# ---------------------------------------------------------------------------
# Helper (used in tests above to construct AnalysisFeed with a PulseBlock)
# ---------------------------------------------------------------------------


def test_stalled_thread_snapshot_field_mapping():
    """stalled_thread_from_snapshot adapts pulse_snapshot_lib field names correctly."""
    snapshot_record = {
        "topic": "old-feature",
        "days_stale": 5,
        "last_entry_at": "2026-03-30T00:00:00Z",
    }
    stalled = stalled_thread_from_snapshot(snapshot_record)
    assert stalled.topic == "old-feature"
    assert stalled.days_since_last == 5
    assert stalled.last_entry_timestamp == "2026-03-30T00:00:00Z"


# ---------------------------------------------------------------------------
# Phase 5 new tests (todos #189–#198)
# ---------------------------------------------------------------------------


def test_load_analysis_feed_malformed_numeric_fields(tmp_path: Path):
    """Malformed numeric field (confidence='high') degrades gracefully instead of raising."""
    data = _make_pulse_block_dict()
    data["coordination_risks"][0]["confidence"] = "high"  # non-numeric
    p = tmp_path / "analysis.json"
    p.write_text(json.dumps({"pulse_block": data}))
    feed = load_analysis_feed_from_file(p, now=NOW)
    assert feed.degraded is True
    assert "failed to parse pulse_block fields" in feed.degraded_reason


def test_load_analysis_feed_malformed_container_shape(tmp_path: Path):
    """Non-dict list element (e.g. a string) degrades gracefully instead of AttributeError."""
    data = _make_pulse_block_dict()
    data["coordination_risks"] = ["not-a-dict"]  # element is a string, not a dict
    p = tmp_path / "analysis.json"
    p.write_text(json.dumps({"pulse_block": data}))
    feed = load_analysis_feed_from_file(p, now=NOW)
    assert feed.degraded is True
    assert "failed to parse pulse_block fields" in feed.degraded_reason


def test_derive_run_status_decision_pipeline_only_is_stale():
    """analysis_feed=None + decision_pipeline present → STALE (not OK), footer matches body."""

    dp = DecisionPipelineStatus(
        recent_decision_count=1,
        recent_decision_titles=["Use async IO"],
        detection_report_path=None,
        detection_report_age_days=None,
        is_detection_fresh=False,
    )
    inputs = _make_minimal_inputs(analysis_feed=None)
    inputs.decision_pipeline = dp
    report = assemble_report(inputs)
    assert "Signal 2 stale" in report.markdown
    assert "Signal 2 ok" not in report.markdown


def test_derive_run_status_degraded_is_unavailable(tmp_path: Path):
    """Degraded AnalysisFeed maps to UNAVAILABLE, not STALE."""
    feed = AnalysisFeed(
        pulse_block=None,
        report_path=None,
        report_age_days=1.0,
        is_fresh=True,
        degraded=True,
        degraded_reason="schema error",
    )
    inputs = _make_minimal_inputs(analysis_feed=feed, trend_signals=None)
    report = assemble_report(inputs)
    assert "Signal 2 unavailable" in report.markdown


def test_derive_run_status_stale_is_stale():
    """Valid-but-old AnalysisFeed maps to STALE."""
    pulse_block = PulseBlock(
        pulse_block_version="1.3",
        coordination_risks=[],
        stalled_threads=[],
        recommended_pairings=[],
        top_actions=[],
        workflow_shape_distribution={},
    )
    feed = AnalysisFeed(
        pulse_block=pulse_block,
        report_path=None,
        report_age_days=30.0,
        is_fresh=False,
        degraded=False,
    )
    inputs = _make_minimal_inputs(analysis_feed=feed, trend_signals=None)
    report = assemble_report(inputs)
    assert "Signal 2 stale" in report.markdown


def test_assemble_report_pulse_block_none_degraded_false_signal2_unavailable():
    """AnalysisFeed(pulse_block=None, degraded=False) → signal2=unavailable, not ok."""
    feed = AnalysisFeed(
        pulse_block=None,
        report_path=None,
        report_age_days=1.0,
        is_fresh=True,
        degraded=False,
    )
    inputs = _make_minimal_inputs(analysis_feed=feed, trend_signals=None)
    report = assemble_report(inputs)
    assert "Signal 2 unavailable" in report.markdown
    assert "Signal 2 ok" not in report.markdown


def test_load_decision_pipeline_mtime_beats_lexicographic(tmp_path: Path):
    """mtime determines most-recent report, not filename lexicographic order."""
    older = tmp_path / "z-decision-candidates.md"
    newer = tmp_path / "a-decision-candidates.md"
    older.write_text("# Old")
    newer.write_text("# New")
    # Make 'older' actually older by backdating its mtime
    old_time = NOW.timestamp() - 86400  # 1 day ago
    os.utime(older, (old_time, old_time))
    result = load_decision_pipeline_status(tmp_path, now=NOW)
    assert result.detection_report_path is not None
    assert result.detection_report_path == str(newer)


def test_load_decision_pipeline_created_at_fallback():
    """recent_decisions entries with 'created_at' key (not 'timestamp') are accepted."""
    result = load_decision_pipeline_status(
        Path("/nonexistent"),
        now=NOW,
        recent_decisions=[
            {"title": "Switch to async IO", "created_at": "2026-04-01T10:00:00Z"},
        ],
        window_days=7,
    )
    assert result.recent_decision_count == 1
    assert "Switch to async IO" in result.recent_decision_titles


def test_stalled_thread_from_snapshot_days_stale_key():
    """stalled_thread_from_snapshot handles days_stale/last_entry_at keys."""
    record = {
        "topic": "stale-thread",
        "days_stale": 14,
        "last_entry_at": "2026-03-20T00:00:00Z",
    }
    result = stalled_thread_from_snapshot(record)
    assert result.topic == "stale-thread"
    assert result.days_since_last == 14
    assert result.last_entry_timestamp == "2026-03-20T00:00:00Z"


def test_assemble_report_no_stalled_thread_double_render(tmp_path: Path):
    """When T2 pulse_block has stalled threads, they are not duplicated in T1."""
    pb_dict = _make_pulse_block_dict(
        stalled=[
            {"topic": "t2-stalled", "days_since_last": 10, "last_entry_timestamp": None}
        ]
    )
    p = tmp_path / "analysis.json"
    p.write_text(json.dumps({"pulse_block": pb_dict}))
    feed = load_analysis_feed_from_file(p, now=NOW)
    # T1 stalled list also contains a stalled thread
    t1_stalled = [
        StalledThreadInfo(
            topic="t1-stalled", days_since_last=5, last_entry_timestamp=None
        )
    ]
    inputs = _make_minimal_inputs(analysis_feed=feed)
    inputs.stalled_threads = t1_stalled
    report = assemble_report(inputs)
    # T2 stalled topic comes from pulse_block — appears in T2 section only
    assert "t2-stalled" in report.markdown
    # T1 stalled topic — appears in T1 section (and possibly exec summary) but NOT in T2
    assert "t1-stalled" in report.markdown
    # T2 section must not contain the T1-only topic
    t2_start = report.markdown.find("## Project Health")
    t2_section = report.markdown[t2_start:] if t2_start != -1 else ""
    assert "t1-stalled" not in t2_section


# ---------------------------------------------------------------------------
# Stalled thread cap tests
# ---------------------------------------------------------------------------


def _make_stalled_list(count: int) -> list[StalledThreadInfo]:
    """Build a list of N stalled thread infos for cap testing."""
    return [
        StalledThreadInfo(
            topic=f"stalled-thread-{i:03d}",
            days_since_last=100 - i,
            last_entry_timestamp=None,
        )
        for i in range(count)
    ]


def test_render_signal1_caps_stalled_at_max():
    """T1 section renders at most _MAX_STALLED_SHOWN stalled threads."""
    from watercooler.pulse_report_lib import _MAX_STALLED_SHOWN

    stalled = _make_stalled_list(20)
    inputs = _make_minimal_inputs()
    inputs.stalled_threads = stalled
    report = assemble_report(inputs)

    t1_start = report.markdown.find("## Session Activity")
    t2_start = report.markdown.find("## Project Health")
    t1_section = (
        report.markdown[t1_start:t2_start]
        if t2_start != -1
        else report.markdown[t1_start:]
    )

    # Count stalled bullet points in T1
    stalled_bullets = [
        line
        for line in t1_section.splitlines()
        if line.startswith("- `stalled-thread-")
    ]
    assert len(stalled_bullets) == _MAX_STALLED_SHOWN

    # Truncation note present
    assert "... and 10 more" in t1_section


def test_render_signal1_no_truncation_note_when_under_cap():
    """T1 section omits truncation note when stalled count <= cap."""
    stalled = _make_stalled_list(5)
    inputs = _make_minimal_inputs()
    inputs.stalled_threads = stalled
    report = assemble_report(inputs)

    t1_start = report.markdown.find("## Session Activity")
    t2_start = report.markdown.find("## Project Health")
    t1_section = (
        report.markdown[t1_start:t2_start]
        if t2_start != -1
        else report.markdown[t1_start:]
    )

    stalled_bullets = [
        line
        for line in t1_section.splitlines()
        if line.startswith("- `stalled-thread-")
    ]
    assert len(stalled_bullets) == 5
    assert "... and" not in t1_section


def test_render_signal2_caps_stalled_at_max(tmp_path: Path):
    """T2 section caps stalled threads from pulse_block at _MAX_STALLED_SHOWN."""
    from watercooler.pulse_report_lib import _MAX_STALLED_SHOWN

    many_stalled = [
        {
            "topic": f"t2-stalled-{i:03d}",
            "days_since_last": 50 - i,
            "last_entry_timestamp": None,
        }
        for i in range(25)
    ]
    pb_dict = _make_pulse_block_dict(stalled=many_stalled)
    p = tmp_path / "analysis.json"
    p.write_text(json.dumps({"pulse_block": pb_dict}))
    feed = load_analysis_feed_from_file(p, now=NOW)

    inputs = _make_minimal_inputs(analysis_feed=feed)
    report = assemble_report(inputs)

    t2_start = report.markdown.find("## Project Health")
    t2_section = report.markdown[t2_start:] if t2_start != -1 else ""

    stalled_bullets = [
        line for line in t2_section.splitlines() if line.startswith("- `t2-stalled-")
    ]
    assert len(stalled_bullets) == _MAX_STALLED_SHOWN
    assert "... and 15 more" in t2_section


def test_exec_summary_shows_full_stalled_count():
    """Executive summary shows the total stalled count, not the capped count."""
    stalled = _make_stalled_list(20)
    inputs = _make_minimal_inputs()
    inputs.stalled_threads = stalled
    report = assemble_report(inputs)

    exec_start = report.markdown.find("## Executive Summary")
    session_start = report.markdown.find("## Session Activity")
    exec_section = (
        report.markdown[exec_start:session_start] if session_start != -1 else ""
    )

    assert "20 stalled thread(s)" in exec_section


def test_render_signal2_degraded_fallback_caps_stalled_at_max():
    """Degraded T2 fallback to T1 stalled threads is also capped."""
    from watercooler.pulse_report_lib import _MAX_STALLED_SHOWN

    feed = AnalysisFeed(
        pulse_block=None,
        report_path="/tmp/fake.json",
        report_age_days=10.0,
        is_fresh=False,
        degraded=True,
        degraded_reason="pulse_block key absent",
    )
    stalled = _make_stalled_list(20)
    inputs = _make_minimal_inputs(analysis_feed=feed)
    inputs.stalled_threads = stalled
    report = assemble_report(inputs)

    t2_start = report.markdown.find("## Project Health")
    t2_section = report.markdown[t2_start:] if t2_start != -1 else ""

    # Degraded mode message present
    assert "Degraded mode" in t2_section

    # Stalled bullets capped
    stalled_bullets = [
        line
        for line in t2_section.splitlines()
        if line.startswith("- `stalled-thread-")
    ]
    assert len(stalled_bullets) == _MAX_STALLED_SHOWN
    assert "... and 10 more" in t2_section


# ---------------------------------------------------------------------------
# load_analysis_feed_from_dict tests
# ---------------------------------------------------------------------------


def _make_analysis_result_dict() -> dict:
    """Build a minimal analysis result dict with valid pulse_block."""
    now = datetime.now(timezone.utc)
    return {
        "schema_version": "1.3",
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "corpus": {"window_entry_count": 10},
        "pulse_block": {
            "pulse_block_version": "1.0",
            "coordination_risks": [
                {
                    "rule_id": "R05",
                    "text": "Closure rate critically low",
                    "confidence": 0.90,
                    "affected_threads": [],
                }
            ],
            "stalled_threads": [],
            "recommended_pairings": [],
            "top_actions": [],
            "workflow_shape_distribution": {},
        },
    }


def test_load_analysis_feed_from_dict_parses_pulse_block():
    data = _make_analysis_result_dict()
    feed = load_analysis_feed_from_dict(data)

    assert not feed.degraded
    assert feed.is_fresh
    assert feed.pulse_block is not None
    assert len(feed.pulse_block.coordination_risks) == 1
    assert feed.report_path == "daemon:analysis_snapshot"


def test_load_analysis_feed_from_dict_degraded_on_missing_block():
    data = _make_analysis_result_dict()
    del data["pulse_block"]
    feed = load_analysis_feed_from_dict(data)

    assert feed.degraded
    assert "pulse_block key absent" in feed.degraded_reason
    assert feed.pulse_block is None


def test_load_analysis_feed_from_dict_freshness_check():
    data = _make_analysis_result_dict()
    # Set generated_at to 30 days ago
    old = datetime.now(timezone.utc) - timedelta(days=30)
    data["generated_at"] = old.strftime("%Y-%m-%dT%H:%M:%SZ")
    feed = load_analysis_feed_from_dict(data, freshness_days=7)

    assert not feed.is_fresh
    assert feed.report_age_days is not None
    assert feed.report_age_days > 29


def test_load_analysis_feed_from_dict_incompatible_version():
    data = _make_analysis_result_dict()
    data["pulse_block"]["pulse_block_version"] = "2.0"
    feed = load_analysis_feed_from_dict(data)

    assert feed.degraded
    assert "incompatible" in feed.degraded_reason


# ---------------------------------------------------------------------------
# _parse_pulse_block — null-field hardening tests (todo 258)
# ---------------------------------------------------------------------------


def _minimal_pulse_block_dict(**overrides) -> dict:
    base = {
        "pulse_block_version": "1.0",
        "coordination_risks": [],
        "stalled_threads": [],
        "recommended_pairings": [],
        "top_actions": [],
        "workflow_shape_distribution": {},
    }
    base.update(overrides)
    return base


def test_parse_pulse_block_null_confidence_coordination_risk():
    """confidence=None in coordination_risks does not raise TypeError."""
    raw = _minimal_pulse_block_dict(
        coordination_risks=[
            {
                "rule_id": "r1",
                "text": "some risk",
                "confidence": None,
                "affected_threads": [],
            },
        ]
    )
    block = _parse_pulse_block(raw)
    assert block.coordination_risks[0].confidence == 0.0


def test_parse_pulse_block_null_confidence_top_action():
    """confidence=None in top_actions does not raise TypeError."""
    raw = _minimal_pulse_block_dict(
        top_actions=[
            {
                "rule_id": "a1",
                "text": "some action",
                "confidence": None,
                "priority": "high",
            },
        ]
    )
    block = _parse_pulse_block(raw)
    assert block.top_actions[0].confidence == 0.0


def test_parse_pulse_block_null_days_since_last():
    """days_since_last=None in stalled_threads does not raise TypeError."""
    raw = _minimal_pulse_block_dict(
        stalled_threads=[
            {
                "topic": "some-thread",
                "days_since_last": None,
                "last_entry_timestamp": None,
            },
        ]
    )
    block = _parse_pulse_block(raw)
    assert block.stalled_threads[0].days_since_last == 0


# ---------------------------------------------------------------------------
# P1.1 — Snapshot enrichment section
# ---------------------------------------------------------------------------


def _make_enrichment(
    *,
    situation_trajectory: str = "Momentum is building.",
    tension_signals: list[str] | None = None,
    coordination_risks: list[str] | None = None,
    recommended_focus: str = "Focus on coordinator v1B rollout.",
) -> dict:
    return {
        "situation_trajectory": situation_trajectory,
        "tension_signals": tension_signals or ["ambiguity in spec"],
        "coordination_risks": coordination_risks or ["alice unblocks bob"],
        "recommended_focus": recommended_focus,
        "executive_summary": "Parent-level summary (should NOT appear).",
        "stable_changing_summary": "Stable/changing (should NOT appear).",
        "generated_at": "2026-04-12T10:00:00+00:00",
        "model": "test-model",
    }


def test_assemble_report_renders_enrichment_section_when_present():
    inputs = _make_minimal_inputs()
    inputs.snapshot_enrichment = _make_enrichment()
    report = assemble_report(inputs)
    assert "## Snapshot analysis" in report.markdown
    assert "Momentum is building." in report.markdown
    assert "ambiguity in spec" in report.markdown
    assert "alice unblocks bob" in report.markdown
    assert "Focus on coordinator v1B rollout." in report.markdown
    # Unrelated snapshot fields must not leak in
    assert "Parent-level summary" not in report.markdown
    assert "Stable/changing" not in report.markdown


def test_assemble_report_omits_enrichment_section_when_none():
    inputs = _make_minimal_inputs()
    # snapshot_enrichment defaults to None
    assert inputs.snapshot_enrichment is None
    report = assemble_report(inputs)
    assert "## Snapshot analysis" not in report.markdown


def test_build_enrichment_section_tolerates_partial_fields():
    from watercooler.pulse_report_lib import _build_enrichment_section

    md = _build_enrichment_section({"situation_trajectory": "Only a trajectory."})
    assert "## Snapshot analysis" in md
    assert "Only a trajectory." in md
    assert "**Tension signals:**" not in md
    assert "**Coordination risks:**" not in md
    assert "**Recommended focus:**" not in md


def test_build_enrichment_section_empty_dict_collapses_to_placeholder():
    from watercooler.pulse_report_lib import _build_enrichment_section

    md = _build_enrichment_section({})
    assert "## Snapshot analysis" in md
    assert "No analytical content available" in md


def test_build_enrichment_section_drops_empty_list_items():
    from watercooler.pulse_report_lib import _build_enrichment_section

    md = _build_enrichment_section(
        {
            "tension_signals": ["real tension", "", "   "],
            "coordination_risks": [],
        }
    )
    assert "- real tension" in md
    # Empty items should not render as bullets
    assert md.count("- ") == 1
    assert "**Coordination risks:**" not in md


def test_sanitize_enrichment_text_strips_leading_heading():
    from watercooler.pulse_report_lib import _sanitize_enrichment_text

    assert _sanitize_enrichment_text("## Fake section") == "Fake section"
    assert _sanitize_enrichment_text("### Deeper") == "Deeper"


def test_sanitize_enrichment_text_strips_list_and_rule_breakers():
    from watercooler.pulse_report_lib import _sanitize_enrichment_text

    # Horizontal rule collapses to empty
    assert _sanitize_enrichment_text("---") == ""
    # Leading bullet stripped
    assert _sanitize_enrichment_text("- already a bullet") == "already a bullet"
    assert _sanitize_enrichment_text("* star bullet") == "star bullet"
    assert _sanitize_enrichment_text("+ plus bullet") == "plus bullet"


def test_sanitize_enrichment_text_collapses_multiline_content():
    from watercooler.pulse_report_lib import _sanitize_enrichment_text

    assert _sanitize_enrichment_text("foo\n\n## Bar") == "foo ## Bar"
    # Embedded newlines flattened to single spaces
    assert _sanitize_enrichment_text("line one\nline two") == "line one line two"
    assert _sanitize_enrichment_text("a\t\tb\n  c") == "a b c"


def test_sanitize_enrichment_text_strips_stacked_quote_heading():
    from watercooler.pulse_report_lib import _sanitize_enrichment_text

    # Iterative strip defuses quote+heading stacking
    assert _sanitize_enrichment_text("> ## X") == "X"
    assert _sanitize_enrichment_text(">>> # Deep") == "Deep"
    assert _sanitize_enrichment_text("- > ## nested") == "nested"


def test_sanitize_enrichment_text_handles_non_string_and_none():
    from watercooler.pulse_report_lib import _sanitize_enrichment_text

    assert _sanitize_enrichment_text(None) == ""
    assert _sanitize_enrichment_text(42) == "42"
    assert _sanitize_enrichment_text(["a", "b"]) == "['a', 'b']"


def test_sanitize_enrichment_text_neutralizes_inline_links():
    from watercooler.pulse_report_lib import _sanitize_enrichment_text

    # Plain link — bracket is escaped, so the raw ``[`` no longer opens a link
    out = _sanitize_enrichment_text("see [docs](https://evil.example/x)")
    assert "\\[docs](https://evil.example/x)" in out
    # The unescaped leading bracket should not appear — every ``[`` in output
    # must be preceded by a backslash.
    for i, ch in enumerate(out):
        if ch == "[":
            assert i > 0 and out[i - 1] == "\\"

    # Image — both ``!`` and ``[`` escaped
    out = _sanitize_enrichment_text("![alt](https://tracker.example/pixel.png)")
    assert "\\!\\[alt](https://tracker.example/pixel.png)" in out
    assert not out.startswith("![")

    # Multiple links in one field — both get escaped
    out = _sanitize_enrichment_text("[one](a) and [two](b)")
    assert out.count("\\[") == 2
    for i, ch in enumerate(out):
        if ch == "[":
            assert i > 0 and out[i - 1] == "\\"


def test_build_enrichment_section_defuses_injection_payloads():
    from watercooler.pulse_report_lib import _build_enrichment_section

    md = _build_enrichment_section(
        {
            "situation_trajectory": "## Fake heading\n\nmore text",
            "recommended_focus": "> ## smuggled",
            "tension_signals": ["- already bulleted", "---", "line\nbreak"],
            "coordination_risks": ["normal risk"],
        }
    )
    # No spurious headings beyond the section title itself
    assert md.count("## ") == 1
    assert "## Snapshot analysis" in md
    # Horizontal-rule line stripped out entirely (collapsed to empty, dropped)
    assert "\n---\n" not in md
    # Stacked quote+heading defused
    assert "> ## smuggled" not in md
    assert "smuggled" in md
    # Multi-line tension collapsed to single bullet line
    assert "- line break" in md
    # Already-bulleted item is not double-bulleted
    assert "- already bulleted" in md
    assert "- - already" not in md


# ---------------------------------------------------------------------------
# Signal 4 — Coordination Insights (tests 12-17)
# ---------------------------------------------------------------------------


def _make_lead_dict(
    topic: str = "my-thread",
    summary: str = "Thread has open plan",
    relevance_tags: list[str] | None = None,
    source_category: str = "stalled_open_loop",
    t2_context: dict | None = None,
) -> dict:
    """Build a serialised coordinator_lead finding dict."""
    if relevance_tags is None:
        relevance_tags = ["pm"]
    return {
        "finding_id": f"fid-{topic}",
        "daemon_name": "project_coordinator",
        "category": "coordinator_lead",
        "topic": topic,
        "severity": "warning",
        "details": {
            "lead": {
                "schema_version": 1,
                "source_category": source_category,
                "source_topic": topic,
                "summary": summary,
                "relevance_tags": relevance_tags,
                "suggested_action": None,
                "t2_context": t2_context,
            }
        },
    }


def test_render_coordination_insights_groups_by_primary_relevance_tag():
    """Test 12: lead with relevance_tags=('planner', 'critic') appears under planner only."""
    lead = _make_lead_dict(
        topic="thread-x",
        summary="planning stall",
        relevance_tags=["planner", "critic"],
    )
    output = _render_coordination_insights([lead])
    assert "thread-x" in output
    assert "planning stall" in output
    # Appears exactly once — not duplicated under critic
    assert output.count("thread-x") == 1
    assert "Planner" in output
    # 'critic' may or may not appear as a group heading — but thread-x should not repeat
    assert output.count("thread-x") == 1


def test_render_coordination_insights_empty_relevance_tags_fallback():
    """Test 12b: lead with relevance_tags=() → rendered in 'general' group, no IndexError."""
    lead = _make_lead_dict(
        topic="thread-y",
        summary="needs attention",
        relevance_tags=[],
    )
    output = _render_coordination_insights([lead])
    assert "thread-y" in output
    assert "needs attention" in output
    assert "General" in output


def test_render_coordination_insights_respects_display_cap():
    """Test 13: 15 leads provided → only _INSIGHTS_DISPLAY_CAP (10) rendered."""
    leads = [
        _make_lead_dict(
            topic=f"thread-{i}", summary=f"summary {i}", relevance_tags=["pm"]
        )
        for i in range(15)
    ]
    output = _render_coordination_insights(leads)
    # Count how many distinct topics appear
    count = sum(1 for i in range(15) if f"thread-{i}" in output)
    assert count == _INSIGHTS_DISPLAY_CAP


def test_render_coordination_insights_with_t2_context():
    """Test 14: lead with t2_context populated → days_since_last callout line appears."""
    lead = _make_lead_dict(
        topic="enriched-thread",
        summary="enriched summary",
        t2_context={
            "schema_version": 2,
            "days_since_last": 12,
            "workflow_shape_name": "waterfall",
            "workflow_shape_id": "wf1",
            "workflow_confidence": 0.85,
            "analysis_stalled": False,
            "has_decision": False,
            "has_closure": False,
            "entry_count_total": 8,
            "recommendation_rule_ids": ["R03"],
        },
    )
    output = _render_coordination_insights([lead])
    assert "enriched-thread" in output
    assert "12d since last entry" in output
    assert "waterfall" in output
    assert "R03" in output


def test_assemble_report_includes_signal4_when_leads_present():
    """Test 15: coordinator_leads=[...] → 'Coordination Insights' section in output."""
    from dataclasses import replace

    lead = _make_lead_dict(topic="topic-a", summary="needs help")
    inputs = replace(_make_minimal_inputs(), coordinator_leads=[lead])
    report = assemble_report(inputs)
    assert "Coordination Insights" in report.markdown
    assert "topic-a" in report.markdown


def test_assemble_report_omits_signal4_when_leads_none():
    """Test 16: coordinator_leads=None → no 'Coordination Insights' section."""
    inputs = _make_minimal_inputs()
    # coordinator_leads defaults to None — no override needed
    report = assemble_report(inputs)
    assert "Coordination Insights" not in report.markdown


def test_assemble_report_omits_signal4_when_leads_empty_list():
    """Test 17: coordinator_leads=[] → no 'Coordination Insights' section (falsy guard)."""
    from dataclasses import replace

    inputs = replace(_make_minimal_inputs(), coordinator_leads=[])
    report = assemble_report(inputs)
    assert "Coordination Insights" not in report.markdown


def test_render_coordination_insights_sanitizes_injection_in_summary():
    """Test 18 — #316: summary with embedded heading smuggling is sanitized before render."""
    lead = _make_lead_dict(
        topic="safe-topic",
        summary="## Injected Section\nmalicious content",
        relevance_tags=["pm"],
    )
    output = _render_coordination_insights([lead])
    # Heading smuggling must be stripped by _sanitize_enrichment_text
    assert "## Injected Section" not in output
    # Content still appears (sanitized form), topic rendered
    assert "safe-topic" in output


def test_render_coordination_insights_sanitizes_injection_in_t2_shape_name():
    """Test 19 — #316: t2_context shape_name with markdown special chars is sanitized."""
    lead = _make_lead_dict(
        topic="t2-topic",
        summary="normal summary",
        relevance_tags=["implementer"],
        t2_context={
            "schema_version": 2,
            "analysis_stalled": False,
            "days_since_last": 5.0,
            "workflow_shape_id": "wf-1",
            "workflow_shape_name": "## Injected Shape",
            "workflow_confidence": 0.9,
            "has_decision": False,
            "has_closure": False,
            "entry_count_total": 10,
            "recommendation_rule_ids": [],
        },
    )
    output = _render_coordination_insights([lead])
    assert "## Injected Shape" not in output
    assert "t2-topic" in output


def test_render_coordination_insights_accepts_v2_t2_context():
    """Test 20 — 3b-1: v2 t2_context with analysis_stalled renders without error."""
    lead = _make_lead_dict(
        topic="v2-thread",
        summary="v2 context test",
        t2_context={
            "schema_version": 2,
            "analysis_stalled": True,
            "days_since_last": 20,
            "workflow_shape_name": "linear",
            "workflow_shape_id": "wf2",
            "workflow_confidence": 0.75,
            "has_decision": False,
            "has_closure": False,
            "entry_count_total": 12,
            "recommendation_rule_ids": ["R01"],
        },
    )
    output = _render_coordination_insights([lead])
    assert "v2-thread" in output
    assert "20d since last entry" in output
    assert "linear" in output
    assert "R01" in output
