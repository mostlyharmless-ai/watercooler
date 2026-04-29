"""Tests for Plan v20 Phase 2 daemon authority labels.

Verifies that ``watercooler_daemon_status`` / ``watercooler_daemon_findings``
output carries truthful ``authority_scope`` + ``execution_mode`` top-level
labels based on the actual daemon runtime in use, and that the hybrid
exception case is explicitly labeled rather than pretending to be the
hosted complement.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from watercooler_mcp.tools.daemon import _attach_authority, _authority_labels


class _FakeHostedCoordinator:
    """Stand-in that passes ``isinstance(..., HostedDaemonCoordinator)``."""
    pass


class _FakeDaemonManager:
    """Stand-in for local ``DaemonManager``."""
    pass


class TestAuthorityLabels:
    """_authority_labels returns truthful scope per runtime type."""

    def test_hosted_coordinator_labels(self):
        from watercooler_mcp.daemons.hosted_coordinator import HostedDaemonCoordinator

        # Bypass __init__; only the isinstance check matters for labelling.
        fake = HostedDaemonCoordinator.__new__(HostedDaemonCoordinator)
        labels = _authority_labels(fake)
        assert labels["authority_scope"] == "hosted_premium_daemons"
        assert labels["execution_mode"] == "hosted"

    def test_local_manager_labels_in_stdio_mode(self):
        from watercooler_mcp.daemons import DaemonManager

        fake = DaemonManager.__new__(DaemonManager)

        class _Mcp:
            transport = "stdio"

        class _Config:
            mcp = _Mcp()

        with patch(
            "watercooler_mcp.config.get_watercooler_config",
            return_value=_Config(),
        ):
            labels = _authority_labels(fake)
        assert labels["authority_scope"] == "local_daemons"
        assert labels["execution_mode"] == "local"
        assert "note" not in labels

    def test_local_manager_in_hybrid_marked_override(self):
        """PR #654 in-PR review round 5 (MEDIUM §4) changed the source of
        truth from static config.transport to the live runtime.surface, so
        this test now sets a local_hybrid runtime instead of patching the
        config. The label + note contract is unchanged."""
        from watercooler_mcp.daemons import DaemonManager
        from watercooler_mcp import memory_sync

        fake = DaemonManager.__new__(DaemonManager)

        class _Runtime:
            surface = "local_hybrid"
            premium_client = object()  # truthy; presence of client is enough

        memory_sync.set_runtime(_Runtime())
        try:
            labels = _authority_labels(fake)
        finally:
            memory_sync.set_runtime(None)

        assert labels["authority_scope"] == "local_daemons_hybrid_override"
        assert labels["execution_mode"] == "local"
        assert "note" in labels
        assert "server_factory.py:424-436" in labels["note"]


class TestAttachAuthority:
    """_attach_authority merges labels into a payload non-destructively."""

    def test_attach_to_empty_payload(self):
        from watercooler_mcp.daemons.hosted_coordinator import HostedDaemonCoordinator

        fake = HostedDaemonCoordinator.__new__(HostedDaemonCoordinator)
        payload = {}
        _attach_authority(payload, fake)
        assert payload["authority_scope"] == "hosted_premium_daemons"
        assert payload["execution_mode"] == "hosted"

    def test_attach_preserves_existing_fields(self):
        from watercooler_mcp.daemons.hosted_coordinator import HostedDaemonCoordinator

        fake = HostedDaemonCoordinator.__new__(HostedDaemonCoordinator)
        payload = {
            "some_daemon": {"status": "running"},
            "count": 5,
        }
        _attach_authority(payload, fake)
        assert payload["some_daemon"] == {"status": "running"}
        assert payload["count"] == 5
        assert payload["authority_scope"] == "hosted_premium_daemons"
        assert payload["execution_mode"] == "hosted"
