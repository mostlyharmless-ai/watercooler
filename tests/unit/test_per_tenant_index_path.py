"""A1-full: the entry-episode mapping cache is per-tenant.

The tenant-blind global cache file was the original cause of the
cross-tenant index_miss (audit-transport-modes-hosted-db-2026-07): one
shared file, rebuilt for whichever tenant last ran. The cache filename
is now keyed by the resolved database wherever a database is known.
"""

from pathlib import Path

from watercooler_memory.backends.graphiti import GraphitiConfig


def test_config_default_is_per_tenant_when_database_known():
    config = GraphitiConfig(database="mostlyharmless_ai_oren_t2")
    assert config.entry_episode_index_path == (
        Path.home()
        / ".watercooler"
        / "graphiti"
        / "entry_episode_index_mostlyharmless_ai_oren_t2.json"
    )


def test_config_default_falls_back_to_global_without_database():
    config = GraphitiConfig(database=None)
    assert config.entry_episode_index_path == (
        Path.home() / ".watercooler" / "graphiti" / "entry_episode_index.json"
    )


def test_two_tenants_never_share_a_cache_file():
    a = GraphitiConfig(database="org_a_t2")
    b = GraphitiConfig(database="org_b_t2")
    assert a.entry_episode_index_path != b.entry_episode_index_path


class TestBulkIndexModeExclusion:
    """PR #1101 review finding 2: backfill_provenance is a mode selector
    and must be mutually exclusive with the other three."""

    def test_backfill_conflicts_with_other_modes(self, monkeypatch):
        import asyncio
        import json
        from unittest.mock import MagicMock

        from watercooler_mcp.tools.memory import _bulk_index_impl

        for other in (
            {"rebuild_index_only": True},
            {"preflight_only": True},
            {"run_pipeline": True},
        ):
            result = asyncio.run(
                _bulk_index_impl(
                    MagicMock(), backfill_provenance=True, **other
                )
            )
            payload = json.loads(result.content[0].text)
            assert "mutually exclusive" in payload["error"], other
