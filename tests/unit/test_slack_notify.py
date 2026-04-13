"""Tests for Slack notification module."""

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from watercooler_mcp.slack.notify import (
    _rate_limit_ok,
    send_webhook,
    notify_new_entry,
    notify_ball_flip,
    notify_handoff,
    notify_status_change,
    _last_notification_time,
)


class TestSendWebhook:
    """Tests for send_webhook function."""

    def test_empty_webhook_url_returns_false(self):
        """Empty webhook URL should return False without making request."""
        assert send_webhook("", {"text": "test"}) is False

    @patch("watercooler_mcp.slack.notify.urllib.request.urlopen")
    def test_successful_webhook_returns_true(self, mock_urlopen):
        """Successful webhook should return True."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = send_webhook("https://hooks.slack.com/test", {"text": "hello"})

        assert result is True
        mock_urlopen.assert_called_once()

    @patch("watercooler_mcp.slack.notify.urllib.request.urlopen")
    def test_failed_webhook_returns_false(self, mock_urlopen):
        """Failed webhook should return False."""
        from urllib.error import URLError

        mock_urlopen.side_effect = URLError("Connection failed")

        result = send_webhook("https://hooks.slack.com/test", {"text": "hello"})

        assert result is False


class TestRateLimiting:
    """Tests for rate limiting functionality."""

    def test_rate_limit_allows_first_call(self):
        """First call should always be allowed."""
        # Reset state
        import watercooler_mcp.slack.notify as notify_module
        notify_module._last_notification_time = 0.0

        result = _rate_limit_ok(0.1)
        assert result is True

    def test_rate_limit_blocks_rapid_calls(self):
        """Rapid calls should be blocked."""
        import watercooler_mcp.slack.notify as notify_module

        # First call
        notify_module._last_notification_time = 0.0
        result1 = _rate_limit_ok(10.0)  # 10 second interval
        assert result1 is True

        # Immediate second call should be blocked
        result2 = _rate_limit_ok(10.0)
        assert result2 is False


class TestNotifyNewEntry:
    """Tests for notify_new_entry function."""

    @patch("watercooler_mcp.slack.notify.is_slack_enabled", return_value=False)
    def test_disabled_slack_returns_false(self, mock_enabled):
        """Should return False when Slack is disabled."""
        result = notify_new_entry(
            topic="test-topic",
            agent="Claude",
            title="Test Entry",
            role="implementer",
            entry_type="Note",
        )
        assert result is False

    @patch("watercooler_mcp.slack.notify.is_slack_enabled", return_value=True)
    @patch("watercooler_mcp.slack.notify.get_slack_config")
    @patch("watercooler_mcp.slack.notify._send_async")
    def test_sends_notification_when_enabled(self, mock_send, mock_config, mock_enabled):
        """Should send notification when Slack is enabled."""
        mock_config.return_value = {
            "webhook_url": "https://hooks.slack.com/test",
            "notify_on_say": True,
            "min_notification_interval": 0.0,
        }
        # Reset rate limit
        import watercooler_mcp.slack.notify as notify_module
        notify_module._last_notification_time = 0.0

        result = notify_new_entry(
            topic="test-topic",
            agent="Claude",
            title="Test Entry",
            role="implementer",
            entry_type="Note",
            ball="Human",
        )

        assert result is True
        mock_send.assert_called_once()

        # Verify payload structure
        call_args = mock_send.call_args
        webhook_url = call_args[0][0]
        payload = call_args[0][1]

        assert webhook_url == "https://hooks.slack.com/test"
        assert "text" in payload
        assert "blocks" in payload
        assert "test-topic" in payload["text"]


class TestNotifyBallFlip:
    """Tests for notify_ball_flip function."""

    @patch("watercooler_mcp.slack.notify.is_slack_enabled", return_value=True)
    @patch("watercooler_mcp.slack.notify.get_slack_config")
    @patch("watercooler_mcp.slack.notify._send_async")
    def test_sends_ball_flip_notification(self, mock_send, mock_config, mock_enabled):
        """Should send ball flip notification."""
        mock_config.return_value = {
            "webhook_url": "https://hooks.slack.com/test",
            "notify_on_ball_flip": True,
            "min_notification_interval": 0.0,
        }
        # Reset rate limit
        import watercooler_mcp.slack.notify as notify_module
        notify_module._last_notification_time = 0.0

        result = notify_ball_flip(
            topic="test-topic",
            from_agent="Claude",
            to_agent="Human",
            title="Ready for review",
        )

        assert result is True
        mock_send.assert_called_once()


class TestNotifyHandoff:
    """Tests for notify_handoff function."""

    @patch("watercooler_mcp.slack.notify.is_slack_enabled", return_value=True)
    @patch("watercooler_mcp.slack.notify.get_slack_config")
    @patch("watercooler_mcp.slack.notify._send_async")
    def test_sends_handoff_notification(self, mock_send, mock_config, mock_enabled):
        """Should send handoff notification."""
        mock_config.return_value = {
            "webhook_url": "https://hooks.slack.com/test",
            "notify_on_handoff": True,
            "min_notification_interval": 0.0,
        }
        # Reset rate limit
        import watercooler_mcp.slack.notify as notify_module
        notify_module._last_notification_time = 0.0

        result = notify_handoff(
            topic="test-topic",
            from_agent="Claude",
            to_agent="Human",
            note="Please review the implementation",
        )

        assert result is True
        mock_send.assert_called_once()


class TestNotifyStatusChange:
    """Tests for notify_status_change function."""

    @patch("watercooler_mcp.slack.notify.is_slack_enabled", return_value=True)
    @patch("watercooler_mcp.slack.notify.get_slack_config")
    @patch("watercooler_mcp.slack.notify._send_async")
    def test_sends_status_change_notification(self, mock_send, mock_config, mock_enabled):
        """Should send status change notification."""
        mock_config.return_value = {
            "webhook_url": "https://hooks.slack.com/test",
            "notify_on_status_change": True,
            "min_notification_interval": 0.0,
        }
        # Reset rate limit
        import watercooler_mcp.slack.notify as notify_module
        notify_module._last_notification_time = 0.0

        result = notify_status_change(
            topic="test-topic",
            old_status="OPEN",
            new_status="CLOSED",
            agent="Claude",
        )

        assert result is True
        mock_send.assert_called_once()

    @patch("watercooler_mcp.slack.notify.is_slack_enabled", return_value=True)
    @patch("watercooler_mcp.slack.notify.get_slack_config")
    @patch("watercooler_mcp.slack.notify._send_async")
    def test_status_change_without_old_status(self, mock_send, mock_config, mock_enabled):
        """Should handle missing old status."""
        mock_config.return_value = {
            "webhook_url": "https://hooks.slack.com/test",
            "notify_on_status_change": True,
            "min_notification_interval": 0.0,
        }
        # Reset rate limit
        import watercooler_mcp.slack.notify as notify_module
        notify_module._last_notification_time = 0.0

        result = notify_status_change(
            topic="test-topic",
            old_status=None,
            new_status="IN_REVIEW",
        )

        assert result is True
        mock_send.assert_called_once()


class TestSlackConfig:
    """Tests for Slack configuration."""

    def test_slack_config_defaults(self):
        """SlackConfig should have sensible defaults."""
        from watercooler.config_schema import SlackConfig

        config = SlackConfig()
        assert config.webhook_url == ""
        assert config.bot_token == ""
        assert config.notify_on_say is True
        assert config.notify_on_ball_flip is True
        assert config.notify_on_status_change is True
        assert config.notify_on_handoff is True
        assert config.is_enabled is False

    def test_slack_config_enabled_with_webhook(self):
        """SlackConfig should be enabled when webhook is set."""
        from watercooler.config_schema import SlackConfig

        config = SlackConfig(webhook_url="https://hooks.slack.com/test")
        assert config.is_enabled is True

    def test_mcp_config_includes_slack(self):
        """McpConfig should include slack section."""
        from watercooler.config_schema import McpConfig

        config = McpConfig()
        assert hasattr(config, "slack")
        assert config.slack.webhook_url == ""
