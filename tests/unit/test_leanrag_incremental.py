"""Unit tests for LeanRAG incremental indexing in the watercooler-cloud backend.

Tests cover:
- has_incremental_state() detection
- incremental_index() fallback to full index when no state
- incremental_index() pipeline with mocked LeanRAG internals
- Error handling in incremental paths
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from watercooler_memory.backends import (
    BackendError,
    ChunkPayload,
    ConfigError,
    IndexResult,
    LeanRAGBackend,
    LeanRAGConfig,
)


@pytest.fixture
def mock_config(tmp_path: Path) -> LeanRAGConfig:
    """Create a mock LeanRAG config for testing."""
    work_dir = tmp_path / "test_corpus"
    work_dir.mkdir()

    leanrag_dir = tmp_path / "fake_leanrag"
    leanrag_dir.mkdir()

    return LeanRAGConfig(
        work_dir=work_dir,
        leanrag_path=leanrag_dir,
        embedding_api_base="http://localhost:8000",
        embedding_model="test-model",
    )


@pytest.fixture
def backend(mock_config: LeanRAGConfig, monkeypatch) -> LeanRAGBackend:
    """Create a LeanRAG backend with validation bypassed."""
    monkeypatch.setattr(LeanRAGBackend, "_validate_config", lambda self: None)
    return LeanRAGBackend(mock_config)


@pytest.fixture
def sample_chunks() -> ChunkPayload:
    """Create a sample ChunkPayload for testing."""
    return ChunkPayload(
        manifest_version="1.0",
        chunks=[
            {
                "id": "chunk-1",
                "text": "OAuth2 enables delegated authorization for API clients.",
                "metadata": {"group_id": "auth-feature", "source": "test"},
            },
            {
                "id": "chunk-2",
                "text": "JWT tokens carry claims as a signed JSON payload.",
                "metadata": {"group_id": "auth-feature", "source": "test"},
            },
        ],
    )


# ================================================================== #
# has_incremental_state()
# ================================================================== #


class TestHasIncrementalState:
    """Tests for LeanRAGBackend.has_incremental_state()."""

    def test_no_work_dir_returns_false(self, monkeypatch):
        """No work_dir configured means no state."""
        config = LeanRAGConfig(
            work_dir=None,
            leanrag_path=Path("/fake"),
        )
        monkeypatch.setattr(LeanRAGBackend, "_validate_config", lambda self: None)
        b = LeanRAGBackend(config)
        assert b.has_incremental_state() is False

    def test_no_state_dir_returns_false(self, backend):
        """No .cluster_state directory means no state."""
        assert backend.has_incremental_state() is False

    def test_empty_state_dir_returns_false(self, backend):
        """Empty .cluster_state directory (no cluster metadata) returns False."""
        state_dir = backend.config.work_dir / ".cluster_state"
        state_dir.mkdir(parents=True)

        # Mock StateManager to say no cluster state
        mock_sm = MagicMock()
        mock_sm.has_cluster_state.return_value = False
        mock_sm.__enter__ = Mock(return_value=mock_sm)
        mock_sm.__exit__ = Mock(return_value=False)

        with patch.dict("sys.modules", {
            "leanrag": MagicMock(),
            "leanrag.clustering": MagicMock(),
            "leanrag.clustering.state_manager": MagicMock(
                StateManager=Mock(return_value=mock_sm)
            ),
        }):
            assert backend.has_incremental_state() is False

    def test_with_cluster_state_returns_true(self, backend):
        """When StateManager has cluster state, returns True."""
        state_dir = backend.config.work_dir / ".cluster_state"
        state_dir.mkdir(parents=True)

        mock_sm = MagicMock()
        mock_sm.has_cluster_state.return_value = True
        mock_sm.__enter__ = Mock(return_value=mock_sm)
        mock_sm.__exit__ = Mock(return_value=False)

        with patch.dict("sys.modules", {
            "leanrag": MagicMock(),
            "leanrag.clustering": MagicMock(),
            "leanrag.clustering.state_manager": MagicMock(
                StateManager=Mock(return_value=mock_sm)
            ),
        }):
            assert backend.has_incremental_state() is True

    def test_exception_returns_false(self, backend):
        """If StateManager raises, gracefully return False."""
        state_dir = backend.config.work_dir / ".cluster_state"
        state_dir.mkdir(parents=True)

        with patch.dict("sys.modules", {
            "leanrag": MagicMock(),
            "leanrag.clustering": MagicMock(),
            "leanrag.clustering.state_manager": MagicMock(
                StateManager=Mock(side_effect=RuntimeError("corrupt DB"))
            ),
        }):
            assert backend.has_incremental_state() is False


# ================================================================== #
# incremental_index() — fallback path
# ================================================================== #


class TestIncrementalIndexFallback:
    """Tests for incremental_index() when no incremental state exists."""

    def test_falls_back_to_full_index_when_no_state(self, backend, sample_chunks):
        """When has_incremental_state() is False, falls back to full index()."""
        mock_result = IndexResult(
            manifest_version="1.0",
            indexed_count=5,
            message="Full build: 5 clusters",
        )

        with patch.object(backend, "has_incremental_state", return_value=False), \
             patch.object(backend, "index", return_value=mock_result) as mock_index:
            result = backend.incremental_index(sample_chunks)
            mock_index.assert_called_once_with(sample_chunks, progress_callback=None)
            assert result.indexed_count == 5
            assert "Full build" in result.message


# ================================================================== #
# incremental_index() — incremental path
# ================================================================== #


class TestIncrementalIndexPipeline:
    """Tests for incremental_index() when incremental state exists."""

    def _setup_chunk_file(self, work_dir: Path, chunks: list[dict]) -> None:
        """Create the threads_chunk.json file that _ensure_chunk_file produces."""
        corpus = [
            {"hash_code": c["id"], "text": c["text"]}
            for c in chunks
        ]
        (work_dir / "threads_chunk.json").write_text(json.dumps(corpus))

    def _setup_entity_file(self, work_dir: Path, entities: list[dict]) -> None:
        """Create the entity.jsonl that triple_extraction produces."""
        with open(work_dir / "entity.jsonl", "w") as f:
            for ent in entities:
                f.write(json.dumps(ent) + "\n")

    def test_incremental_path_called_when_state_exists(self, backend, sample_chunks):
        """When state exists, incremental_update pipeline is used."""
        import numpy as np

        work_dir = backend.config.work_dir
        self._setup_chunk_file(work_dir, sample_chunks.chunks)
        self._setup_entity_file(work_dir, [
            {"entity_name": "OAuth2", "description": "OAuth2 framework"},
            {"entity_name": "JWT", "description": "JSON Web Tokens"},
        ])

        # Mock IncrementalResult
        mock_inc_result = MagicMock()
        mock_inc_result.entities_assigned = 2
        mock_inc_result.entities_orphaned = 0
        mock_inc_result.communities_resummmarized = 1
        mock_inc_result.duration_seconds = 0.5

        mock_triple_extraction = AsyncMock()
        mock_embedding = MagicMock(return_value=np.random.randn(2, 1024))
        mock_incremental_update = MagicMock(return_value=mock_inc_result)

        with patch.object(backend, "has_incremental_state", return_value=True), \
             patch.object(backend, "_ensure_chunk_file", return_value=work_dir / "threads_chunk.json"), \
             patch.dict("sys.modules", {
                 "leanrag": MagicMock(),
                 "leanrag.extraction": MagicMock(),
                 "leanrag.extraction.chunk": MagicMock(
                     triple_extraction=mock_triple_extraction,
                 ),
                 "leanrag.core": MagicMock(),
                 "leanrag.core.llm": MagicMock(
                     generate_text_async=MagicMock(),
                     embedding=mock_embedding,
                 ),
                 "leanrag.pipelines": MagicMock(),
                 "leanrag.pipelines.incremental": MagicMock(
                     incremental_update=mock_incremental_update,
                 ),
             }):
            result = backend.incremental_index(sample_chunks)

        assert result.indexed_count == 2
        assert "Incremental index" in result.message
        assert "2 entities assigned" in result.message

    def test_no_entities_extracted_returns_zero(self, backend, sample_chunks):
        """When triple extraction produces no entities, return zero."""
        work_dir = backend.config.work_dir
        self._setup_chunk_file(work_dir, sample_chunks.chunks)
        # No entity.jsonl file created

        mock_triple_extraction = AsyncMock()

        with patch.object(backend, "has_incremental_state", return_value=True), \
             patch.object(backend, "_ensure_chunk_file", return_value=work_dir / "threads_chunk.json"), \
             patch.dict("sys.modules", {
                 "leanrag": MagicMock(),
                 "leanrag.extraction": MagicMock(),
                 "leanrag.extraction.chunk": MagicMock(
                     triple_extraction=mock_triple_extraction,
                 ),
                 "leanrag.core": MagicMock(),
                 "leanrag.core.llm": MagicMock(
                     generate_text_async=MagicMock(),
                 ),
                 "leanrag.pipelines": MagicMock(),
                 "leanrag.pipelines.incremental": MagicMock(),
             }):
            result = backend.incremental_index(sample_chunks)

        assert result.indexed_count == 0
        assert "No entities" in result.message

    def test_config_error_on_missing_files(self, backend, sample_chunks):
        """FileNotFoundError is wrapped as ConfigError."""
        with patch.object(backend, "has_incremental_state", return_value=True), \
             patch.object(backend, "_ensure_chunk_file", side_effect=FileNotFoundError("missing")):
            with pytest.raises(ConfigError, match="Required LeanRAG files"):
                backend.incremental_index(sample_chunks)

    def test_backend_error_on_unexpected_exception(self, backend, sample_chunks):
        """Unexpected exceptions are wrapped as BackendError."""
        with patch.object(backend, "has_incremental_state", return_value=True), \
             patch.object(backend, "_ensure_chunk_file", side_effect=ValueError("bad data")):
            with pytest.raises(BackendError, match="Incremental index error"):
                backend.incremental_index(sample_chunks)

    def test_progress_callback_called(self, backend, sample_chunks):
        """Progress callback is invoked at each stage."""
        import numpy as np

        work_dir = backend.config.work_dir
        self._setup_chunk_file(work_dir, sample_chunks.chunks)
        self._setup_entity_file(work_dir, [
            {"entity_name": "OAuth2", "description": "OAuth2 framework"},
        ])

        mock_inc_result = MagicMock()
        mock_inc_result.entities_assigned = 1
        mock_inc_result.entities_orphaned = 0
        mock_inc_result.communities_resummmarized = 0
        mock_inc_result.duration_seconds = 0.1

        callback = MagicMock()

        with patch.object(backend, "has_incremental_state", return_value=True), \
             patch.object(backend, "_ensure_chunk_file", return_value=work_dir / "threads_chunk.json"), \
             patch.dict("sys.modules", {
                 "leanrag": MagicMock(),
                 "leanrag.extraction": MagicMock(),
                 "leanrag.extraction.chunk": MagicMock(
                     triple_extraction=AsyncMock(),
                 ),
                 "leanrag.core": MagicMock(),
                 "leanrag.core.llm": MagicMock(
                     generate_text_async=MagicMock(),
                     embedding=MagicMock(return_value=np.random.randn(1, 1024)),
                 ),
                 "leanrag.pipelines": MagicMock(),
                 "leanrag.pipelines.incremental": MagicMock(
                     incremental_update=MagicMock(return_value=mock_inc_result),
                 ),
             }):
            backend.incremental_index(sample_chunks, progress_callback=callback)

        # Should have called the callback for triple_extraction and incremental_update stages
        stages = [call.args[0] for call in callback.call_args_list]
        assert "triple_extraction" in stages
        assert "incremental_update" in stages
