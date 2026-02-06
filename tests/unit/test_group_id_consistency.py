"""Unit tests for group_id namespace isolation between Graphiti and LeanRAG.

Ensures that both backends can coexist in the same FalkorDB instance
by using different database name prefixes:
- Graphiti: "watercooler_cloud"
- LeanRAG:  "leanrag_watercooler_cloud"

Closes #142.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from watercooler_mcp import memory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _enable_leanrag(monkeypatch, isolated_config, clean_api_keys, tmp_path):
    """Set up environment so load_leanrag_config() progresses past guards.

    This fixture:
    - Enables memory and the LeanRAG backend
    - Points LEANRAG_PATH to a temp directory that exists
    - Mocks LeanRAGConfig.from_unified() to return a simple mock config
    """
    from watercooler.config_facade import config as cfg

    monkeypatch.setenv("WATERCOOLER_LEANRAG_ENABLED", "1")

    # Create a fake leanrag directory so the path-exists check passes
    fake_leanrag = tmp_path / "fake_leanrag"
    fake_leanrag.mkdir()
    monkeypatch.setenv("LEANRAG_PATH", str(fake_leanrag))

    cfg.reset()
    yield
    cfg.reset()


@pytest.fixture
def _mock_leanrag_config_class():
    """Mock the LeanRAGConfig import inside load_leanrag_config."""
    mock_cfg = MagicMock()
    mock_cfg.from_unified.return_value = MagicMock()
    with patch(
        "watercooler_mcp.memory.LeanRAGConfig",
        mock_cfg,
        create=True,
    ) as m:
        # Patch the dynamic import too (inside the try block in load_leanrag_config)
        with patch.dict(
            "sys.modules",
            {"watercooler_memory.backends.leanrag": MagicMock(LeanRAGConfig=mock_cfg)},
        ):
            yield m


# ---------------------------------------------------------------------------
# Tests: LeanRAG database name has leanrag_ prefix
# ---------------------------------------------------------------------------


class TestLeanRAGDatabasePrefix:
    """Verify auto-derived LeanRAG database names carry the leanrag_ prefix."""

    def test_leanrag_database_name_has_prefix(
        self, _enable_leanrag, _mock_leanrag_config_class, tmp_path
    ):
        """Auto-derived database name should start with 'leanrag_'."""
        # Point code_path at a directory named 'watercooler-cloud'
        code_path = tmp_path / "watercooler-cloud"
        code_path.mkdir(exist_ok=True)

        config = memory.load_leanrag_config(code_path=str(code_path))
        assert config is not None

        db_name = config.work_dir.name
        assert db_name.startswith("leanrag_"), (
            f"Expected leanrag_ prefix, got: {db_name}"
        )
        assert db_name == "leanrag_watercooler_cloud"

    def test_leanrag_prefix_with_hyphenated_repo(
        self, _enable_leanrag, _mock_leanrag_config_class, tmp_path
    ):
        """Repo 'my-test-repo' should become 'leanrag_my_test_repo'."""
        code_path = tmp_path / "my-test-repo"
        code_path.mkdir(exist_ok=True)

        config = memory.load_leanrag_config(code_path=str(code_path))
        assert config is not None
        assert config.work_dir.name == "leanrag_my_test_repo"

    def test_leanrag_prefix_with_capitalized_repo(
        self, _enable_leanrag, _mock_leanrag_config_class, tmp_path
    ):
        """Repo 'MyTestRepo' should become 'leanrag_mytestrepo'."""
        code_path = tmp_path / "MyTestRepo"
        code_path.mkdir(exist_ok=True)

        config = memory.load_leanrag_config(code_path=str(code_path))
        assert config is not None
        assert config.work_dir.name == "leanrag_mytestrepo"

    def test_explicit_database_override_no_prefix(
        self, monkeypatch, _enable_leanrag, _mock_leanrag_config_class, tmp_path
    ):
        """WATERCOOLER_LEANRAG_DATABASE should be used as-is (no prefix)."""
        from watercooler.config_facade import config as cfg

        monkeypatch.setenv("WATERCOOLER_LEANRAG_DATABASE", "custom_db_name")
        cfg.reset()

        code_path = tmp_path / "watercooler-cloud"
        code_path.mkdir(exist_ok=True)

        config = memory.load_leanrag_config(code_path=str(code_path))
        assert config is not None
        assert config.work_dir.name == "custom_db_name"


# ---------------------------------------------------------------------------
# Tests: Graphiti and LeanRAG produce different database names
# ---------------------------------------------------------------------------


class TestGraphitiLeanRAGIsolation:
    """Verify Graphiti and LeanRAG resolve to distinct FalkorDB database names."""

    def test_graphiti_and_leanrag_different_names(
        self,
        monkeypatch,
        isolated_config,
        clean_api_keys,
        _mock_leanrag_config_class,
        tmp_path,
    ):
        """Both backends active for the same project must use different database names."""
        from watercooler.config_facade import config as cfg

        code_path = tmp_path / "watercooler-cloud"
        code_path.mkdir(exist_ok=True)

        # Enable Graphiti
        monkeypatch.setenv("WATERCOOLER_GRAPHITI_ENABLED", "1")
        monkeypatch.setenv("LLM_API_KEY", "sk-test-llm")
        monkeypatch.setenv("EMBEDDING_API_KEY", "sk-test-embed")

        # Enable LeanRAG
        monkeypatch.setenv("WATERCOOLER_LEANRAG_ENABLED", "1")
        fake_leanrag = tmp_path / "fake_leanrag"
        fake_leanrag.mkdir(exist_ok=True)
        monkeypatch.setenv("LEANRAG_PATH", str(fake_leanrag))

        cfg.reset()

        graphiti_cfg = memory.load_graphiti_config(code_path=str(code_path))
        leanrag_cfg = memory.load_leanrag_config(code_path=str(code_path))

        assert graphiti_cfg is not None, "Graphiti config should load"
        assert leanrag_cfg is not None, "LeanRAG config should load"

        graphiti_db = graphiti_cfg.database
        leanrag_db = leanrag_cfg.work_dir.name

        assert graphiti_db != leanrag_db, (
            f"Graphiti ({graphiti_db}) and LeanRAG ({leanrag_db}) "
            "must use different database names"
        )
        assert graphiti_db == "watercooler_cloud"
        assert leanrag_db == "leanrag_watercooler_cloud"
