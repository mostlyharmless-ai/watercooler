"""Tests for compound artifact daemon wiring (issue #214)."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from watercooler.config_schema import CompoundConfig, DaemonsConfig, McpConfig
from watercooler_mcp.daemons.compound import (
  generate_compound_artifacts,
  should_generate_compound_artifacts,
)


class TestCompoundConfigDefaults:
  """Tests for CompoundConfig schema defaults."""

  def test_enabled_defaults_to_false(self):
    """CompoundConfig.enabled defaults to False (opt-in required)."""
    cfg = CompoundConfig()
    assert cfg.enabled is False

  def test_auto_report_on_closure_defaults_to_true(self):
    """CompoundConfig.auto_report_on_closure defaults to True."""
    cfg = CompoundConfig()
    assert cfg.auto_report_on_closure is True

  def test_auto_learnings_defaults_to_true(self):
    """CompoundConfig.auto_learnings defaults to True."""
    cfg = CompoundConfig()
    assert cfg.auto_learnings is True

  def test_auto_suggestions_defaults_to_true(self):
    """CompoundConfig.auto_suggestions defaults to True."""
    cfg = CompoundConfig()
    assert cfg.auto_suggestions is True

  def test_config_path_reachable_via_full_config(self):
    """config.full().mcp.daemons.compound.enabled is accessible."""
    from watercooler.config_facade import config

    c = config.full()
    assert c.mcp.daemons.compound.enabled is False


class TestShouldGenerateCompoundArtifacts:
  """Tests for should_generate_compound_artifacts()."""

  def test_returns_false_by_default(self):
    """Returns False when compound.enabled is not set (default config)."""
    result = should_generate_compound_artifacts()
    assert result is False

  def test_returns_true_when_enabled_in_config(self):
    """Returns True when mocked config sets compound.enabled=True."""
    compound_cfg = CompoundConfig(enabled=True)
    daemons_cfg = DaemonsConfig(enabled=True, compound=compound_cfg)
    mcp_cfg = McpConfig(daemons=daemons_cfg)

    mock_full = MagicMock()
    mock_full.mcp = mcp_cfg

    mock_config = MagicMock()
    mock_config.full.return_value = mock_full

    with patch(
      "watercooler.config_facade.config",
      mock_config,
    ):
      result = should_generate_compound_artifacts()

    assert result is True

  def test_returns_false_on_config_exception(self):
    """Returns False gracefully when config loading raises an exception."""
    mock_config = MagicMock()
    mock_config.full.side_effect = RuntimeError("config unavailable")

    with patch(
      "watercooler.config_facade.config",
      mock_config,
    ):
      result = should_generate_compound_artifacts()

    assert result is False


class TestGenerateCompoundArtifacts:
  """Tests for generate_compound_artifacts() stub."""

  def test_noop_when_disabled(self, tmp_path: Path, caplog):
    """No-op (no log) when compound artifact generation is disabled."""
    with patch(
      "watercooler_mcp.daemons.compound.should_generate_compound_artifacts",
      return_value=False,
    ):
      with caplog.at_level(logging.INFO):
        generate_compound_artifacts("test-topic", tmp_path)

    assert "compound" not in caplog.text.lower() or "pending" not in caplog.text.lower()

  def test_logs_when_enabled(self, tmp_path: Path):
    """Logs an info message when compound artifact generation is enabled."""
    import watercooler_mcp.daemons.compound as compound_mod

    records: list = []

    class _Capture(logging.Handler):
      def emit(self, record: logging.LogRecord) -> None:
        records.append(record)

    handler = _Capture()
    compound_mod.logger.addHandler(handler)
    original_level = compound_mod.logger.level
    compound_mod.logger.setLevel(logging.INFO)
    try:
      with patch(
        "watercooler_mcp.daemons.compound.should_generate_compound_artifacts",
        return_value=True,
      ):
        generate_compound_artifacts("my-topic", tmp_path)
    finally:
      compound_mod.logger.removeHandler(handler)
      compound_mod.logger.setLevel(original_level)

    messages = " ".join(r.getMessage() for r in records)
    assert "my-topic" in messages
    assert "pending" in messages.lower()

  def test_returns_none_when_disabled(self, tmp_path: Path):
    """Returns None (no error) when disabled."""
    with patch(
      "watercooler_mcp.daemons.compound.should_generate_compound_artifacts",
      return_value=False,
    ):
      result = generate_compound_artifacts("test-topic", tmp_path)

    assert result is None

  def test_returns_none_when_enabled(self, tmp_path: Path):
    """Returns None (no error) when enabled (stub — no actual generation)."""
    with patch(
      "watercooler_mcp.daemons.compound.should_generate_compound_artifacts",
      return_value=True,
    ):
      result = generate_compound_artifacts("test-topic", tmp_path)

    assert result is None
