"""Unit tests for watercooler.analysis_lib — extracted analysis library."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from watercooler.analysis_lib import (
    SHAPE_NAMES,
    build_pulse_block,
    classify_thread_shape,
    evaluate_rules,
    iso_week_key,
    list_topics,
    normalize_agent,
    parse_ts,
    read_thread_meta,
    run_analysis,
    ts_to_iso,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

FIXTURE_GRAPH = Path(__file__).resolve().parent.parent / "integration" / ".cli-threads" / "graph" / "baseline"


def _make_graph_dir(tmp_path: Path, topics: dict[str, list[dict[str, Any]]]) -> Path:
    """Create a minimal graph directory with given topics and entries."""
    graph_dir = tmp_path / "graph" / "baseline"
    for topic, entries in topics.items():
        thread_dir = graph_dir / "threads" / topic
        thread_dir.mkdir(parents=True)
        meta = {"status": "open"}
        (thread_dir / "meta.json").write_text(json.dumps(meta))
        with open(thread_dir / "entries.jsonl", "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
    return graph_dir


def _make_entry(
    index: int,
    role: str = "implementer",
    entry_type: str = "Note",
    agent: str = "Claude Code (jay)",
    days_ago: int = 3,
) -> dict[str, Any]:
    """Build a minimal entry dict."""
    ts = datetime.now(tz=timezone.utc) - timedelta(days=days_ago)
    return {
        "index": index,
        "role": role,
        "entry_type": entry_type,
        "agent": agent,
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "title": f"Test entry {index}",
    }


# ---------------------------------------------------------------------------
# timestamp helpers
# ---------------------------------------------------------------------------

def test_parse_ts_utc():
    dt = parse_ts("2026-03-15T10:00:00Z")
    assert dt.tzinfo is not None
    assert dt.year == 2026


def test_ts_to_iso():
    dt = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
    assert ts_to_iso(dt) == "2026-03-15T10:00:00Z"


def test_iso_week_key():
    dt = datetime(2026, 3, 15, tzinfo=timezone.utc)
    key = iso_week_key(dt)
    assert key.startswith("2026-W")


# ---------------------------------------------------------------------------
# normalize_agent
# ---------------------------------------------------------------------------

def test_normalize_agent_strips_platform():
    assert normalize_agent("Claude Code (jay)") == "jay"


def test_normalize_agent_passthrough():
    assert normalize_agent("jay") == "jay"


def test_normalize_agent_double_wrapped():
    """Daemon-written entries had user_tag='(system)' producing 'Daemon ((system))'."""
    assert normalize_agent("Daemon ((system))") == "system"


def test_normalize_agent_unclosed_paren():
    """Belt-and-suspenders for entries already stored with '(system' as agent."""
    assert normalize_agent("(system") == "system"


def test_normalize_agent_empty_tag_passthrough():
    """Empty parens should not extract an empty string."""
    assert normalize_agent("Agent ()") == "Agent ()"


# ---------------------------------------------------------------------------
# list_topics / read_thread_meta
# ---------------------------------------------------------------------------

def test_list_topics_finds_threads(tmp_path):
    graph_dir = _make_graph_dir(tmp_path, {
        "topic-a": [_make_entry(0)],
        "topic-b": [_make_entry(0)],
    })
    topics = list_topics(graph_dir)
    assert set(topics) == {"topic-a", "topic-b"}


def test_list_topics_empty(tmp_path):
    assert list_topics(tmp_path / "nonexistent") == []


def test_read_thread_meta(tmp_path):
    graph_dir = _make_graph_dir(tmp_path, {"t": [_make_entry(0)]})
    meta = read_thread_meta(graph_dir, "t")
    assert meta["status"] == "open"


# ---------------------------------------------------------------------------
# classify_thread_shape
# ---------------------------------------------------------------------------

def test_classify_shape_too_few_entries():
    entries = [_make_entry(0), _make_entry(1)]
    shape = classify_thread_shape(entries)
    assert shape["shape_id"] == "S06"


def test_classify_shape_returns_valid_shape():
    entries = [
        _make_entry(i, role="planner", entry_type="Plan") for i in range(4)
    ] + [
        _make_entry(i + 4, role="implementer") for i in range(3)
    ] + [
        _make_entry(7, role="critic", entry_type="Decision"),
        _make_entry(8, role="tester", entry_type="Closure"),
    ]
    shape = classify_thread_shape(entries)
    assert shape["shape_id"] in SHAPE_NAMES


# ---------------------------------------------------------------------------
# evaluate_rules
# ---------------------------------------------------------------------------

def test_evaluate_rules_fires_r05_on_low_closure():
    metrics = {
        "closure_rate": 0.20,
        "review_capture_rate": 0.50,
        "stalled_thread_count": 0,
    }
    recs = evaluate_rules(
        metrics=metrics,
        contributors={},
        window_thread_records=[],
        now=datetime.now(tz=timezone.utc),
    )
    rule_ids = [r["rule_id"] for r in recs]
    assert "R05" in rule_ids


def test_evaluate_rules_no_rules_when_healthy():
    metrics = {
        "closure_rate": 0.80,
        "review_capture_rate": 0.60,
        "stalled_thread_count": 0,
    }
    recs = evaluate_rules(
        metrics=metrics,
        contributors={},
        window_thread_records=[],
        now=datetime.now(tz=timezone.utc),
    )
    assert len(recs) == 0


# ---------------------------------------------------------------------------
# build_pulse_block
# ---------------------------------------------------------------------------

def test_build_pulse_block_includes_stalled():
    thread_records = [
        {
            "topic": "stale-thread",
            "stalled": True,
            "days_since_last": 20,
            "last_entry_timestamp": "2026-03-01T00:00:00Z",
            "contributors": ["jay"],
        },
    ]
    pb = build_pulse_block(
        recommendations=[],
        window_thread_records=thread_records,
        contributors={},
        shape_distribution={},
        stalled_topics=None,
    )
    assert pb["pulse_block_version"] == "1.0"
    assert len(pb["stalled_threads"]) == 1
    assert pb["stalled_threads"][0]["topic"] == "stale-thread"


# ---------------------------------------------------------------------------
# run_analysis — integration-level
# ---------------------------------------------------------------------------

def test_run_analysis_returns_expected_schema(tmp_path):
    entries = [_make_entry(i, days_ago=2) for i in range(5)]
    entries.append(_make_entry(5, role="critic", entry_type="Decision", days_ago=1))
    graph_dir = _make_graph_dir(tmp_path, {"test-thread": entries})
    since = datetime.now(tz=timezone.utc) - timedelta(days=7)

    result = run_analysis(
        graph_dir=graph_dir,
        since_dt=since,
        include_closed=False,
        code_branch="*",
    )

    assert result["schema_version"] == "1.3"
    assert "generated_at" in result
    assert result["corpus"]["window_entry_count"] == 6
    assert result["corpus"]["window_thread_count"] == 1
    assert "pulse_block" in result
    assert result["pulse_block"]["pulse_block_version"] == "1.0"


def test_run_analysis_empty_window(tmp_path):
    entries = [_make_entry(i, days_ago=30) for i in range(3)]
    graph_dir = _make_graph_dir(tmp_path, {"old-thread": entries})
    since = datetime.now(tz=timezone.utc) - timedelta(days=1)

    result = run_analysis(
        graph_dir=graph_dir,
        since_dt=since,
        include_closed=False,
        code_branch="*",
    )

    assert result["corpus"]["window_entry_count"] == 0


@pytest.mark.skipif(
    not FIXTURE_GRAPH.exists(),
    reason="integration fixture graph not present",
)
def test_run_analysis_on_fixture_graph():
    """Run against the integration fixture graph to verify no crashes."""
    since = datetime(2020, 1, 1, tzinfo=timezone.utc)
    result = run_analysis(
        graph_dir=FIXTURE_GRAPH,
        since_dt=since,
        include_closed=True,
        code_branch="*",
    )
    assert result["schema_version"] == "1.3"
    assert result["corpus"]["window_entry_count"] > 0
