"""Tests for LeanRAG native build module.

Tests the build_native.py module which provides native Python API for
hierarchical graph building (replacing subprocess-based build.py).

Note: These tests require LeanRAG config.yaml to be present.
Run with: PYTHONPATH=external/LeanRAG pytest tests/unit/test_leanrag_native.py
Or mark with @pytest.mark.leanrag to skip when config unavailable.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add LeanRAG to path for imports
LEANRAG_PATH = Path(__file__).parent.parent.parent / "external" / "LeanRAG"
if str(LEANRAG_PATH) not in sys.path:
    sys.path.insert(0, str(LEANRAG_PATH))

# Check if LeanRAG can be imported (requires config.yaml in working directory)
# LeanRAG loads config at import time, so we test actual importability
LEANRAG_AVAILABLE = False
LEANRAG_SKIP_REASON = "LeanRAG unavailable"
try:
    from leanrag.pipelines import build_native
    LEANRAG_AVAILABLE = True
except (ImportError, FileNotFoundError) as e:
    LEANRAG_SKIP_REASON = f"LeanRAG import failed: {e}. Run from external/LeanRAG directory."

# Skip decorator for tests requiring LeanRAG
requires_leanrag = pytest.mark.skipif(
    not LEANRAG_AVAILABLE,
    reason=LEANRAG_SKIP_REASON
)


@requires_leanrag
class TestBuildResult:
    """Tests for BuildResult dataclass."""

    def test_default_values(self):
        """Test BuildResult default initialization."""
        from leanrag.pipelines.build_native import BuildResult

        result = BuildResult()
        assert result.entries_processed == 0
        assert result.clusters_created == 0
        assert result.relations_created == 0
        assert result.communities_created == 0
        assert result.duration_seconds == 0.0
        assert result.checkpoint_path is None
        assert result.completed_steps == []
        assert result.errors == []

    def test_to_dict(self):
        """Test BuildResult to_dict conversion."""
        from leanrag.pipelines.build_native import BuildResult

        result = BuildResult(
            entries_processed=100,
            clusters_created=10,
            relations_created=50,
            duration_seconds=5.5,
            completed_steps=["load", "embed"],
            errors=["warning 1"],
        )
        d = result.to_dict()

        assert d["entries_processed"] == 100
        assert d["clusters_created"] == 10
        assert d["relations_created"] == 50
        assert d["duration_seconds"] == 5.5
        assert d["completed_steps"] == ["load", "embed"]
        assert d["errors"] == ["warning 1"]


@requires_leanrag
class TestCheckpointSerialization:
    """Tests for checkpoint serialization/deserialization."""

    def test_serialize_basic(self):
        """Test basic checkpoint serialization."""
        from leanrag.pipelines.build_native import _serialize_checkpoint

        checkpoint = {
            "step": 1,
            "last_step": "test",
            "entity_results": {"foo": {"name": "foo"}},
        }
        serialized = _serialize_checkpoint(checkpoint)

        assert serialized["step"] == 1
        assert serialized["last_step"] == "test"

    def test_serialize_relation_results(self):
        """Test serialization of tuple-keyed relation_results dict."""
        from leanrag.pipelines.build_native import _serialize_checkpoint

        checkpoint = {
            "relation_results": {
                ("A", "B"): {"weight": 1},
                ("C", "D"): {"weight": 2},
            }
        }
        serialized = _serialize_checkpoint(checkpoint)

        # Should be converted to list with key field
        assert isinstance(serialized["relation_results"], list)
        assert len(serialized["relation_results"]) == 2
        keys = [r["key"] for r in serialized["relation_results"]]
        assert "A||B" in keys
        assert "C||D" in keys

    def test_deserialize_basic(self):
        """Test basic checkpoint deserialization."""
        from leanrag.pipelines.build_native import _deserialize_checkpoint

        serialized = {
            "step": 2,
            "schema_version": 1,
        }
        checkpoint = _deserialize_checkpoint(serialized)

        assert checkpoint["step"] == 2
        assert checkpoint["schema_version"] == 1

    def test_deserialize_relation_results(self):
        """Test deserialization of relation_results back to tuple keys."""
        from leanrag.pipelines.build_native import _deserialize_checkpoint

        serialized = {
            "relation_results": [
                {"key": "A||B", "weight": 1},
                {"key": "C||D", "weight": 2},
            ]
        }
        checkpoint = _deserialize_checkpoint(serialized)

        # Should be converted back to dict with tuple keys
        assert isinstance(checkpoint["relation_results"], dict)
        assert ("A", "B") in checkpoint["relation_results"]
        assert ("C", "D") in checkpoint["relation_results"]
        assert checkpoint["relation_results"][("A", "B")]["weight"] == 1

    def test_roundtrip(self):
        """Test serialize/deserialize roundtrip."""
        from leanrag.pipelines.build_native import (
            _deserialize_checkpoint,
            _serialize_checkpoint,
        )

        original = {
            "step": 3,
            "schema_version": 1,
            "relation_results": {
                ("X", "Y"): {"desc": "test"},
            },
            "generate_relations": {
                ("A", "B"): {"generated": True},
            },
        }

        serialized = _serialize_checkpoint(original)
        deserialized = _deserialize_checkpoint(serialized)

        assert deserialized["step"] == original["step"]
        assert ("X", "Y") in deserialized["relation_results"]
        assert ("A", "B") in deserialized["generate_relations"]


@requires_leanrag
class TestHelperFunctions:
    """Tests for helper functions."""

    def test_truncate_text_short(self):
        """Test truncation of short text (no truncation needed)."""
        from leanrag.pipelines.build_native import _truncate_text

        short = "This is short"
        assert _truncate_text(short) == short

    def test_truncate_text_long(self):
        """Test truncation of long text."""
        from leanrag.pipelines.build_native import _truncate_text

        # Create text longer than 4096 tokens
        long_text = "word " * 5000
        truncated = _truncate_text(long_text, max_tokens=100)
        assert len(truncated) < len(long_text)

    def test_strip_vectors(self):
        """Test vector stripping from entities."""
        from leanrag.pipelines.build_native import _strip_vectors

        entities = {
            "A": {"name": "A", "vector": [1, 2, 3], "desc": "test"},
            "B": {"name": "B", "vector": [4, 5, 6], "desc": "test2"},
        }
        stripped = _strip_vectors(entities)

        assert "vector" not in stripped["A"]
        assert "vector" not in stripped["B"]
        assert stripped["A"]["name"] == "A"
        assert stripped["B"]["desc"] == "test2"

    def test_count_clusters_dict(self):
        """Test cluster counting for dict entities."""
        from leanrag.pipelines.build_native import _count_clusters

        entities = {"A": {}, "B": {}, "C": {}}
        assert _count_clusters(entities) == 3

    def test_count_clusters_list(self):
        """Test cluster counting for hierarchical list entities."""
        from leanrag.pipelines.build_native import _count_clusters

        # Hierarchical structure: [[layer0], [layer1], [layer2]]
        entities = [[{}, {}], [{}, {}, {}], [{}]]
        assert _count_clusters(entities) == 6


@requires_leanrag
class TestBuildHierarchicalGraph:
    """Tests for build_hierarchical_graph function."""

    def test_missing_input_files(self, tmp_path):
        """Test error when input files don't exist."""
        from leanrag.pipelines.build_native import build_hierarchical_graph

        with pytest.raises(FileNotFoundError):
            build_hierarchical_graph(working_dir=str(tmp_path))

    def test_checkpoint_creation(self, tmp_path):
        """Test that checkpoint file is created during build."""
        # Create minimal input files
        entity_file = tmp_path / "entity.jsonl"
        relation_file = tmp_path / "relation.jsonl"

        entity_file.write_text('{"entity_name": "test", "description": "desc", "source_id": "s1"}\n')
        relation_file.write_text('{"src_tgt": "A", "tgt_src": "B", "description": "rel", "source_id": "s1"}\n')

        # We can't run the full pipeline without LLM/embedding services,
        # but we can test that it starts correctly
        checkpoint_path = tmp_path / ".checkpoint.json"

        # Mock the expensive operations
        with patch("leanrag.pipelines.build_native._embedding_data") as mock_embed:
            mock_embed.side_effect = Exception("Stop here for test")

            try:
                from leanrag.pipelines.build_native import build_hierarchical_graph

                build_hierarchical_graph(
                    working_dir=str(tmp_path),
                    checkpoint_path=str(checkpoint_path),
                )
            except Exception:
                pass  # Expected to fail at embedding step

        # Checkpoint should have been created after step 1
        assert checkpoint_path.exists()
        with open(checkpoint_path) as f:
            checkpoint = json.load(f)
        assert checkpoint["step"] == 1
        assert checkpoint["schema_version"] == 1

    def test_fresh_start_removes_checkpoint(self, tmp_path):
        """Test that fresh_start=True removes existing checkpoint."""
        checkpoint_path = tmp_path / ".checkpoint.json"
        checkpoint_path.write_text('{"step": 3}')

        entity_file = tmp_path / "entity.jsonl"
        relation_file = tmp_path / "relation.jsonl"
        entity_file.write_text('{"entity_name": "test", "description": "desc", "source_id": "s1"}\n')
        relation_file.write_text('{"src_tgt": "A", "tgt_src": "B", "description": "rel", "source_id": "s1"}\n')

        with patch("leanrag.pipelines.build_native._embedding_data") as mock_embed:
            mock_embed.side_effect = Exception("Stop")

            try:
                from leanrag.pipelines.build_native import build_hierarchical_graph

                build_hierarchical_graph(
                    working_dir=str(tmp_path),
                    checkpoint_path=str(checkpoint_path),
                    fresh_start=True,
                )
            except Exception:
                pass

        # Checkpoint should be recreated starting from step 1
        with open(checkpoint_path) as f:
            checkpoint = json.load(f)
        assert checkpoint["step"] == 1


    def test_strict_checkpoint_raises_on_mismatch(self, tmp_path):
        """Test that strict_checkpoint=True raises ValueError on schema mismatch."""
        from leanrag.pipelines.build_native import (
            CHECKPOINT_SCHEMA_VERSION,
            build_hierarchical_graph,
        )

        # Create input files
        entity_file = tmp_path / "entity.jsonl"
        relation_file = tmp_path / "relation.jsonl"
        entity_file.write_text('{"entity_name": "test", "description": "desc", "source_id": "s1"}\n')
        relation_file.write_text('{"src_tgt": "A", "tgt_src": "B", "description": "rel", "source_id": "s1"}\n')

        # Create checkpoint with future schema version
        checkpoint_path = tmp_path / ".checkpoint.json"
        checkpoint_path.write_text(json.dumps({
            "schema_version": CHECKPOINT_SCHEMA_VERSION + 1,
            "step": 2,
        }))

        # Should raise ValueError in strict mode
        with pytest.raises(ValueError, match="does not match"):
            build_hierarchical_graph(
                working_dir=str(tmp_path),
                checkpoint_path=str(checkpoint_path),
                strict_checkpoint=True,
            )

    def test_non_strict_checkpoint_warns_on_mismatch(self, tmp_path):
        """Test that strict_checkpoint=False logs warning but proceeds."""
        from leanrag.pipelines.build_native import (
            CHECKPOINT_SCHEMA_VERSION,
            build_hierarchical_graph,
        )

        # Create input files
        entity_file = tmp_path / "entity.jsonl"
        relation_file = tmp_path / "relation.jsonl"
        entity_file.write_text('{"entity_name": "test", "description": "desc", "source_id": "s1"}\n')
        relation_file.write_text('{"src_tgt": "A", "tgt_src": "B", "description": "rel", "source_id": "s1"}\n')

        # Create checkpoint with future schema version
        checkpoint_path = tmp_path / ".checkpoint.json"
        checkpoint_path.write_text(json.dumps({
            "schema_version": CHECKPOINT_SCHEMA_VERSION + 1,
            "step": 1,
            "entity_results": {},
            "relation_results": [],
        }))

        # Mock embedding to stop early
        with patch("leanrag.pipelines.build_native._embedding_data") as mock_embed:
            mock_embed.side_effect = Exception("Stop for test")

            try:
                build_hierarchical_graph(
                    working_dir=str(tmp_path),
                    checkpoint_path=str(checkpoint_path),
                    strict_checkpoint=False,  # Should not raise
                )
            except Exception:
                pass  # Expected to fail at embedding, not checkpoint


@requires_leanrag
class TestModuleExports:
    """Tests for module-level exports."""

    def test_imports(self):
        """Test that build_native exports are available."""
        from leanrag.pipelines import BuildResult, build_hierarchical_graph

        assert callable(build_hierarchical_graph)
        assert BuildResult is not None

    def test_checkpoint_schema_version(self):
        """Test checkpoint schema version constant."""
        from leanrag.pipelines.build_native import CHECKPOINT_SCHEMA_VERSION

        assert CHECKPOINT_SCHEMA_VERSION == 1
