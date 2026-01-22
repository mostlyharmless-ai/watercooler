"""Slack CLI commands for watercooler.

Provides setup, test, and status commands for Slack webhook integration.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Check for tomlkit (needed for config writing)
try:
    import tomlkit

    HAS_TOMLKIT = True
except ImportError:
    HAS_TOMLKIT = False


def _get_user_config_path() -> Path:
    """Get path to user config file (~/.watercooler/config.toml)."""
    return Path.home() / ".watercooler" / "config.toml"


def _load_config_toml(path: Path) -> "tomlkit.TOMLDocument":
    """Load existing config or create empty document."""
    if not HAS_TOMLKIT:
        raise RuntimeError("tomlkit required. Install with: pip install tomlkit")

    if path.exists():
        with open(path, "r") as f:
            return tomlkit.load(f)
    return tomlkit.document()


def _save_config_toml(path: Path, doc: "tomlkit.TOMLDocument") -> None:
    """Save config document to file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write(tomlkit.dumps(doc))


def _test_webhook(webhook_url: str, message: str = "🎉 Watercooler connected!") -> bool:
    """Test a webhook URL by sending a message.

    Args:
        webhook_url: Slack webhook URL
        message: Test message to send

    Returns:
        True if successful, False otherwise
    """
    import json
    import urllib.request
    import urllib.error

    if not webhook_url:
        return False

    try:
        payload = {"text": message}
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status == 200
    except (urllib.error.URLError, Exception):
        return False


def _load_slack_config() -> Dict[str, Any]:
    """Load current Slack configuration.

    Returns dict with webhook_url and notification toggles.
    """
    # Try to load from watercooler_mcp config if available
    try:
        from watercooler_mcp.config import get_slack_config

        return get_slack_config()
    except ImportError:
        pass

    # Fallback: read directly from config file
    config_path = _get_user_config_path()
    if not config_path.exists():
        return {}

    if not HAS_TOMLKIT:
        return {}

    doc = _load_config_toml(config_path)
    mcp = doc.get("mcp", {})
    slack = mcp.get("slack", {})
    return dict(slack) if slack else {}


def _mask_webhook_url(url: str) -> str:
    """Mask webhook URL for display (show first/last parts)."""
    if not url:
        return ""
    if len(url) < 50:
        return url[:20] + "..." + url[-10:]
    # https://hooks.slack.com/services/T.../B.../...
    parts = url.split("/")
    if len(parts) >= 6:
        return f"{parts[0]}//{parts[2]}/services/{parts[4][:4]}.../{parts[5][:4]}..."
    return url[:30] + "..."


def slack_setup(webhook_url: Optional[str] = None) -> int:
    """Interactive Slack webhook setup.

    Args:
        webhook_url: Optional pre-provided webhook URL

    Returns:
        Exit code (0 for success, 1 for error)
    """
    if not HAS_TOMLKIT:
        print("❌ tomlkit required for config management.", file=sys.stderr)
        print("   Install with: pip install tomlkit", file=sys.stderr)
        return 1

    print("Slack Webhook Setup")
    print("=" * 40)
    print()

    if not webhook_url:
        print("To get a webhook URL:")
        print("1. Go to https://api.slack.com/apps?new_app=1")
        print("2. Create app 'From scratch', name it 'Watercooler'")
        print("3. Go to 'Incoming Webhooks' → Toggle On")
        print("4. Click 'Add New Webhook to Workspace'")
        print("5. Select channel and authorize")
        print()

        try:
            webhook_url = input("Paste your webhook URL: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            return 1

    if not webhook_url:
        print("❌ No webhook URL provided.", file=sys.stderr)
        return 1

    # Validate URL format
    if not webhook_url.startswith("https://hooks.slack.com/"):
        print("❌ Invalid webhook URL format.", file=sys.stderr)
        print("   URL should start with: https://hooks.slack.com/", file=sys.stderr)
        return 1

    # Test webhook
    print("Testing webhook... ", end="", flush=True)
    if _test_webhook(webhook_url):
        print("✓")
    else:
        print("❌")
        print("Failed to send test message. Check the URL and try again.", file=sys.stderr)
        return 1

    # Save to config
    config_path = _get_user_config_path()
    doc = _load_config_toml(config_path)

    # Ensure [mcp] section exists
    if "mcp" not in doc:
        doc.add("mcp", tomlkit.table())

    mcp = doc["mcp"]

    # Ensure [mcp.slack] section exists
    if "slack" not in mcp:
        mcp.add("slack", tomlkit.table())

    slack = mcp["slack"]

    # Update webhook URL
    if "webhook_url" in slack:
        slack["webhook_url"] = webhook_url
    else:
        slack.add("webhook_url", webhook_url)

    # Add default notification settings if not present
    defaults = {
        "notify_on_say": True,
        "notify_on_ball_flip": True,
        "notify_on_status_change": True,
        "notify_on_handoff": True,
    }
    for key, default in defaults.items():
        if key not in slack:
            slack.add(key, default)

    _save_config_toml(config_path, doc)
    print(f"✓ Saved to {config_path}")
    print()
    print("Slack notifications enabled:")
    print("  ✓ New entries (say)")
    print("  ✓ Ball flips")
    print("  ✓ Status changes")
    print("  ✓ Handoffs")
    return 0


def slack_test() -> int:
    """Send a test notification to verify webhook.

    Returns:
        Exit code (0 for success, 1 for error)
    """
    config = _load_slack_config()
    webhook_url = config.get("webhook_url", "")

    # Also check environment variable
    if not webhook_url:
        webhook_url = os.getenv("WATERCOOLER_SLACK_WEBHOOK", "")

    if not webhook_url:
        print("❌ No webhook configured.", file=sys.stderr)
        print("   Run: watercooler slack setup", file=sys.stderr)
        return 1

    print("Testing webhook... ", end="", flush=True)
    if _test_webhook(webhook_url, "🧪 Test message from watercooler CLI"):
        print("✓ Message sent!")
        return 0
    else:
        print("❌ Failed")
        print("Check your webhook URL and try again.", file=sys.stderr)
        return 1


def slack_status() -> int:
    """Show current Slack configuration status.

    Returns:
        Exit code (0 for success)
    """
    config = _load_slack_config()
    webhook_url = config.get("webhook_url", "")

    # Also check environment variable
    env_webhook = os.getenv("WATERCOOLER_SLACK_WEBHOOK", "")
    if env_webhook and not webhook_url:
        webhook_url = env_webhook

    print("Slack Integration Status")
    print("=" * 40)

    if webhook_url:
        print(f"Webhook:  ✓ Configured ({_mask_webhook_url(webhook_url)})")
        if env_webhook:
            print("          (from WATERCOOLER_SLACK_WEBHOOK)")
    else:
        print("Webhook:  ❌ Not configured")
        print()
        print("Run: watercooler slack setup")
        return 0

    print()
    print("Notifications:")

    # Show notification settings
    notify_say = config.get("notify_on_say", True)
    notify_ball = config.get("notify_on_ball_flip", True)
    notify_status = config.get("notify_on_status_change", True)
    notify_handoff = config.get("notify_on_handoff", True)

    print(f"  say:           {'✓' if notify_say else '✗'}")
    print(f"  ball_flip:     {'✓' if notify_ball else '✗'}")
    print(f"  status_change: {'✓' if notify_status else '✗'}")
    print(f"  handoff:       {'✓' if notify_handoff else '✗'}")

    # Show rate limit setting
    min_interval = config.get("min_notification_interval", 1.0)
    print()
    print(f"Rate limit: {min_interval}s between notifications")

    return 0


def slack_disable() -> int:
    """Disable Slack notifications by clearing webhook URL.

    Returns:
        Exit code (0 for success, 1 for error)
    """
    if not HAS_TOMLKIT:
        print("❌ tomlkit required for config management.", file=sys.stderr)
        return 1

    config_path = _get_user_config_path()
    if not config_path.exists():
        print("No config file found. Slack is not configured.")
        return 0

    doc = _load_config_toml(config_path)

    mcp = doc.get("mcp", {})
    slack = mcp.get("slack", {})

    if "webhook_url" in slack:
        slack["webhook_url"] = ""
        _save_config_toml(config_path, doc)
        print("✓ Slack notifications disabled.")
        print(f"  Config updated: {config_path}")
    else:
        print("Slack webhook not configured.")

    return 0
