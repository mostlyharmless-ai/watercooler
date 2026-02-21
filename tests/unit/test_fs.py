from __future__ import annotations

from pathlib import Path
import time

from watercooler.fs import (
    read,
    write,
    _backup_file,
    thread_path,
    lock_path_for_topic,
    read_body,
    THREAD_CATEGORIES,
    has_structured_layout,
    ensure_directory_structure,
    migrate_to_structured_layout,
    discover_thread_files,
    find_thread_path,
)


def test_read_write_roundtrip(tmp_path: Path):
    p = tmp_path / "a.txt"
    write(p, "hello")
    assert read(p) == "hello"


def test_backup_rotation(tmp_path: Path):
    p = tmp_path / "t.md"
    write(p, "v1")
    _backup_file(p, keep=2, topic="t")
    time.sleep(0.01)
    write(p, "v2")
    _backup_file(p, keep=2, topic="t")
    time.sleep(0.01)
    write(p, "v3")
    _backup_file(p, keep=2, topic="t")
    backups = sorted((tmp_path / ".backups").glob("t.*.md"))
    assert len(backups) == 2


def test_paths(tmp_path: Path):
    tp = thread_path("topic/name", tmp_path)
    assert tp.name.endswith("topic-name.md")
    lp = lock_path_for_topic("topic", tmp_path)
    assert lp.name == ".topic.lock"


def test_paths_sanitize_illegal_characters(tmp_path: Path):
    tp = thread_path("gh:caleb/watercooler", tmp_path)
    assert tp.name == "gh-caleb-watercooler.md"
    lp = lock_path_for_topic("gh:caleb/watercooler", tmp_path)
    assert lp.name == ".gh-caleb-watercooler.lock"


def test_read_body_string_or_file(tmp_path: Path):
    assert read_body(None) == ""
    assert read_body("plain text") == "plain text"
    p = tmp_path / "b.txt"
    write(p, "file body")
    assert read_body(str(p)) == "file body"
    assert read_body(f"@{p}") == "file body"
    missing = tmp_path / "missing.txt"
    assert read_body(f"@{missing}") == f"@{missing}"


# =============================================================================
# Structured directory layout tests
# =============================================================================


class TestHasStructuredLayout:
    def test_false_for_flat_layout(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        assert has_structured_layout(threads_dir) is False

    def test_true_when_threads_subdir_exists(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        (threads_dir / "threads").mkdir(parents=True)
        assert has_structured_layout(threads_dir) is True


class TestEnsureDirectoryStructure:
    def test_creates_all_directories(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        created = ensure_directory_structure(threads_dir)

        # All THREAD_CATEGORIES should exist
        for cat in THREAD_CATEGORIES:
            assert (threads_dir / cat).is_dir()

        # Non-thread dirs should exist too
        assert (threads_dir / "compound" / "learnings").is_dir()
        assert (threads_dir / "compound" / "reports").is_dir()
        assert (threads_dir / "compound" / "suggestions").is_dir()
        assert (threads_dir / "logs" / "agent").is_dir()
        assert (threads_dir / "logs" / "mcp").is_dir()
        assert (threads_dir / ".watercooler").is_dir()

        assert len(created) > 0

    def test_idempotent(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        first = ensure_directory_structure(threads_dir)
        second = ensure_directory_structure(threads_dir)
        assert len(first) > 0
        assert len(second) == 0  # nothing new to create


class TestMigrateToStructuredLayout:
    def test_moves_root_md_to_threads_subdir(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        (threads_dir / "my-topic.md").write_text("# content")
        (threads_dir / "other.md").write_text("# other")

        moved = migrate_to_structured_layout(threads_dir)

        assert len(moved) == 2
        # Files should be in threads/ now
        assert (threads_dir / "threads" / "my-topic.md").exists()
        assert (threads_dir / "threads" / "other.md").exists()
        # Old locations should be gone
        assert not (threads_dir / "my-topic.md").exists()
        assert not (threads_dir / "other.md").exists()

    def test_skips_hidden_files(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        (threads_dir / ".hidden.md").write_text("# hidden")
        (threads_dir / "_internal.md").write_text("# internal")
        (threads_dir / "normal.md").write_text("# normal")

        moved = migrate_to_structured_layout(threads_dir)

        assert len(moved) == 1
        assert moved[0][0].name == "normal.md"
        # Hidden files should remain at root
        assert (threads_dir / ".hidden.md").exists()
        assert (threads_dir / "_internal.md").exists()

    def test_idempotent(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        (threads_dir / "topic.md").write_text("# content")

        first = migrate_to_structured_layout(threads_dir)
        second = migrate_to_structured_layout(threads_dir)

        assert len(first) == 1
        assert len(second) == 0  # already moved

    def test_skips_collision(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        (threads_dir / "threads").mkdir(parents=True)
        (threads_dir / "topic.md").write_text("# root version")
        (threads_dir / "threads" / "topic.md").write_text("# subdir version")

        moved = migrate_to_structured_layout(threads_dir)

        assert len(moved) == 0  # collision, not moved
        assert (threads_dir / "topic.md").exists()  # root file kept
        # Subdir version unchanged
        assert (threads_dir / "threads" / "topic.md").read_text() == "# subdir version"


class TestThreadPathStructured:
    def test_new_thread_routes_to_threads_subdir(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        ensure_directory_structure(threads_dir)

        tp = thread_path("new-topic", threads_dir)
        assert tp == threads_dir / "threads" / "new-topic.md"

    def test_existing_root_thread_found(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        ensure_directory_structure(threads_dir)
        # Place a file at root (legacy)
        (threads_dir / "legacy.md").write_text("# legacy")

        tp = thread_path("legacy", threads_dir)
        assert tp == threads_dir / "legacy.md"

    def test_existing_in_category_subdir(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        ensure_directory_structure(threads_dir)
        (threads_dir / "closed" / "done.md").write_text("# done")

        tp = thread_path("done", threads_dir)
        assert tp == threads_dir / "closed" / "done.md"

    def test_flat_layout_routes_to_root(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        # No structured layout

        tp = thread_path("topic", threads_dir)
        assert tp == threads_dir / "topic.md"


class TestDiscoverThreadFilesStructured:
    def test_finds_files_in_categories(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        ensure_directory_structure(threads_dir)
        (threads_dir / "threads" / "a.md").write_text("# a")
        (threads_dir / "closed" / "b.md").write_text("# b")

        found = discover_thread_files(threads_dir)
        names = [p.name for p in found]
        assert "a.md" in names
        assert "b.md" in names

    def test_ignores_non_category_dirs(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        ensure_directory_structure(threads_dir)
        (threads_dir / "threads" / "a.md").write_text("# a")
        # Put a file in compound/ (not a thread category)
        (threads_dir / "compound" / "reports" / "report.md").write_text("# report")

        found = discover_thread_files(threads_dir)
        names = [p.name for p in found]
        assert "a.md" in names
        assert "report.md" not in names

    def test_category_filter(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        ensure_directory_structure(threads_dir)
        (threads_dir / "threads" / "a.md").write_text("# a")
        (threads_dir / "closed" / "b.md").write_text("# b")

        found = discover_thread_files(threads_dir, category="closed")
        names = [p.name for p in found]
        assert names == ["b.md"]

    def test_flat_layout_backward_compat(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        (threads_dir / "a.md").write_text("# a")
        sub = threads_dir / "custom"
        sub.mkdir()
        (sub / "b.md").write_text("# b")

        found = discover_thread_files(threads_dir)
        names = [p.name for p in found]
        assert "a.md" in names
        assert "b.md" in names

    def test_flat_layout_skips_graph_dir(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        (threads_dir / "a.md").write_text("# a")
        graph = threads_dir / "graph"
        graph.mkdir()
        (graph / "meta.md").write_text("# not a thread")

        found = discover_thread_files(threads_dir)
        names = [p.name for p in found]
        assert "a.md" in names
        assert "meta.md" not in names


class TestFindThreadPathStructured:
    def test_finds_root(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        ensure_directory_structure(threads_dir)
        (threads_dir / "root-topic.md").write_text("# root")

        result = find_thread_path("root-topic", threads_dir)
        assert result == threads_dir / "root-topic.md"

    def test_finds_in_category(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        ensure_directory_structure(threads_dir)
        (threads_dir / "debug" / "bug-123.md").write_text("# bug")

        result = find_thread_path("bug-123", threads_dir)
        assert result == threads_dir / "debug" / "bug-123.md"

    def test_returns_none_when_missing(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        ensure_directory_structure(threads_dir)

        result = find_thread_path("nonexistent", threads_dir)
        assert result is None

    def test_structured_does_not_search_non_category(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        ensure_directory_structure(threads_dir)
        # Put file in compound/ — should NOT be found
        (threads_dir / "compound" / "reports" / "report.md").write_text("# report")

        result = find_thread_path("report", threads_dir)
        assert result is None
