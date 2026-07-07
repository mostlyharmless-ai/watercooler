"""Tests for PremiumClientPool — per-(repo, branch) client selection.

Incident bug-hybrid-static-x-repo-cross-tenant-t2-scope: the hybrid
premium client froze the boot repo's X-Repo into its transport, so every
cross-repo memory submission asserted the wrong tenant. The pool restores
the per-call ``code_path`` contract at the transport layer: one Bearer
identity, N single-repo header sets, server-validated per request.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from watercooler_mcp.premium_client import (
    PremiumClientPool,
    PremiumToolClient,
    select_pool_client,
)

TRANSPORT_CONFIG = {
    "url": "https://hosted.example/mcp/premium",
    "proxy_repo": "mostlyharmless-ai/watercooler",
    "proxy_branch": "main",
}


def _default_client() -> PremiumToolClient:
    client = MagicMock(spec=PremiumToolClient)
    client.resolved_repo = "mostlyharmless-ai/watercooler"
    client.resolved_branch = "main"
    return client


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> PremiumClientPool:
    return PremiumClientPool(TRANSPORT_CONFIG, _default_client())


@pytest.fixture(autouse=True)
def _stub_factory(monkeypatch: pytest.MonkeyPatch):
    """Stub from_transport_config so no real transport is built."""

    def _fake_factory(cls, transport_config, **kwargs):
        client = MagicMock(spec=PremiumToolClient)
        client.resolved_repo = transport_config.get("proxy_repo", "")
        client.resolved_branch = transport_config.get("proxy_branch", "")
        return client

    monkeypatch.setattr(
        PremiumToolClient,
        "from_transport_config",
        classmethod(_fake_factory),
    )
    yield


class TestClientForRepo:
    def test_same_repo_branch_returns_same_object(self, pool) -> None:
        a = pool.client_for_repo("mostlyharmless-ai/watercooler-site")
        b = pool.client_for_repo("mostlyharmless-ai/watercooler-site")
        assert a is b

    def test_different_repos_get_different_clients(self, pool) -> None:
        a = pool.client_for_repo("mostlyharmless-ai/watercooler-site")
        b = pool.client_for_repo("mostlyharmless-ai/datasette")
        assert a is not b
        assert a.resolved_repo == "mostlyharmless-ai/watercooler-site"
        assert b.resolved_repo == "mostlyharmless-ai/datasette"

    def test_default_repo_returns_default_instance(self, pool) -> None:
        # Slug normalization: case-folded, .git-stripped input still maps
        # to the boot client.
        got = pool.client_for_repo("Mostlyharmless-AI/Watercooler-Cloud.git")
        assert got is pool.default

    def test_empty_slug_raises(self, pool) -> None:
        with pytest.raises(ValueError, match="empty repo slug"):
            pool.client_for_repo("")

    def test_branch_key_separates_same_repo(self, pool, monkeypatch) -> None:
        """Review finding 2 (bug-hybrid-static-x-repo-cross-tenant-t2-scope:3):
        same-repo multi-branch sessions must not reuse the first branch seen.
        """
        branches = iter(["feat/a", "feat/b"])
        ctx = MagicMock()
        with patch(
            "watercooler.config_facade.config"
        ) as mock_config:
            mock_config.context.side_effect = lambda p: MagicMock(
                code_branch=next(branches)
            )
            a = pool.client_for_repo(
                "mostlyharmless-ai/watercooler-site", repo_root=Path("/x")
            )
            b = pool.client_for_repo(
                "mostlyharmless-ai/watercooler-site", repo_root=Path("/x")
            )
        assert a is not b
        assert a.resolved_branch == "feat/a"
        assert b.resolved_branch == "feat/b"

    def test_branch_resolution_failure_falls_back_to_default_branch(
        self, pool
    ) -> None:
        with patch("watercooler.config_facade.config") as mock_config:
            mock_config.context.side_effect = RuntimeError("no git")
            got = pool.client_for_repo(
                "mostlyharmless-ai/watercooler-site", repo_root=Path("/x")
            )
        assert got.resolved_branch == "main"

    def test_concurrent_same_key_single_entry(self, pool) -> None:
        results: list = []

        def grab() -> None:
            results.append(
                pool.client_for_repo("mostlyharmless-ai/watercooler-site")
            )

        threads = [threading.Thread(target=grab) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len({id(r) for r in results}) == 1


class TestClientForPath:
    def test_derives_slug_from_git_remote(self, pool) -> None:
        with patch(
            "watercooler.path_resolver.derive_repo_slug",
            return_value="mostlyharmless-ai/watercooler-site",
        ):
            got = pool.client_for_path("/some/where")
        assert got.resolved_repo == "mostlyharmless-ai/watercooler-site"

    def test_underivable_slug_raises_value_error(self, pool) -> None:
        with patch(
            "watercooler.path_resolver.derive_repo_slug",
            side_effect=RuntimeError("no remote"),
        ):
            with pytest.raises(ValueError, match="no repo slug derivable"):
                pool.client_for_path("/some/where")


class TestSelectPoolClient:
    def test_no_pool_returns_premium_client(self) -> None:
        runtime = MagicMock()
        runtime.premium_pool = None
        assert select_pool_client(runtime, "/p") is runtime.premium_client

    def test_no_code_path_returns_pool_default(self, pool) -> None:
        runtime = MagicMock()
        runtime.premium_pool = pool
        assert select_pool_client(runtime, None) is pool.default

    def test_code_path_selects_per_repo_client(self, pool) -> None:
        runtime = MagicMock()
        runtime.premium_pool = pool
        with patch(
            "watercooler.path_resolver.derive_repo_slug",
            return_value="mostlyharmless-ai/datasette",
        ):
            got = select_pool_client(runtime, "/p")
        assert got.resolved_repo == "mostlyharmless-ai/datasette"

    def test_underivable_path_falls_back_to_default(self, pool) -> None:
        runtime = MagicMock()
        runtime.premium_pool = pool
        with patch(
            "watercooler.path_resolver.derive_repo_slug",
            side_effect=RuntimeError("no remote"),
        ):
            got = select_pool_client(runtime, "/p")
        assert got is pool.default
