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

    def test_falls_back_to_full_index_when_no_state(self, backend):
        """When has_incremental_state() is False, falls back to full index().

        Note: Must use >= 5 chunks to avoid the degenerate-rebuild guard (MAJOR-4).
        """
        enough_chunks = ChunkPayload(
            manifest_version="1.0",
            chunks=[
                {"id": f"chunk-{i}", "text": f"Chunk {i} content about auth."}
                for i in range(6)
            ],
        )
        mock_result = IndexResult(
            manifest_version="1.0",
            indexed_count=5,
            message="Full build: 5 clusters",
        )

        with patch.object(backend, "has_incremental_state", return_value=False), \
             patch.object(backend, "index", return_value=mock_result) as mock_index:
            result = backend.incremental_index(enough_chunks)
            mock_index.assert_called_once_with(enough_chunks, progress_callback=None)
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
        mock_inc_result.communities_resummarized = 1
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
        mock_inc_result.communities_resummarized = 0
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


# ================================================================== #
# MAJOR-1: llm_func is passed to incremental_update
# ================================================================== #


class TestLlmFuncWiring:
    """Tests for MAJOR-1: llm_func must be wired through to incremental_update."""

    def _setup_chunk_file(self, work_dir: Path, chunks: list[dict]) -> None:
        corpus = [
            {"hash_code": c["id"], "text": c["text"]}
            for c in chunks
        ]
        (work_dir / "threads_chunk.json").write_text(json.dumps(corpus))

    def _setup_entity_file(self, work_dir: Path, entities: list[dict]) -> None:
        with open(work_dir / "entity.jsonl", "w") as f:
            for ent in entities:
                f.write(json.dumps(ent) + "\n")

    def test_llm_func_passed_to_incremental_update(self, backend, sample_chunks):
        """Verify that generate_text is passed as llm_func."""
        import numpy as np

        work_dir = backend.config.work_dir
        self._setup_chunk_file(work_dir, sample_chunks.chunks)
        self._setup_entity_file(work_dir, [
            {"entity_name": "OAuth2", "description": "OAuth2 framework"},
        ])

        mock_inc_result = MagicMock()
        mock_inc_result.entities_assigned = 1
        mock_inc_result.entities_orphaned = 0
        mock_inc_result.communities_resummarized = 0
        mock_inc_result.duration_seconds = 0.1

        mock_generate_text = MagicMock()
        mock_incremental_update = MagicMock(return_value=mock_inc_result)

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
                     generate_text=mock_generate_text,
                     embedding=MagicMock(return_value=np.random.randn(1, 1024)),
                 ),
                 "leanrag.pipelines": MagicMock(),
                 "leanrag.pipelines.incremental": MagicMock(
                     incremental_update=mock_incremental_update,
                 ),
             }):
            backend.incremental_index(sample_chunks)

        # incremental_update must have been called with llm_func keyword argument
        call_kwargs = mock_incremental_update.call_args
        assert "llm_func" in call_kwargs.kwargs or (
            len(call_kwargs.args) > 5 and call_kwargs.args[5] is not None
        ), "llm_func must be passed to incremental_update"
        # The llm_func should be generate_text
        if "llm_func" in call_kwargs.kwargs:
            assert call_kwargs.kwargs["llm_func"] is mock_generate_text


# ================================================================== #
# MAJOR-2: Deterministic entity ID hashing
# ================================================================== #


class TestDeterministicEntityIdHash:
    """Tests for MAJOR-2: entity IDs must be deterministic across processes."""

    def test_same_name_produces_same_id(self):
        """hashlib.sha256 hashing is deterministic across calls."""
        import hashlib

        name = "OAuth2"
        id1 = int(hashlib.sha256(name.encode()).hexdigest(), 16) % (2**31)
        id2 = int(hashlib.sha256(name.encode()).hexdigest(), 16) % (2**31)
        assert id1 == id2

    def test_different_names_produce_different_ids(self):
        """Different names should produce different IDs (collision improbable)."""
        import hashlib

        names = ["OAuth2", "JWT", "RBAC", "SAML", "OpenID"]
        ids = [
            int(hashlib.sha256(n.encode()).hexdigest(), 16) % (2**31)
            for n in names
        ]
        assert len(set(ids)) == len(names), "All names should produce unique IDs"

    def test_id_fits_in_int32(self):
        """Entity IDs must fit in a 32-bit signed integer (SQLite compat)."""
        import hashlib

        for name in ["OAuth2", "JWT_TOKENS", "A" * 1000]:
            entity_id = int(hashlib.sha256(name.encode()).hexdigest(), 16) % (2**31)
            assert 0 <= entity_id < 2**31


# ================================================================== #
# MAJOR-4: Guard against degenerate single-entry full rebuild
# ================================================================== #


class TestDegenerateRebuildGuard:
    """Tests for MAJOR-4: too-few chunks should skip full rebuild."""

    def test_single_chunk_returns_skip_result(self, backend):
        """One chunk should be skipped (not trigger degenerate UMAP)."""
        single_chunk = ChunkPayload(
            manifest_version="1.0",
            chunks=[{"id": "c1", "text": "Only one chunk."}],
        )

        with patch.object(backend, "has_incremental_state", return_value=False):
            result = backend.incremental_index(single_chunk)

        assert result.indexed_count == 0
        assert "Skipped" in result.message
        assert "insufficient" in result.message

    def test_four_chunks_returns_skip_result(self, backend):
        """Four chunks should still be skipped (< 5 threshold)."""
        chunks = ChunkPayload(
            manifest_version="1.0",
            chunks=[{"id": f"c{i}", "text": f"Chunk {i}"} for i in range(4)],
        )

        with patch.object(backend, "has_incremental_state", return_value=False):
            result = backend.incremental_index(chunks)

        assert result.indexed_count == 0
        assert "Skipped" in result.message

    def test_five_chunks_triggers_full_build(self, backend, sample_chunks):
        """Five chunks should trigger full build (>= threshold)."""
        chunks = ChunkPayload(
            manifest_version="1.0",
            chunks=[{"id": f"c{i}", "text": f"Chunk {i} with content"} for i in range(5)],
        )

        mock_result = IndexResult(
            manifest_version="1.0",
            indexed_count=5,
            message="Full build: 5 clusters",
        )

        with patch.object(backend, "has_incremental_state", return_value=False), \
             patch.object(backend, "index", return_value=mock_result) as mock_index:
            result = backend.incremental_index(chunks)
            mock_index.assert_called_once()
            assert result.indexed_count == 5
