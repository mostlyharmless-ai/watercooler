"""Unit tests for watercooler.baseline_graph.pipeline module.

Tests the baseline graph pipeline infrastructure:
- PipelineState and ThreadState for incremental builds
- PipelineConfig, LLMConfig, EmbeddingConfig dataclasses
- PipelineResult dataclass
- Storage primitives (atomic writes, path resolution)
- BaselineGraphRunner logic (without external services)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch

import pytest

from watercooler.baseline_graph.pipeline.state import (
    PipelineState,
    ThreadState,
)
from watercooler.baseline_graph.pipeline.config import (
    LLMConfig,
    EmbeddingConfig,
    PipelineConfig,
)
from watercooler.baseline_graph.pipeline.runner import (
    PipelineResult,
    BaselineGraphRunner,
    run_pipeline,
)
from watercooler.baseline_graph import storage


# ============================================================================
# Test ThreadState
# ============================================================================


class TestThreadState:
    """Tests for ThreadState dataclass."""

    def test_thread_state_creation(self):
        """Test basic ThreadState creation."""
        state = ThreadState(topic="test-topic", mtime=1000.0, entry_count=5)
        assert state.topic == "test-topic"
        assert state.mtime == 1000.0
        assert state.entry_count == 5
        assert state.summary == ""
        assert state.entry_summaries == {}
        assert state.entry_embeddings == {}

    def test_thread_state_with_data(self):
        """Test ThreadState with cached data."""
        state = ThreadState(
            topic="feature",
            mtime=2000.0,
            entry_count=3,
            summary="Thread summary",
            entry_summaries={"entry1": "Summary 1", "entry2": "Summary 2"},
            entry_embeddings={"entry1": [0.1, 0.2, 0.3]},
        )
        assert state.summary == "Thread summary"
        assert len(state.entry_summaries) == 2
        assert len(state.entry_embeddings) == 1
        assert state.entry_embeddings["entry1"] == [0.1, 0.2, 0.3]


# ============================================================================
# Test PipelineState
# ============================================================================


class TestPipelineState:
    """Tests for PipelineState class."""

    def test_empty_state_creation(self):
        """Test creating empty pipeline state."""
        state = PipelineState()
        assert state.version == "1.0"
        assert state.last_run == ""
        assert state.threads == {}

    def test_load_from_nonexistent_file(self, tmp_path):
        """Test loading state from nonexistent file returns empty state."""
        state = PipelineState.load(tmp_path / "missing.json")
        assert state.version == "1.0"
        assert state.threads == {}

    def test_load_from_corrupted_file(self, tmp_path):
        """Test loading state from corrupted file returns empty state."""
        state_path = tmp_path / "state.json"
        state_path.write_text("not valid json {{{")

        state = PipelineState.load(state_path)
        assert state.version == "1.0"
        assert state.threads == {}

    def test_save_and_load_roundtrip(self, tmp_path):
        """Test saving and loading state."""
        state = PipelineState()
        state.update_thread(
            topic="test-topic",
            mtime=1000.0,
            entry_count=5,
            summary="Thread summary",
            entry_summaries={"e1": "Summary 1"},
            entry_embeddings={"e1": [0.1, 0.2]},
        )

        state_path = tmp_path / "state.json"
        state.save(state_path)

        loaded = PipelineState.load(state_path)
        assert loaded.version == "1.0"
        assert "test-topic" in loaded.threads
        assert loaded.threads["test-topic"].summary == "Thread summary"
        assert loaded.threads["test-topic"].entry_count == 5

    def test_is_thread_changed_new_thread(self):
        """Test that new threads are detected as changed."""
        state = PipelineState()
        assert state.is_thread_changed("new-topic", 1000.0, 5) is True

    def test_is_thread_changed_same(self):
        """Test that unchanged threads are not detected as changed."""
        state = PipelineState()
        state.update_thread("topic", mtime=1000.0, entry_count=5)

        assert state.is_thread_changed("topic", 1000.0, 5) is False

    def test_is_thread_changed_mtime_changed(self):
        """Test that mtime changes are detected."""
        state = PipelineState()
        state.update_thread("topic", mtime=1000.0, entry_count=5)

        assert state.is_thread_changed("topic", 2000.0, 5) is True

    def test_is_thread_changed_entry_count_changed(self):
        """Test that entry count changes are detected."""
        state = PipelineState()
        state.update_thread("topic", mtime=1000.0, entry_count=5)

        assert state.is_thread_changed("topic", 1000.0, 6) is True

    def test_get_cached_summary(self):
        """Test retrieving cached thread summary."""
        state = PipelineState()
        state.update_thread("topic", mtime=1000.0, entry_count=1, summary="My summary")

        assert state.get_cached_summary("topic") == "My summary"
        assert state.get_cached_summary("missing") is None

    def test_get_cached_entry_summary(self):
        """Test retrieving cached entry summary."""
        state = PipelineState()
        state.update_thread(
            "topic", mtime=1000.0, entry_count=1,
            entry_summaries={"e1": "Entry summary"}
        )

        assert state.get_cached_entry_summary("topic", "e1") == "Entry summary"
        assert state.get_cached_entry_summary("topic", "e2") is None
        assert state.get_cached_entry_summary("missing", "e1") is None

    def test_get_cached_entry_embedding(self):
        """Test retrieving cached entry embedding."""
        state = PipelineState()
        state.update_thread(
            "topic", mtime=1000.0, entry_count=1,
            entry_embeddings={"e1": [0.1, 0.2, 0.3]}
        )

        assert state.get_cached_entry_embedding("topic", "e1") == [0.1, 0.2, 0.3]
        assert state.get_cached_entry_embedding("topic", "e2") is None

    def test_remove_deleted_threads(self):
        """Test removing threads that no longer exist."""
        state = PipelineState()
        state.update_thread("topic1", mtime=1000.0, entry_count=1)
        state.update_thread("topic2", mtime=1000.0, entry_count=1)
        state.update_thread("topic3", mtime=1000.0, entry_count=1)

        removed = state.remove_deleted_threads({"topic1", "topic3"})

        assert removed == ["topic2"]
        assert "topic1" in state.threads
        assert "topic2" not in state.threads
        assert "topic3" in state.threads


# ============================================================================
# Test Config Classes
# ============================================================================


class TestLLMConfig:
    """Tests for LLMConfig dataclass."""

    def test_default_values(self, monkeypatch):
        """Test default LLM config values."""
        # Clear env vars to test fallback defaults
        monkeypatch.delenv("LLM_API_BASE", raising=False)
        monkeypatch.delenv("LLM_MODEL", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)

        config = LLMConfig()
        assert config.timeout == 120.0
        assert config.max_tokens == 256
        # API base comes from unified config or env, just check it's a string
        assert isinstance(config.api_base, str)

    def test_from_env(self, monkeypatch):
        """Test LLMConfig.from_env()."""
        monkeypatch.setenv("LLM_TIMEOUT", "60.0")
        monkeypatch.setenv("LLM_MAX_TOKENS", "512")

        config = LLMConfig.from_env()
        assert config.timeout == 60.0
        assert config.max_tokens == 512


class TestEmbeddingConfig:
    """Tests for EmbeddingConfig dataclass."""

    def test_default_values(self, monkeypatch):
        """Test default embedding config values."""
        monkeypatch.delenv("EMBEDDING_API_BASE", raising=False)
        monkeypatch.delenv("EMBEDDING_MODEL", raising=False)

        config = EmbeddingConfig()
        assert config.timeout == 60.0
        assert isinstance(config.embedding_dim, int)

    def test_from_env(self, monkeypatch):
        """Test EmbeddingConfig.from_env()."""
        monkeypatch.setenv("EMBEDDING_TIMEOUT", "30.0")

        config = EmbeddingConfig.from_env()
        assert config.timeout == 30.0


class TestPipelineConfig:
    """Tests for PipelineConfig dataclass."""

    def test_default_output_dir(self, tmp_path):
        """Test that output_dir defaults to threads_dir/graph/baseline."""
        config = PipelineConfig(threads_dir=tmp_path)
        assert config.output_dir == tmp_path / "graph" / "baseline"

    def test_custom_output_dir(self, tmp_path):
        """Test custom output directory."""
        custom_output = tmp_path / "custom"
        config = PipelineConfig(threads_dir=tmp_path, output_dir=custom_output)
        assert config.output_dir == custom_output

    def test_validate_missing_threads_dir(self, tmp_path):
        """Test validation fails for missing threads directory."""
        config = PipelineConfig(threads_dir=tmp_path / "missing")
        errors = config.validate()
        assert len(errors) == 1
        assert "not found" in errors[0]

    def test_validate_valid_config(self, tmp_path):
        """Test validation passes for valid config."""
        config = PipelineConfig(threads_dir=tmp_path)
        errors = config.validate()
        assert errors == []

    def test_feature_flags(self, tmp_path):
        """Test feature flags are properly set."""
        config = PipelineConfig(
            threads_dir=tmp_path,
            extractive_only=True,
            skip_embeddings=True,
            skip_closed=True,
            fresh=True,
            incremental=True,
        )
        assert config.extractive_only is True
        assert config.skip_embeddings is True
        assert config.skip_closed is True
        assert config.fresh is True
        assert config.incremental is True


# ============================================================================
# Test PipelineResult
# ============================================================================


class TestPipelineResult:
    """Tests for PipelineResult dataclass."""

    def test_success_result(self, tmp_path):
        """Test creating a successful result."""
        result = PipelineResult(
            success=True,
            threads_processed=10,
            entries_processed=50,
            nodes_created=60,
            edges_created=100,
            embeddings_generated=50,
            duration_seconds=5.5,
            output_dir=tmp_path,
        )
        assert result.success is True
        assert result.threads_processed == 10
        assert result.entries_processed == 50
        assert result.error is None

    def test_failure_result(self, tmp_path):
        """Test creating a failure result."""
        result = PipelineResult(
            success=False,
            threads_processed=0,
            entries_processed=0,
            nodes_created=0,
            edges_created=0,
            embeddings_generated=0,
            duration_seconds=0.1,
            output_dir=tmp_path,
            error="Configuration error",
        )
        assert result.success is False
        assert result.error == "Configuration error"


# ============================================================================
# Test Storage Primitives
# ============================================================================


class TestStoragePaths:
    """Tests for storage path resolution functions."""

    def test_get_graph_dir(self, tmp_path):
        """Test graph directory path resolution."""
        graph_dir = storage.get_graph_dir(tmp_path)
        assert graph_dir == tmp_path / "graph" / "baseline"

    def test_get_thread_graph_dir(self, tmp_path):
        """Test per-thread graph directory path."""
        graph_dir = tmp_path / "graph" / "baseline"
        thread_dir = storage.get_thread_graph_dir(graph_dir, "test-topic")
        assert thread_dir == graph_dir / "threads" / "test-topic"

    def test_ensure_graph_dir_creates_directories(self, tmp_path):
        """Test that ensure_graph_dir creates directories."""
        graph_dir = storage.ensure_graph_dir(tmp_path)
        assert graph_dir.exists()
        assert graph_dir.is_dir()

    def test_ensure_thread_graph_dir_creates_directories(self, tmp_path):
        """Test that ensure_thread_graph_dir creates directories."""
        graph_dir = storage.ensure_graph_dir(tmp_path)
        thread_dir = storage.ensure_thread_graph_dir(graph_dir, "my-topic")
        assert thread_dir.exists()
        assert thread_dir.is_dir()


class TestAtomicWrites:
    """Tests for atomic write operations."""

    def test_atomic_write_json(self, tmp_path):
        """Test atomic JSON file writing."""
        path = tmp_path / "data.json"
        data = {"key": "value", "count": 42}

        storage.atomic_write_json(path, data)

        assert path.exists()
        with open(path) as f:
            loaded = json.load(f)
        assert loaded == data

    def test_atomic_write_json_creates_parent_dirs(self, tmp_path):
        """Test atomic JSON write creates parent directories."""
        path = tmp_path / "nested" / "dir" / "data.json"
        data = {"test": True}

        storage.atomic_write_json(path, data)

        assert path.exists()

    def test_atomic_write_jsonl(self, tmp_path):
        """Test atomic JSONL file writing."""
        path = tmp_path / "data.jsonl"
        items = [
            {"id": 1, "name": "first"},
            {"id": 2, "name": "second"},
            {"id": 3, "name": "third"},
        ]

        storage.atomic_write_jsonl(path, items)

        assert path.exists()
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 3
        assert json.loads(lines[0]) == {"id": 1, "name": "first"}

    def test_atomic_write_jsonl_empty_list(self, tmp_path):
        """Test atomic JSONL write with empty list."""
        path = tmp_path / "empty.jsonl"
        storage.atomic_write_jsonl(path, [])

        assert path.exists()
        assert path.read_text() == ""

    def test_atomic_write_is_atomic(self, tmp_path):
        """Test that writes are atomic (no partial writes)."""
        path = tmp_path / "data.json"

        # Write initial data
        storage.atomic_write_json(path, {"version": 1})

        # Write new data - should completely replace
        storage.atomic_write_json(path, {"version": 2, "extra": "data"})

        with open(path) as f:
            loaded = json.load(f)
        assert loaded == {"version": 2, "extra": "data"}


class TestThreadGraphOperations:
    """Tests for thread-level graph operations."""

    @pytest.fixture
    def graph_dir(self, tmp_path):
        """Create and return graph directory."""
        return storage.ensure_graph_dir(tmp_path)

    def test_write_and_load_thread_meta(self, graph_dir):
        """Test writing and loading thread metadata."""
        topic = "test-topic"
        meta = {"id": f"thread:{topic}", "title": "Test Thread", "status": "OPEN"}
        entries = {"e1": {"id": "entry:e1", "body": "Entry 1"}}
        edges = {"e1e2": {"source": "e1", "target": "e2", "type": "followed_by"}}

        storage.write_thread_graph(graph_dir, topic, meta, entries, edges)

        loaded_meta = storage.load_thread_meta(graph_dir, topic)
        assert loaded_meta is not None
        assert loaded_meta["title"] == "Test Thread"

    def test_load_thread_meta_missing(self, graph_dir):
        """Test loading missing thread metadata returns None."""
        meta = storage.load_thread_meta(graph_dir, "nonexistent")
        assert meta is None

    def test_load_thread_entries(self, graph_dir):
        """Test loading thread entries."""
        topic = "test-topic"
        meta = {"id": f"thread:{topic}"}
        entries = {
            "e1": {"id": "entry:e1", "body": "Entry 1"},
            "e2": {"id": "entry:e2", "body": "Entry 2"},
        }
        edges = {}

        storage.write_thread_graph(graph_dir, topic, meta, entries, edges)

        # load_thread_entries returns a generator, convert to list
        loaded_entries = list(storage.load_thread_entries(graph_dir, topic))
        assert len(loaded_entries) == 2
        entry_ids = [e["id"] for e in loaded_entries]
        assert "entry:e1" in entry_ids

    def test_load_thread_edges(self, graph_dir):
        """Test loading thread edges."""
        topic = "test-topic"
        meta = {"id": f"thread:{topic}"}
        entries = {}
        edges = {
            "edge1": {"source": "e1", "target": "e2", "type": "followed_by"},
            "edge2": {"source": "t1", "target": "e1", "type": "contains"},
        }

        storage.write_thread_graph(graph_dir, topic, meta, entries, edges)

        loaded_edges = storage.load_thread_edges(graph_dir, topic)
        assert len(loaded_edges) == 2


# ============================================================================
# Test BaselineGraphRunner
# ============================================================================


class TestBaselineGraphRunner:
    """Tests for BaselineGraphRunner class."""

    @pytest.fixture
    def mock_config(self, tmp_path):
        """Create a mock pipeline config."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        return PipelineConfig(threads_dir=threads_dir)

    def test_runner_creation(self, mock_config):
        """Test runner creation with default options."""
        runner = BaselineGraphRunner(mock_config)
        assert runner.config == mock_config
        assert runner.verbose is False
        assert runner.auto_server is True

    def test_runner_verbose_mode(self, mock_config):
        """Test runner with verbose mode."""
        runner = BaselineGraphRunner(mock_config, verbose=True)
        assert runner.verbose is True

    def test_clear_cache_fresh_mode(self, mock_config):
        """Test cache clearing in fresh mode."""
        mock_config.fresh = True
        mock_config.output_dir.mkdir(parents=True)
        (mock_config.output_dir / "test.json").write_text("{}")

        runner = BaselineGraphRunner(mock_config)
        runner._clear_cache()

        assert not mock_config.output_dir.exists()

    def test_clear_cache_not_fresh_mode(self, mock_config):
        """Test cache is preserved when not in fresh mode."""
        mock_config.fresh = False
        mock_config.output_dir.mkdir(parents=True)
        test_file = mock_config.output_dir / "test.json"
        test_file.write_text("{}")

        runner = BaselineGraphRunner(mock_config)
        runner._clear_cache()

        assert test_file.exists()

    def test_state_path(self, mock_config):
        """Test state file path generation."""
        runner = BaselineGraphRunner(mock_config)
        state_path = runner._state_path()
        assert state_path == mock_config.output_dir / "state.json"

    def test_load_state_non_incremental(self, mock_config):
        """Test state loading in non-incremental mode."""
        mock_config.incremental = False
        runner = BaselineGraphRunner(mock_config)
        runner._load_state()

        assert runner._state is not None
        assert runner._state.threads == {}

    def test_load_state_incremental_no_file(self, mock_config):
        """Test state loading in incremental mode with no prior state."""
        mock_config.incremental = True
        runner = BaselineGraphRunner(mock_config)
        runner._load_state()

        assert runner._state is not None
        assert runner._state.last_run == ""

    def test_load_state_incremental_with_file(self, mock_config):
        """Test state loading in incremental mode with prior state."""
        mock_config.incremental = True
        mock_config.output_dir.mkdir(parents=True)

        # Create prior state
        state = PipelineState()
        state.update_thread("topic1", mtime=1000.0, entry_count=5)
        state.save(mock_config.output_dir / "state.json")

        runner = BaselineGraphRunner(mock_config)
        runner._load_state()

        assert "topic1" in runner._state.threads

    def test_run_validation_failure(self, tmp_path):
        """Test pipeline run with validation failure."""
        config = PipelineConfig(threads_dir=tmp_path / "missing")

        runner = BaselineGraphRunner(config, auto_server=False)
        result = runner.run()

        assert result.success is False
        assert "not found" in result.error

    def test_run_no_threads(self, mock_config):
        """Test pipeline run with no threads."""
        runner = BaselineGraphRunner(mock_config, auto_server=False)
        result = runner.run()

        assert result.success is False
        assert "No threads found" in result.error


class TestRunPipeline:
    """Tests for run_pipeline convenience function."""

    def test_run_pipeline_empty_dir(self, tmp_path):
        """Test running pipeline on empty directory."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        result = run_pipeline(threads_dir, auto_server=False)

        assert result.success is False
        assert "No threads found" in result.error

    def test_run_pipeline_with_options(self, tmp_path):
        """Test run_pipeline with various options."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        result = run_pipeline(
            threads_dir,
            extractive_only=True,
            skip_embeddings=True,
            verbose=True,
            auto_server=False,
        )

        # Should fail due to no threads, but options should be passed
        assert result.success is False


# ============================================================================
# Test Incremental Build Logic
# ============================================================================


class TestIncrementalBuild:
    """Tests for incremental build detection."""

    @pytest.fixture
    def threads_dir(self, tmp_path):
        """Create threads directory with sample threads."""
        d = tmp_path / "threads"
        d.mkdir()

        # Create sample thread
        thread = dedent("""\
            # test-topic — Test Thread
            Status: OPEN
            Ball: Agent

            ---
            Entry: Agent (user) 2025-01-01T12:00:00Z
            Role: implementer
            Type: Note
            Title: Test entry

            Entry body text.
            <!-- Entry-ID: 01TEST00000000000000000001 -->

            ---
        """)
        (d / "test-topic.md").write_text(thread, encoding="utf-8")
        return d

    def test_detect_changed_thread(self, threads_dir):
        """Test detection of changed thread."""
        config = PipelineConfig(
            threads_dir=threads_dir,
            incremental=True,
        )

        # Create prior state with old mtime
        prior_state = PipelineState()
        prior_state.update_thread("test-topic", mtime=0, entry_count=1)

        # Get actual mtime
        thread_path = threads_dir / "test-topic.md"
        current_mtime = thread_path.stat().st_mtime

        # Should detect change due to mtime difference
        assert prior_state.is_thread_changed("test-topic", current_mtime, 1) is True

    def test_detect_unchanged_thread(self, threads_dir):
        """Test unchanged thread is not detected as changed."""
        thread_path = threads_dir / "test-topic.md"
        current_mtime = thread_path.stat().st_mtime

        state = PipelineState()
        state.update_thread("test-topic", mtime=current_mtime, entry_count=1)

        assert state.is_thread_changed("test-topic", current_mtime, 1) is False

    def test_cached_data_applied(self, threads_dir):
        """Test that cached summaries are applied to unchanged threads."""
        thread_path = threads_dir / "test-topic.md"
        current_mtime = thread_path.stat().st_mtime

        state = PipelineState()
        state.update_thread(
            "test-topic",
            mtime=current_mtime,
            entry_count=1,
            summary="Cached thread summary",
            entry_summaries={"01TEST00000000000000000001": "Cached entry summary"},
        )

        assert state.get_cached_summary("test-topic") == "Cached thread summary"
        assert state.get_cached_entry_summary(
            "test-topic", "01TEST00000000000000000001"
        ) == "Cached entry summary"


# ============================================================================
# Test Edge Cases
# ============================================================================


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_pipeline_state_empty_summary(self):
        """Test that empty string summary returns None from cache."""
        state = PipelineState()
        state.update_thread("topic", mtime=1000.0, entry_count=1, summary="")

        # Empty string should return None
        assert state.get_cached_summary("topic") is None

    def test_pipeline_state_thread_update_overwrites(self):
        """Test that update_thread completely replaces thread state."""
        state = PipelineState()
        state.update_thread(
            "topic", mtime=1000.0, entry_count=5,
            summary="Original",
            entry_summaries={"e1": "S1", "e2": "S2"}
        )

        # Update with fewer entries
        state.update_thread(
            "topic", mtime=2000.0, entry_count=1,
            summary="New",
            entry_summaries={"e1": "S1-updated"}
        )

        assert state.threads["topic"].entry_count == 1
        assert len(state.threads["topic"].entry_summaries) == 1
        assert "e2" not in state.threads["topic"].entry_summaries

    def test_storage_write_handles_special_characters(self, tmp_path):
        """Test that storage handles special characters in data."""
        path = tmp_path / "special.json"
        data = {
            "unicode": "日本語テスト",
            "emoji": "🔥✨",
            "quotes": 'He said "hello"',
            "newlines": "line1\nline2",
        }

        storage.atomic_write_json(path, data)
        with open(path) as f:
            loaded = json.load(f)

        assert loaded == data

    def test_storage_topic_with_special_characters(self, tmp_path):
        """Test graph directory for topic with hyphens."""
        graph_dir = storage.ensure_graph_dir(tmp_path)
        thread_dir = storage.ensure_thread_graph_dir(graph_dir, "feature-auth-v2")

        assert thread_dir.exists()
        assert thread_dir.name == "feature-auth-v2"

    def test_config_test_limit(self, tmp_path):
        """Test that test_limit is properly set."""
        config = PipelineConfig(threads_dir=tmp_path, test_limit=5)
        assert config.test_limit == 5

    def test_result_with_zero_duration(self, tmp_path):
        """Test result with zero duration."""
        result = PipelineResult(
            success=True,
            threads_processed=0,
            entries_processed=0,
            nodes_created=0,
            edges_created=0,
            embeddings_generated=0,
            duration_seconds=0.0,
            output_dir=tmp_path,
        )
        assert result.duration_seconds == 0.0


# ============================================================================
# Test Server Management (Mocked)
# ============================================================================


class TestServerManagement:
    """Tests for server management with mocked dependencies."""

    @pytest.fixture
    def config_with_threads(self, tmp_path):
        """Create config with a sample thread."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        thread = dedent("""\
            # test — Test Thread
            Status: OPEN
            Ball: Agent

            ---
        """)
        (threads_dir / "test.md").write_text(thread)

        return PipelineConfig(threads_dir=threads_dir)

    def test_ensure_servers_disabled(self, config_with_threads):
        """Test that server check is skipped when auto_server=False."""
        runner = BaselineGraphRunner(config_with_threads, auto_server=False)
        result = runner._ensure_servers()
        assert result is True

    def test_ensure_servers_extractive_only_no_llm(self, config_with_threads):
        """Test that LLM server is not needed in extractive mode."""
        config_with_threads.extractive_only = True

        runner = BaselineGraphRunner(config_with_threads, auto_server=True)

        # Mock the server manager
        mock_manager = MagicMock()
        mock_manager.check_embedding_server.return_value = True
        runner._server_manager = mock_manager

        result = runner._ensure_servers()
        assert result is True
        mock_manager.check_llm_server.assert_not_called()

    def test_stop_servers_not_called_when_disabled(self, config_with_threads):
        """Test servers are not stopped when stop_servers=False."""
        runner = BaselineGraphRunner(config_with_threads, stop_servers=False)
        runner._server_manager = MagicMock()

        runner._stop_servers_if_needed()

        runner._server_manager.stop_servers.assert_not_called()
