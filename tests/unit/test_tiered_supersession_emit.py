"""Tiered supersession emit (Decision 01KWJK1CS4C5DY8CS735ZBMMQP).

Emit writes afforded ``superseded_by`` links for strong-basis pairs only;
``temporal_only`` pairs are returned (``written: False``) but never written.
Covers the backend filter, the daemon's written/held accounting, and the
config validator.
"""

from __future__ import annotations

import types

import pytest

from watercooler.config_schema import EnrichSupersessionConfig
from watercooler_memory.backends.graphiti import (
    DEFAULT_EMIT_BASES,
    GraphitiBackend,
)
from watercooler_mcp.daemons.enrich_supersession import EnrichSupersessionDaemon


# --------------------------------------------------------------------- #
# Backend filter (unbound method + fake graph — no FalkorDB needed)
# --------------------------------------------------------------------- #


class _FakeGraph:
    """Captures queries; serves a canned read result."""

    def __init__(self, read_rows):
        self._read_rows = read_rows
        self.writes: list[dict] = []

    def query(self, cypher, params=None):
        if "SET e.superseded_by" in cypher:
            self.writes.append(dict(params))
            return types.SimpleNamespace(result_set=[])
        return types.SimpleNamespace(result_set=self._read_rows)


def _fake_backend_self(graph):
    return types.SimpleNamespace(
        config=types.SimpleNamespace(database="db_t2"),
        _sanitize_thread_id=lambda self_or_gid: "db_t2",
        _falkor_graph=lambda name: graph,
    )


# Read rows: e.uuid, source_node_uuid, name, valid_at, invalid_at,
# superseded_by, created_at
_T0, _T1, _T2 = "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"
_ROWS = [
    # strong pair: superseded s1 → successor u1 (same source X, same name R)
    ["s1", "X", "R", _T0, _T1, None, "c1"],
    ["u1", "X", "R", _T1, None, None, "c2"],
    # weak pair: superseded s2 → successor u2 (nothing shared but the instant)
    ["s2", "Y", "Q", _T0, _T2, None, "c3"],
    ["u2", "Z", "P", _T2, None, None, "c4"],
]


def _run_enrich(graph, **kwargs):
    fake = _fake_backend_self(graph)
    # Unbound call: reuse the real method against the fake self.
    return GraphitiBackend.enrich_superseded_by(fake, "db_t2", **kwargs)


def test_emit_writes_strong_basis_only():
    g = _FakeGraph(_ROWS)
    pairs = _run_enrich(g, dry_run=False)

    by_uuid = {p["superseded"]: p for p in pairs}
    assert by_uuid["s1"]["basis"] == "same_source_and_name"
    assert by_uuid["s1"]["written"] is True
    assert by_uuid["s2"]["basis"] == "temporal_only"
    assert by_uuid["s2"]["written"] is False

    # Exactly one write hit the graph — the strong pair.
    assert [w["uuid"] for w in g.writes] == ["s1"]


def test_dry_run_marks_would_write_but_writes_nothing():
    g = _FakeGraph(_ROWS)
    pairs = _run_enrich(g, dry_run=True)
    assert g.writes == []
    flags = {p["superseded"]: p["written"] for p in pairs}
    assert flags == {"s1": True, "s2": False}


def test_emit_bases_override_can_include_temporal_only():
    g = _FakeGraph(_ROWS)
    pairs = _run_enrich(
        g, dry_run=False, emit_bases=DEFAULT_EMIT_BASES | {"temporal_only"}
    )
    assert {w["uuid"] for w in g.writes} == {"s1", "s2"}
    assert all(p["written"] for p in pairs)


# --------------------------------------------------------------------- #
# Daemon accounting
# --------------------------------------------------------------------- #


class _FakeTieredBackend:
    def __init__(self, pairs):
        self._pairs = pairs
        self.config = types.SimpleNamespace(database="db_t2")
        self.kwargs: list[dict] = []

    def enrich_superseded_by(self, group_id, *, dry_run=False, emit_bases=None):
        self.kwargs.append({"dry_run": dry_run, "emit_bases": emit_bases})
        return list(self._pairs)


_TIERED_PAIRS = [
    {"superseded": "s1", "successor": "u1", "name": "R",
     "basis": "same_source_and_name", "written": True},
    {"superseded": "s2", "successor": "u2", "name": "Q",
     "basis": "temporal_only", "written": False},
]


def test_daemon_reports_written_and_held():
    be = _FakeTieredBackend(_TIERED_PAIRS)
    d = EnrichSupersessionDaemon(backend=be, emit_mode="emit")
    d._emit_supersession_candidates = lambda gid: None  # not under test
    findings = d.tick()

    assert d._last_tick_candidates == 2
    assert d._last_tick_written == 1  # only the strong pair counts as written
    f = findings[0]
    assert f.details["eligible"] == 1
    assert f.details["held"] == 1
    assert "held 1 below emit tier" in f.message


def test_daemon_passes_configured_bases_to_backend():
    be = _FakeTieredBackend(_TIERED_PAIRS)
    d = EnrichSupersessionDaemon(
        backend=be, emit_mode="emit", emit_bases=frozenset({"same_source"})
    )
    d._emit_supersession_candidates = lambda gid: None
    d.tick()
    assert be.kwargs[0]["emit_bases"] == frozenset({"same_source"})


def test_daemon_monitor_mode_written_stays_zero():
    be = _FakeTieredBackend(_TIERED_PAIRS)
    d = EnrichSupersessionDaemon(backend=be, emit_mode="monitor")
    d.tick()
    assert be.kwargs[0]["dry_run"] is True
    assert d._last_tick_written == 0


# --------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------- #


def test_config_default_is_strong_tier():
    cfg = EnrichSupersessionConfig()
    assert set(cfg.emit_bases) == DEFAULT_EMIT_BASES
    assert "temporal_only" not in cfg.emit_bases


def test_config_rejects_unknown_basis():
    with pytest.raises(ValueError, match="unknown bases"):
        EnrichSupersessionConfig(emit_bases=["same_source", "vibes"])


def test_config_allows_explicit_temporal_only_opt_in():
    cfg = EnrichSupersessionConfig(emit_bases=["temporal_only"])
    assert cfg.emit_bases == ["temporal_only"]
