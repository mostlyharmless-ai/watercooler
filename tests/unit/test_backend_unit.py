"""Unit tests for memory backend internal logic."""

import pytest
from pathlib import Path
from unittest.mock import Mock, patch

from watercooler_memory.backends.graphiti import GraphitiBackend, GraphitiConfig
from watercooler_memory.backends.leanrag import LeanRAGBackend, LeanRAGConfig


class TestGraphitiSanitization:
    """Unit tests for Graphiti thread ID sanitization logic."""

    @pytest.fixture
    def backend(self) -> GraphitiBackend:
        """Create Graphiti backend for testing (no test_mode)."""
        config = GraphitiConfig(
            work_dir=Path("/tmp/test"),
            openai_api_key="test-key",
            test_mode=False,
        )
        # Mock validation to avoid requiring Graphiti submodule in CI
        with patch.object(GraphitiBackend, '_validate_config'):
            return GraphitiBackend(config)

    @pytest.fixture
    def backend_test_mode(self) -> GraphitiBackend:
        """Create Graphiti backend with test_mode enabled."""
        config = GraphitiConfig(
            work_dir=Path("/tmp/test"),
            openai_api_key="test-key",
            test_mode=True,
        )
        # Mock validation to avoid requiring Graphiti submodule in CI
        with patch.object(GraphitiBackend, '_validate_config'):
            return GraphitiBackend(config)

    def test_sanitize_basic_alphanumeric(self, backend: GraphitiBackend):
        """Test sanitization of simple alphanumeric thread IDs."""
        result = backend._sanitize_thread_id("simple-thread-name")
        assert result == "simple-thread-name"  # Hyphens preserved

    def test_sanitize_special_chars(self, backend: GraphitiBackend):
        """Test sanitization preserves printable special characters."""
        result = backend._sanitize_thread_id("thread@with#special$chars!")
        assert result == "thread@with#special$chars!"  # Printable chars preserved

    def test_sanitize_consecutive_special_chars(self, backend: GraphitiBackend):
        """Test sanitization preserves consecutive special chars."""
        result = backend._sanitize_thread_id("thread@@##name")
        assert result == "thread@@##name"  # Consecutive chars preserved

    def test_sanitize_empty_string(self, backend: GraphitiBackend):
        """Test sanitization handles empty strings."""
        result = backend._sanitize_thread_id("")
        assert result == "unknown"

    def test_sanitize_starts_with_number(self, backend: GraphitiBackend):
        """Test sanitization prepends 't_' when thread ID starts with number."""
        result = backend._sanitize_thread_id("123-thread")
        assert result == "t_123-thread"  # Hyphens preserved

    def test_sanitize_length_limit_production(self, backend: GraphitiBackend):
        """Test sanitization enforces 64-char limit in production mode."""
        long_name = "a" * 100
        result = backend._sanitize_thread_id(long_name)
        assert len(result) == 64

    def test_sanitize_length_limit_test_mode(self, backend_test_mode: GraphitiBackend):
        """Test sanitization reserves space for pytest__ prefix in test mode."""
        # In test mode, max length should be 64 - 8 = 56 before prefix is added
        long_name = "a" * 100
        result = backend_test_mode._sanitize_thread_id(long_name)
        # Result should be 56 chars + "pytest__" = 64 chars total
        assert len(result) == 64
        assert result.startswith("pytest__")
        assert len(result.replace("pytest__", "")) == 56

    def test_sanitize_test_mode_adds_prefix(self, backend_test_mode: GraphitiBackend):
        """Test that test_mode=True adds pytest__ prefix."""
        result = backend_test_mode._sanitize_thread_id("my-thread")
        assert result.startswith("pytest__")
        assert result == "pytest__my-thread"  # Hyphens preserved

    def test_sanitize_test_mode_no_double_prefix(self, backend_test_mode: GraphitiBackend):
        """Test that pytest__ prefix is not duplicated."""
        result = backend_test_mode._sanitize_thread_id("pytest__my-thread")
        assert result.startswith("pytest__")
        # Should not have double prefix
        assert result.count("pytest__") == 1

    def test_sanitize_production_no_test_prefix(self, backend: GraphitiBackend):
        """Test that production mode (test_mode=False) does NOT add pytest__ prefix."""
        result = backend._sanitize_thread_id("thread-name")

        # Production mode should NOT have pytest__ prefix
        assert not result.startswith("pytest__")
        assert result == "thread-name"  # Hyphens preserved, no prefix


class TestLeanRAGTestMode:
    """Unit tests for LeanRAG test_mode prefix application."""

    def test_apply_test_prefix_disabled(self):
        """Test that test_mode=False does not modify work_dir."""
        config = LeanRAGConfig(test_mode=False)
        # Mock validation to avoid requiring LeanRAG submodule in CI
        with patch.object(LeanRAGBackend, '_validate_config'):
            backend = LeanRAGBackend(config)

        original = Path("/tmp/leanrag-work")
        result = backend._apply_test_prefix(original)

        assert result == original
        assert result.name == "leanrag-work"

    def test_apply_test_prefix_enabled(self):
        """Test that test_mode=True adds pytest__ prefix to work_dir basename."""
        config = LeanRAGConfig(test_mode=True)
        # Mock validation to avoid requiring LeanRAG submodule in CI
        with patch.object(LeanRAGBackend, '_validate_config'):
            backend = LeanRAGBackend(config)

        original = Path("/tmp/leanrag-work")
        result = backend._apply_test_prefix(original)

        assert result != original
        assert result.parent == original.parent  # Parent unchanged
        assert result.name == "pytest__leanrag-work"

    def test_apply_test_prefix_no_duplicate(self):
        """Test that pytest__ prefix is not duplicated."""
        config = LeanRAGConfig(test_mode=True)
        # Mock validation to avoid requiring LeanRAG submodule in CI
        with patch.object(LeanRAGBackend, '_validate_config'):
            backend = LeanRAGBackend(config)

        original = Path("/tmp/pytest__leanrag-work")
        result = backend._apply_test_prefix(original)

        # Should not add second prefix
        assert result == original
        assert result.name == "pytest__leanrag-work"
        assert result.name.count("pytest__") == 1
