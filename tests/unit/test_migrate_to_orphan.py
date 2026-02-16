"""Tests for migrate-to-orphan-branch migration tool.

Validates _migrate_to_orphan_impl and its helpers:
- _parse_commit_footers: extracts entry_id→code_branch from git log
- _tag_graph_entries: adds code_branch to graph JSONL nodes
- _migrate_to_orphan_impl: end-to-end migration (dry_run and live)
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Dict
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> str:
    """Run a git command, raise on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_threads_repo(tmp_path: Path) -> Path:
    """Create a fake separate threads repo with commits and footers."""
    repo = tmp_path / "threads-repo"
    repo.mkdir()
    _git(["init"], repo)
    _git(["config", "user.name", "Test"], repo)
    _git(["config", "user.email", "test@example.com"], repo)

    # Create a thread file and commit with footers
    thread = repo / "design-review.md"
    thread.write_text(
        "# design-review — Thread\n\n"
        "Status: OPEN\nBall: Claude\n\n---\n\n"
        "Entry: Claude (dev) 2025-01-15T10:00:00Z\n"
        "Role: planner\nType: Plan\n"
        "Title: Initial design\n"
        "<!-- Entry-ID: 01ENTRY001 -->\n\n"
        "Design details here.\n"
    )
    _git(["add", "design-review.md"], repo)
    _git(
        [
            "commit",
            "-m",
            "Add design review\n\n"
            "Watercooler-Entry-ID: 01ENTRY001\n"
            "Watercooler-Topic: design-review\n"
            "Code-Branch: feature/auth\n"
            "Code-Commit: abc1234\n",
        ],
        repo,
    )

    # Add a second entry on a different "code branch"
    thread2 = repo / "api-spec.md"
    thread2.write_text(
        "# api-spec — Thread\n\n"
        "Status: OPEN\nBall: -\n\n---\n\n"
        "Entry: Claude (dev) 2025-01-16T10:00:00Z\n"
        "Role: implementer\nType: Note\n"
        "Title: API spec draft\n"
        "<!-- Entry-ID: 01ENTRY002 -->\n\n"
        "API specification content.\n"
    )
    _git(["add", "api-spec.md"], repo)
    _git(
        [
            "commit",
            "-m",
            "Add API spec\n\n"
            "Watercooler-Entry-ID: 01ENTRY002\n"
            "Watercooler-Topic: api-spec\n"
            "Code-Branch: main\n"
            "Code-Commit: def5678\n",
        ],
        repo,
    )

    return repo


def _init_threads_repo_with_graph(tmp_path: Path) -> Path:
    """Create a threads repo with both markdown and graph data."""
    repo = _init_threads_repo(tmp_path)

    # Add graph/baseline data
    graph_dir = repo / "graph" / "baseline" / "threads" / "design-review"
    graph_dir.mkdir(parents=True)

    meta = {"topic": "design-review", "title": "Design Review", "status": "OPEN"}
    (graph_dir / "meta.json").write_text(json.dumps(meta))

    entry_node = {
        "id": "entry:01ENTRY001",
        "type": "entry",
        "entry_id": "01ENTRY001",
        "thread_topic": "design-review",
        "index": 0,
        "agent": "Claude",
        "role": "planner",
        "entry_type": "Plan",
        "title": "Initial design",
        "body": "Design details here.",
        "timestamp": "2025-01-15T10:00:00Z",
    }
    (graph_dir / "entries.jsonl").write_text(json.dumps(entry_node) + "\n")

    _git(["add", "-A"], repo)
    _git(["commit", "-m", "Add graph data"], repo)

    return repo


def _init_code_repo(tmp_path: Path) -> Path:
    """Create a minimal code repo (for orphan branch target)."""
    repo = tmp_path / "code-repo"
    repo.mkdir()
    _git(["init"], repo)
    _git(["config", "user.name", "Test"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    (repo / "README.md").write_text("# Code Repo\n")
    _git(["add", "README.md"], repo)
    _git(["commit", "-m", "Initial commit"], repo)
    return repo


def _git_available() -> bool:
    return shutil.which("git") is not None


# ---------------------------------------------------------------------------
# Tests: _parse_commit_footers
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _git_available(), reason="git not available")
class TestParseCommitFooters:
    """Test extraction of entry_id → code_branch from commit footers."""

    def test_parses_entry_id_and_code_branch(self, tmp_path):
        from watercooler_mcp.tools.migration import _parse_commit_footers

        repo = _init_threads_repo(tmp_path)
        mapping = _parse_commit_footers(repo)

        assert "01ENTRY001" in mapping
        assert mapping["01ENTRY001"] == "feature/auth"
        assert "01ENTRY002" in mapping
        assert mapping["01ENTRY002"] == "main"

    def test_returns_empty_for_no_footers(self, tmp_path):
        from watercooler_mcp.tools.migration import _parse_commit_footers

        repo = tmp_path / "empty-repo"
        repo.mkdir()
        _git(["init"], repo)
        _git(["config", "user.name", "Test"], repo)
        _git(["config", "user.email", "test@example.com"], repo)
        (repo / "file.txt").write_text("hello")
        _git(["add", "file.txt"], repo)
        _git(["commit", "-m", "No footers here"], repo)

        mapping = _parse_commit_footers(repo)
        assert mapping == {}

    def test_handles_nonexistent_path(self):
        from watercooler_mcp.tools.migration import _parse_commit_footers

        mapping = _parse_commit_footers(Path("/nonexistent/repo"))
        assert mapping == {}


# ---------------------------------------------------------------------------
# Tests: _tag_graph_entries
# ---------------------------------------------------------------------------


class TestTagGraphEntries:
    """Test adding code_branch to graph JSONL entry nodes."""

    def test_tags_entries_with_code_branch(self, tmp_path):
        from watercooler_mcp.tools.migration import _tag_graph_entries

        # Create graph structure
        graph_dir = tmp_path / "graph" / "baseline"
        thread_dir = graph_dir / "threads" / "design-review"
        thread_dir.mkdir(parents=True)

        entry_node = {
            "id": "entry:01ENTRY001",
            "entry_id": "01ENTRY001",
            "body": "test",
        }
        (thread_dir / "entries.jsonl").write_text(json.dumps(entry_node) + "\n")

        mapping = {"01ENTRY001": "feature/auth"}
        tagged = _tag_graph_entries(graph_dir, mapping)

        assert tagged == 1

        # Verify the file was updated
        updated = json.loads((thread_dir / "entries.jsonl").read_text().strip())
        assert updated["code_branch"] == "feature/auth"

    def test_skips_already_tagged(self, tmp_path):
        from watercooler_mcp.tools.migration import _tag_graph_entries

        graph_dir = tmp_path / "graph" / "baseline"
        thread_dir = graph_dir / "threads" / "topic-a"
        thread_dir.mkdir(parents=True)

        entry_node = {
            "id": "entry:01X",
            "entry_id": "01X",
            "code_branch": "main",
        }
        (thread_dir / "entries.jsonl").write_text(json.dumps(entry_node) + "\n")

        mapping = {"01X": "feature/override"}
        tagged = _tag_graph_entries(graph_dir, mapping)

        assert tagged == 0
        # Should keep original value
        updated = json.loads((thread_dir / "entries.jsonl").read_text().strip())
        assert updated["code_branch"] == "main"

    def test_no_graph_dir(self, tmp_path):
        from watercooler_mcp.tools.migration import _tag_graph_entries

        # Non-existent graph dir
        tagged = _tag_graph_entries(tmp_path / "nonexistent", {"x": "y"})
        assert tagged == 0

    def test_handles_multiple_entries(self, tmp_path):
        from watercooler_mcp.tools.migration import _tag_graph_entries

        graph_dir = tmp_path / "graph" / "baseline"
        thread_dir = graph_dir / "threads" / "multi"
        thread_dir.mkdir(parents=True)

        entries = [
            {"id": "entry:A", "entry_id": "A", "body": "first"},
            {"id": "entry:B", "entry_id": "B", "body": "second"},
            {"id": "entry:C", "entry_id": "C", "body": "third", "code_branch": "existing"},
        ]
        content = "\n".join(json.dumps(e) for e in entries) + "\n"
        (thread_dir / "entries.jsonl").write_text(content)

        mapping = {"A": "feature/x", "B": "feature/y", "C": "feature/z"}
        tagged = _tag_graph_entries(graph_dir, mapping)

        # A and B should be tagged, C already has code_branch
        assert tagged == 2

        lines = (thread_dir / "entries.jsonl").read_text().strip().split("\n")
        nodes = [json.loads(line) for line in lines]
        assert nodes[0]["code_branch"] == "feature/x"
        assert nodes[1]["code_branch"] == "feature/y"
        assert nodes[2]["code_branch"] == "existing"  # Unchanged


# ---------------------------------------------------------------------------
# Tests: _migrate_to_orphan_impl (dry run)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _git_available(), reason="git not available")
class TestMigrateToOrphanDryRun:
    """Test dry-run mode of the migration."""

    def test_dry_run_reports_files(self, tmp_path):
        from watercooler_mcp.tools.migration import _migrate_to_orphan_impl

        threads_repo = _init_threads_repo(tmp_path)
        code_repo = _init_code_repo(tmp_path)

        result_json = _migrate_to_orphan_impl(
            code_path=str(code_repo),
            threads_repo_path=str(threads_repo),
            dry_run=True,
        )
        result = json.loads(result_json)

        assert result["success"] is True
        assert result["dry_run"] is True
        assert result["threads_found"] == 2  # design-review.md, api-spec.md
        assert result["entry_branch_mappings"] == 2
        assert "thread_files" in result

    def test_dry_run_with_graph(self, tmp_path):
        from watercooler_mcp.tools.migration import _migrate_to_orphan_impl

        threads_repo = _init_threads_repo_with_graph(tmp_path)
        code_repo = _init_code_repo(tmp_path)

        result_json = _migrate_to_orphan_impl(
            code_path=str(code_repo),
            threads_repo_path=str(threads_repo),
            dry_run=True,
        )
        result = json.loads(result_json)

        assert result["success"] is True
        assert result["threads_found"] == 2
        assert result["graph_files_found"] >= 2  # meta.json + entries.jsonl

    def test_invalid_threads_repo(self, tmp_path):
        from watercooler_mcp.tools.migration import _migrate_to_orphan_impl

        code_repo = _init_code_repo(tmp_path)

        result_json = _migrate_to_orphan_impl(
            code_path=str(code_repo),
            threads_repo_path=str(tmp_path / "nonexistent"),
            dry_run=True,
        )
        result = json.loads(result_json)

        assert result["success"] is False
        assert "does not exist" in result["error"]

    def test_invalid_code_repo(self, tmp_path):
        from watercooler_mcp.tools.migration import _migrate_to_orphan_impl

        threads_repo = _init_threads_repo(tmp_path)

        result_json = _migrate_to_orphan_impl(
            code_path=str(tmp_path / "nonexistent"),
            threads_repo_path=str(threads_repo),
            dry_run=True,
        )
        result = json.loads(result_json)

        assert result["success"] is False
        assert "does not exist" in result["error"]


# ---------------------------------------------------------------------------
# Tests: _migrate_to_orphan_impl (live run with mocked worktree)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _git_available(), reason="git not available")
class TestMigrateToOrphanLive:
    """Test live migration with mocked _ensure_worktree."""

    def test_copies_files_to_worktree(self, tmp_path):
        from watercooler_mcp.tools.migration import _migrate_to_orphan_impl

        threads_repo = _init_threads_repo_with_graph(tmp_path)
        code_repo = _init_code_repo(tmp_path)

        # Create a fake worktree directory (mock _ensure_worktree)
        fake_wt = tmp_path / "worktree"
        fake_wt.mkdir()
        _git(["init"], fake_wt)
        _git(["config", "user.name", "Test"], fake_wt)
        _git(["config", "user.email", "test@example.com"], fake_wt)

        with patch(
            "watercooler_mcp.config._ensure_worktree",
            return_value=fake_wt,
        ), patch(
            "watercooler_mcp.config.ORPHAN_BRANCH_NAME",
            "main",
        ):
            result_json = _migrate_to_orphan_impl(
                code_path=str(code_repo),
                threads_repo_path=str(threads_repo),
                dry_run=False,
            )

        result = json.loads(result_json)

        assert result["threads_copied"] == 2
        assert result["graph_files_copied"] >= 2
        assert result["entries_tagged"] == 1  # 01ENTRY001 tagged

        # Verify files exist in worktree
        assert (fake_wt / "design-review.md").exists()
        assert (fake_wt / "api-spec.md").exists()
        assert (fake_wt / "graph" / "baseline" / "threads" / "design-review" / "entries.jsonl").exists()

        # Verify graph entry was tagged
        entry_line = (
            fake_wt / "graph" / "baseline" / "threads" / "design-review" / "entries.jsonl"
        ).read_text().strip()
        entry = json.loads(entry_line)
        assert entry["code_branch"] == "feature/auth"

    def test_handles_worktree_creation_failure(self, tmp_path):
        from watercooler_mcp.tools.migration import _migrate_to_orphan_impl

        threads_repo = _init_threads_repo(tmp_path)
        code_repo = _init_code_repo(tmp_path)

        with patch(
            "watercooler_mcp.config._ensure_worktree",
            return_value=None,
        ):
            result_json = _migrate_to_orphan_impl(
                code_path=str(code_repo),
                threads_repo_path=str(threads_repo),
                dry_run=False,
            )

        result = json.loads(result_json)
        assert result["success"] is False
        assert "worktree" in result["error"].lower()


# ---------------------------------------------------------------------------
# Tests: MCP tool wrapper
# ---------------------------------------------------------------------------


class TestOrphanMigrateWrapper:
    """Test the MCP tool wrapper validation."""

    @staticmethod
    def _get_registered_tools():
        """Register tools into a mock and return the captured functions."""
        from watercooler_mcp.tools.migration import register_migration_tools

        mcp = MagicMock()
        registered = {}

        def fake_tool(name):
            def decorator(fn):
                registered[name] = fn
                return fn
            return decorator

        mcp.tool = fake_tool
        register_migration_tools(mcp)
        return registered

    def test_rejects_missing_code_path(self):
        """The wrapper should reject calls without code_path."""
        import asyncio

        registered = self._get_registered_tools()
        wrapper = registered["watercooler_migrate_to_orphan_branch"]

        loop = asyncio.new_event_loop()
        try:
            result_json = loop.run_until_complete(
                wrapper(MagicMock(), code_path="", threads_repo_path="/some/path")
            )
        finally:
            loop.close()

        result = json.loads(result_json)
        assert result["success"] is False
        assert "code_path" in result["error"]

    def test_rejects_missing_threads_repo_path(self):
        """The wrapper should reject calls without threads_repo_path."""
        import asyncio

        registered = self._get_registered_tools()
        wrapper = registered["watercooler_migrate_to_orphan_branch"]

        loop = asyncio.new_event_loop()
        try:
            result_json = loop.run_until_complete(
                wrapper(MagicMock(), code_path="/some/path", threads_repo_path="")
            )
        finally:
            loop.close()

        result = json.loads(result_json)
        assert result["success"] is False
        assert "threads_repo_path" in result["error"]
