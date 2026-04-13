"""Unit tests for the watercooler_pulse_snapshot MCP tool."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from watercooler.pulse_snapshot_lib import derive_repo_key
from watercooler_mcp.daemons.state import DaemonCheckpoint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_snapshot(
    repo_key: str | None = None,
    *,
    generated_at: str | None = None,
    report_path: str | None = None,
) -> dict:
    """Build a minimal but valid v1.0 snapshot dict."""
    return {
        "snapshot_version": "1.0",
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "repo_key": repo_key or "abc123def456",
        "window_days": 7,
        "code_branch": "*",
        "corpus": {"session_context_threads": 1, "contributors_active": 1},
        "contributors": {"jay": {"name": "jay", "session_count": 1}},
        "queue_pending": 0,
        "stalled_threads": [],
        "risk_surface_tags": [],
        "analysis": {
            "latest_report_path": report_path,
            "latest_report_age_days": None,
            "is_fresh": False,
        },
    }


def _make_checkpoint(
    repo_key: str,
    snapshot: dict,
    dimension_scores: dict | None = None,
) -> DaemonCheckpoint:
    """Build a DaemonCheckpoint with a snapshot (and optional dimension_scores) in extras."""
    cp = DaemonCheckpoint(daemon_name="pulse_snapshot")
    project_state: dict = {"pulse_snapshot": snapshot}
    if dimension_scores is not None:
        project_state["dimension_scores"] = dimension_scores
    cp.extras = {"projects": {repo_key: project_state}}
    return cp


def _make_dimension_scores() -> dict:
    """Build a minimal dimension_scores dict matching the daemon's output shape."""
    return {
        "activity_tempo": {
            "level_score": 0.75,
            "level_label": "active",
            "baseline_score": 0.60,
            "trend_delta": 0.15,
            "trend_label": "improving",
            "confidence": 0.9,
            "watch": False,
            "notes": [],
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "supersession_rate_used": 0.1,
    }


def _empty_checkpoint() -> DaemonCheckpoint:
    return DaemonCheckpoint(daemon_name="pulse_snapshot")


def _call_tool(
    code_path: str = ".",
    *,
    daemon=None,
    manager: object = "auto",
    checkpoint: DaemonCheckpoint | None = None,
    config_enabled: bool | None = None,
    git_root: Path | None = None,
):
    """Call _pulse_snapshot_impl with controlled mocks.

    Args:
        daemon: If set, manager.get_daemon("pulse_snapshot") returns this.
        manager: Provide an explicit manager mock, or "auto" to create one.
        checkpoint: Checkpoint returned by load_checkpoint(). Defaults to empty.
        config_enabled: pulse_snapshot.enabled value. None = don't mock config.
        git_root: Root returned by _discover_git(). None = no git root.
    """
    from watercooler_mcp.tools.daemon import _pulse_snapshot_impl

    fake_ctx = MagicMock()

    if manager == "auto":
        manager = MagicMock()
        manager.get_daemon.return_value = daemon

    # Apply safe defaults to any MagicMock daemon so that tests that don't
    # exercise dimension_scores or namespace don't cause JSON serialization
    # failures or incorrect namespace values.
    _daemon_candidate = daemon if daemon is not None else (
        manager.get_daemon.return_value if isinstance(manager, MagicMock) else None
    )
    if isinstance(_daemon_candidate, MagicMock):
        if not isinstance(_daemon_candidate.state_namespace, str):
            _daemon_candidate.state_namespace = ""
        if not isinstance(_daemon_candidate.get_dimension_scores.return_value, (dict, type(None))):
            _daemon_candidate.get_dimension_scores.return_value = None

    if checkpoint is None:
        checkpoint = _empty_checkpoint()

    _git_details = MagicMock()
    _git_details.root = git_root

    cp_mock = checkpoint

    # Lazy imports inside _pulse_snapshot_impl must be patched at their source.
    mock_config = MagicMock()
    if config_enabled is not None:
        mock_config.mcp.daemons.pulse_snapshot.enabled = config_enabled

    with (
        patch("watercooler_mcp.daemons.get_daemon_manager", return_value=manager),
        patch("watercooler_mcp.daemons.state.load_checkpoint", return_value=cp_mock),
        patch("watercooler_mcp.config._discover_git", return_value=_git_details),
        patch("watercooler.config_loader.load_config", return_value=mock_config),
    ):
        result = _pulse_snapshot_impl(fake_ctx, code_path=code_path)

    return json.loads(result)


# ---------------------------------------------------------------------------
# Basic status cases
# ---------------------------------------------------------------------------


def test_tool_invalid_code_path():
    """Non-existent path → error before any daemon or config lookup."""
    result = _call_tool(code_path="/nonexistent/path/xyz")
    assert result["status"] == "error"
    assert result["reason"] == "invalid_code_path"


def test_tool_daemon_not_running(tmp_path):
    """No daemon manager, no checkpoint → daemon_not_running."""
    result = _call_tool(
        code_path=str(tmp_path),
        manager=None,
        config_enabled=True,
    )
    assert result["status"] == "unavailable"
    assert result["reason"] == "daemon_not_running"


def test_tool_config_disabled_ignores_checkpoint(tmp_path):
    """Config explicitly disabled → disabled, even if checkpoint has data."""
    repo_key = derive_repo_key(tmp_path)
    snap = _make_snapshot(repo_key=repo_key)
    cp = _make_checkpoint(repo_key, snap)

    result = _call_tool(
        code_path=str(tmp_path),
        manager=None,
        checkpoint=cp,
        config_enabled=False,
    )
    assert result["status"] == "unavailable"
    assert result["reason"] == "disabled"


def test_tool_no_snapshot_yet(tmp_path):
    """Daemon running but hasn't ticked yet + empty checkpoint → no_snapshot."""
    daemon = MagicMock()
    daemon.get_snapshot.return_value = None
    daemon.status_summary.return_value = {"repo_key": "abc123deadbeef"}

    result = _call_tool(code_path=str(tmp_path), daemon=daemon, config_enabled=True)
    assert result["status"] == "unavailable"
    assert result["reason"] == "no_snapshot"
    assert "daemon_repo_key" in result
    assert result["daemon_repo_key"] == "abc123deadbeef"


def test_tool_returns_snapshot(tmp_path):
    """In-process daemon returns snapshot → status ok."""
    repo_key = derive_repo_key(tmp_path)
    snapshot = _make_snapshot(repo_key=repo_key)
    daemon = MagicMock()
    daemon.get_snapshot.return_value = snapshot

    result = _call_tool(code_path=str(tmp_path), daemon=daemon)
    assert result["status"] == "ok"
    assert result["snapshot"]["snapshot_version"] == "1.0"
    assert "contributors" in result["snapshot"]
    assert "source" not in result  # daemon path doesn't add source


def test_tool_uses_correct_repo_key(tmp_path):
    """get_snapshot() is called with repo_key derived from code_path."""
    snapshot = _make_snapshot()
    daemon = MagicMock()
    daemon.get_snapshot.return_value = snapshot

    _call_tool(code_path=str(tmp_path), daemon=daemon)

    expected_key = derive_repo_key(tmp_path)
    daemon.get_snapshot.assert_called_once_with(expected_key)


def test_tool_snapshot_is_json_serializable(tmp_path):
    """Tool output must be valid JSON."""
    snapshot = _make_snapshot(repo_key=derive_repo_key(tmp_path))
    daemon = MagicMock()
    daemon.get_snapshot.return_value = snapshot

    result = _call_tool(code_path=str(tmp_path), daemon=daemon)
    assert isinstance(result, dict)


def test_tool_two_repos_different_keys(tmp_path):
    """Two different code_path values produce different repo keys."""
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()

    key_a = derive_repo_key(repo_a)
    key_b = derive_repo_key(repo_b)
    assert key_a != key_b

    snap_a = _make_snapshot(repo_key=key_a)
    snap_b = _make_snapshot(repo_key=key_b)

    def _get_snap(key: str):
        if key == key_a:
            return snap_a
        if key == key_b:
            return snap_b
        return None

    daemon = MagicMock()
    daemon.get_snapshot.side_effect = _get_snap
    manager = MagicMock()
    manager.get_daemon.return_value = daemon

    result_a = _call_tool(code_path=str(repo_a), manager=manager)
    result_b = _call_tool(code_path=str(repo_b), manager=manager)

    assert result_a["snapshot"]["repo_key"] == key_a
    assert result_b["snapshot"]["repo_key"] == key_b


# ---------------------------------------------------------------------------
# #492 — Checkpoint fallback (cross-process reads)
# ---------------------------------------------------------------------------


def test_tool_checkpoint_fallback_when_manager_missing_but_feature_enabled(tmp_path):
    """No daemon manager + config enabled + checkpoint has data → status ok."""
    repo_key = derive_repo_key(tmp_path)
    snap = _make_snapshot(repo_key=repo_key)
    cp = _make_checkpoint(repo_key, snap)

    result = _call_tool(
        code_path=str(tmp_path),
        manager=None,
        checkpoint=cp,
        config_enabled=True,
    )
    assert result["status"] == "ok"
    assert result["source"] == "checkpoint"
    assert "age_seconds" in result


def test_tool_checkpoint_fallback_when_daemon_not_in_process(tmp_path):
    """Daemon not in process (manager exists but pulse_snapshot not registered) + checkpoint → ok."""
    repo_key = derive_repo_key(tmp_path)
    snap = _make_snapshot(repo_key=repo_key)
    cp = _make_checkpoint(repo_key, snap)

    manager = MagicMock()
    manager.get_daemon.return_value = None  # not in this process

    result = _call_tool(
        code_path=str(tmp_path),
        manager=manager,
        checkpoint=cp,
        config_enabled=True,
    )
    assert result["status"] == "ok"
    assert result["source"] == "checkpoint"


def test_tool_daemon_present_takes_precedence_over_checkpoint(tmp_path):
    """In-process daemon always wins over checkpoint."""
    repo_key = derive_repo_key(tmp_path)
    daemon_snap = _make_snapshot(repo_key=repo_key, generated_at="2026-01-01T10:00:00+00:00")
    checkpoint_snap = _make_snapshot(repo_key=repo_key, generated_at="2026-01-01T09:00:00+00:00")

    daemon = MagicMock()
    daemon.get_snapshot.return_value = daemon_snap
    cp = _make_checkpoint(repo_key, checkpoint_snap)

    result = _call_tool(code_path=str(tmp_path), daemon=daemon, checkpoint=cp)
    assert result["status"] == "ok"
    assert "source" not in result  # daemon path, no "source" key


def test_tool_checkpoint_returns_age_seconds(tmp_path):
    """Checkpoint-sourced snapshot includes source + age_seconds."""
    repo_key = derive_repo_key(tmp_path)
    now = datetime.now(timezone.utc)
    old = now - timedelta(seconds=300)
    snap = _make_snapshot(repo_key=repo_key, generated_at=old.isoformat())
    cp = _make_checkpoint(repo_key, snap)

    result = _call_tool(
        code_path=str(tmp_path),
        manager=None,
        checkpoint=cp,
        config_enabled=True,
    )
    assert result["status"] == "ok"
    assert result["source"] == "checkpoint"
    assert "age_seconds" in result
    # Should be roughly 300 seconds, allow wide margin for CI timing
    assert 0 < result["age_seconds"] < 600


def test_tool_no_checkpoint_returns_unavailable(tmp_path):
    """No manager, no checkpoint data → unavailable."""
    result = _call_tool(
        code_path=str(tmp_path),
        manager=None,
        checkpoint=_empty_checkpoint(),
        config_enabled=True,
    )
    assert result["status"] == "unavailable"
    assert result["reason"] == "daemon_not_running"


def test_tool_checkpoint_wrong_repo_key(tmp_path):
    """Checkpoint exists for a different repo_key → no_snapshot / daemon_not_running."""
    wrong_key = "wrongkey123456"
    snap = _make_snapshot(repo_key=wrong_key)
    cp = _make_checkpoint(wrong_key, snap)

    result = _call_tool(
        code_path=str(tmp_path),
        manager=None,
        checkpoint=cp,
        config_enabled=True,
    )
    assert result["status"] == "unavailable"


def test_tool_checkpoint_fallback_first_tick(tmp_path):
    """Checkpoint from first tick (last_run=0 since it's the pre-LLM flush) returns ok."""
    repo_key = derive_repo_key(tmp_path)
    now = datetime.now(timezone.utc)
    snap = _make_snapshot(repo_key=repo_key, generated_at=now.isoformat())
    cp = _make_checkpoint(repo_key, snap)
    # last_run is still 0 (BaseDaemon hasn't updated it yet — pre-LLM flush)
    assert cp.last_run == 0.0

    result = _call_tool(
        code_path=str(tmp_path),
        manager=None,
        checkpoint=cp,
        config_enabled=True,
    )
    assert result["status"] == "ok"
    assert result["source"] == "checkpoint"
    assert "age_seconds" in result


# ---------------------------------------------------------------------------
# #489 — Response sanitization (no absolute paths)
# ---------------------------------------------------------------------------


def test_tool_scrubs_report_path_from_response(tmp_path):
    """latest_report_path must not appear in tool response; report_found must be present."""
    repo_key = derive_repo_key(tmp_path)
    snap = _make_snapshot(repo_key=repo_key, report_path="/home/user/project/report.md")
    daemon = MagicMock()
    daemon.get_snapshot.return_value = snap

    result = _call_tool(code_path=str(tmp_path), daemon=daemon)
    analysis = result["snapshot"]["analysis"]
    assert "latest_report_path" not in analysis
    assert analysis["report_found"] is True


def test_tool_scrubs_report_path_when_no_report(tmp_path):
    """When no report path, report_found is False and latest_report_path absent."""
    repo_key = derive_repo_key(tmp_path)
    snap = _make_snapshot(repo_key=repo_key, report_path=None)
    daemon = MagicMock()
    daemon.get_snapshot.return_value = snap

    result = _call_tool(code_path=str(tmp_path), daemon=daemon)
    analysis = result["snapshot"]["analysis"]
    assert "latest_report_path" not in analysis
    assert analysis["report_found"] is False


def test_tool_enrichment_status_not_configured_by_default(tmp_path):
    """Snapshot without LLM fields → enrichment_status: not_configured."""
    repo_key = derive_repo_key(tmp_path)
    snap = _make_snapshot(repo_key=repo_key)
    daemon = MagicMock()
    daemon.get_snapshot.return_value = snap

    result = _call_tool(code_path=str(tmp_path), daemon=daemon)
    assert result["snapshot"]["enrichment_status"] == "not_configured"


def test_tool_enrichment_status_available(tmp_path):
    """Snapshot with llm_enrichment → enrichment_status: available."""
    repo_key = derive_repo_key(tmp_path)
    snap = _make_snapshot(repo_key=repo_key)
    snap["llm_enrichment"] = {"executive_summary": "all good"}
    daemon = MagicMock()
    daemon.get_snapshot.return_value = snap

    result = _call_tool(code_path=str(tmp_path), daemon=daemon)
    assert result["snapshot"]["enrichment_status"] == "available"


def test_tool_enrichment_status_error(tmp_path):
    """Snapshot with llm_enrichment_error → enrichment_status: error."""
    repo_key = derive_repo_key(tmp_path)
    snap = _make_snapshot(repo_key=repo_key)
    snap["llm_enrichment_error"] = "timeout"
    daemon = MagicMock()
    daemon.get_snapshot.return_value = snap

    result = _call_tool(code_path=str(tmp_path), daemon=daemon)
    assert result["snapshot"]["enrichment_status"] == "error"


def test_tool_enrichment_status_pending(tmp_path):
    """Snapshot with _llm_configured=True and no enrichment → enrichment_status: pending."""
    repo_key = derive_repo_key(tmp_path)
    snap = _make_snapshot(repo_key=repo_key)
    snap["_llm_configured"] = True
    daemon = MagicMock()
    daemon.get_snapshot.return_value = snap

    result = _call_tool(code_path=str(tmp_path), daemon=daemon)
    assert result["snapshot"]["enrichment_status"] == "pending"
    assert "_llm_configured" not in result["snapshot"]


# ---------------------------------------------------------------------------
# #489 — _sanitize_snapshot unit tests
# ---------------------------------------------------------------------------


def test_sanitize_snapshot_strips_report_path():
    from watercooler_mcp.tools.daemon import _sanitize_snapshot
    snap = {
        "analysis": {"latest_report_path": "/home/user/report.md", "is_fresh": True},
    }
    result = _sanitize_snapshot(snap)
    assert "latest_report_path" not in result["analysis"]
    assert result["analysis"]["report_found"] is True


def test_sanitize_snapshot_does_not_mutate_original():
    from watercooler_mcp.tools.daemon import _sanitize_snapshot
    snap = {
        "analysis": {"latest_report_path": "/home/user/report.md"},
    }
    _sanitize_snapshot(snap)
    assert snap["analysis"]["latest_report_path"] == "/home/user/report.md"


def test_sanitize_snapshot_removes_llm_configured_flag():
    from watercooler_mcp.tools.daemon import _sanitize_snapshot
    snap = {"analysis": {}, "_llm_configured": True}
    result = _sanitize_snapshot(snap)
    assert "_llm_configured" not in result
    assert result["enrichment_status"] == "pending"


# ---------------------------------------------------------------------------
# #489 — git root resolution
# ---------------------------------------------------------------------------


def test_tool_uses_git_root_for_repo_key(tmp_path):
    """When _discover_git returns a git root, it's used for derive_repo_key."""
    git_root = tmp_path / "git_root"
    git_root.mkdir()

    repo_key = derive_repo_key(git_root)
    snap = _make_snapshot(repo_key=repo_key)
    daemon = MagicMock()
    daemon.get_snapshot.return_value = snap

    # tmp_path is not the git root, but git_root is
    result = _call_tool(
        code_path=str(tmp_path),
        daemon=daemon,
        git_root=git_root,
    )
    assert result["status"] == "ok"
    daemon.get_snapshot.assert_called_once_with(repo_key)


def test_tool_non_git_dir_uses_raw_path(tmp_path):
    """When _discover_git returns no root, raw resolved path is used."""
    repo_key = derive_repo_key(tmp_path)
    snap = _make_snapshot(repo_key=repo_key)
    daemon = MagicMock()
    daemon.get_snapshot.return_value = snap

    # git_root=None → falls through to raw tmp_path
    result = _call_tool(
        code_path=str(tmp_path),
        daemon=daemon,
        git_root=None,
    )
    assert result["status"] == "ok"
    daemon.get_snapshot.assert_called_once_with(repo_key)


# ---------------------------------------------------------------------------
# dimension_scores surfacing (#bug-pulse-snapshot-dimension-scores-not-exposed)
# ---------------------------------------------------------------------------


def test_tool_daemon_path_includes_dimension_scores(tmp_path):
    """In-process daemon: dimension_scores from daemon appear in result."""
    repo_key = derive_repo_key(tmp_path)
    snap = _make_snapshot(repo_key=repo_key)
    scores = _make_dimension_scores()

    daemon = MagicMock()
    daemon.get_snapshot.return_value = snap
    daemon.get_dimension_scores.return_value = scores
    daemon.state_namespace = ""

    result = _call_tool(code_path=str(tmp_path), daemon=daemon)
    assert result["status"] == "ok"
    assert "dimension_scores" in result
    assert result["dimension_scores"]["activity_tempo"]["level_label"] == "active"
    # Scores come from the same source as the snapshot (daemon), not the checkpoint.
    daemon.get_dimension_scores.assert_called_once_with(repo_key)


def test_tool_daemon_path_no_dimension_scores_omits_key(tmp_path):
    """In-process daemon: when get_dimension_scores returns None, key is absent."""
    repo_key = derive_repo_key(tmp_path)
    snap = _make_snapshot(repo_key=repo_key)

    daemon = MagicMock()
    daemon.get_snapshot.return_value = snap
    daemon.get_dimension_scores.return_value = None
    daemon.state_namespace = ""

    result = _call_tool(code_path=str(tmp_path), daemon=daemon)
    assert result["status"] == "ok"
    assert "dimension_scores" not in result


def test_tool_checkpoint_path_includes_dimension_scores(tmp_path):
    """Checkpoint fallback: dimension_scores stored alongside snapshot are surfaced."""
    repo_key = derive_repo_key(tmp_path)
    snap = _make_snapshot(repo_key=repo_key)
    scores = _make_dimension_scores()
    cp = _make_checkpoint(repo_key, snap, dimension_scores=scores)

    result = _call_tool(
        code_path=str(tmp_path),
        manager=None,
        checkpoint=cp,
        config_enabled=True,
    )
    assert result["status"] == "ok"
    assert result["source"] == "checkpoint"
    assert "dimension_scores" in result
    assert result["dimension_scores"]["activity_tempo"]["level_label"] == "active"


def test_tool_checkpoint_path_no_dimension_scores_omits_key(tmp_path):
    """Checkpoint fallback: when dimension_scores absent from checkpoint, key is omitted."""
    repo_key = derive_repo_key(tmp_path)
    snap = _make_snapshot(repo_key=repo_key)
    cp = _make_checkpoint(repo_key, snap)  # no dimension_scores

    result = _call_tool(
        code_path=str(tmp_path),
        manager=None,
        checkpoint=cp,
        config_enabled=True,
    )
    assert result["status"] == "ok"
    assert result["source"] == "checkpoint"
    assert "dimension_scores" not in result


def test_tool_checkpoint_uses_daemon_state_namespace(tmp_path):
    """Checkpoint load uses daemon.state_namespace, not a hardcoded empty string."""
    from watercooler_mcp.tools.daemon import _pulse_snapshot_impl

    repo_key = derive_repo_key(tmp_path)
    snap = _make_snapshot(repo_key=repo_key)
    scores = _make_dimension_scores()
    cp = _make_checkpoint(repo_key, snap, dimension_scores=scores)

    # Daemon is in-process but returns no snapshot — forces checkpoint fallback
    # while still allowing namespace extraction from daemon.state_namespace.
    daemon = MagicMock()
    daemon.get_snapshot.return_value = None
    daemon.get_dimension_scores.return_value = None
    daemon.state_namespace = "scoped-ns"
    daemon.status_summary.return_value = {"repo_key": repo_key}

    manager = MagicMock()
    manager.get_daemon.return_value = daemon

    _git_details = MagicMock()
    _git_details.root = None
    mock_config = MagicMock()
    mock_config.mcp.daemons.pulse_snapshot.enabled = True

    with (
        patch("watercooler_mcp.daemons.get_daemon_manager", return_value=manager),
        patch("watercooler_mcp.config._discover_git", return_value=_git_details),
        patch("watercooler.config_loader.load_config", return_value=mock_config),
        patch("watercooler_mcp.daemons.state.load_checkpoint", return_value=cp) as mock_lc,
        patch("watercooler_mcp.daemons.ensure_hosted_scope_for_current_context"),
    ):
        _pulse_snapshot_impl(MagicMock(), code_path=str(tmp_path))

    # Confirm load_checkpoint was called with namespace from daemon.state_namespace.
    mock_lc.assert_called_once_with("pulse_snapshot", namespace="scoped-ns")


# ---------------------------------------------------------------------------
# D4 Phase 2 — "degraded mode" note never appears in tool output
# ---------------------------------------------------------------------------


def test_tool_d4_notes_no_degraded_mode_string(tmp_path):
    """Tool response: execution_momentum notes must not contain 'degraded mode'."""
    repo_key = derive_repo_key(tmp_path)
    snapshot = _make_snapshot(repo_key=repo_key)

    # Build dimension_scores with execution_momentum present
    dimension_scores = {
        "execution_momentum": {
            "level_score": 0.5,
            "level_label": "mixed",
            "baseline_score": 0.5,
            "trend_delta": 0.0,
            "trend_label": "stable",
            "confidence": 0.8,
            "watch": False,
            "notes": ["Momentum cold-start: no D4-specific signals yet"],
        },
    }
    daemon = MagicMock()
    daemon.get_snapshot.return_value = snapshot
    daemon.get_dimension_scores.return_value = dimension_scores

    result = _call_tool(code_path=str(tmp_path), daemon=daemon)

    d4 = result.get("dimension_scores", {}).get("execution_momentum", {})
    notes = d4.get("notes", [])
    assert not any("degraded mode" in n for n in notes), (
        f"execution_momentum notes must not contain 'degraded mode', got: {notes}"
    )
