from __future__ import annotations

from pathlib import Path
import time

from watercooler.fs import read, write, _backup_file, thread_path, lock_path_for_topic, read_body


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
