"""Acceptance-test matrix for the tool x capability x authority matrix (PR1b).

``TOOL_MATRIX`` in ``capabilities.py`` is the single source of truth for the
tool-surface consolidation. These tests pin it against the live registered
surface across all four deployment shapes so that adding, renaming, or
collapsing a tool cannot silently drift the matrix, the derived capability
map, or the alias registry out of agreement.
"""

from __future__ import annotations

import asyncio
import functools
from unittest.mock import MagicMock

import pytest

from watercooler_mcp.aliases import TOOL_ALIASES
from watercooler_mcp.capabilities import (
    HYBRID_DEFAULT_ROUTES,
    TOOL_MATRIX,
    CapabilityProfile,
    _ALL_CAPABILITY_IDS,
    _TOOL_CAPABILITY_MAP,
    tool_authority,
    tool_capability,
)

_SURFACES = ["local_full", "local_hybrid", "hosted_full", "hosted_premium"]
_AUTHORITY_LEVELS = {"L1", "L2", "L3"}


@functools.lru_cache(maxsize=None)
def _surface_tool_names(surface: str) -> frozenset[str]:
    """Build a FastMCP server for *surface*; return its registered tool names.

    ``local_hybrid`` is built with the *real* default hybrid shape — the
    ``HYBRID_DEFAULT_ROUTES`` capability profile and a premium client — so the
    suite pins the actual default remote/disabled routing and the premium
    proxy mount path, not a synthetic "all-auto-routes-local" hybrid. Pattern
    mirrors ``tests/test_server_factory.py``.
    """
    from watercooler_mcp.server_factory import build_mcp_server
    from watercooler_mcp.tool_runtime import ToolRuntime

    if surface == "local_hybrid":
        premium_client = MagicMock()
        premium_client.proxy_server.return_value = MagicMock()
        runtime = ToolRuntime(
            surface="local_hybrid",
            capability_profile=CapabilityProfile(routes=HYBRID_DEFAULT_ROUTES),
            premium_client=premium_client,
        )
    else:
        runtime = ToolRuntime(
            surface=surface,  # type: ignore[arg-type]
            capability_profile=CapabilityProfile(),
        )
    mcp = build_mcp_server(runtime)
    return frozenset(t.name for t in asyncio.run(mcp.list_tools()))


def _all_registered_tools() -> set[str]:
    names: set[str] = set()
    for surface in _SURFACES:
        names |= _surface_tool_names(surface)
    return names


# ---------------------------------------------------------------------------
# Matrix validity
# ---------------------------------------------------------------------------


class TestMatrixValidity:
    def test_every_spec_has_valid_capability(self):
        bad = {
            n: s.capability
            for n, s in TOOL_MATRIX.items()
            if s.capability not in _ALL_CAPABILITY_IDS
        }
        assert not bad, f"TOOL_MATRIX entries with unknown capability: {bad}"

    def test_every_spec_has_valid_authority(self):
        bad = {
            n: s.authority
            for n, s in TOOL_MATRIX.items()
            if s.authority not in _AUTHORITY_LEVELS
        }
        assert not bad, f"TOOL_MATRIX entries with invalid authority: {bad}"

    def test_capability_map_is_exact_projection(self):
        """_TOOL_CAPABILITY_MAP must be derived from TOOL_MATRIX — no drift."""
        assert _TOOL_CAPABILITY_MAP == {
            n: s.capability for n, s in TOOL_MATRIX.items()
        }


# ---------------------------------------------------------------------------
# Matrix <-> live registered surface completeness
# ---------------------------------------------------------------------------


class TestMatrixCompleteness:
    def test_matrix_covers_every_registered_tool(self):
        """No tool may register on any surface without a TOOL_MATRIX entry."""
        missing = _all_registered_tools() - set(TOOL_MATRIX)
        assert not missing, (
            "tools registered but absent from TOOL_MATRIX (capability + "
            f"authority would be unresolved): {sorted(missing)}"
        )

    def test_matrix_has_no_stale_entries(self):
        """Every TOOL_MATRIX entry must register on at least one surface."""
        stale = set(TOOL_MATRIX) - _all_registered_tools()
        assert not stale, (
            f"TOOL_MATRIX entries not registered on any surface: {sorted(stale)}"
        )


# ---------------------------------------------------------------------------
# Per-deployment-shape acceptance snapshot
# ---------------------------------------------------------------------------


class TestPerSurfaceAcceptance:
    @pytest.mark.parametrize("surface", _SURFACES)
    def test_surface_is_subset_of_matrix(self, surface):
        assert _surface_tool_names(surface) <= set(TOOL_MATRIX)

    @pytest.mark.parametrize("surface", _SURFACES)
    def test_every_registered_tool_resolves_capability_and_authority(self, surface):
        for name in _surface_tool_names(surface):
            cap = tool_capability(name)
            assert cap in _ALL_CAPABILITY_IDS, (surface, name, cap)
            auth = tool_authority(name)
            assert auth in _AUTHORITY_LEVELS, (surface, name, auth)


# ---------------------------------------------------------------------------
# Argument-sensitive authority resolution
# ---------------------------------------------------------------------------


class TestAuthorityResolution:
    def test_static_authority(self):
        assert tool_authority("watercooler_read_thread") == "L1"
        assert tool_authority("watercooler_delete_thread") == "L3"
        assert tool_authority("watercooler_set_status") == "L3"
        assert tool_authority("watercooler_bulk_index") == "L2"

    def test_write_authority_is_arg_sensitive(self):
        assert tool_authority("watercooler_write") == "L1"
        assert tool_authority("watercooler_write", {"authority_mode": "ordinary"}) == "L1"
        assert tool_authority("watercooler_write", {"authority_mode": "decision"}) == "L3"
        assert tool_authority("watercooler_write", {"authority_mode": "closure"}) == "L3"

    def test_unknown_tool_raises(self):
        with pytest.raises(ValueError, match="Unknown tool"):
            tool_authority("nonexistent_tool")


# ---------------------------------------------------------------------------
# Hybrid remote-leg ↔ premium-mount coherence
# ---------------------------------------------------------------------------


class TestHybridRemoteLegMountCoherence:
    """Every tool the local_hybrid surface can route to its premium remote leg
    must actually be mounted on hosted_premium. Otherwise the forward fails at
    runtime with ``Unknown tool: '<name>'`` even though the matrix is internally
    consistent (the tool registers on some *other* surface).

    Regression guard for the list_decisions+supersession gap: the tool routed
    memory_query→remote, the wrapper forwarded ``watercooler_list_decisions`` to
    hosted_premium, but it was only registered on hosted_full. See thread
    ``list-decisions-supersession-hosted-premium-unmounted``.
    """

    def test_mixed_tools_mounted_on_premium(self):
        """MIXED tools route some arg-modes to the remote leg via a local
        wrapper that forwards by tool *name*; that name must exist on
        hosted_premium."""
        from watercooler_mcp.capabilities import MIXED_TOOL_NAMES

        premium = _surface_tool_names("hosted_premium")
        missing = set(MIXED_TOOL_NAMES) - premium
        assert not missing, (
            "MIXED tools route remote via name-forwarding but are not mounted "
            f"on hosted_premium (remote leg → 'Unknown tool'): {sorted(missing)}"
        )

    def test_proxy_mounted_remote_tools_exist_on_premium(self):
        """Tools the hybrid surface mounts from the premium proxy must exist on
        hosted_premium (the proxy's backing surface)."""
        from watercooler_mcp.capabilities import (
            CapabilityProfile,
            HYBRID_DEFAULT_ROUTES,
        )
        from watercooler_mcp.server_factory import (
            mountable_remote_tools_for_hybrid,
        )
        from watercooler_mcp.tool_runtime import ToolRuntime

        premium = _surface_tool_names("hosted_premium")
        premium_client = MagicMock()
        premium_client.proxy_server.return_value = MagicMock()
        rt = ToolRuntime(
            surface="local_hybrid",
            capability_profile=CapabilityProfile(routes=HYBRID_DEFAULT_ROUTES),
            premium_client=premium_client,
        )
        missing = mountable_remote_tools_for_hybrid(rt) - premium
        assert not missing, (
            "hybrid proxy-mounts tools absent from hosted_premium "
            f"(remote leg → 'Unknown tool'): {sorted(missing)}"
        )


# ---------------------------------------------------------------------------
# Alias registry agreement
# ---------------------------------------------------------------------------


class TestAliasTargetsValid:
    def test_alias_canonicals_are_in_matrix(self):
        """Every alias must forward to a tool that exists in TOOL_MATRIX.

        ``TOOL_ALIASES`` ships empty in PR1a; this guards later PRs from
        registering an alias whose canonical target is mistyped or not a
        real tool.
        """
        bad = {
            legacy: alias.canonical
            for legacy, alias in TOOL_ALIASES.items()
            if alias.canonical not in TOOL_MATRIX
        }
        assert not bad, f"aliases pointing at non-matrix tools: {bad}"
