"""Tests for sync_repair.py — derived-only dirt classification and auto-clean."""

import pytest
from pathlib import Path

from watercooler.sync_repair import (
    DERIVED_FILE_PATTERNS,
    DiagnosticReport,
    _parse_porcelain_filename,
)


class TestDerivedFilePatterns:
    """Verify DERIVED_FILE_PATTERNS contains expected entries."""

    def test_annotation_state_is_derived(self):
        assert "annotation_state.json" in DERIVED_FILE_PATTERNS


class TestDirtyDerivedOnly:
    """Test DiagnosticReport.dirty_derived_only property."""

    def test_no_dirty_files(self):
        report = DiagnosticReport(dirty_files=[])
        assert report.dirty_derived_only is False

    def test_all_derived(self):
        report = DiagnosticReport(dirty_files=[
            " M graph/baseline/threads/topic-a/annotation_state.json",
            "?? graph/baseline/threads/topic-b/annotation_state.json",
        ])
        assert report.dirty_derived_only is True

    def test_mixed_content(self):
        report = DiagnosticReport(dirty_files=[
            " M graph/baseline/threads/topic-a/annotation_state.json",
            " M graph/baseline/threads/topic-a/meta.json",
        ])
        assert report.dirty_derived_only is False

    def test_only_content_files(self):
        report = DiagnosticReport(dirty_files=[
            " M graph/baseline/threads/topic-a/entries.jsonl",
        ])
        assert report.dirty_derived_only is False

    def test_derived_with_different_porcelain_prefixes(self):
        """Various git status prefixes should all be recognized as derived."""
        report = DiagnosticReport(dirty_files=[
            "M  graph/baseline/threads/t1/annotation_state.json",
            " M graph/baseline/threads/t2/annotation_state.json",
            "?? graph/baseline/threads/t3/annotation_state.json",
            "A  graph/baseline/threads/t4/annotation_state.json",
        ])
        assert report.dirty_derived_only is True

    def test_stripped_porcelain_lines(self):
        """diagnose() stores line.strip() — verify parsing handles that."""
        report = DiagnosticReport(dirty_files=[
            "M graph/baseline/threads/t1/annotation_state.json",
            "?? graph/baseline/threads/t2/annotation_state.json",
        ])
        assert report.dirty_derived_only is True


class TestParsePorcelainFilename:
    """Test _parse_porcelain_filename helper."""

    def test_raw_porcelain_modified(self):
        assert _parse_porcelain_filename(" M graph/baseline/threads/t/annotation_state.json") == \
            "graph/baseline/threads/t/annotation_state.json"

    def test_raw_porcelain_untracked(self):
        assert _parse_porcelain_filename("?? graph/baseline/threads/t/annotation_state.json") == \
            "graph/baseline/threads/t/annotation_state.json"

    def test_stripped_porcelain(self):
        """After line.strip(), ' M filename' becomes 'M filename'."""
        assert _parse_porcelain_filename("M graph/baseline/threads/t/annotation_state.json") == \
            "graph/baseline/threads/t/annotation_state.json"

    def test_rename(self):
        assert _parse_porcelain_filename("R  old.json -> new.json") == "new.json"

    def test_empty_string(self):
        assert _parse_porcelain_filename("") == ""

    def test_no_space(self):
        assert _parse_porcelain_filename("??") == ""

    def test_filename_starting_with_M(self):
        """Regression: old lstrip approach would eat leading M from filename."""
        assert _parse_porcelain_filename("?? META.json") == "META.json"
