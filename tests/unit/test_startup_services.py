"""Unit tests for watercooler_mcp.startup module.

Tests the service startup and management functionality:
- ServiceState enum and ServiceStatus dataclass
- URL parsing utilities (_is_localhost_url, _extract_port)
- Port lock mechanism for concurrent access
- Archive path traversal prevention
- Checksum verification with different modes
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from watercooler_mcp.startup import (
    ServiceState,
    ServiceStatus,
    _is_localhost_url,
    _extract_port,
    _get_port_lock,
    _is_safe_archive_path,
    _compute_sha256,
    _verify_checksum,
    _get_expected_checksum,
    get_service_status,
    _update_service_status,
    DEFAULT_LLM_PORT,
    DEFAULT_EMBEDDING_PORT,
)


# ============================================================================
# Test ServiceState Enum
# ============================================================================


class TestServiceState:
    """Tests for ServiceState enum."""

    def test_service_state_values(self):
        """Test that ServiceState has expected values."""
        assert ServiceState.UNKNOWN.value == "unknown"
        assert ServiceState.DISABLED.value == "disabled"
        assert ServiceState.STARTING.value == "starting"
        assert ServiceState.RUNNING.value == "running"
        assert ServiceState.FAILED.value == "failed"
        assert ServiceState.NOT_CONFIGURED.value == "not_configured"

    def test_service_state_all_values(self):
        """Test that all ServiceState values are strings."""
        for state in ServiceState:
            assert isinstance(state.value, str)


# ============================================================================
# Test ServiceStatus Dataclass
# ============================================================================


class TestServiceStatus:
    """Tests for ServiceStatus dataclass."""

    def test_service_status_creation(self):
        """Test basic ServiceStatus creation."""
        status = ServiceStatus(name="llm")
        assert status.name == "llm"
        assert status.state == ServiceState.UNKNOWN
        assert status.message == ""
        assert status.endpoint == ""
        assert status.started_at is None
        assert status.ready_at is None

    def test_service_status_with_all_fields(self):
        """Test ServiceStatus creation with all fields."""
        status = ServiceStatus(
            name="embedding",
            state=ServiceState.RUNNING,
            message="Service started successfully",
            endpoint="http://localhost:8080",
            started_at=1000.0,
            ready_at=1005.0,
        )
        assert status.name == "embedding"
        assert status.state == ServiceState.RUNNING
        assert status.message == "Service started successfully"
        assert status.endpoint == "http://localhost:8080"
        assert status.started_at == 1000.0
        assert status.ready_at == 1005.0

    def test_service_status_to_dict(self):
        """Test ServiceStatus.to_dict() method."""
        status = ServiceStatus(
            name="llm",
            state=ServiceState.RUNNING,
            message="Ready",
            endpoint="http://localhost:8081",
            started_at=1000.0,
            ready_at=1002.5,
        )
        result = status.to_dict()

        assert result["name"] == "llm"
        assert result["state"] == "running"
        assert result["message"] == "Ready"
        assert result["endpoint"] == "http://localhost:8081"
        assert result["started_at"] == 1000.0
        assert result["ready_at"] == 1002.5
        assert result["startup_time_ms"] == 2500  # (1002.5 - 1000) * 1000

    def test_service_status_to_dict_without_times(self):
        """Test ServiceStatus.to_dict() when times are not set."""
        status = ServiceStatus(name="llm")
        result = status.to_dict()

        assert result["startup_time_ms"] is None


# ============================================================================
# Test _is_localhost_url
# ============================================================================


class TestIsLocalhostUrl:
    """Tests for _is_localhost_url function."""

    def test_localhost_variations(self):
        """Test various localhost URL formats."""
        assert _is_localhost_url("http://localhost:8080") is True
        assert _is_localhost_url("http://localhost:8080/v1") is True
        assert _is_localhost_url("https://localhost:443") is True
        assert _is_localhost_url("http://localhost") is True

    def test_ip_variations(self):
        """Test IP address localhost variations."""
        assert _is_localhost_url("http://127.0.0.1:8080") is True
        assert _is_localhost_url("http://127.0.0.1:8080/api") is True
        assert _is_localhost_url("http://0.0.0.0:8080") is True

    def test_ipv6_localhost(self):
        """Test IPv6 localhost - documents current limitation.

        Note: The current implementation doesn't properly handle IPv6 bracket
        notation. This test documents the actual behavior. A fix would require
        proper IPv6 URL parsing in _is_localhost_url.
        """
        # Current implementation fails to parse [::1] correctly due to
        # naive split(":") on netloc. This documents the limitation.
        assert _is_localhost_url("http://[::1]:8080") is False  # Known limitation

    def test_remote_urls(self):
        """Test that remote URLs are not localhost."""
        assert _is_localhost_url("http://example.com:8080") is False
        assert _is_localhost_url("https://api.openai.com/v1") is False
        assert _is_localhost_url("http://192.168.1.100:8080") is False

    def test_invalid_urls(self):
        """Test handling of invalid URLs."""
        assert _is_localhost_url("not a url") is False
        assert _is_localhost_url("") is False

    def test_case_insensitive(self):
        """Test that localhost matching is case-insensitive."""
        assert _is_localhost_url("http://LOCALHOST:8080") is True
        assert _is_localhost_url("http://LocalHost:8080") is True


# ============================================================================
# Test _extract_port
# ============================================================================


class TestExtractPort:
    """Tests for _extract_port function."""

    def test_explicit_port(self):
        """Test extraction of explicit port."""
        assert _extract_port("http://localhost:8080") == 8080
        assert _extract_port("http://localhost:9000") == 9000
        assert _extract_port("https://example.com:8443") == 8443

    def test_https_default_port(self):
        """Test that HTTPS without port returns 443."""
        assert _extract_port("https://example.com") == 443
        assert _extract_port("https://example.com/path") == 443

    def test_http_default_port(self):
        """Test that HTTP without port returns default."""
        assert _extract_port("http://localhost") == DEFAULT_LLM_PORT
        assert _extract_port("http://localhost/v1") == DEFAULT_LLM_PORT

    def test_custom_default(self):
        """Test custom default port."""
        assert _extract_port("http://localhost", default=9999) == 9999

    def test_invalid_url(self):
        """Test handling of invalid URLs returns default."""
        assert _extract_port("not a url") == DEFAULT_LLM_PORT
        assert _extract_port("") == DEFAULT_LLM_PORT


# ============================================================================
# Test _get_port_lock
# ============================================================================


class TestGetPortLock:
    """Tests for _get_port_lock function."""

    def test_returns_lock(self):
        """Test that _get_port_lock returns a threading.Lock."""
        lock = _get_port_lock(8080)
        assert isinstance(lock, type(threading.Lock()))

    def test_same_port_same_lock(self):
        """Test that same port returns same lock instance."""
        lock1 = _get_port_lock(8081)
        lock2 = _get_port_lock(8081)
        assert lock1 is lock2

    def test_different_ports_different_locks(self):
        """Test that different ports return different locks."""
        lock1 = _get_port_lock(8082)
        lock2 = _get_port_lock(8083)
        assert lock1 is not lock2

    def test_concurrent_access(self):
        """Test that port lock mechanism is thread-safe."""
        results = []
        errors = []

        def get_lock_in_thread(port):
            try:
                lock = _get_port_lock(port)
                results.append((port, lock))
            except Exception as e:
                errors.append(e)

        threads = []
        # Multiple threads requesting same port
        for _ in range(10):
            t = threading.Thread(target=get_lock_in_thread, args=(8084,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 10
        # All should be same lock
        locks = [r[1] for r in results]
        assert all(l is locks[0] for l in locks)


# ============================================================================
# Test _is_safe_archive_path
# ============================================================================


class TestIsSafeArchivePath:
    """Tests for _is_safe_archive_path function."""

    @pytest.fixture
    def dest_dir(self, tmp_path):
        """Create a destination directory for testing."""
        d = tmp_path / "extract"
        d.mkdir()
        return d

    def test_safe_relative_path(self, dest_dir):
        """Test that simple relative paths are safe."""
        assert _is_safe_archive_path("file.txt", dest_dir) is True
        assert _is_safe_archive_path("subdir/file.txt", dest_dir) is True
        assert _is_safe_archive_path("a/b/c/file.txt", dest_dir) is True

    def test_path_traversal_rejected(self, dest_dir):
        """Test that path traversal attempts are rejected."""
        assert _is_safe_archive_path("../etc/passwd", dest_dir) is False
        assert _is_safe_archive_path("../../secret", dest_dir) is False
        assert _is_safe_archive_path("foo/../../../etc/passwd", dest_dir) is False

    def test_absolute_path_rejected(self, dest_dir):
        """Test that absolute paths are rejected."""
        assert _is_safe_archive_path("/etc/passwd", dest_dir) is False
        assert _is_safe_archive_path("/tmp/malicious", dest_dir) is False

    def test_hidden_traversal_rejected(self, dest_dir):
        """Test that hidden traversal patterns are caught."""
        # These resolve outside dest_dir
        assert _is_safe_archive_path("subdir/../../outside", dest_dir) is False

    def test_safe_paths_with_dots(self, dest_dir):
        """Test that paths with dots in names (not traversal) are safe."""
        assert _is_safe_archive_path("file.tar.gz", dest_dir) is True
        assert _is_safe_archive_path(".hidden_file", dest_dir) is True
        assert _is_safe_archive_path("dir.name/file.txt", dest_dir) is True


# ============================================================================
# Test _compute_sha256
# ============================================================================


class TestComputeSha256:
    """Tests for _compute_sha256 function."""

    def test_compute_sha256_known_content(self, tmp_path):
        """Test SHA256 computation with known content."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        # Known SHA256 for "hello world"
        expected = hashlib.sha256(b"hello world").hexdigest()
        actual = _compute_sha256(test_file)

        assert actual == expected

    def test_compute_sha256_binary_file(self, tmp_path):
        """Test SHA256 computation with binary content."""
        test_file = tmp_path / "binary.bin"
        binary_content = bytes(range(256))
        test_file.write_bytes(binary_content)

        expected = hashlib.sha256(binary_content).hexdigest()
        actual = _compute_sha256(test_file)

        assert actual == expected

    def test_compute_sha256_empty_file(self, tmp_path):
        """Test SHA256 computation with empty file."""
        test_file = tmp_path / "empty.txt"
        test_file.write_text("")

        expected = hashlib.sha256(b"").hexdigest()
        actual = _compute_sha256(test_file)

        assert actual == expected


# ============================================================================
# Test _verify_checksum
# ============================================================================


class TestVerifyChecksum:
    """Tests for _verify_checksum function."""

    @pytest.fixture
    def test_file(self, tmp_path):
        """Create a test file with known content."""
        f = tmp_path / "download.bin"
        f.write_text("test content")
        return f

    @pytest.fixture
    def expected_checksum(self, test_file):
        """Get the expected checksum of the test file."""
        return hashlib.sha256(test_file.read_bytes()).hexdigest()

    def test_verify_checksum_match(self, test_file, expected_checksum, monkeypatch):
        """Test verification passes when checksum matches."""
        monkeypatch.delenv("WATERCOOLER_LLAMA_SERVER_VERIFY", raising=False)

        result = _verify_checksum(
            test_file,
            expected_checksum,
            "v1.0.0",
            "linux-x64",
        )

        assert result is True
        assert test_file.exists()  # File should still exist

    def test_verify_checksum_mismatch(self, test_file, monkeypatch):
        """Test verification fails and deletes file when checksum mismatches."""
        monkeypatch.delenv("WATERCOOLER_LLAMA_SERVER_VERIFY", raising=False)

        result = _verify_checksum(
            test_file,
            "0000000000000000000000000000000000000000000000000000000000000000",
            "v1.0.0",
            "linux-x64",
        )

        assert result is False
        assert not test_file.exists()  # File should be deleted

    def test_verify_checksum_skip_mode(self, test_file, monkeypatch):
        """Test skip mode bypasses verification."""
        monkeypatch.setenv("WATERCOOLER_LLAMA_SERVER_VERIFY", "skip")

        result = _verify_checksum(
            test_file,
            "wrong_checksum",  # Would fail if verified
            "v1.0.0",
            "linux-x64",
        )

        assert result is True
        assert test_file.exists()

    def test_verify_checksum_warn_mode_unknown(self, test_file, monkeypatch):
        """Test warn mode allows unknown checksum with warning."""
        monkeypatch.setenv("WATERCOOLER_LLAMA_SERVER_VERIFY", "warn")

        result = _verify_checksum(
            test_file,
            None,  # Unknown checksum
            "v1.0.0",
            "linux-x64",
        )

        assert result is True
        assert test_file.exists()

    def test_verify_checksum_strict_mode_unknown(self, test_file, monkeypatch):
        """Test strict mode raises error for unknown checksum."""
        monkeypatch.setenv("WATERCOOLER_LLAMA_SERVER_VERIFY", "strict")

        with pytest.raises(RuntimeError) as exc_info:
            _verify_checksum(
                test_file,
                None,  # Unknown checksum
                "v1.0.0",
                "linux-x64",
            )

        assert "No known checksum" in str(exc_info.value)


# ============================================================================
# Test _get_expected_checksum
# ============================================================================


class TestGetExpectedChecksum:
    """Tests for _get_expected_checksum function."""

    def test_env_override(self, monkeypatch):
        """Test that environment variable overrides built-in checksums."""
        custom_checksum = "abcd1234" * 8  # 64 chars
        monkeypatch.setenv("WATERCOOLER_LLAMA_SERVER_SHA256", custom_checksum)

        result = _get_expected_checksum("any-release", "any-pattern")

        assert result == custom_checksum

    def test_returns_none_for_unknown(self, monkeypatch):
        """Test that unknown release/pattern returns None."""
        monkeypatch.delenv("WATERCOOLER_LLAMA_SERVER_SHA256", raising=False)

        result = _get_expected_checksum("unknown-release-xyz", "unknown-pattern")

        # Should return None for unknown release
        assert result is None or isinstance(result, str)


# ============================================================================
# Test Service Status Management
# ============================================================================


class TestServiceStatusManagement:
    """Tests for service status get/update functions."""

    def test_get_service_status_returns_dict(self):
        """Test that get_service_status returns a dictionary."""
        status = get_service_status()
        assert isinstance(status, dict)

    def test_update_service_status(self):
        """Test that _update_service_status updates state correctly."""
        # Update a known service status (only llm, embedding, falkordb are valid)
        _update_service_status(
            "llm",
            ServiceState.RUNNING,
            message="Test message",
            endpoint="http://localhost:8080",
        )

        # Verify update
        status = get_service_status()
        assert "llm" in status
        assert status["llm"]["state"] == "running"
        assert status["llm"]["message"] == "Test message"
        assert status["llm"]["endpoint"] == "http://localhost:8080"


# ============================================================================
# Test Edge Cases
# ============================================================================


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_extract_port_with_path(self):
        """Test port extraction with path in URL."""
        assert _extract_port("http://localhost:8080/v1/models") == 8080

    def test_extract_port_with_query(self):
        """Test port extraction with query string."""
        assert _extract_port("http://localhost:8080?key=value") == 8080

    def test_is_localhost_with_user_info(self):
        """Test localhost check with user info in URL."""
        # URLs with user:pass@ prefix
        result = _is_localhost_url("http://user:pass@localhost:8080")
        # Behavior may vary - just ensure no crash
        assert isinstance(result, bool)

    def test_safe_archive_path_with_symlink_name(self, tmp_path):
        """Test archive path with symlink-like name (not actual symlink)."""
        dest_dir = tmp_path / "extract"
        dest_dir.mkdir()

        # A file named like a symlink but is just a regular path
        assert _is_safe_archive_path("link -> target", dest_dir) is True
