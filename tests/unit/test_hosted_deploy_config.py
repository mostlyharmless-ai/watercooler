"""The shipped Railway deployment config must parse, validate, and enable
exactly what it claims: enrich_supersession in monitor mode, nothing else.

Guards the Dockerfile-shipped ``deploy/hosted-config.toml`` against schema
drift — a file that stops validating would be silently discarded by
``_resolve_daemon_config`` (it falls back to hosted defaults), turning the
operator opt-in into a no-op.
"""

from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 — tomllib is stdlib only on 3.11+
    import tomli as tomllib

from watercooler.config_schema import DaemonsConfig
from watercooler_mcp.daemons import daemon_execution_policy
from watercooler_mcp.daemons.hosted_coordinator import (
    HostedDaemonCoordinator,
    _deep_merge,
)

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "deploy" / "hosted-config.toml"


def _resolved() -> DaemonsConfig:
    raw = tomllib.loads(_CONFIG_PATH.read_text())
    daemons = raw["mcp"]["daemons"]
    merged = _deep_merge(HostedDaemonCoordinator._hosted_daemon_defaults(), daemons)
    return DaemonsConfig.model_validate(merged)


def test_deploy_config_parses_and_validates():
    cfg = _resolved()
    assert cfg.enabled is True  # hosted global gate survives the merge


def test_enrich_supersession_enabled_tiered_emit():
    """Tiered emit ratified 2026-07-02 (Decision 01KWJK1CS4C5DY8CS735ZBMMQP):
    emit mode on, strong bases only (schema default excludes temporal_only)."""
    cfg = _resolved()
    assert cfg.enrich_supersession.enabled is True
    assert cfg.enrich_supersession.emit_mode == "emit"
    assert cfg.enrich_supersession.interval == 900.0  # schema default kept
    assert set(cfg.enrich_supersession.emit_bases) == {
        "same_source_and_name", "same_source", "same_name",
    }
    assert "temporal_only" not in cfg.enrich_supersession.emit_bases


def test_enrich_supersession_routes_hosted():
    cfg = _resolved()
    assert (
        daemon_execution_policy(
            "enrich_supersession",
            cfg.enrich_supersession,
            transport="hybrid",
            in_hosted_coordinator=True,
        )
        == "hosted"
    )


def test_no_other_daemon_touched():
    """The file must contain only the enrich_supersession stanza."""
    raw = tomllib.loads(_CONFIG_PATH.read_text())
    assert set(raw.keys()) == {"mcp"}
    assert set(raw["mcp"].keys()) == {"daemons"}
    assert set(raw["mcp"]["daemons"].keys()) == {"enrich_supersession"}
