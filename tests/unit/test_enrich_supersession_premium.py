"""D7 regression: the supersession enricher is a premium/hosted daemon.

It writes the shared T2 FalkorDB graph (like ``t2_indexer``), so per the ratified
classification rule (``daemon-sanity-check:1``: premium = "Railway-only infrastructure
dependencies") it must route hosted in hybrid/proxy — never strand its ``superseded_by``
writes on a local backend the advertised memory tools cannot see.
"""

from types import SimpleNamespace

from watercooler_mcp.daemons import _PREMIUM_DAEMONS, daemon_execution_policy


def _cfg(enabled=True, route="auto"):
    return SimpleNamespace(enabled=enabled, route=route)


def test_enrich_supersession_is_premium():
    assert "enrich_supersession" in _PREMIUM_DAEMONS


def test_routes_hosted_in_hybrid():
    assert (
        daemon_execution_policy(
            "enrich_supersession", _cfg(), transport="hybrid", in_hosted_coordinator=False
        )
        == "hosted"
    )


def test_registers_hosted_in_coordinator():
    assert (
        daemon_execution_policy(
            "enrich_supersession", _cfg(), transport="hybrid", in_hosted_coordinator=True
        )
        == "hosted"
    )


def test_runs_local_only_in_single_process_transport():
    # A pure-local (stdio) deployment has no hosted split, so it runs locally.
    assert (
        daemon_execution_policy(
            "enrich_supersession", _cfg(), transport="stdio", in_hosted_coordinator=False
        )
        == "local"
    )


def test_disabled_is_skipped_everywhere():
    assert (
        daemon_execution_policy(
            "enrich_supersession",
            _cfg(enabled=False),
            transport="hybrid",
            in_hosted_coordinator=True,
        )
        == "skip"
    )
