"""Unit tests for commands.setup_stop_hook()."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from watercooler.commands import setup_stop_hook

FAKE_BIN = "/home/user/.local/bin/watercooler-stop-hook"


def _has_nested_stop_hook(stop: list, bin_path: str) -> bool:
    """Return True if a nested `{"hooks": [{"command": bin_path, ...}]}` entry exists."""
    for entry in stop:
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
    # Point sys.argv[0] at a directory with no watercooler-stop-hook sibling
    # so the sibling-bin check falls through to shutil.which.
    fake_self = str(tmp_path / "bin" / "watercooler")
    with patch("shutil.which", return_value=FAKE_BIN), patch("sys.argv", [fake_self]):
        yield FAKE_BIN


# --- Binary resolution ----------------------------------------------------


def test_binary_not_found_exits_with_error(tmp_path, capsys):
    fake_self = str(tmp_path / "bin" / "watercooler")
    with patch("shutil.which", return_value=None), patch("sys.argv", [fake_self]):
        rc = setup_stop_hook(
            settings_path=tmp_path / "settings.json",
            local_settings_path=tmp_path / "settings.local.json",
        )
    assert rc == 1
    assert "watercooler-stop-hook" in capsys.readouterr().err


# --- Fresh / empty state --------------------------------------------------


def test_fresh_settings_file(tmp_path, fake_which):
    settings = tmp_path / "settings.json"
    local = tmp_path / "settings.local.json"

    rc = setup_stop_hook(settings_path=settings, local_settings_path=local)

    assert rc == 0
    assert _has_nested_stop_hook(_read_settings(settings)["hooks"]["Stop"], FAKE_BIN)


def test_writes_nested_hook_envelope(tmp_path, fake_which):
    settings = tmp_path / "settings.json"
    local = tmp_path / "settings.local.json"

    rc = setup_stop_hook(settings_path=settings, local_settings_path=local)

    assert rc == 0
    stop = _read_settings(settings)["hooks"]["Stop"]
    assert len(stop) == 1
    assert stop[0]["hooks"] == [{"type": "command", "command": FAKE_BIN}]


def test_appends_to_existing_settings(tmp_path, fake_which):
    settings = tmp_path / "settings.json"
    local = tmp_path / "settings.local.json"
    _write_settings(settings, {"hooks": {"PostCompact": []}})

    rc = setup_stop_hook(settings_path=settings, local_settings_path=local)

    assert rc == 0
    data = _read_settings(settings)
    assert _has_nested_stop_hook(data["hooks"]["Stop"], FAKE_BIN)
    assert "PostCompact" in data["hooks"]


# --- Duplicate detection --------------------------------------------------


def test_existing_new_style_hook_in_settings_json(tmp_path, fake_which):
    settings = tmp_path / "settings.json"
    local = tmp_path / "settings.local.json"
    _write_settings(
        settings,
        {"hooks": {"Stop": [{"type": "command", "command": FAKE_BIN}]}},
    )

    rc = setup_stop_hook(settings_path=settings, local_settings_path=local)

    assert rc == 0
    assert len(_read_settings(settings)["hooks"]["Stop"]) == 1


def test_existing_hook_in_settings_local_short_circuits(tmp_path, fake_which):
    settings = tmp_path / "settings.json"
    local = tmp_path / "settings.local.json"
    _write_settings(
        local,
        {"hooks": {"Stop": [{"type": "command", "command": FAKE_BIN}]}},
    )

    rc = setup_stop_hook(settings_path=settings, local_settings_path=local)

    assert rc == 0
    assert not settings.exists()


# --- JSON validation ------------------------------------------------------


def test_malformed_json_exits_with_error(tmp_path, fake_which):
    settings = tmp_path / "settings.json"
    local = tmp_path / "settings.local.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{ bad json }", encoding="utf-8")

    rc = setup_stop_hook(settings_path=settings, local_settings_path=local)

    assert rc == 1
    assert settings.read_text(encoding="utf-8") == "{ bad json }"


def test_malformed_local_settings_fails_loud(tmp_path, fake_which, capsys):
    settings = tmp_path / "settings.json"
    local = tmp_path / "settings.local.json"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("{ bad json }", encoding="utf-8")

    rc = setup_stop_hook(settings_path=settings, local_settings_path=local)

    assert rc == 1
    assert "invalid JSON" in capsys.readouterr().err
    assert not settings.exists()


def test_invalid_stop_shape_exits_with_error(tmp_path, fake_which):
    settings = tmp_path / "settings.json"
    local = tmp_path / "settings.local.json"
    _write_settings(settings, {"hooks": {"Stop": "not-a-list"}})

    rc = setup_stop_hook(settings_path=settings, local_settings_path=local)

    assert rc == 1
