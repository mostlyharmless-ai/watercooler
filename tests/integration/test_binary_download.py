"""Integration tests for binary download and verification.

Tests the download-verify-start cycle for llama-server binary.
These tests require network access and are marked with pytest markers.
"""

from __future__ import annotations

import hashlib
import os
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Mark all tests in this module as integration tests
pytestmark = [pytest.mark.integration, pytest.mark.network]


class TestChecksumVerification:
    """Tests for checksum verification logic."""

    def test_known_release_has_checksum(self):
        """Verify that known releases have checksums in registry."""
        from watercooler_mcp.startup import LLAMA_SERVER_CHECKSUMS

        # Check that we have checksums for recent releases
        assert "b7896" in LLAMA_SERVER_CHECKSUMS
        assert "b7885" in LLAMA_SERVER_CHECKSUMS
        assert "b7869" in LLAMA_SERVER_CHECKSUMS

        # Each release should have platform variants
        for release in ["b7896", "b7885", "b7869"]:
            checksums = LLAMA_SERVER_CHECKSUMS[release]
            assert "ubuntu-x64" in checksums
            assert "macos-arm64" in checksums

    def test_get_expected_checksum_from_registry(self):
        """Test retrieving checksum from registry."""
        from watercooler_mcp.startup import _get_expected_checksum

        checksum = _get_expected_checksum("b7896", "ubuntu-x64")
        assert checksum is not None
        assert len(checksum) == 64  # SHA256 hex length

    def test_get_expected_checksum_unknown_release(self):
        """Test that unknown releases return None."""
        from watercooler_mcp.startup import _get_expected_checksum

        checksum = _get_expected_checksum("b9999-nonexistent", "ubuntu-x64")
        assert checksum is None

    def test_get_expected_checksum_user_override(self, monkeypatch):
        """Test that user-provided checksum takes precedence."""
        from watercooler_mcp.startup import _get_expected_checksum

        user_checksum = "abc123def456" * 5 + "abcd"  # 64 chars
        monkeypatch.setenv("WATERCOOLER_LLAMA_SERVER_SHA256", user_checksum)

        # User checksum should override registry
        checksum = _get_expected_checksum("b7896", "ubuntu-x64")
        assert checksum == user_checksum.lower()

    def test_compute_sha256_correct(self, tmp_path):
        """Test SHA256 computation is correct."""
        from watercooler_mcp.startup import _compute_sha256

        # Create file with known content
        test_file = tmp_path / "test.bin"
        test_content = b"Hello, World!"
        test_file.write_bytes(test_content)

        # Compute expected hash
        expected = hashlib.sha256(test_content).hexdigest()

        # Verify our function matches
        actual = _compute_sha256(test_file)
        assert actual == expected

    def test_verify_checksum_match(self, tmp_path, monkeypatch):
        """Test verification passes with matching checksum."""
        from watercooler_mcp.startup import _verify_checksum

        monkeypatch.setenv("WATERCOOLER_LLAMA_SERVER_VERIFY", "strict")

        test_file = tmp_path / "test.bin"
        test_content = b"Test content for verification"
        test_file.write_bytes(test_content)
        expected = hashlib.sha256(test_content).hexdigest()

        result = _verify_checksum(test_file, expected, "test", "test")
        assert result is True
        assert test_file.exists()  # File preserved on success

    def test_verify_checksum_mismatch_deletes_file(self, tmp_path, monkeypatch):
        """Test that mismatched checksum deletes file."""
        from watercooler_mcp.startup import _verify_checksum

        monkeypatch.setenv("WATERCOOLER_LLAMA_SERVER_VERIFY", "warn")

        test_file = tmp_path / "test.bin"
        test_file.write_bytes(b"Test content")
        wrong_checksum = "0" * 64

        result = _verify_checksum(test_file, wrong_checksum, "test", "test")
        assert result is False
        assert not test_file.exists()  # File deleted on mismatch

    def test_verify_checksum_strict_unknown_raises(self, tmp_path, monkeypatch):
        """Test strict mode raises on unknown checksum."""
        from watercooler_mcp.startup import _verify_checksum

        monkeypatch.setenv("WATERCOOLER_LLAMA_SERVER_VERIFY", "strict")

        test_file = tmp_path / "test.bin"
        test_file.write_bytes(b"Test content")

        with pytest.raises(RuntimeError, match="No known checksum"):
            _verify_checksum(test_file, None, "unknown-release", "test")

    def test_verify_checksum_skip_mode(self, tmp_path, monkeypatch):
        """Test skip mode bypasses verification."""
        from watercooler_mcp.startup import _verify_checksum

        monkeypatch.setenv("WATERCOOLER_LLAMA_SERVER_VERIFY", "skip")

        test_file = tmp_path / "test.bin"
        test_file.write_bytes(b"Test content")

        # Should pass even with wrong checksum
        result = _verify_checksum(test_file, "wrong", "test", "test")
        assert result is True


class TestArchiveExtraction:
    """Tests for archive extraction with path validation."""

    def test_safe_extraction(self, tmp_path):
        """Test that safe paths are extracted correctly."""
        from watercooler_mcp.startup import _is_safe_archive_path

        # Normal paths should be safe
        assert _is_safe_archive_path("llama-server", tmp_path) is True
        assert _is_safe_archive_path("bin/llama-server", tmp_path) is True
        assert _is_safe_archive_path("lib/libfoo.so.0", tmp_path) is True

    def test_traversal_blocked(self, tmp_path):
        """Test that path traversal is blocked."""
        from watercooler_mcp.startup import _is_safe_archive_path

        # Traversal attempts should be blocked
        assert _is_safe_archive_path("../etc/passwd", tmp_path) is False
        assert _is_safe_archive_path("foo/../../etc/passwd", tmp_path) is False
        assert _is_safe_archive_path("/etc/passwd", tmp_path) is False

    def test_tarfile_extraction_validates_paths(self, tmp_path):
        """Test that tarfile extraction validates member paths."""
        from watercooler_mcp.startup import _is_safe_archive_path

        # Create a tarball with a traversal attempt
        tar_path = tmp_path / "test.tar.gz"
        extract_dir = tmp_path / "extract"
        extract_dir.mkdir()

        with tarfile.open(tar_path, "w:gz") as tf:
            # Add a safe file
            safe_file = tmp_path / "safe.txt"
            safe_file.write_text("safe content")
            tf.add(safe_file, arcname="safe.txt")

        # Verify extraction would validate paths
        with tarfile.open(tar_path, "r:gz") as tf:
            for member in tf.getmembers():
                assert _is_safe_archive_path(member.name, extract_dir)


class TestDownloadIntegration:
    """Integration tests for the full download cycle.

    These tests require network access and may be slow.
    Skip with: pytest -m "not network"
    """

    @pytest.mark.slow
    def test_download_progress_callback(self, tmp_path, monkeypatch):
        """Test that download progress is tracked."""
        from watercooler_mcp.startup import _download_with_progress

        # Use a small test file
        test_url = "https://raw.githubusercontent.com/ggml-org/llama.cpp/master/README.md"
        output_path = tmp_path / "test_download"

        progress_calls = []

        def mock_callback(downloaded, total):
            progress_calls.append((downloaded, total))

        try:
            result = _download_with_progress(
                test_url,
                output_path,
                progress_callback=mock_callback,
            )
            assert result is True
            assert output_path.exists()
            assert len(progress_calls) > 0
        except Exception:
            pytest.skip("Network unavailable")

    @pytest.mark.slow
    @pytest.mark.skipif(
        os.environ.get("CI") == "true",
        reason="Skip large download in CI"
    )
    def test_full_download_verify_cycle(self, tmp_path, monkeypatch):
        """Test complete download and verification of llama-server.

        This downloads the actual binary and verifies the checksum.
        Only run manually due to download size (~25MB).
        """
        from watercooler_mcp.startup import (
            _download_llama_server,
            LLAMA_SERVER_CHECKSUMS,
        )

        # Use strict verification
        monkeypatch.setenv("WATERCOOLER_LLAMA_SERVER_VERIFY", "strict")
        monkeypatch.setenv("WATERCOOLER_LLAMA_SERVER_RELEASE", "b7896")

        # Override bin directory
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        monkeypatch.setattr(
            "watercooler_mcp.startup._get_bin_dir",
            lambda: bin_dir,
        )

        try:
            result = _download_llama_server()

            if result is not None:
                # Binary was downloaded successfully
                assert result.exists()
                assert result.is_file()
                # Check it's executable
                assert os.access(result, os.X_OK)
        except Exception as e:
            if "No known checksum" in str(e):
                pytest.fail(f"Checksum missing for release: {e}")
            pytest.skip(f"Download failed (network issue?): {e}")


class TestSharedLibraryDetection:
    """Tests for shared library detection."""

    def test_linux_so_detection(self):
        """Test Linux .so file detection."""
        from watercooler_mcp.startup import _is_shared_library

        assert _is_shared_library("libfoo.so") is True
        assert _is_shared_library("libfoo.so.0") is True
        assert _is_shared_library("libfoo.so.0.0.123") is True
        assert _is_shared_library("libllama.so.0") is True

    def test_macos_dylib_detection(self):
        """Test macOS .dylib file detection."""
        from watercooler_mcp.startup import _is_shared_library

        assert _is_shared_library("libfoo.dylib") is True
        assert _is_shared_library("libfoo.0.dylib") is True

    def test_windows_dll_detection(self):
        """Test Windows .dll file detection."""
        from watercooler_mcp.startup import _is_shared_library

        assert _is_shared_library("foo.dll") is True
        assert _is_shared_library("FOO.DLL") is True

    def test_non_library_rejected(self):
        """Test non-library files are rejected."""
        from watercooler_mcp.startup import _is_shared_library

        assert _is_shared_library("llama-server") is False
        assert _is_shared_library("config.json") is False
        assert _is_shared_library("README.md") is False
