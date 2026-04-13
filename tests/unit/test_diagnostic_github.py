"""Unit tests for GitHub-related diagnostic functions.

Tests cover:
- _check_github_rate_limit(): Rate limit status checking
- _check_gh_version(): gh CLI version checking
"""

import json
import subprocess
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from watercooler_mcp.tools.diagnostic import (
    _check_github_rate_limit,
    _check_gh_version,
    RATE_LIMIT_WARNING_THRESHOLD,
)


class TestCheckGithubRateLimit:
    """Tests for _check_github_rate_limit function."""

    def test_rate_limit_ok(self):
        """Test successful rate limit check with healthy remaining calls."""
        # Reset is 30 minutes from now
        reset_ts = int(datetime.now(tz=timezone.utc).timestamp()) + 1800

        mock_response = {
            "resources": {
                "core": {
                    "remaining": 4500,
                    "limit": 5000,
                    "reset": reset_ts,
                }
            }
        }

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(mock_response),
                stderr="",
            )

            result = _check_github_rate_limit()

            assert result["status"] == "ok"
            assert result["remaining"] == 4500
            assert result["limit"] == 5000
            assert result["percent"] == 90
            assert result["reset_minutes"] is not None
            assert result["reset_minutes"] >= 29  # ~30 minutes
            assert len(result["warnings"]) == 0

    def test_rate_limit_warning(self):
        """Test rate limit warning when below threshold."""
        reset_ts = int(datetime.now(tz=timezone.utc).timestamp()) + 600

        mock_response = {
            "resources": {
                "core": {
                    "remaining": 400,  # 8% - below 10% threshold
                    "limit": 5000,
                    "reset": reset_ts,
                }
            }
        }

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(mock_response),
                stderr="",
            )

            result = _check_github_rate_limit()

            assert result["status"] == "warning"
            assert result["remaining"] == 400
            assert result["percent"] == 8
            assert len(result["warnings"]) == 1
            assert "Approaching rate limit" in result["warnings"][0]

    def test_rate_limit_exhausted(self):
        """Test rate limit exhausted (0 remaining)."""
        reset_ts = int(datetime.now(tz=timezone.utc).timestamp()) + 1200  # 20 min

        mock_response = {
            "resources": {
                "core": {
                    "remaining": 0,
                    "limit": 5000,
                    "reset": reset_ts,
                }
            }
        }

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(mock_response),
                stderr="",
            )

            result = _check_github_rate_limit()

            assert result["status"] == "limited"
            assert result["remaining"] == 0
            assert result["percent"] == 0
            assert len(result["warnings"]) == 1
            assert "RATE LIMITED" in result["warnings"][0]
            assert len(result["recommendations"]) == 1
            assert "Wait" in result["recommendations"][0]

    def test_gh_not_installed(self):
        """Test when gh CLI is not installed."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("gh not found")

            result = _check_github_rate_limit()

            assert result["status"] == "gh_not_installed"
            assert "gh CLI not installed" in result["warnings"][0]
            assert len(result["recommendations"]) == 1

    def test_timeout(self):
        """Test timeout during rate limit check."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("gh", 10)

            result = _check_github_rate_limit()

            assert result["status"] == "timeout"
            assert "timed out" in result["warnings"][0]

    def test_invalid_json_response(self):
        """Test handling of invalid JSON from gh api."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="not valid json",
                stderr="",
            )

            result = _check_github_rate_limit()

            assert result["status"] == "error"
            assert "Invalid JSON" in result["warnings"][0]

    def test_reset_time_in_past(self):
        """Test when reset time is in the past."""
        # Reset was 5 minutes ago
        reset_ts = int(datetime.now(tz=timezone.utc).timestamp()) - 300

        mock_response = {
            "resources": {
                "core": {
                    "remaining": 5000,
                    "limit": 5000,
                    "reset": reset_ts,
                }
            }
        }

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(mock_response),
                stderr="",
            )

            result = _check_github_rate_limit()

            assert result["reset_minutes"] == 0

    def test_total_seconds_handles_long_duration(self):
        """Test that reset time calculation works for >24 hour durations.

        This tests the fix for delta.seconds vs delta.total_seconds().
        delta.seconds only returns seconds within current day (0-86399).
        """
        # Reset is 25 hours from now
        reset_ts = int(datetime.now(tz=timezone.utc).timestamp()) + (25 * 3600)

        mock_response = {
            "resources": {
                "core": {
                    "remaining": 5000,
                    "limit": 5000,
                    "reset": reset_ts,
                }
            }
        }

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(mock_response),
                stderr="",
            )

            result = _check_github_rate_limit()

            # Should be ~1500 minutes (25 hours), not ~60 (1 hour due to bug)
            assert result["reset_minutes"] >= 1490
            assert result["reset_minutes"] <= 1510


class TestCheckGhVersion:
    """Tests for _check_gh_version function."""

    def test_version_ok(self):
        """Test current/recent version is ok."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="gh version 2.83.2 (2025-01-15)\nhttps://github.com/cli/cli/releases/tag/v2.83.2",
                stderr="",
            )

            result = _check_gh_version()

            assert result["status"] == "ok"
            assert result["version"] == "2.83.2"
            assert result["major"] == 2
            assert result["minor"] == 83
            assert result["is_outdated"] is False
            assert len(result["warnings"]) == 0

    def test_version_outdated(self):
        """Test outdated version triggers warning."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="gh version 2.10.0 (2023-01-01)",
                stderr="",
            )

            result = _check_gh_version()

            assert result["status"] == "outdated"
            assert result["version"] == "2.10.0"
            assert result["is_outdated"] is True
            assert len(result["warnings"]) == 1
            assert "outdated" in result["warnings"][0].lower()
            assert len(result["recommendations"]) >= 1

    def test_gh_not_installed(self):
        """Test when gh CLI is not installed."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("gh not found")

            result = _check_gh_version()

            assert result["status"] == "not_installed"
            assert "not installed" in result["warnings"][0].lower()
            assert len(result["recommendations"]) == 1
            assert "https://cli.github.com" in result["recommendations"][0]

    def test_timeout(self):
        """Test timeout during version check."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("gh", 5)

            result = _check_gh_version()

            assert result["status"] == "timeout"

    def test_parse_error(self):
        """Test handling of unparseable version output."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="some unexpected output format",
                stderr="",
            )

            result = _check_gh_version()

            # Should not crash, should indicate parse issue
            assert result["version"] is None

    def test_version_with_prerelease(self):
        """Test parsing version with prerelease suffix."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="gh version 2.85.0-rc1 (2025-02-01)",
                stderr="",
            )

            result = _check_gh_version()

            assert result["status"] == "ok"
            # Version may include or strip prerelease suffix depending on parsing
            assert result["version"] in ("2.85.0", "2.85.0-rc1")
            assert result["major"] == 2
            assert result["minor"] == 85


class TestRateLimitThreshold:
    """Tests for the rate limit warning threshold constant."""

    def test_threshold_value(self):
        """Verify threshold is 10%."""
        assert RATE_LIMIT_WARNING_THRESHOLD == 0.1

    def test_threshold_used_correctly(self):
        """Verify threshold boundary is correct (just above vs just below)."""
        reset_ts = int(datetime.now(tz=timezone.utc).timestamp()) + 600

        # Just above threshold (should be ok)
        mock_above = {
            "resources": {
                "core": {
                    "remaining": 501,  # 10.02% - just above
                    "limit": 5000,
                    "reset": reset_ts,
                }
            }
        }

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(mock_above),
                stderr="",
            )
            result = _check_github_rate_limit()
            assert result["status"] == "ok"

        # Just below threshold (should warn)
        mock_below = {
            "resources": {
                "core": {
                    "remaining": 499,  # 9.98% - just below
                    "limit": 5000,
                    "reset": reset_ts,
                }
            }
        }

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(mock_below),
                stderr="",
            )
            result = _check_github_rate_limit()
            assert result["status"] == "warning"
