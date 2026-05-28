"""Tests for `ProjectCoordinatorDaemon._load_extras` / `_save_extras`
scoping under `projects[scope_id]["project_coordinator"]` per #561.

Pre-fix: extras were stored flat at the checkpoint root. Post-fix
they live under `projects[scope_id]["project_coordinator"]` —
matching the established pattern from pulse_snapshot,
analysis_snapshot, and trend_snapshot. Migration is one-shot:
loading a legacy flat checkpoint preserves state in-memory; the next
save writes to the new path and removes the legacy flat keys.

Closes #561 (cloud Design (hosted) v3 entry
`01KR5TWPZWB9SDEC03Z9ESX2KJ` Fix 5; execution-roadmap
`01KR5Y2A6FBS7T83K9ASY6B25P` H1 fourth item).
"""

from __future__ import annotations

import pytest

from watercooler.config_schema import ProjectCoordinatorConfig
from watercooler.project_coordinator_lib import (
    ActiveSignalEntry,
    BurstBaseline,
    CoordinatorExtras,
)
from watercooler_mcp.daemons.project_coordinator import ProjectCoordinatorDaemon


def _make_daemon(scope_id: str = "deadbeefdead") -> ProjectCoordinatorDaemon:
    """Construct a daemon with a fresh checkpoint and a stubbed scope_id.

    `_resolve_threads_dir` is the place that sets `_scope_id` in the
    real flow; for these focused tests we set it directly.
    """
    daemon = ProjectCoordinatorDaemon(
        interval=300,
        config=ProjectCoordinatorConfig(enabled=False),
    )
    daemon._scope_id = scope_id
    return daemon


class TestLoadExtrasNewShape:
    """`_load_extras` reads from the new scoped path when present."""

    def test_loads_from_projects_scope_path(self) -> None:
        """Standard post-fix path: `projects[scope_id][project_coordinator]`."""
        d = _make_daemon(scope_id="abc123")
        d._checkpoint.extras = {
            "projects": {
                "abc123": {
                    "project_coordinator": {
                        "seen_contributors": {"caleb": 100.0, "jay": 200.0},
                    },
                },
            },
        }

        d._load_extras()
        assert d._extras.seen_contributors == {"caleb": 100.0, "jay": 200.0}

    def test_other_scopes_extras_are_not_loaded(self) -> None:
        """Different scope_id's extras are isolated.

        This is the load-bearing invariant — pre-fix, two repos'
        contributors collided; post-fix they're isolated.
        """
        d = _make_daemon(scope_id="repo-a")
        d._checkpoint.extras = {
            "projects": {
                "repo-a": {
                    "project_coordinator": {"seen_contributors": {"a": 1.0}},
                },
                "repo-b": {
                    "project_coordinator": {"seen_contributors": {"b": 2.0}},
                },
            },
        }

        d._load_extras()
        assert d._extras.seen_contributors == {"a": 1.0}
        assert "b" not in d._extras.seen_contributors

    def test_empty_extras_yields_default(self) -> None:
        """No data anywhere → fresh empty CoordinatorExtras."""
        d = _make_daemon(scope_id="abc123")
        d._checkpoint.extras = {}

        d._load_extras()
        assert d._extras.seen_contributors == {}
        assert d._extras.burst_baselines == {}


class TestLoadExtrasLegacyMigration:
    """Migration path: pre-#561 flat extras at root are read once and
    written to the new shape on next save.
    """

    def test_legacy_flat_shape_loads_correctly(self) -> None:
        """Pre-fix flat shape is recognised and read into memory."""
        d = _make_daemon(scope_id="abc123")
        d._checkpoint.extras = {
            "seen_contributors": {"legacy_user": 50.0},
            "burst_baselines": {
                "old_topic": BurstBaseline(
                    baseline_rate=1.5,
                    last_entry_count=10,
                    last_tick_time=99.0,
                ).to_dict(),
            },
        }

        d._load_extras()
        assert d._extras.seen_contributors == {"legacy_user": 50.0}
        assert "old_topic" in d._extras.burst_baselines
        assert d._extras.burst_baselines["old_topic"].baseline_rate == 1.5

    def test_legacy_then_save_uses_new_shape(self) -> None:
        """After migrate-load, save writes to projects[scope] AND clears legacy keys."""
        d = _make_daemon(scope_id="abc123")
        d._checkpoint.extras = {
            "seen_contributors": {"legacy_user": 50.0},
        }

        d._load_extras()
        d._save_extras()

        # New shape present
        projects = d._checkpoint.extras.get("projects", {})
        assert "abc123" in projects
        pc_state = projects["abc123"]["project_coordinator"]
        assert pc_state["seen_contributors"] == {"legacy_user": 50.0}

        # Legacy flat key cleared
        assert "seen_contributors" not in d._checkpoint.extras
        assert "burst_baselines" not in d._checkpoint.extras

    def test_save_clears_every_persisted_legacy_key(self) -> None:
        """Migration cleanup removes EVERY field ``CoordinatorExtras.to_dict``
        persists at root, not just a subset.

        Regression for the omission caught in PR #793 review:
        ``last_stance_signatures`` was missing from the cleanup tuple,
        leaving an orphaned root key after migration. Subsequent loads
        prefer the scoped path, but the unused root key sat around as
        dead data.
        """
        d = _make_daemon(scope_id="abc123")
        # Plant every persisted field at root in the legacy flat shape.
        d._checkpoint.extras = {
            "seen_contributors": {"u": 1.0},
            "burst_baselines": {},
            "active_signals": {},
            "last_stance_signatures": {"sig-a": "hash-a"},
            "cleared_stance_fids": [],
        }

        d._load_extras()
        d._save_extras()

        # Every legacy root key cleared (none should leak as dead data).
        for legacy_key in (
            "seen_contributors",
            "burst_baselines",
            "active_signals",
            "last_stance_signatures",
            "cleared_stance_fids",
        ):
            assert legacy_key not in d._checkpoint.extras, (
                f"Legacy root key {legacy_key!r} should be cleared on migration"
            )


class TestSaveExtras:
    """`_save_extras` writes under projects[scope_id][project_coordinator]."""

    def test_save_to_new_shape(self) -> None:
        """Standard save: extras land under scoped path."""
        d = _make_daemon(scope_id="abc123")
        d._extras.seen_contributors["x"] = 1.0
        d._save_extras()

        projects = d._checkpoint.extras["projects"]
        assert projects["abc123"]["project_coordinator"]["seen_contributors"] == {
            "x": 1.0
        }

    def test_save_preserves_other_scopes(self) -> None:
        """Saving for repo-a does NOT clobber repo-b's existing state."""
        d = _make_daemon(scope_id="repo-a")
        d._checkpoint.extras = {
            "projects": {
                "repo-b": {
                    "project_coordinator": {"seen_contributors": {"b": 99.0}},
                },
            },
        }
        d._extras.seen_contributors["a"] = 1.0
        d._save_extras()

        projects = d._checkpoint.extras["projects"]
        # repo-b's pre-existing state is untouched.
        assert projects["repo-b"]["project_coordinator"]["seen_contributors"] == {
            "b": 99.0
        }
        # repo-a has its own scope state.
        assert projects["repo-a"]["project_coordinator"]["seen_contributors"] == {
            "a": 1.0
        }

    def test_save_with_unset_scope_id_uses_unscoped(self) -> None:
        """Empty scope_id → `_unscoped` bucket (defensive fallback)."""
        d = _make_daemon(scope_id="")
        d._extras.seen_contributors["x"] = 1.0
        d._save_extras()

        projects = d._checkpoint.extras["projects"]
        assert "_unscoped" in projects

    def test_round_trip_under_same_scope(self) -> None:
        """Save → load returns identical state."""
        d = _make_daemon(scope_id="abc123")
        d._extras.seen_contributors["x"] = 1.0
        d._extras.burst_baselines["t1"] = BurstBaseline(
            baseline_rate=2.0, last_entry_count=5, last_tick_time=42.0
        )
        d._extras.active_signals["t1"] = ActiveSignalEntry(
            categories={"stale_thread"}, last_evaluated_at=100.0
        )
        d._save_extras()

        # Construct a fresh daemon with the same scope and the saved
        # checkpoint; load should produce identical extras.
        d2 = _make_daemon(scope_id="abc123")
        d2._checkpoint.extras = d._checkpoint.extras
        d2._load_extras()

        assert d2._extras.seen_contributors == {"x": 1.0}
        assert "t1" in d2._extras.burst_baselines
        assert d2._extras.burst_baselines["t1"].baseline_rate == 2.0
        assert "t1" in d2._extras.active_signals
        assert d2._extras.active_signals["t1"].categories == {"stale_thread"}


class TestMultiRepoIsolation:
    """The load-bearing #561 fix: extras for repo-a don't leak to repo-b."""

    def test_two_scopes_have_isolated_state(self) -> None:
        """Two daemons, two scopes, one shared checkpoint dict — no leakage."""
        # Simulate one shared checkpoint dict (e.g., a future case where
        # one process serves multiple scopes; today's per-scope managers
        # already isolate, but the data shape must support it).
        shared_extras: dict = {}

        d_a = _make_daemon(scope_id="repo-a")
        d_a._checkpoint.extras = shared_extras
        d_a._extras.seen_contributors["alice"] = 1.0
        d_a._save_extras()

        d_b = _make_daemon(scope_id="repo-b")
        d_b._checkpoint.extras = shared_extras
        d_b._load_extras()
        # repo-b loads from a fresh slot — alice is repo-a's
        assert "alice" not in d_b._extras.seen_contributors

        d_b._extras.seen_contributors["bob"] = 2.0
        d_b._save_extras()

        # Verify both scopes coexist in the shared extras dict.
        projects = shared_extras["projects"]
        assert projects["repo-a"]["project_coordinator"]["seen_contributors"] == {
            "alice": 1.0
        }
        assert projects["repo-b"]["project_coordinator"]["seen_contributors"] == {
            "bob": 2.0
        }
