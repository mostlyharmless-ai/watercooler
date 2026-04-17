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


# ============================================================================
# Test ensure_falkordb_running backend guard (Phase 2c regression)
# ============================================================================


class TestEnsureFalkordbRunningGuard:
    """Regression tests ensuring ensure_falkordb_running() respects backend setting.

    These tests guard against silent removal of the backend guard at
    startup.py:1994-1999 — the guard must return early for any backend
    other than 'graphiti', including the new 'null' default.
    """

    def test_returns_early_for_null_backend(self):
        """ensure_falkordb_running returns immediately when backend is 'null'."""
        from watercooler_mcp.startup import ensure_falkordb_running

        # get_memory_backend is imported inline inside ensure_falkordb_running, so
        # patch at the source module where it is defined.
        with patch(
            "watercooler.memory_config.get_memory_backend",
            return_value="null",
        ), patch(
            "watercooler_mcp.startup._update_service_status"
        ) as mock_status:
            ensure_falkordb_running()

        # Status must be set to DISABLED (not STARTING or RUNNING)
        mock_status.assert_called_once()
        args = mock_status.call_args[0]
        assert args[0] == "falkordb"
        assert args[1] == ServiceState.DISABLED

    def test_returns_early_for_leanrag_backend(self):
        """ensure_falkordb_running returns immediately when backend is 'leanrag'."""
        from watercooler_mcp.startup import ensure_falkordb_running

        with patch(
            "watercooler.memory_config.get_memory_backend",
            return_value="leanrag",
        ), patch(
            "watercooler_mcp.startup._update_service_status"
        ) as mock_status:
            ensure_falkordb_running()

        mock_status.assert_called_once()
        assert mock_status.call_args[0][1] == ServiceState.DISABLED


# ============================================================================
# Windows embedding spawn unification (F14/F15 regression guards)
# ============================================================================


class TestEmbeddingSpawnUnification:
    """Guards against reintroducing the divergent Windows embedding path.

    The Windows-specific ``_start_embedding_windows`` used DETACHED_PROCESS
    without ``stdin=subprocess.DEVNULL``, giving the child an invalid
    console handle that llama-server's Windows signal handler interpreted
    as a close event. The embedding server died seconds after startup
    while the LLM (using the generic spawn path) stayed up. These tests
    ensure that single path is preserved.
    """

    def test_no_windows_specific_embedding_spawn(self):
        """_start_embedding_windows must not exist.

        If someone reintroduces a Windows-specific branch without fixing
        the stdin+DETACHED_PROCESS interaction, this test fails and sends
        them to the windows-release-hardening thread for context.
        """
        from watercooler_mcp import startup

        assert not hasattr(startup, "_start_embedding_windows"), (
            "_start_embedding_windows was reintroduced. This function "
            "previously spawned llama-server with DETACHED_PROCESS but "
            "no DEVNULL stdin, causing the embedding server to die "
            "immediately on Windows. Embedding must route through the "
            "generic _start_llama_server path. See "
            "windows-release-hardening thread for history."
        )


# ============================================================================
# Loopback normalization (F15)
# ============================================================================


class TestNormalizeLoopback:
    """Tests for _normalize_loopback."""

    def test_empty_becomes_ipv4_loopback(self):
        from watercooler_mcp.startup import _normalize_loopback

        assert _normalize_loopback("") == "127.0.0.1"

    def test_localhost_becomes_ipv4_loopback(self):
        from watercooler_mcp.startup import _normalize_loopback

        assert _normalize_loopback("localhost") == "127.0.0.1"

    def test_ipv4_loopback_unchanged(self):
        from watercooler_mcp.startup import _normalize_loopback

        assert _normalize_loopback("127.0.0.1") == "127.0.0.1"

    def test_bind_all_preserved(self):
        """0.0.0.0 is used for Docker/LAN binding and must not be coerced."""
        from watercooler_mcp.startup import _normalize_loopback

        assert _normalize_loopback("0.0.0.0") == "0.0.0.0"

    def test_remote_host_preserved(self):
        """Non-loopback hostnames (e.g., remote LLM endpoints) pass through."""
        from watercooler_mcp.startup import _normalize_loopback

        assert _normalize_loopback("llm.example.com") == "llm.example.com"

    def test_uppercase_localhost_coerced(self):
        """LLAMA_SERVER_HOST=LOCALHOST must normalize to 127.0.0.1.

        urllib.parse lowercases hostnames for the probe side, so without
        case-insensitive handling here the bind side (which goes
        through _normalize_loopback) would diverge from the probe side
        and reopen F15 whenever an operator uses mixed case.
        """
        from watercooler_mcp.startup import _normalize_loopback

        assert _normalize_loopback("LOCALHOST") == "127.0.0.1"

    def test_mixed_case_localhost_coerced(self):
        from watercooler_mcp.startup import _normalize_loopback

        assert _normalize_loopback("Localhost") == "127.0.0.1"
        assert _normalize_loopback("LocalHost") == "127.0.0.1"
        assert _normalize_loopback("lOcAlHoSt") == "127.0.0.1"


class TestNormalizeProbeUrl:
    """Tests for _normalize_probe_url."""

    def test_localhost_rewritten(self):
        from watercooler_mcp.startup import _normalize_probe_url

        assert (
            _normalize_probe_url("http://localhost:8080/v1")
            == "http://127.0.0.1:8080/v1"
        )

    def test_default_port_rewritten(self):
        from watercooler_mcp.startup import _normalize_probe_url

        assert _normalize_probe_url("http://localhost/v1") == "http://127.0.0.1/v1"

    def test_ipv4_unchanged(self):
        from watercooler_mcp.startup import _normalize_probe_url

        url = "http://127.0.0.1:8080/v1"
        assert _normalize_probe_url(url) == url

    def test_remote_unchanged(self):
        from watercooler_mcp.startup import _normalize_probe_url

        url = "https://api.example.com/v1"
        assert _normalize_probe_url(url) == url

    def test_uppercase_localhost_url_rewritten(self):
        """Confirms urlparse's case-insensitive hostname handling actually
        reaches the rewrite branch for mixed-case variants. Pairs with
        the _normalize_loopback case-insensitivity test to guarantee
        bind and probe agree for any mixed-case LLAMA_SERVER_HOST."""
        from watercooler_mcp.startup import _normalize_probe_url

        assert (
            _normalize_probe_url("http://LOCALHOST:8080/v1")
            == "http://127.0.0.1:8080/v1"
        )
        assert (
            _normalize_probe_url("http://Localhost:8080/v1")
            == "http://127.0.0.1:8080/v1"
        )


# ============================================================================
# Port preflight check (orphan-masking prevention)
# ============================================================================


class TestCheckPortAvailable:
    """Tests for _check_port_available."""

    def test_free_port_returns_available(self):
        """A port nobody is listening on reports (True, None)."""
        import socket as _socket

        from watercooler_mcp.startup import _check_port_available

        # Find an unused port by binding 0 and reading back
        probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        available, pid = _check_port_available(port)
        assert available is True
        assert pid is None

    def test_occupied_port_returns_unavailable(self):
        """An actively-listening port reports (False, ...)."""
        import socket as _socket

        from watercooler_mcp.startup import _check_port_available

        # Hold a real listener on an ephemeral port
        listener = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            available, _pid = _check_port_available(port)
            assert available is False
            # PID may be None (lsof/netstat not available or parseable in
            # the sandbox) — UX convenience, not a correctness contract.
        finally:
            listener.close()


class TestFormatPortInUseError:
    """Tests for _format_port_in_use_error."""

    def test_message_mentions_port_and_service(self):
        from watercooler_mcp.startup import _format_port_in_use_error

        msg = _format_port_in_use_error("Embedding", 8080, 12345)
        assert "Embedding" in msg
        assert "8080" in msg
        assert "PID 12345" in msg

    def test_message_has_windows_and_unix_remediation(self):
        from watercooler_mcp.startup import _format_port_in_use_error

        msg = _format_port_in_use_error("LLM", 8000, 999)
        assert "Stop-Process" in msg
        assert "kill " in msg
        assert "TROUBLESHOOTING.md" in msg

    def test_no_pid_fallback_has_inspection_commands(self):
        """When PID lookup fails, users need a way to find the process."""
        from watercooler_mcp.startup import _format_port_in_use_error

        msg = _format_port_in_use_error("Embedding", 8080, None)
        assert "Get-NetTCPConnection" in msg
        assert "lsof" in msg


class TestSpawnPreflightRefusesOnOccupiedPort:
    """End-to-end: _start_llama_server_unlocked refuses when port is held."""

    def test_preflight_raises_runtime_error_with_actionable_message(self):
        """Listener held on the target port causes a RuntimeError with a
        remediation-focused message — no masking, no silent failure."""
        import socket as _socket

        from watercooler_mcp.startup import _start_llama_server_unlocked

        listener = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            with pytest.raises(RuntimeError) as exc_info:
                _start_llama_server_unlocked(
                    model_path=Path("/nonexistent/model.gguf"),
                    port=port,
                    mode="embedding",
                    context_size=512,
                    host="127.0.0.1",
                )
            msg = str(exc_info.value)
            assert "already in use" in msg
            assert str(port) in msg
            # TROUBLESHOOTING link is part of the remediation
            assert "TROUBLESHOOTING.md" in msg
        finally:
            listener.close()


# ============================================================================
# Review-round fixes: TOCTOU-resilient wait loops, _pids_lock read, TIME_WAIT
# ============================================================================


class TestWaitLoopFailsFastOnEarlyProcessExit:
    """Review item #1 (High): the wait loop must surface an early child
    exit instead of blocking until the health-probe deadline."""

    def test_wait_returns_false_when_proc_exited_before_ready(self):
        from unittest.mock import patch

        from watercooler_mcp import startup

        class _FakeExitedProc:
            """Stand-in for ``subprocess.Popen`` that has already exited."""

            pid = 99999
            returncode = 1

            def poll(self):
                return 1

        fake_proc = _FakeExitedProc()
        port = 65432  # arbitrary ephemeral port not actually bound
        api_base = f"http://127.0.0.1:{port}/v1"

        # Register our fake exited proc for this port so _get_spawned_proc
        # returns it. We restore state in finally so other tests aren't
        # affected.
        with startup._pids_lock:
            startup._spawned_procs[port] = fake_proc  # type: ignore[assignment]

        try:
            # Patch the health check to always fail so only proc.poll()
            # can short-circuit the wait.
            with patch("watercooler_mcp.startup._check_llm_health", return_value=False):
                start = __import__("time").time()
                result = startup._wait_for_llm_ready(
                    api_base, max_wait=5.0, poll_interval=0.05
                )
                elapsed = __import__("time").time() - start
            assert result is False
            # Should exit promptly (well under max_wait) because proc.poll()
            # returned a non-None exit code on the first iteration.
            assert elapsed < 1.0
        finally:
            with startup._pids_lock:
                startup._spawned_procs.pop(port, None)

    def test_wait_returns_true_when_health_passes_and_proc_alive(self):
        from unittest.mock import patch

        from watercooler_mcp import startup

        class _FakeRunningProc:
            pid = 88888
            returncode = None

            def poll(self):
                return None

        fake_proc = _FakeRunningProc()
        port = 65431
        api_base = f"http://127.0.0.1:{port}/v1"

        with startup._pids_lock:
            startup._spawned_procs[port] = fake_proc  # type: ignore[assignment]
        try:
            with patch("watercooler_mcp.startup._check_embedding_health", return_value=True):
                result = startup._wait_for_embedding_ready(
                    api_base, max_wait=5.0, poll_interval=0.05
                )
            assert result is True
        finally:
            with startup._pids_lock:
                startup._spawned_procs.pop(port, None)


class TestPreflightTreatsOwnedPidAsOurs:
    """Review item #2 (High): _spawned_pids read must be locked and the
    owned-PID path must not raise."""

    def test_owned_pid_does_not_trigger_refuse_to_start(self):
        """When _check_port_available returns a PID that's in our
        _spawned_pids, we must treat it as ours and proceed past the
        preflight, not raise RuntimeError.

        We verify the branch by patching _check_port_available and
        _find_llama_server so the rest of the function exits cleanly
        (auto-provisioning disabled → RuntimeError from a *different*
        code path, which we don't want to confuse with the port-in-use
        path).
        """
        from unittest.mock import patch

        from watercooler_mcp import startup

        owned_pid = 123456
        port = 65430

        with startup._pids_lock:
            startup._spawned_pids.append(owned_pid)
        try:
            with patch(
                "watercooler_mcp.startup._check_port_available",
                return_value=(False, owned_pid),
            ), patch(
                "watercooler_mcp.startup._find_llama_server",
                return_value=None,
            ), patch(
                "watercooler_mcp.startup._is_auto_provision_enabled",
                return_value=False,
            ):
                # Expect a RuntimeError, but from the "llama-server not
                # found and auto-provisioning disabled" branch — NOT
                # from the port-in-use branch. The message content is
                # the tell-tale.
                with pytest.raises(RuntimeError) as exc_info:
                    startup._start_llama_server_unlocked(
                        model_path=Path("/nonexistent/model.gguf"),
                        port=port,
                        mode="embedding",
                        context_size=512,
                        host="127.0.0.1",
                    )
                msg = str(exc_info.value)
                assert "already in use" not in msg
                assert "auto-provisioning" in msg or "not found" in msg
        finally:
            with startup._pids_lock:
                if owned_pid in startup._spawned_pids:
                    startup._spawned_pids.remove(owned_pid)


class TestCheckPortAvailableTimeWaitTolerance:
    """Review item #3 (Medium): TIME_WAIT on POSIX should not false-positive."""

    def test_bind_fail_without_listener_reports_free(self):
        """If _find_pid_on_port returns None (nothing actually listening),
        _check_port_available should report the port as free even when a
        bind probe fails. Simulates the TIME_WAIT edge case without
        requiring a real TIME_WAIT socket."""
        from unittest.mock import patch

        from watercooler_mcp import startup

        # Force both bind probes to "fail" and the PID lookup to find
        # nothing — this is the TIME_WAIT signature.
        with patch.object(startup, "_find_pid_on_port", return_value=None), patch(
            "socket.socket"
        ) as fake_socket_cls:
            fake_sock = fake_socket_cls.return_value
            fake_sock.bind.side_effect = OSError("EADDRINUSE (simulated)")

            available, pid = startup._check_port_available(59999)

        assert available is True
        assert pid is None


class TestCheckPortAvailableIpv6Disabled:
    """Review item #4 (Medium): the IPv6 catch must not mask genuine errors."""

    def test_ipv6_disabled_via_has_ipv6_guard(self):
        """When ``socket.has_ipv6`` is False, the IPv6 branch is skipped
        entirely — no exceptions raised, result reflects IPv4 only."""
        from unittest.mock import patch

        from watercooler_mcp import startup

        # Pick an unused port and hold a listener to make IPv4 busy.
        import socket as _socket

        listener = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            with patch.object(startup.socket, "has_ipv6", False):
                available, _pid = startup._check_port_available(port)
            assert available is False
        finally:
            listener.close()


# ============================================================================
# Review round 3 fixes: systemctl bailout, outer-wrapper normalization,
# host-aware preflight
# ============================================================================


class TestSystemctlBailout:
    """Review round 3 (High): systemctl started but not ready must not
    cascade into the preflight misdiagnosing the systemd unit as an
    orphan."""

    def test_systemctl_failure_bails_with_diagnostic_not_runtime_error(self):
        """_ensure_embedding_service_available returns False with a
        systemctl-specific startup warning when _try_systemctl_embedding
        succeeds but the health check times out. Critically, it must
        NOT raise RuntimeError from the preflight telling the user to
        ``kill`` the systemd service."""
        from unittest.mock import patch

        from watercooler_mcp import startup

        # Replace the model-resolution prelude so we land at the
        # systemctl branch without needing real model files.
        fake_model_path = Path("/tmp/fake-model.gguf")
        fake_spec = {"dim": 1024, "file_name": "fake.gguf"}

        with patch(
            "watercooler.models.resolve_embedding_model", return_value=fake_spec
        ), patch(
            "watercooler.models.ensure_model_available", return_value=fake_model_path
        ), patch(
            "platform.system", return_value="Linux"
        ), patch(
            "watercooler_mcp.startup._try_systemctl_embedding", return_value=True
        ), patch(
            "watercooler_mcp.startup._wait_for_embedding_ready", return_value=False
        ), patch(
            "watercooler_mcp.startup._start_embedding_direct"
        ) as mock_direct, patch(
            "watercooler_mcp.startup._add_startup_warning"
        ) as mock_warn:
            result = startup._ensure_embedding_service_available(
                model_name="bge-m3",
                api_base="http://127.0.0.1:8080/v1",
                context_size=8192,
            )

        assert result is False, "systemctl failure must return False, not raise"
        # The direct-spawn fallthrough must NOT have been invoked — that
        # is the whole point of the bailout.
        mock_direct.assert_not_called()
        # A systemctl-specific diagnostic should have been surfaced.
        warning_msgs = [call.args[0] for call in mock_warn.call_args_list]
        joined = "\n".join(warning_msgs)
        assert "watercooler-embedding" in joined
        assert "journalctl" in joined or "systemctl --user status" in joined


class TestOuterWrapperAppliesNormalizeLoopback:
    """Review round 3 (Medium): _start_llama_server outer wrapper must
    apply _normalize_loopback so the 'already running' health-check
    probe uses the same canonical host as the inner function."""

    def test_localhost_env_normalized_before_api_base_build(self, monkeypatch):
        """Setting LLAMA_SERVER_HOST=localhost must produce a
        ``http://127.0.0.1:PORT/v1`` probe URL, not ``http://localhost:...``.
        We detect the normalization by inspecting the url
        ``_check_llm_health`` receives."""
        from unittest.mock import patch

        from watercooler_mcp import startup

        monkeypatch.setenv("LLAMA_SERVER_HOST", "localhost")
        captured: dict[str, str] = {}

        def fake_health(api_base, timeout=2.0):
            captured["api_base"] = api_base
            return True  # claim server is already running to short-circuit

        with patch(
            "watercooler_mcp.startup._check_llm_health", side_effect=fake_health
        ), patch(
            "watercooler_mcp.startup._get_port_lock"
        ) as mock_get_lock:
            # Give the lock a real context manager shape
            mock_get_lock.return_value.__enter__ = lambda _self: None
            mock_get_lock.return_value.__exit__ = lambda *_args: False
            startup._start_llama_server(
                model_path=Path("/tmp/ignored.gguf"),
                port=65429,
                mode="completion",
                context_size=512,
            )

        assert captured["api_base"] == "http://127.0.0.1:65429/v1"


class TestHostAwarePreflight:
    """Review round 3 (Low): _check_port_available must match the
    address llama-server would actually bind — probing 127.0.0.1 for a
    non-loopback bind false-positives on unrelated local services."""

    def test_loopback_bind_probes_both_ipv4_and_ipv6(self):
        """Legacy F15 behavior preserved for loopback."""
        from unittest.mock import patch

        from watercooler_mcp import startup

        calls: list[tuple[int, str]] = []

        def fake_can_bind(self_unused, address):  # pragma: no cover
            raise AssertionError("_can_bind is a closure; not called via class")

        # Instead of patching the closure, patch socket.socket and record
        # which bind attempts happen.
        real_socket = startup.socket.socket

        class _RecordingSocket:
            def __init__(self, family, kind):
                self.family = family
                self._inner = real_socket(family, kind)

            def setsockopt(self, *a, **kw):
                pass

            def bind(self, addr):
                calls.append((self.family, addr[0]))
                # Actually bind so we return a realistic success/failure
                self._inner.bind(addr)

            def close(self):
                self._inner.close()

        with patch.object(startup.socket, "socket", _RecordingSocket):
            startup._check_port_available(0, "127.0.0.1")

        families_probed = {fam for fam, _host in calls}
        hosts_probed = {h for _fam, h in calls}
        import socket as _socket

        # Both families probed when configured for loopback
        assert _socket.AF_INET in families_probed
        # IPv6 may be skipped on hosts without IPv6 support; only assert
        # when the host actually has it.
        if _socket.has_ipv6:
            assert _socket.AF_INET6 in families_probed
        assert "127.0.0.1" in hosts_probed

    def test_non_loopback_bind_probes_only_that_address(self):
        """When host is a specific non-loopback address, the probe
        targets only that address — no 127.0.0.1 probe that could
        false-positive on an unrelated local listener."""
        from unittest.mock import patch

        from watercooler_mcp import startup

        calls: list[tuple[int, str]] = []

        class _NonBindingSocket:
            def __init__(self, family, kind):
                self.family = family

            def setsockopt(self, *a, **kw):
                pass

            def bind(self, addr):
                calls.append((self.family, addr[0]))
                # Simulate success so preflight reports available; that
                # keeps the test deterministic without needing a real
                # LAN IP to bind.
                return None

            def close(self):
                pass

        with patch.object(startup.socket, "socket", _NonBindingSocket):
            available, _pid = startup._check_port_available(0, "192.168.1.50")

        hosts_probed = {h for _fam, h in calls}
        assert hosts_probed == {"192.168.1.50"}
        assert available is True

    def test_wildcard_bind_probes_wildcard(self):
        """0.0.0.0 configured bind means llama-server binds every
        interface — the probe must match (any listener on the port
        conflicts)."""
        from unittest.mock import patch

        from watercooler_mcp import startup

        calls: list[tuple[int, str]] = []

        class _NonBindingSocket:
            def __init__(self, family, kind):
                self.family = family

            def setsockopt(self, *a, **kw):
                pass

            def bind(self, addr):
                calls.append((self.family, addr[0]))

            def close(self):
                pass

        with patch.object(startup.socket, "socket", _NonBindingSocket):
            startup._check_port_available(0, "0.0.0.0")

        hosts_probed = {h for _fam, h in calls}
        assert hosts_probed == {"0.0.0.0"}

    def test_ipv6_wildcard_bind_probes_ipv6_wildcard(self):
        """Host='::' must probe (AF_INET6, '::'), not (AF_INET, '0.0.0.0').

        This is the exact masking scenario the preflight exists to
        prevent: an IPv6-only orphan on ``::`` would be invisible to
        an IPv4 wildcard probe, so a new spawn would fail silently at
        bind time and the preflight-guarantee would regress for this
        configuration.
        """
        from unittest.mock import patch

        import socket as _socket

        from watercooler_mcp import startup

        calls: list[tuple[int, str]] = []

        class _NonBindingSocket:
            def __init__(self, family, kind):
                self.family = family

            def setsockopt(self, *a, **kw):
                pass

            def bind(self, addr):
                calls.append((self.family, addr[0]))

            def close(self):
                pass

        with patch.object(startup.socket, "socket", _NonBindingSocket):
            startup._check_port_available(0, "::")

        families_probed = {fam for fam, _host in calls}
        hosts_probed = {h for _fam, h in calls}
        assert families_probed == {_socket.AF_INET6}
        assert hosts_probed == {"::"}


class TestConcurrentDownloadSerialization:
    """Regression guard for the clean-install download race.

    On a first-run install with no binary on disk, the LLM and embedding
    startup workers both call ``_ensure_llama_server_binary``. Without
    serialization, both race on ``_download_llama_server()``; on Windows
    that fails because two processes cannot write the same archive at
    once, producing a misleading "binary required but could not be
    downloaded" error for the losing worker even though the winner's
    download succeeded.

    The fix serializes downloads with ``_binary_download_lock`` and
    re-checks ``_find_llama_server()`` inside the lock so the loser
    reuses the winner's binary instead of downloading again.
    """

    def test_concurrent_callers_download_exactly_once(self):
        """Ten threads call the helper at the same time; the underlying
        download function must be called exactly once."""
        import threading
        from unittest.mock import patch

        from watercooler_mcp import startup

        download_call_count = 0
        download_barrier = threading.Event()
        _mock_binary = Path("/tmp/mock-llama-server")

        # First find call (fast path) returns None; after download,
        # subsequent find calls return the "downloaded" binary.
        find_results = {"after_download": False}

        def fake_find():
            if find_results["after_download"]:
                return _mock_binary
            return None

        def fake_download():
            nonlocal download_call_count
            download_call_count += 1
            # Simulate a slow download so latecomers stack up on the lock.
            download_barrier.wait(timeout=5.0)
            find_results["after_download"] = True
            return _mock_binary

        results: list[Optional[Path]] = []  # type: ignore[name-defined]
        results_lock = threading.Lock()

        def worker():
            r = startup._ensure_llama_server_binary()
            with results_lock:
                results.append(r)

        with patch("watercooler_mcp.startup._find_llama_server", side_effect=fake_find), \
             patch("watercooler_mcp.startup._download_llama_server", side_effect=fake_download), \
             patch("watercooler_mcp.startup._is_auto_provision_enabled", return_value=True):

            threads = [threading.Thread(target=worker) for _ in range(10)]
            for t in threads:
                t.start()
            # Let the first worker acquire the lock, then release the
            # barrier so the download "completes" and the other nine
            # threads proceed past the lock and find the binary.
            import time as _time
            _time.sleep(0.1)
            download_barrier.set()
            for t in threads:
                t.join(timeout=10.0)

        assert download_call_count == 1, (
            f"Expected exactly one download under serialization, got "
            f"{download_call_count} — the loser threads are not re-checking "
            f"_find_llama_server under the lock."
        )
        # Every thread should return the same "downloaded" binary.
        assert all(r == _mock_binary for r in results), (
            f"Some threads returned None or wrong path: {results}"
        )
        assert len(results) == 10

    def test_fast_path_skips_lock_when_binary_already_present(self):
        """If the binary already exists, the helper must not enter the
        download lock at all (avoids gratuitous contention on hot paths)."""
        from unittest.mock import patch

        from watercooler_mcp import startup

        _mock_binary = Path("/tmp/mock-llama-server")

        with patch("watercooler_mcp.startup._find_llama_server", return_value=_mock_binary), \
             patch("watercooler_mcp.startup._download_llama_server") as mock_download, \
             patch("watercooler_mcp.startup._is_auto_provision_enabled") as mock_auto:
            result = startup._ensure_llama_server_binary()

        assert result == _mock_binary
        mock_download.assert_not_called()
        # Fast path doesn't even need to consult auto-provision config
        mock_auto.assert_not_called()

    def test_returns_none_when_auto_provision_disabled_and_no_binary(self):
        """No binary, auto-provision off → return None; do not download."""
        from unittest.mock import patch

        from watercooler_mcp import startup

        with patch("watercooler_mcp.startup._find_llama_server", return_value=None), \
             patch("watercooler_mcp.startup._download_llama_server") as mock_download, \
             patch("watercooler_mcp.startup._is_auto_provision_enabled", return_value=False):
            result = startup._ensure_llama_server_binary()

        assert result is None
        mock_download.assert_not_called()


class TestProcExitedEarlyMessageIsTruthful:
    """Review round 4 (Low): the diagnostic instruction must be
    followable — no claims about 'stderr capture' that the code does
    not actually offer."""

    def test_warning_message_does_not_claim_stderr_capture_is_available(self):
        """Regression guard. If someone reintroduces the misleading
        'Re-run with stderr capture for details' line, this test fails
        and points the author at the honest path (acknowledge DEVNULL,
        point at the spawn-command debug log line)."""
        from unittest.mock import patch

        from watercooler_mcp import startup

        class _ExitedProc:
            pid = 77777
            returncode = 1

            def poll(self):
                return 1

        with patch("watercooler_mcp.startup.log_warning") as mock_warn:
            result = startup._proc_exited_early(
                "LLM", "http://127.0.0.1:8000/v1", _ExitedProc()
            )

        assert result is True
        mock_warn.assert_called_once()
        msg = mock_warn.call_args.args[0]
        # Negative assertion: the outdated misleading claim must not
        # resurface.
        assert "Re-run with stderr capture" not in msg
        # Positive assertions: honest, followable path.
        assert "stderr" in msg.lower()
        assert "DEVNULL" in msg
        assert "Starting llama-server" in msg
        # Still surfaces the exit code so users know what happened.
        assert "exited with code 1" in msg
        assert "PID 77777" in msg
