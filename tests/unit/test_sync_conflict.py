"""Tests for sync/conflict.py - conflict detection and resolution."""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from watercooler_mcp.sync import (
    # Enums
    ConflictType,
    ConflictScope,
    # Data classes
    ConflictInfo,
    # Classes
    ConflictResolver,
    # Pure merge functions
    merge_manifest_content,
    merge_jsonl_content,
    merge_thread_content,
    # Convenience functions
    has_graph_conflicts_only,
    has_thread_conflicts_only,
)


# =============================================================================
# Enum Tests
# =============================================================================


class TestConflictType:
    """Tests for ConflictType enum."""

    def test_all_types_exist(self):
        """All expected conflict types should exist."""
        expected = ["NONE", "MERGE", "REBASE", "CHERRY_PICK"]
        for name in expected:
            assert hasattr(ConflictType, name)

    def test_values_are_strings(self):
        """Conflict type values should be lowercase strings."""
        for ct in ConflictType:
            assert isinstance(ct.value, str)
            assert ct.value == ct.value.lower()


class TestConflictScope:
    """Tests for ConflictScope enum."""

    def test_all_scopes_exist(self):
        """All expected conflict scopes should exist."""
        expected = ["NONE", "GRAPH_ONLY", "THREAD_ONLY", "MIXED"]
        for name in expected:
            assert hasattr(ConflictScope, name)


# =============================================================================
# ConflictInfo Tests
# =============================================================================


class TestConflictInfo:
    """Tests for ConflictInfo dataclass."""

    def test_default_values(self):
        """ConflictInfo should have sensible defaults."""
        info = ConflictInfo()
        assert info.conflict_type == ConflictType.NONE
        assert info.scope == ConflictScope.NONE
        assert info.conflicting_files == []
        assert info.can_auto_resolve is False
        assert info.resolution_strategy is None

    def test_has_conflicts_false_when_empty(self):
        """has_conflicts should return False when no files."""
        info = ConflictInfo()
        assert info.has_conflicts is False

    def test_has_conflicts_true_when_files(self):
        """has_conflicts should return True when files present."""
        info = ConflictInfo(conflicting_files=["file.md"])
        assert info.has_conflicts is True

    def test_full_info(self):
        """ConflictInfo should store all fields."""
        info = ConflictInfo(
            conflict_type=ConflictType.REBASE,
            scope=ConflictScope.THREAD_ONLY,
            conflicting_files=["topic.md", "other.md"],
            can_auto_resolve=True,
            resolution_strategy="merge_by_entry_id",
        )
        assert info.conflict_type == ConflictType.REBASE
        assert info.scope == ConflictScope.THREAD_ONLY
        assert len(info.conflicting_files) == 2
        assert info.can_auto_resolve is True
        assert info.resolution_strategy == "merge_by_entry_id"


# =============================================================================
# Pure Merge Function Tests
# =============================================================================


class TestMergeManifestContent:
    """Tests for merge_manifest_content function."""

    def test_merge_takes_newer_timestamp(self):
        """Merged manifest should have the newer timestamp."""
        ours = json.dumps({
            "version": "1.0",
            "last_updated": "2025-01-01T00:00:00Z",
            "topics_synced": {"topic-a": True},
        })
        theirs = json.dumps({
            "version": "1.0",
            "last_updated": "2025-01-02T00:00:00Z",
            "topics_synced": {"topic-b": True},
        })
        result = merge_manifest_content(ours, theirs)
        merged = json.loads(result)
        assert merged["last_updated"] == "2025-01-02T00:00:00Z"

    def test_merge_combines_topics(self):
        """Merged manifest should have topics from both versions."""
        ours = json.dumps({
            "version": "1.0",
            "last_updated": "2025-01-01T00:00:00Z",
            "topics_synced": {"topic-a": True},
        })
        theirs = json.dumps({
            "version": "1.0",
            "last_updated": "2025-01-02T00:00:00Z",
            "topics_synced": {"topic-b": True},
        })
        result = merge_manifest_content(ours, theirs)
        merged = json.loads(result)
        assert "topic-a" in merged["topics_synced"]
        assert "topic-b" in merged["topics_synced"]

    def test_merge_theirs_overwrites_same_topic(self):
        """When same topic exists, theirs should overwrite ours."""
        ours = json.dumps({
            "version": "1.0",
            "last_updated": "2025-01-01T00:00:00Z",
            "topics_synced": {"topic-a": False},
        })
        theirs = json.dumps({
            "version": "1.0",
            "last_updated": "2025-01-02T00:00:00Z",
            "topics_synced": {"topic-a": True},
        })
        result = merge_manifest_content(ours, theirs)
        merged = json.loads(result)
        assert merged["topics_synced"]["topic-a"] is True

    def test_merge_preserves_extra_fields(self):
        """Merged manifest should preserve extra fields from ours."""
        ours = json.dumps({
            "version": "1.0",
            "last_updated": "2025-01-01T00:00:00Z",
            "topics_synced": {},
            "custom_field": "preserved",
        })
        theirs = json.dumps({
            "version": "1.0",
            "last_updated": "2025-01-02T00:00:00Z",
            "topics_synced": {},
        })
        result = merge_manifest_content(ours, theirs)
        merged = json.loads(result)
        assert merged["custom_field"] == "preserved"


class TestMergeJsonlContent:
    """Tests for merge_jsonl_content function."""

    def test_merge_deduplicates_by_uuid(self):
        """Merged JSONL should deduplicate entries by UUID."""
        ours = '{"uuid": "abc", "data": "ours"}\n{"uuid": "def", "data": "unique"}\n'
        theirs = '{"uuid": "abc", "data": "theirs"}\n{"uuid": "ghi", "data": "other"}\n'
        result = merge_jsonl_content(ours, theirs)
        lines = [json.loads(line) for line in result.strip().split("\n")]
        uuids = [l["uuid"] for l in lines]
        assert len(uuids) == 3
        assert "abc" in uuids
        assert "def" in uuids
        assert "ghi" in uuids

    def test_merge_uses_first_occurrence(self):
        """When duplicate UUID, ours (first) should be kept."""
        ours = '{"uuid": "abc", "data": "ours"}\n'
        theirs = '{"uuid": "abc", "data": "theirs"}\n'
        result = merge_jsonl_content(ours, theirs)
        lines = [json.loads(line) for line in result.strip().split("\n")]
        assert len(lines) == 1
        assert lines[0]["data"] == "ours"

    def test_merge_handles_id_field(self):
        """Merge should also deduplicate by 'id' field if 'uuid' missing."""
        ours = '{"id": "abc", "data": "ours"}\n'
        theirs = '{"id": "abc", "data": "theirs"}\n{"id": "def", "data": "other"}\n'
        result = merge_jsonl_content(ours, theirs)
        lines = [json.loads(line) for line in result.strip().split("\n")]
        ids = [l["id"] for l in lines]
        assert len(ids) == 2
        assert "abc" in ids
        assert "def" in ids

    def test_merge_handles_empty_lines(self):
        """Merge should skip empty lines."""
        ours = '{"uuid": "abc"}\n\n{"uuid": "def"}\n'
        theirs = '\n{"uuid": "ghi"}\n\n'
        result = merge_jsonl_content(ours, theirs)
        lines = [json.loads(line) for line in result.strip().split("\n") if line]
        assert len(lines) == 3

    def test_merge_returns_empty_for_empty_input(self):
        """Merge of empty inputs should return empty."""
        result = merge_jsonl_content("", "")
        assert result == ""


class TestMergeThreadContent:
    """Tests for merge_thread_content function."""

    def test_merge_non_overlapping_entries(self):
        """Non-overlapping entries should merge successfully."""
        ours = """# Thread: Test
Status: OPEN

---

Entry: Agent1 2025-01-01T00:00:00Z
Role: implementer
Title: First entry
<!-- Entry-ID: 01ABC -->

Body of first entry.
"""
        theirs = """# Thread: Test
Status: OPEN

---

Entry: Agent2 2025-01-02T00:00:00Z
Role: planner
Title: Second entry
<!-- Entry-ID: 01DEF -->

Body of second entry.
"""
        merged, had_conflicts = merge_thread_content(ours, theirs)
        assert had_conflicts is False
        assert "01ABC" in merged or "First entry" in merged
        assert "01DEF" in merged or "Second entry" in merged

    def test_merge_same_entry_same_content(self):
        """Same Entry-ID with same content should not conflict."""
        entry = """# Thread: Test
Status: OPEN

---

Entry: Agent1 2025-01-01T00:00:00Z
Role: implementer
Title: Same entry
<!-- Entry-ID: 01ABC -->

Same body content.
"""
        merged, had_conflicts = merge_thread_content(entry, entry)
        assert had_conflicts is False
        assert "Same body content" in merged

    def test_merge_detects_body_conflict(self):
        """Same Entry-ID with different body should conflict."""
        ours = """# Thread: Test

---

Entry: Agent1 2025-01-01T00:00:00Z
<!-- Entry-ID: 01ABC -->

Ours body.
"""
        theirs = """# Thread: Test

---

Entry: Agent1 2025-01-01T00:00:00Z
<!-- Entry-ID: 01ABC -->

Different body.
"""
        merged, had_conflicts = merge_thread_content(ours, theirs)
        assert had_conflicts is True
        assert merged == ""

    def test_merge_detects_title_conflict(self):
        """Same Entry-ID with different title should conflict."""
        ours = """# Thread: Test

---

Entry: Agent1 2025-01-01T00:00:00Z
Title: Title One
<!-- Entry-ID: 01ABC -->

Same body.
"""
        theirs = """# Thread: Test

---

Entry: Agent1 2025-01-01T00:00:00Z
Title: Title Two
<!-- Entry-ID: 01ABC -->

Same body.
"""
        merged, had_conflicts = merge_thread_content(ours, theirs)
        assert had_conflicts is True


# =============================================================================
# ConflictResolver Tests
# =============================================================================


class TestConflictResolver:
    """Tests for ConflictResolver class."""

    def test_detect_no_conflicts(self):
        """detect() should return NONE when no conflicts."""
        repo = MagicMock()
        repo.git_dir = "/tmp/.git"
        repo.git.status.return_value = "M  file.py\n"

        resolver = ConflictResolver(repo)
        info = resolver.detect()

        assert info.conflict_type == ConflictType.NONE
        assert not info.has_conflicts

    def test_detect_merge_conflict(self, tmp_path):
        """detect() should identify merge conflicts."""
        repo = MagicMock()
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "MERGE_HEAD").touch()
        repo.git_dir = str(git_dir)
        repo.git.status.return_value = "UU file.md\n"

        resolver = ConflictResolver(repo)
        info = resolver.detect()

        assert info.conflict_type == ConflictType.MERGE
        assert info.has_conflicts

    def test_detect_rebase_conflict(self, tmp_path):
        """detect() should identify rebase conflicts."""
        repo = MagicMock()
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "rebase-merge").mkdir()
        repo.git_dir = str(git_dir)
        repo.git.status.return_value = "UU topic.md\n"

        resolver = ConflictResolver(repo)
        info = resolver.detect()

        assert info.conflict_type == ConflictType.REBASE
        assert info.has_conflicts

    def test_detect_graph_only_scope(self, tmp_path):
        """detect() should identify graph-only conflicts."""
        repo = MagicMock()
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "MERGE_HEAD").touch()
        repo.git_dir = str(git_dir)
        repo.git.status.return_value = "UU graph/baseline/nodes.jsonl\nUU graph/baseline/edges.jsonl\n"

        resolver = ConflictResolver(repo)
        info = resolver.detect()

        assert info.scope == ConflictScope.GRAPH_ONLY
        assert info.can_auto_resolve is True
        assert info.resolution_strategy == "deduplicate_by_uuid"

    def test_detect_thread_only_scope(self, tmp_path):
        """detect() should identify thread-only conflicts."""
        repo = MagicMock()
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "MERGE_HEAD").touch()
        repo.git_dir = str(git_dir)
        repo.git.status.return_value = "UU topic.md\nUU other.md\n"

        resolver = ConflictResolver(repo)
        info = resolver.detect()

        assert info.scope == ConflictScope.THREAD_ONLY
        assert info.can_auto_resolve is True
        assert info.resolution_strategy == "merge_by_entry_id"

    def test_detect_mixed_scope(self, tmp_path):
        """detect() should identify mixed conflicts."""
        repo = MagicMock()
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "MERGE_HEAD").touch()
        repo.git_dir = str(git_dir)
        repo.git.status.return_value = "UU topic.md\nUU graph/baseline/nodes.jsonl\n"

        resolver = ConflictResolver(repo)
        info = resolver.detect()

        assert info.scope == ConflictScope.MIXED
        assert info.can_auto_resolve is False

    def test_can_auto_resolve_graph(self, tmp_path):
        """can_auto_resolve should return True for graph-only."""
        repo = MagicMock()
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "MERGE_HEAD").touch()
        repo.git_dir = str(git_dir)
        repo.git.status.return_value = "UU graph/baseline/manifest.json\n"

        resolver = ConflictResolver(repo)
        assert resolver.can_auto_resolve() is True

    def test_can_auto_resolve_thread(self, tmp_path):
        """can_auto_resolve should return True for thread-only."""
        repo = MagicMock()
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "MERGE_HEAD").touch()
        repo.git_dir = str(git_dir)
        repo.git.status.return_value = "UU topic.md\n"

        resolver = ConflictResolver(repo)
        assert resolver.can_auto_resolve() is True

    def test_abort_merge(self, tmp_path):
        """abort_resolution should abort merge."""
        repo = MagicMock()
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "MERGE_HEAD").touch()
        repo.git_dir = str(git_dir)

        resolver = ConflictResolver(repo)
        result = resolver.abort_resolution()

        assert result is True
        repo.git.merge.assert_called_with("--abort")

    def test_abort_rebase(self, tmp_path):
        """abort_resolution should abort rebase."""
        repo = MagicMock()
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "rebase-merge").mkdir()
        repo.git_dir = str(git_dir)

        resolver = ConflictResolver(repo)
        result = resolver.abort_resolution()

        assert result is True
        repo.git.rebase.assert_called_with("--abort")


# =============================================================================
# Convenience Function Tests
# =============================================================================


class TestConvenienceFunctions:
    """Tests for convenience functions."""

    def test_has_graph_conflicts_only_true(self, tmp_path):
        """has_graph_conflicts_only returns True for graph conflicts."""
        repo = MagicMock()
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "MERGE_HEAD").touch()
        repo.git_dir = str(git_dir)
        repo.git.status.return_value = "UU graph/baseline/nodes.jsonl\n"

        assert has_graph_conflicts_only(repo) is True

    def test_has_graph_conflicts_only_false_for_thread(self, tmp_path):
        """has_graph_conflicts_only returns False for thread conflicts."""
        repo = MagicMock()
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "MERGE_HEAD").touch()
        repo.git_dir = str(git_dir)
        repo.git.status.return_value = "UU topic.md\n"

        assert has_graph_conflicts_only(repo) is False

    def test_has_thread_conflicts_only_true(self, tmp_path):
        """has_thread_conflicts_only returns True for thread conflicts."""
        repo = MagicMock()
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "MERGE_HEAD").touch()
        repo.git_dir = str(git_dir)
        repo.git.status.return_value = "UU topic.md\n"

        assert has_thread_conflicts_only(repo) is True

    def test_has_thread_conflicts_only_false_for_graph(self, tmp_path):
        """has_thread_conflicts_only returns False for graph conflicts."""
        repo = MagicMock()
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "MERGE_HEAD").touch()
        repo.git_dir = str(git_dir)
        repo.git.status.return_value = "UU graph/baseline/nodes.jsonl\n"

        assert has_thread_conflicts_only(repo) is False
