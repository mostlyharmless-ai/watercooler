"""Tests for ``HostedDaemonCoordinator.acknowledge_finding`` keep-alive.

Closes #607 — pre-fix, ``acknowledge_finding`` neither acquired
``self._lock`` nor refreshed ``entry.last_touched``, so a user
acknowledging findings could have their scope reaped by the idle-TTL
reaper mid-acknowledgement. Post-fix, ``acknowledge_finding`` calls
``touch_scope`` (which acquires the lock + refreshes the timestamp)
before delegating to the disk write.
"""

from __future__ import annotations

import time
from unittest.mock import patch

from watercooler_mcp.daemons.hosted_coordinator import (
    HostedDaemonCoordinator,
    HostedScopeKey,
    _ScopeEntry,
)
from watercooler_mcp.daemons.manager import DaemonManager


def _make_coord_with_scope(
    *,
    user_id: str = "user-a",
    repo: str = "org/repo",
    idle_ttl: float = 1800.0,
    initial_offset: float = 0.0,
) -> tuple[HostedDaemonCoordinator, str, _ScopeEntry]:
    """Spin up a coordinator with one live scope at a known timestamp.

    Returns ``(coord, scope_id, entry)``. *initial_offset* lets the
    caller backdate the scope's ``last_touched`` so reaper assertions
    can use a deterministic delta.
    """
    coord = HostedDaemonCoordinator(idle_ttl=idle_ttl)
    key = HostedScopeKey(user_id=user_id, repo=repo, branch=None)
    scope_id = key.scope_id
    entry = _ScopeEntry(
        key=key,
        manager=DaemonManager(),
        last_touched=time.monotonic() - initial_offset,
    )
    coord._scopes[scope_id] = entry
    return coord, scope_id, entry


class TestAcknowledgeFindingKeepsAliveScope:
    """``acknowledge_finding`` refreshes ``entry.last_touched``.

    Pre-#607-fix: the call delegated straight to ``_ack_finding``
    without touching the scope's heartbeat, so a long-idle scope
    could be reaped between the acknowledge and the next request.
    """

    def test_acknowledge_finding_refreshes_last_touched(self) -> None:
        """``last_touched`` advances after acknowledge."""
        coord, scope_id, entry = _make_coord_with_scope(initial_offset=600.0)
        before = entry.last_touched

        with patch(
            "watercooler_mcp.daemons.state.acknowledge_finding",
            return_value=True,
        ):
            coord.acknowledge_finding(scope_id, "thread_auditor", "fid-123")

        assert entry.last_touched > before, (
            "acknowledge_finding must refresh last_touched to prevent reaper race"
        )

    def test_acknowledge_finding_blocks_reaper(self) -> None:
        """A scope that just acknowledged is NOT reaped on the next sweep."""
        # Backdate by more than idle_ttl so reaper would have reaped pre-fix.
        coord, scope_id, _ = _make_coord_with_scope(
            idle_ttl=60.0, initial_offset=120.0
        )

        with patch(
            "watercooler_mcp.daemons.state.acknowledge_finding",
            return_value=True,
        ):
            coord.acknowledge_finding(scope_id, "thread_auditor", "fid-123")

        reaped = coord.teardown_idle_scopes()
        assert reaped == 0, (
            f"acknowledge_finding should keep scope alive; reaper tore down "
            f"{reaped} scope(s)"
        )
        assert scope_id in coord._scopes

    def test_acknowledge_finding_writes_disk_state(self) -> None:
        """The underlying ``_ack_finding`` is still called (functional preservation)."""
        coord, scope_id, _ = _make_coord_with_scope()

        with patch(
            "watercooler_mcp.daemons.state.acknowledge_finding",
            return_value=True,
        ) as mock_ack:
            ok = coord.acknowledge_finding(scope_id, "thread_auditor", "fid-xyz")

        assert ok is True
        mock_ack.assert_called_once_with(
            "thread_auditor", "fid-xyz", namespace=scope_id
        )

    def test_acknowledge_finding_with_unknown_scope_still_writes(self) -> None:
        """Defensive: an unknown ``scope_id`` is a no-op for keep-alive but
        still writes the on-disk state. Mirrors the pre-fix behaviour for
        the disk path; only the keep-alive is gated on the scope existing.
        """
        coord = HostedDaemonCoordinator()  # no scopes

        with patch(
            "watercooler_mcp.daemons.state.acknowledge_finding",
            return_value=True,
        ) as mock_ack:
            ok = coord.acknowledge_finding(
                "unknown-user:unknown-repo", "thread_auditor", "fid-abc"
            )

        assert ok is True
        mock_ack.assert_called_once()

    def test_acknowledge_finding_with_empty_scope_id_writes_to_unscoped_namespace(
        self,
    ) -> None:
        """Empty ``scope_id`` skips touch_scope and writes to namespace=''."""
        coord = HostedDaemonCoordinator()

        with patch(
            "watercooler_mcp.daemons.state.acknowledge_finding",
            return_value=True,
        ) as mock_ack:
            coord.acknowledge_finding(None, "thread_auditor", "fid-q")

        # touch_scope is a no-op without a scope_id; ack still goes through.
        mock_ack.assert_called_once_with(
            "thread_auditor", "fid-q", namespace=""
        )
