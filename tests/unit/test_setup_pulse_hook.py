"""Unit tests for commands.setup_pulse_hook()."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from watercooler.commands import setup_pulse_hook

FAKE_BIN = "/home/user/.local/bin/watercooler-capture-theme"


def _has_nested_capture_hook(post_compact: list, bin_path: str) -> bool:
    """Return True if a nested `{"hooks": [{"command": bin_path, ...}]}` entry exists."""
    for entry in post_compact:
        if not isinstance(entry, dict):
            continue
        for sub in entry.get("hooks", []):
            if isinstance(sub, dict) and sub.get("command") == bin_path:
                return True
    return False


def _write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _read_settings(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def fake_which(tmp_path):
    # Point sys.argv[0] to a directory that has no watercooler-capture-theme
    # sibling so the sibling-bin check falls through to shutil.which.
    fake_self = str(tmp_path / "bin" / "watercooler")
    with patch("shutil.which", return_value=FAKE_BIN), \
         patch("sys.argv", [fake_self]):
        yield FAKE_BIN


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------

def test_binary_not_found_exits_with_error(tmp_path, capsys):
    """No sibling and shutil.which() returns None — must return 1 with a clear message."""
    fake_self = str(tmp_path / "bin" / "watercooler")
    with patch("shutil.which", return_value=None), \
         patch("sys.argv", [fake_self]):
        rc = setup_pulse_hook(
            settings_path=tmp_path / "settings.json",
            local_settings_path=tmp_path / "settings.local.json",
        )
    assert rc == 1
    captured = capsys.readouterr()
    assert "watercooler-capture-theme" in captured.err


# ---------------------------------------------------------------------------
# Fresh / empty state
# ---------------------------------------------------------------------------

def test_fresh_settings_file(tmp_path, fake_which, capsys):
    """No settings files exist — creates settings.json from scratch."""
    settings = tmp_path / "settings.json"
    local = tmp_path / "settings.local.json"

    rc = setup_pulse_hook(settings_path=settings, local_settings_path=local)

    assert rc == 0
    data = _read_settings(settings)
    hooks = data["hooks"]["PostCompact"]
    assert _has_nested_capture_hook(hooks, FAKE_BIN)


def test_writes_nested_hook_envelope(tmp_path, fake_which):
    """Regression: must emit `{"hooks": [{"type": "command", ...}]}` envelope.

    Claude Code only pipes stdin to PostCompact hooks wired with the nested
    envelope. The bare `{"type": "command", ...}` shape loads without error but
    produces empty stdin, silently breaking the capture hook.
    See bug-postcompact-hook-stdin-empty.
    """
    settings = tmp_path / "settings.json"
    local = tmp_path / "settings.local.json"

    rc = setup_pulse_hook(settings_path=settings, local_settings_path=local)

    assert rc == 0
    post_compact = _read_settings(settings)["hooks"]["PostCompact"]
    assert len(post_compact) == 1
    entry = post_compact[0]
    assert "hooks" in entry, f"expected nested envelope, got bare: {entry}"
    assert entry["hooks"] == [{"type": "command", "command": FAKE_BIN}]


def test_appends_to_existing_settings(tmp_path, fake_which):
    """Valid JSON object, no existing hook — appends new entry."""
    settings = tmp_path / "settings.json"
    local = tmp_path / "settings.local.json"
    _write_settings(settings, {"hooks": {"SomeOtherHook": []}})

    rc = setup_pulse_hook(settings_path=settings, local_settings_path=local)

    assert rc == 0
    data = _read_settings(settings)
    hooks = data["hooks"]["PostCompact"]
    assert _has_nested_capture_hook(hooks, FAKE_BIN)
    # Pre-existing hook key should still be present
    assert "SomeOtherHook" in data["hooks"]


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def test_existing_new_style_hook_in_settings_json(tmp_path, fake_which, capsys):
    """Existing watercooler-capture-theme hook in settings.json — exits 0, no duplicate."""
    settings = tmp_path / "settings.json"
    local = tmp_path / "settings.local.json"
    _write_settings(settings, {
        "hooks": {
            "PostCompact": [{"type": "command", "command": FAKE_BIN}]
        }
    })

    rc = setup_pulse_hook(settings_path=settings, local_settings_path=local)

    assert rc == 0
    data = _read_settings(settings)
    assert len(data["hooks"]["PostCompact"]) == 1  # no duplicate


def test_existing_old_style_hook_in_settings_json(tmp_path, fake_which, capsys):
    """Existing python3 .../capture_theme.py hook — exits 0, no duplicate."""
    settings = tmp_path / "settings.json"
    local = tmp_path / "settings.local.json"
    _write_settings(settings, {
        "hooks": {
            "PostCompact": [
                {"type": "command", "command": "python3 /repo/.claude/skills/project-pulse/scripts/capture_theme.py"}
            ]
        }
    })

    rc = setup_pulse_hook(settings_path=settings, local_settings_path=local)

    assert rc == 0
    data = _read_settings(settings)
    assert len(data["hooks"]["PostCompact"]) == 1  # no new entry added


def test_existing_hook_in_settings_local_short_circuits(tmp_path, fake_which, capsys):
    """Hook in settings.local.json — exits 0, no write to settings.json."""
    settings = tmp_path / "settings.json"
    local = tmp_path / "settings.local.json"
    _write_settings(local, {
        "hooks": {
            "PostCompact": [{"type": "command", "command": FAKE_BIN}]
        }
    })

    rc = setup_pulse_hook(settings_path=settings, local_settings_path=local)

    assert rc == 0
    assert not settings.exists()  # should not have been created


# ---------------------------------------------------------------------------
# JSON validation
# ---------------------------------------------------------------------------

def test_malformed_json_exits_with_error(tmp_path, fake_which, capsys):
    """Invalid JSON in settings.json — exits 1, no overwrite."""
    settings = tmp_path / "settings.json"
    local = tmp_path / "settings.local.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{ bad json }", encoding="utf-8")

    rc = setup_pulse_hook(settings_path=settings, local_settings_path=local)

    assert rc == 1
    # Content must be unchanged
    assert settings.read_text(encoding="utf-8") == "{ bad json }"


def test_invalid_hooks_shape_exits_with_error(tmp_path, fake_which, capsys):
    """'hooks' exists but is not an object — exits 1."""
    settings = tmp_path / "settings.json"
    local = tmp_path / "settings.local.json"
    _write_settings(settings, {"hooks": ["not", "an", "object"]})

    rc = setup_pulse_hook(settings_path=settings, local_settings_path=local)

    assert rc == 1


def test_invalid_postcompact_shape_exits_with_error(tmp_path, fake_which, capsys):
    """hooks.PostCompact exists but is not an array — exits 1."""
    settings = tmp_path / "settings.json"
    local = tmp_path / "settings.local.json"
    _write_settings(settings, {"hooks": {"PostCompact": "not-an-array"}})

    rc = setup_pulse_hook(settings_path=settings, local_settings_path=local)

    assert rc == 1


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------

def test_atomic_write_uses_replace(tmp_path, fake_which):
    """Verify that os.replace is called (atomic write) and the file is written."""
    settings = tmp_path / "settings.json"
    local = tmp_path / "settings.local.json"

    replace_calls: list = []
    real_replace = os.replace

    def tracked_replace(src: str, dst: str) -> None:
        replace_calls.append((src, dst))
        real_replace(src, dst)

    with patch("os.replace", side_effect=tracked_replace):
        rc = setup_pulse_hook(settings_path=settings, local_settings_path=local)

    assert rc == 0
    assert len(replace_calls) == 1
    # Destination should be the settings path (may be Path or str)
    assert Path(replace_calls[0][1]) == settings
