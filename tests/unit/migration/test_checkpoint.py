"""Unit tests for the migration Checkpoint."""

from __future__ import annotations

from pathlib import Path

import pytest

from watercooler.migration.checkpoint import Checkpoint


class TestCheckpoint:
    def test_empty_when_file_missing(self, tmp_path: Path) -> None:
        ck = Checkpoint(tmp_path / "nope.jsonl")
        assert len(ck) == 0
        assert "x" not in ck

    def test_loads_existing_entries(self, tmp_path: Path) -> None:
        p = tmp_path / "cp.jsonl"
        p.write_text("E1\nE2\n  \nE3\n")
        ck = Checkpoint(p)
        assert len(ck) == 3
        assert "E1" in ck
        assert "E2" in ck
        assert "E3" in ck
        assert "E4" not in ck

    def test_add_appends_and_caches(self, tmp_path: Path) -> None:
        p = tmp_path / "cp.jsonl"
        ck = Checkpoint(p)
        ck.add("E1")
        ck.add("E2")
        assert len(ck) == 2
        assert "E1" in ck
        assert p.read_text() == "E1\nE2\n"

    def test_add_idempotent(self, tmp_path: Path) -> None:
        ck = Checkpoint(tmp_path / "cp.jsonl")
        ck.add("E1")
        ck.add("E1")  # second add is a no-op
        assert len(ck) == 1
        assert (tmp_path / "cp.jsonl").read_text() == "E1\n"

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        deep = tmp_path / "deep" / "nested" / "cp.jsonl"
        ck = Checkpoint(deep)
        ck.add("E1")
        assert deep.exists()

    def test_reset(self, tmp_path: Path) -> None:
        p = tmp_path / "cp.jsonl"
        ck = Checkpoint(p)
        ck.add("E1")
        ck.add("E2")
        ck.reset()
        assert len(ck) == 0
        assert not p.exists()
