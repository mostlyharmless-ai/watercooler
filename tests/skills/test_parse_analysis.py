"""Unit tests for parse_analysis.py evaluate_rules() and build_pulse_block()."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add the skills script directory to sys.path so we can import parse_analysis
_SCRIPTS_DIR = str(
  Path(__file__).resolve().parents[2]
  / ".claude" / "skills" / "watercooler-analysis" / "scripts"
)
if _SCRIPTS_DIR not in sys.path:
  sys.path.insert(0, _SCRIPTS_DIR)

from parse_analysis import RULE_THRESHOLDS, build_pulse_block, evaluate_rules

NOW = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)


def make_thread_record(**overrides):
  """Factory for minimal valid thread records."""
  defaults = {
    "topic": "test-thread",
    "status": "open",
    "entry_count": 5,
    "entry_count_total": 5,
    "out_of_window": False,
    "has_closure": False,
    "has_critic": False,
    "has_decision": False,
    "last_entry_timestamp": "2026-03-19T12:00:00Z",
    "days_since_last": 1,
    "stalled": False,
    "workflow_shape": {"shape_id": "S06", "shape_name": "Unclassified / mixed", "confidence": 0.0, "qualifier": ""},
    "role_distribution": {"implementer": 0.5, "planner": 0.2, "critic": 0.1, "tester": 0.1, "pm": 0.05, "scribe": 0.05},
    "entry_type_distribution": {"Note": 0.8, "Decision": 0.0, "Plan": 0.1, "PR": 0.0, "Closure": 0.1},
    "contributors": ["alice"],
    "entries": [],
    "decision_timestamps": [],
  }
  defaults.update(overrides)
  return defaults


def make_contributor(**overrides):
  """Factory for minimal valid contributor profiles."""
  defaults = {
    "entry_count": 10,
    "role_distribution": {"implementer": 0.4, "planner": 0.2, "critic": 0.1, "tester": 0.1, "pm": 0.1, "scribe": 0.1},
    "type_distribution": {"Note": 0.6, "Decision": 0.1, "Plan": 0.1, "PR": 0.1, "Closure": 0.1},
    "threads_contributed": ["test-thread"],
    "dominant_role": "implementer",
    "dominant_type": "Note",
    "shape_distribution": {"S01": 0, "S02": 0, "S03": 0, "S04": 0, "S05": 0, "S06": 1},
    "dominant_shapes": ["S06"],
    "behavioral_profile": "test profile",
  }
  defaults.update(overrides)
  return defaults


def _base_metrics(closure_rate=0.70, review_capture_rate=0.50, stalled_thread_count=0):
  return {
    "closure_rate": closure_rate,
    "review_capture_rate": review_capture_rate,
    "stalled_thread_count": stalled_thread_count,
  }


# ---------------------------------------------------------------------------
# R01 / R05 mutual exclusion
# ---------------------------------------------------------------------------

def test_r05_fires_when_closure_rate_below_040():
  recs = evaluate_rules(_base_metrics(closure_rate=0.30), {}, [], NOW)
  rule_ids = [r["rule_id"] for r in recs]
  assert "R05" in rule_ids
  assert "R01" not in rule_ids


def test_r01_fires_when_closure_rate_between_040_and_060():
  recs = evaluate_rules(_base_metrics(closure_rate=0.50), {}, [], NOW)
  rule_ids = [r["rule_id"] for r in recs]
  assert "R01" in rule_ids
  assert "R05" not in rule_ids


# ---------------------------------------------------------------------------
# R02
# ---------------------------------------------------------------------------

def test_r02_fires_when_review_capture_low():
  recs = evaluate_rules(_base_metrics(review_capture_rate=0.20), {}, [], NOW)
  rule_ids = [r["rule_id"] for r in recs]
  assert "R02" in rule_ids


def test_r02_does_not_fire_when_review_capture_ok():
  recs = evaluate_rules(_base_metrics(review_capture_rate=0.50), {}, [], NOW)
  rule_ids = [r["rule_id"] for r in recs]
  assert "R02" not in rule_ids


# ---------------------------------------------------------------------------
# R03
# ---------------------------------------------------------------------------

def test_r03_fires_with_stalled_threads():
  threads = [
    make_thread_record(topic="stalled-1", stalled=True),
    make_thread_record(topic="stalled-2", stalled=True),
  ]
  recs = evaluate_rules(_base_metrics(stalled_thread_count=2), {}, threads, NOW)
  r03 = [r for r in recs if r["rule_id"] == "R03"]
  assert len(r03) == 1
  assert set(r03[0]["affected_threads"]) == {"stalled-1", "stalled-2"}


def test_r03_suppressed_when_thread_has_closure():
  threads = [make_thread_record(topic="closed-stalled", stalled=True, has_closure=True)]
  recs = evaluate_rules(_base_metrics(stalled_thread_count=1), {}, threads, NOW)
  r03 = [r for r in recs if r["rule_id"] == "R03"]
  assert len(r03) == 0


def test_r03_suppressed_when_recent_decision():
  recent_ts = (NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
  threads = [make_thread_record(
    topic="decided-stalled", stalled=True, decision_timestamps=[recent_ts],
  )]
  recs = evaluate_rules(_base_metrics(stalled_thread_count=1), {}, threads, NOW)
  r03 = [r for r in recs if r["rule_id"] == "R03"]
  assert len(r03) == 0


def test_r03_not_suppressed_when_old_decision():
  old_ts = (NOW - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
  threads = [make_thread_record(
    topic="old-decision-stalled", stalled=True, decision_timestamps=[old_ts],
  )]
  recs = evaluate_rules(_base_metrics(stalled_thread_count=1), {}, threads, NOW)
  r03 = [r for r in recs if r["rule_id"] == "R03"]
  assert len(r03) == 1


# ---------------------------------------------------------------------------
# R04
# ---------------------------------------------------------------------------

def test_r04_fires_for_planner_tester_gap():
  contributors = {
    "alice": make_contributor(role_distribution={
      "implementer": 0.0, "planner": 0.60, "critic": 0.0,
      "tester": 0.0, "pm": 0.2, "scribe": 0.2,
    }),
  }
  recs = evaluate_rules(_base_metrics(), contributors, [], NOW)
  r04 = [r for r in recs if r["rule_id"] == "R04"]
  assert len(r04) == 1


# ---------------------------------------------------------------------------
# R06 corpus-level
# ---------------------------------------------------------------------------

def test_r06_fires_when_heavy_implementer_low_critic():
  corpus_rd = {"implementer": 0.80, "critic": 0.05, "planner": 0.05, "tester": 0.05, "pm": 0.03, "scribe": 0.02}
  recs = evaluate_rules(_base_metrics(), {}, [], NOW, corpus_role_distribution=corpus_rd)
  r06 = [r for r in recs if r["rule_id"] == "R06"]
  assert len(r06) == 1


def test_r06_does_not_fire_when_implementer_balanced():
  corpus_rd = {"implementer": 0.50, "critic": 0.15, "planner": 0.15, "tester": 0.10, "pm": 0.05, "scribe": 0.05}
  recs = evaluate_rules(_base_metrics(), {}, [], NOW, corpus_role_distribution=corpus_rd)
  r06 = [r for r in recs if r["rule_id"] == "R06"]
  assert len(r06) == 0


# ---------------------------------------------------------------------------
# build_pulse_block
# ---------------------------------------------------------------------------

def test_build_pulse_block_empty_recommendations():
  pb = build_pulse_block([], [], {}, {"S01": {"count": 0, "pct": 0.0}})
  assert pb["coordination_risks"] == []
  assert pb["stalled_threads"] == []
  assert pb["recommended_pairings"] == []
  assert pb["top_actions"] == []
  assert "pulse_block_version" in pb


def test_build_pulse_block_stalled_filter_empty_list():
  threads = [make_thread_record(topic="stalled-a", stalled=True)]
  pb = build_pulse_block([], threads, {}, {}, stalled_topics=[])
  assert pb["stalled_threads"] == []


def test_build_pulse_block_stalled_filter_none_passes_all():
  threads = [make_thread_record(topic="stalled-a", stalled=True)]
  pb = build_pulse_block([], threads, {}, {}, stalled_topics=None)
  assert len(pb["stalled_threads"]) == 1


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------

def test_pairing_zero_complementarity_single_contributor():
  contributors = {"alice": make_contributor()}
  recs = [{
    "rule_id": "R04", "text": "test", "confidence": 0.72,
    "priority": "monitor", "affected_threads": [], "affected_contributors": ["alice"],
  }]
  pb = build_pulse_block(recs, [], contributors, {})
  assert len(pb["recommended_pairings"]) == 1
  assert pb["recommended_pairings"][0]["recommended_partner"] is None


def test_pairing_positive_complementarity():
  contributors = {
    "alice": make_contributor(role_distribution={
      "implementer": 0.60, "planner": 0.30, "critic": 0.0,
      "tester": 0.0, "pm": 0.05, "scribe": 0.05,
    }),
    "bob": make_contributor(role_distribution={
      "implementer": 0.10, "planner": 0.05, "critic": 0.30,
      "tester": 0.40, "pm": 0.10, "scribe": 0.05,
    }),
  }
  recs = [{
    "rule_id": "R04", "text": "test", "confidence": 0.72,
    "priority": "monitor", "affected_threads": [], "affected_contributors": ["alice"],
  }]
  pb = build_pulse_block(recs, [], contributors, {})
  assert len(pb["recommended_pairings"]) == 1
  assert pb["recommended_pairings"][0]["recommended_partner"] == "bob"


# ---------------------------------------------------------------------------
# top_actions capped at 3
# ---------------------------------------------------------------------------

def test_top_actions_capped_at_3():
  recs = [
    {"rule_id": f"R0{i}", "text": f"rec {i}", "confidence": 0.9 - i * 0.05,
     "priority": "actionable", "affected_threads": [], "affected_contributors": []}
    for i in range(1, 6)
  ]
  pb = build_pulse_block(recs, [], {}, {})
  assert len(pb["top_actions"]) == 3


# ---------------------------------------------------------------------------
# R04 negative (gap below threshold)
# ---------------------------------------------------------------------------

def test_r04_does_not_fire_when_gap_below_threshold():
  contributors = {
    "alice": make_contributor(role_distribution={
      "implementer": 0.2, "planner": 0.30, "critic": 0.1,
      "tester": 0.10, "pm": 0.15, "scribe": 0.15,
    }),
  }
  recs = evaluate_rules(_base_metrics(), contributors, [], NOW)
  r04 = [r for r in recs if r["rule_id"] == "R04"]
  assert len(r04) == 0


# ---------------------------------------------------------------------------
# Boundary value tests for R01/R05
# ---------------------------------------------------------------------------

def test_r01_does_not_fire_at_exact_060():
  recs = evaluate_rules(_base_metrics(closure_rate=0.60), {}, [], NOW)
  rule_ids = [r["rule_id"] for r in recs]
  assert "R01" not in rule_ids
  assert "R05" not in rule_ids


def test_r05_not_r01_at_exact_040():
  recs = evaluate_rules(_base_metrics(closure_rate=0.40), {}, [], NOW)
  rule_ids = [r["rule_id"] for r in recs]
  assert "R01" in rule_ids
  assert "R05" not in rule_ids


# ---------------------------------------------------------------------------
# coordination_risks filters by priority
# ---------------------------------------------------------------------------

def test_coordination_risks_filters_monitor_priority():
  recs = [
    {"rule_id": "R01", "text": "test", "confidence": 0.85,
     "priority": "actionable", "affected_threads": [], "affected_contributors": []},
    {"rule_id": "R04", "text": "test", "confidence": 0.72,
     "priority": "monitor", "affected_threads": [], "affected_contributors": ["alice"]},
  ]
  pb = build_pulse_block(recs, [], {"alice": make_contributor()}, {})
  cr_ids = [r["rule_id"] for r in pb["coordination_risks"]]
  assert "R01" in cr_ids
  assert "R04" not in cr_ids
