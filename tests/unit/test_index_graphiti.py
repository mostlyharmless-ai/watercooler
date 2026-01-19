"""Tests for the index_graphiti.py script.

Tests the ChunkTimingStats class and related utilities.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add scripts directory to path for imports
scripts_dir = Path(__file__).parent.parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

from index_graphiti import ChunkTimingStats


class TestChunkTimingStats:
    """Tests for ChunkTimingStats dataclass."""

    def test_initial_state(self):
        """Test that ChunkTimingStats initializes with empty state."""
        stats = ChunkTimingStats()
        assert stats.chunks_processed == 0
        assert stats.total_chunks == 0
        assert stats.dedup_times == []
        assert stats.index_times == []
        assert stats.checkpoint_times == []
        assert stats.total_times == []

    def test_add_timing_records_all_times(self):
        """Test that add_timing records all timing components."""
        stats = ChunkTimingStats()
        stats.add_timing(dedup=0.5, index=2.0, checkpoint=0.1)

        assert stats.dedup_times == [0.5]
        assert stats.index_times == [2.0]
        assert stats.checkpoint_times == [0.1]
        assert stats.total_times == [2.6]
        assert stats.chunks_processed == 1

    def test_add_timing_increments_chunks_processed(self):
        """Test that add_timing increments chunks_processed counter."""
        stats = ChunkTimingStats()
        stats.add_timing(1.0, 1.0, 0.1)
        stats.add_timing(1.5, 1.5, 0.1)
        stats.add_timing(2.0, 2.0, 0.1)

        assert stats.chunks_processed == 3

    def test_running_avg_empty_list(self):
        """Test that running_avg returns 0.0 for empty list."""
        stats = ChunkTimingStats()
        assert stats.running_avg([]) == 0.0

    def test_running_avg_fewer_than_window(self):
        """Test running_avg when fewer samples than window size."""
        stats = ChunkTimingStats()
        times = [1.0, 2.0, 3.0]
        # Window=5, but only 3 samples -> average all 3
        assert stats.running_avg(times, window=5) == pytest.approx(2.0)

    def test_running_avg_more_than_window(self):
        """Test running_avg uses only last N samples when more than window."""
        stats = ChunkTimingStats()
        times = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
        # Window=3 -> average last 3: (5+6+7)/3 = 6.0
        assert stats.running_avg(times, window=3) == pytest.approx(6.0)

    def test_running_avg_exact_window(self):
        """Test running_avg when exactly window samples."""
        stats = ChunkTimingStats()
        times = [2.0, 4.0, 6.0, 8.0, 10.0]
        # Window=5, exactly 5 samples -> average all: 30/5 = 6.0
        assert stats.running_avg(times, window=5) == pytest.approx(6.0)

    def test_overall_avg_empty(self):
        """Test overall_avg returns 0.0 when no chunks processed."""
        stats = ChunkTimingStats()
        assert stats.overall_avg() == 0.0

    def test_overall_avg_with_data(self):
        """Test overall_avg calculates mean of all total_times."""
        stats = ChunkTimingStats()
        stats.add_timing(1.0, 1.0, 0.0)  # total: 2.0
        stats.add_timing(1.0, 2.0, 0.0)  # total: 3.0
        stats.add_timing(1.0, 3.0, 0.0)  # total: 4.0
        # Average: (2+3+4)/3 = 3.0
        assert stats.overall_avg() == pytest.approx(3.0)

    def test_eta_seconds_no_data(self):
        """Test eta_seconds returns 0.0 when no timing data."""
        stats = ChunkTimingStats()
        assert stats.eta_seconds(remaining=10) == 0.0

    def test_eta_seconds_calculates_correctly(self):
        """Test eta_seconds estimates time based on average."""
        stats = ChunkTimingStats()
        # Add timings that average to 2.0 seconds per chunk
        stats.add_timing(1.0, 0.5, 0.5)  # total: 2.0
        stats.add_timing(1.0, 0.5, 0.5)  # total: 2.0

        # 5 chunks remaining * 2.0 sec/chunk = 10.0 seconds
        assert stats.eta_seconds(remaining=5) == pytest.approx(10.0)

    def test_format_eta_seconds(self):
        """Test format_eta returns seconds format for small values."""
        stats = ChunkTimingStats()
        stats.add_timing(1.0, 1.0, 0.0)  # 2.0 sec/chunk

        # 10 remaining * 2.0 = 20 seconds
        assert stats.format_eta(remaining=10) == "20.0s"

    def test_format_eta_minutes(self):
        """Test format_eta returns minutes format for values >= 60s."""
        stats = ChunkTimingStats()
        stats.add_timing(5.0, 5.0, 0.0)  # 10.0 sec/chunk

        # 12 remaining * 10.0 = 120 seconds = 2.0 minutes
        assert stats.format_eta(remaining=12) == "2.0m"

    def test_format_eta_boundary(self):
        """Test format_eta at the 60-second boundary."""
        stats = ChunkTimingStats()
        stats.add_timing(1.0, 1.0, 0.0)  # 2.0 sec/chunk

        # 30 remaining * 2.0 = 60 seconds = 1.0 minutes
        assert stats.format_eta(remaining=30) == "1.0m"

    def test_summary_dict_empty(self):
        """Test summary_dict returns empty dict when no data."""
        stats = ChunkTimingStats()
        assert stats.summary_dict() == {}

    def test_summary_dict_complete(self):
        """Test summary_dict returns all statistics."""
        stats = ChunkTimingStats()
        stats.add_timing(0.1, 1.0, 0.05)  # total: 1.15
        stats.add_timing(0.2, 2.0, 0.05)  # total: 2.25
        stats.add_timing(0.3, 3.0, 0.05)  # total: 3.35

        summary = stats.summary_dict()

        assert summary["chunks_processed"] == 3
        assert summary["avg_total"] == pytest.approx(2.25)  # (1.15+2.25+3.35)/3
        assert summary["avg_dedup"] == pytest.approx(0.2)   # (0.1+0.2+0.3)/3
        assert summary["avg_index"] == pytest.approx(2.0)   # (1+2+3)/3
        assert summary["avg_checkpoint"] == pytest.approx(0.05)
        assert summary["min_total"] == pytest.approx(1.15)
        assert summary["max_total"] == pytest.approx(3.35)


class TestAllFlagValidation:
    """Tests for --all flag file filtering."""

    def test_non_thread_files_filtered(self, tmp_path):
        """Test that common non-thread files are filtered out."""
        # Create test files
        (tmp_path / "thread1.md").write_text("# Thread 1")
        (tmp_path / "thread2.md").write_text("# Thread 2")
        (tmp_path / "README.md").write_text("# README")
        (tmp_path / "CHANGELOG.md").write_text("# Changes")
        (tmp_path / "index.md").write_text("# Index")

        # Simulate the filtering logic from index_graphiti.py
        non_thread_files = {'readme.md', 'changelog.md', 'index.md', 'license.md'}
        thread_files = sorted([
            f.name for f in tmp_path.glob("*.md")
            if f.is_file() and f.name.lower() not in non_thread_files
        ])

        assert thread_files == ["thread1.md", "thread2.md"]

    def test_case_insensitive_filtering(self, tmp_path):
        """Test that filtering is case-insensitive."""
        # Create files with various cases
        (tmp_path / "README.MD").write_text("# README")
        (tmp_path / "Readme.md").write_text("# Readme")
        (tmp_path / "thread.md").write_text("# Thread")

        non_thread_files = {'readme.md', 'changelog.md', 'index.md', 'license.md'}
        thread_files = sorted([
            f.name for f in tmp_path.glob("*.md")
            if f.is_file() and f.name.lower() not in non_thread_files
        ])

        assert thread_files == ["thread.md"]
