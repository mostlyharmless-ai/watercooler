"""Regression tests for hosted_semantic FalkorDB env-var resolution.

Background: ``hosted_semantic._get_falkor_client()`` historically read only
``FALKORDB_URL`` and fell back to a hardcoded ``redis://falkordb.railway.internal:6379``
default. The rest of the system (memory_config, config_loader, Graphiti backend)
honors the canonical ``FALKORDB_HOST`` / ``FALKORDB_PORT`` /
``FALKORDB_USERNAME`` / ``FALKORDB_PASSWORD`` env-var contract. When the
hosted FalkorDB service was renamed on Railway the divergent contract caused
``find_similar`` (and other hosted semantic ops) to silently target the stale
hardcoded hostname, returning ``connect_failed: Name or service not known``.

These tests pin the canonical env-var contract so the divergence cannot
re-emerge.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_falkor_env(monkeypatch):
    """Strip every FalkorDB env var so tests start from a clean slate."""
    for var in (
        "FALKORDB_URL",
        "FALKORDB_HOST",
        "FALKORDB_PORT",
        "FALKORDB_USERNAME",
        "FALKORDB_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)


def test_resolve_falkor_kwargs_prefers_canonical_host_env(monkeypatch):
    """FALKORDB_HOST takes precedence over FALKORDB_URL — matches the rest of the system."""
    from watercooler_mcp.hosted_semantic import _resolve_falkor_kwargs

    monkeypatch.setenv("FALKORDB_HOST", "adequate-exploration.railway.internal")
    monkeypatch.setenv("FALKORDB_PORT", "6379")
    # Even with a stale FALKORDB_URL set, FALKORDB_HOST wins.
    monkeypatch.setenv("FALKORDB_URL", "redis://falkordb.railway.internal:6379")

    kwargs = _resolve_falkor_kwargs()

    assert kwargs["host"] == "adequate-exploration.railway.internal"
    assert kwargs["port"] == 6379


def test_resolve_falkor_kwargs_picks_up_credentials_from_canonical_env(monkeypatch):
    """USERNAME and PASSWORD env vars flow into the FalkorDB() kwargs."""
    from watercooler_mcp.hosted_semantic import _resolve_falkor_kwargs

    monkeypatch.setenv("FALKORDB_HOST", "host.example")
    monkeypatch.setenv("FALKORDB_USERNAME", "alice")
    monkeypatch.setenv("FALKORDB_PASSWORD", "secret")

    kwargs = _resolve_falkor_kwargs()

    assert kwargs["host"] == "host.example"
    assert kwargs["username"] == "alice"
    assert kwargs["password"] == "secret"


def test_resolve_falkor_kwargs_omits_blank_credentials(monkeypatch):
    """Empty/whitespace USERNAME/PASSWORD must not enter kwargs (FalkorDB SDK treats '' as set)."""
    from watercooler_mcp.hosted_semantic import _resolve_falkor_kwargs

    monkeypatch.setenv("FALKORDB_HOST", "host.example")
    monkeypatch.setenv("FALKORDB_USERNAME", "  ")
    monkeypatch.setenv("FALKORDB_PASSWORD", "")

    kwargs = _resolve_falkor_kwargs()

    assert "username" not in kwargs
    assert "password" not in kwargs


def test_resolve_falkor_kwargs_invalid_port_falls_back_to_6379(monkeypatch):
    """A non-numeric FALKORDB_PORT must not crash; default to 6379."""
    from watercooler_mcp.hosted_semantic import _resolve_falkor_kwargs

    monkeypatch.setenv("FALKORDB_HOST", "host.example")
    monkeypatch.setenv("FALKORDB_PORT", "not-a-number")

    kwargs = _resolve_falkor_kwargs()

    assert kwargs["port"] == 6379


def test_resolve_falkor_kwargs_falls_back_to_url_when_host_unset(monkeypatch):
    """Legacy FALKORDB_URL still works when FALKORDB_HOST isn't set."""
    from watercooler_mcp.hosted_semantic import _resolve_falkor_kwargs

    monkeypatch.setenv("FALKORDB_URL", "redis://user:pw@legacy.example:6380")

    kwargs = _resolve_falkor_kwargs()

    assert kwargs["host"] == "legacy.example"
    assert kwargs["port"] == 6380
    assert kwargs["username"] == "user"
    assert kwargs["password"] == "pw"


def test_resolve_falkor_kwargs_default_is_localhost_not_stale_railway_hostname():
    """No env vars → default to localhost, NOT a stale Railway internal hostname.

    The prior default ``redis://falkordb.railway.internal:6379`` was a footgun:
    it implied "this is correct in Railway" but actually pointed at a service
    name that had since been renamed. ``localhost`` makes misconfiguration
    fail loudly in production rather than silently against the wrong host.
    """
    from watercooler_mcp.hosted_semantic import _resolve_falkor_kwargs

    kwargs = _resolve_falkor_kwargs()

    assert kwargs["host"] == "localhost"
    assert kwargs["port"] == 6379


def test_default_falkor_url_constant_does_not_reference_stale_railway_hostname():
    """Source-level guard: the default URL must not point at a Railway internal hostname.

    The stale default caused find_similar to silently target the old service
    name. This test will fail if anyone re-introduces the footgun.
    """
    from watercooler_mcp import hosted_semantic

    assert "railway.internal" not in hosted_semantic.DEFAULT_FALKOR_URL
