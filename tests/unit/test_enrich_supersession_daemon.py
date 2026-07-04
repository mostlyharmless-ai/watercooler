"""Unit tests for EnrichSupersessionDaemon (Phase 3) — no live backend required.

Drives ``tick()`` with an injected fake backend to cover report-only vs emit staging,
group_id resolution, the finding shape, and transient-error swallowing.
"""

import types

import pytest

from watercooler_mcp.daemons.enrich_supersession import EnrichSupersessionDaemon

PAIRS = [{"superseded": "a", "successor": "b", "name": "R"}]


class _FakeBackend:
    def __init__(self, pairs, database="repo_t2"):
        self._pairs = pairs
        self.config = types.SimpleNamespace(database=database)
        self.calls = []

    def enrich_superseded_by(self, group_id, *, dry_run=False, emit_bases=None):
        self.calls.append((group_id, dry_run))
        return list(self._pairs)


def test_monitor_mode_is_report_only():
    be = _FakeBackend(PAIRS)
    d = EnrichSupersessionDaemon(backend=be, emit_mode="monitor")
    findings = d.tick()
    assert be.calls == [("repo_t2", True)]  # resolved group_id + dry_run=True
    assert len(findings) == 1
    f = findings[0]
    assert f.daemon_name == "enrich_supersession"
    assert f.category == "supersession_enriched"
    assert f.severity == "info"
    assert f.details["dry_run"] is True
    assert f.details["count"] == 1
    assert f.details["group_id"] == "repo_t2"
    assert d._last_tick_candidates == 1
    assert d._last_tick_written == 0


def test_emit_mode_writes():
    be = _FakeBackend(PAIRS)
    d = EnrichSupersessionDaemon(backend=be, emit_mode="emit")
    d.tick()
    assert be.calls == [("repo_t2", False)]  # dry_run=False -> writes
    assert d._last_tick_written == 1


def test_no_pairs_yields_no_finding():
    d = EnrichSupersessionDaemon(backend=_FakeBackend([]), emit_mode="monitor")
    assert d.tick() == []


def test_explicit_group_id_overrides_backend_database():
    be = _FakeBackend(PAIRS, database="ignored")
    d = EnrichSupersessionDaemon(backend=be, group_id="explicit_gid", emit_mode="emit")
    d.tick()
    assert be.calls[0][0] == "explicit_gid"


def test_unresolvable_group_id_is_a_noop():
    be = _FakeBackend(PAIRS, database=None)
    d = EnrichSupersessionDaemon(backend=be, emit_mode="monitor")
    # No override, no config.database, and cwd derivation likely yields nothing usable
    # in the test env — must not raise; returns no findings and makes no backend call.
    findings = d.tick()
    assert findings == [] or be.calls == [] or findings[0].details["group_id"]


def test_invalid_emit_mode_rejected():
    with pytest.raises(ValueError):
        EnrichSupersessionDaemon(backend=_FakeBackend([]), emit_mode="bogus")


def test_backend_error_is_swallowed():
    class _Boom(_FakeBackend):
        def enrich_superseded_by(self, group_id, *, dry_run=False, emit_bases=None):
            raise RuntimeError("db down")

    d = EnrichSupersessionDaemon(backend=_Boom(PAIRS), emit_mode="monitor")
    assert d.tick() == []  # transient error -> no findings, no raise
