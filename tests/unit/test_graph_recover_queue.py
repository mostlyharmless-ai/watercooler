"""Unit tests for graph recovery target resolution.

Tests cover:
- resolve_recovery_targets() extraction

Note: Graph recovery execution and queue integration tests were removed
when recover_graph was moved to scripts/recover_baseline_graph.py.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from watercooler.baseline_graph import storage


# ================================================================== #
# resolve_recovery_targets
# ================================================================== #


class TestResolveRecoveryTargets:
    """Tests for the extracted resolve_recovery_targets() function."""

    def test_invalid_mode_returns_error(self, tmp_path):
        from watercooler.baseline_graph.sync import resolve_recovery_targets

        topics, errors = resolve_recovery_targets(tmp_path, mode="bogus")
        assert topics == []
        assert any("Invalid mode" in e for e in errors)

    def test_selective_without_topics_returns_error(self, tmp_path):
        from watercooler.baseline_graph.sync import resolve_recovery_targets

        topics, errors = resolve_recovery_targets(tmp_path, mode="selective")
        assert topics == []
        assert any("requires topics" in e for e in errors)

    def test_selective_filters_to_existing_files(self, tmp_path):
        from watercooler.baseline_graph.sync import resolve_recovery_targets

        (tmp_path / "alpha.md").write_text("# Alpha")
        (tmp_path / "beta.md").write_text("# Beta")

        # Write graph data so topics are discoverable
        graph_dir = storage.ensure_graph_dir(tmp_path)
        for topic in ("alpha", "beta"):
            td = storage.ensure_thread_graph_dir(graph_dir, topic)
            storage.atomic_write_json(td / "meta.json", {
                "id": f"thread:{topic}", "type": "thread", "topic": topic,
                "status": "OPEN", "entry_count": 0,
            })

        topics, errors = resolve_recovery_targets(
            tmp_path, mode="selective", topics=["alpha", "gamma"]
        )
        assert errors == []
        assert topics == ["alpha"]

    def test_selective_no_matching_files(self, tmp_path):
        from watercooler.baseline_graph.sync import resolve_recovery_targets

        (tmp_path / "alpha.md").write_text("# Alpha")

        topics, errors = resolve_recovery_targets(
            tmp_path, mode="selective", topics=["missing"]
        )
        assert topics == []
        assert any("No matching" in e for e in errors)

    def test_all_mode_returns_all_topics(self, tmp_path):
        from watercooler.baseline_graph.sync import resolve_recovery_targets

        (tmp_path / "one.md").write_text("# One")
        (tmp_path / "two.md").write_text("# Two")
        (tmp_path / "three.md").write_text("# Three")

        # Write graph data so topics are discoverable
        graph_dir = storage.ensure_graph_dir(tmp_path)
        for topic in ("one", "two", "three"):
            td = storage.ensure_thread_graph_dir(graph_dir, topic)
            storage.atomic_write_json(td / "meta.json", {
                "id": f"thread:{topic}", "type": "thread", "topic": topic,
                "status": "OPEN", "entry_count": 0,
            })

        topics, errors = resolve_recovery_targets(tmp_path, mode="all")
        assert errors == []
        assert sorted(topics) == ["one", "three", "two"]

    @patch("watercooler.baseline_graph.sync.check_graph_health")
    def test_stale_mode_uses_health_check(self, mock_health, tmp_path):
        from watercooler.baseline_graph.sync import resolve_recovery_targets

        (tmp_path / "good.md").write_text("# Good")
        (tmp_path / "stale.md").write_text("# Stale")
        (tmp_path / "broken.md").write_text("# Broken")

        mock_report = MagicMock()
        mock_report.stale_threads = ["stale"]
        mock_report.error_details = {"broken": "some error"}
        mock_health.return_value = mock_report

        topics, errors = resolve_recovery_targets(tmp_path, mode="stale")
        assert errors == []
        assert sorted(topics) == ["broken", "stale"]
        mock_health.assert_called_once_with(tmp_path)

    @patch("watercooler.baseline_graph.sync.check_graph_health")
    def test_stale_mode_nothing_to_recover(self, mock_health, tmp_path):
        from watercooler.baseline_graph.sync import resolve_recovery_targets

        mock_report = MagicMock()
        mock_report.stale_threads = []
        mock_report.error_details = {}
        mock_health.return_value = mock_report

        topics, errors = resolve_recovery_targets(tmp_path, mode="stale")
        assert topics == []
        assert errors == []


# ================================================================== #
# _graph_recover_impl stub
# ================================================================== #


class TestGraphRecoverImplStub:
    """Test that the MCP tool returns the script redirect message."""

    @pytest.mark.anyio
    async def test_recover_impl_returns_script_message(self):
        from watercooler_mcp.tools.graph import _graph_recover_impl

        result = await _graph_recover_impl(MagicMock())
        output = json.loads(result)
        assert output["status"] == "moved_to_script"
        assert "recover_baseline_graph.py" in output["script"]
