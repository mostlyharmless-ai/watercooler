"""Unit tests for the orphan-branch scanner in migration/_orphan.py.

Pins the recursive-iteration fix: prior `Path.iterdir()` form skipped
nested topics like ``fix/some-feature``, silently dropping their entries
from the migration. The recursive form picks them up.
"""

from __future__ import annotations

import json
from pathlib import Path

from watercooler.migration._orphan import scan_orphan_entries


def _make_thread(threads_dir: Path, topic: str, entries: list[dict]) -> None:
    """Write a thread's entries.jsonl + topic.json under
    ``<threads_dir>/graph/baseline/threads/<topic>/``.
    """
    thread_dir = threads_dir / "graph" / "baseline" / "threads" / topic
    thread_dir.mkdir(parents=True, exist_ok=True)
    with (thread_dir / "entries.jsonl").open("w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


class TestScanOrphanEntries:
    def test_yields_flat_topic_entries(self, tmp_path: Path) -> None:
        _make_thread(
            tmp_path,
            "feature-auth",
            [
                {"id": "01HX1", "entry_id": "01HX1", "index": 0, "title": "First"},
                {"id": "01HX2", "entry_id": "01HX2", "index": 1, "title": "Second"},
            ],
        )
        results = list(scan_orphan_entries(tmp_path))
        assert {e["entry_id"] for e in results} == {"01HX1", "01HX2"}
        assert all(e["_topic"] == "feature-auth" for e in results)

    def test_yields_nested_topic_entries(self, tmp_path: Path) -> None:
        """Pin: nested topics like ``fix/auth-bug`` must be visited.

        The prior iterator (``threads_root.iterdir()``) only walked the
        first level, so an entries.jsonl one level deeper than expected
        was silently skipped — this is exactly how the production T1
        backfill on 2026-04-27 lost the 2 entries in
        ``fix/t2-auto-indexing-decouple-memory-sync``.
        """
        _make_thread(
            tmp_path,
            "fix/auth-bug",
            [
                {"id": "01HY1", "entry_id": "01HY1", "index": 0, "title": "Bug"},
                {"id": "01HY2", "entry_id": "01HY2", "index": 1, "title": "Fix"},
            ],
        )
        results = list(scan_orphan_entries(tmp_path))
        assert {e["entry_id"] for e in results} == {"01HY1", "01HY2"}
        assert all(e["_topic"] == "fix/auth-bug" for e in results)

    def test_yields_mixed_flat_and_nested(self, tmp_path: Path) -> None:
        _make_thread(
            tmp_path,
            "feature-flat",
            [{"id": "01F1", "entry_id": "01F1", "index": 0}],
        )
        _make_thread(
            tmp_path,
            "fix/nested-one",
            [{"id": "01N1", "entry_id": "01N1", "index": 0}],
        )
        _make_thread(
            tmp_path,
            "release/v1/changelog",
            [{"id": "01R1", "entry_id": "01R1", "index": 0}],
        )
        results = list(scan_orphan_entries(tmp_path))
        topics = {e["_topic"] for e in results}
        assert topics == {"feature-flat", "fix/nested-one", "release/v1/changelog"}
        assert {e["entry_id"] for e in results} == {"01F1", "01N1", "01R1"}

    def test_uses_forward_slash_in_topic_name_on_all_oses(
        self, tmp_path: Path
    ) -> None:
        """Topic names must use ``/`` even on Windows-style backslash hosts.

        Downstream consumers (``get_entries_for_thread``, hosted upsert
        tool) treat the topic as a forward-slash path. Using
        ``Path.as_posix()`` instead of ``str(path)`` gives that
        invariant for free.
        """
        _make_thread(
            tmp_path,
            "fix/auth/multi-segment",
            [{"id": "01M1", "entry_id": "01M1", "index": 0}],
        )
        results = list(scan_orphan_entries(tmp_path))
        assert len(results) == 1
        assert results[0]["_topic"] == "fix/auth/multi-segment"
        assert "\\" not in results[0]["_topic"]

    def test_missing_threads_root_raises(self, tmp_path: Path) -> None:
        """When the worktree doesn't have the expected layout, fail
        loudly — not silently produce zero entries."""
        import pytest

        with pytest.raises(RuntimeError, match="Threads root not found"):
            list(scan_orphan_entries(tmp_path))

    def test_no_duplicates_within_a_single_scan(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Pin: within a single ``scan_orphan_entries`` call, the
        ``seen_topics`` guard deduplicates if ``rglob`` somehow yields
        the same ``entries.jsonl`` path more than once. Plain
        filesystems never do this, but FUSE / overlay / network mounts
        can produce phantom duplicates — the guard is defense-in-depth
        against that.

        We force the duplicate by monkey-patching ``Path.rglob`` to
        yield the same path twice, then assert the second yield is
        suppressed by ``seen_topics``.
        """
        _make_thread(
            tmp_path,
            "feature-x",
            [{"id": "01X1", "entry_id": "01X1", "index": 0}],
        )
        threads_root = tmp_path / "graph" / "baseline" / "threads"
        real_path = threads_root / "feature-x" / "entries.jsonl"

        # Replace rglob on the threads_root Path object with one that
        # returns the same entries.jsonl twice.
        original_rglob = Path.rglob

        def fake_rglob(self, pattern):
            if self == threads_root and pattern == "entries.jsonl":
                return iter([real_path, real_path])
            return original_rglob(self, pattern)

        monkeypatch.setattr(Path, "rglob", fake_rglob)

        results = list(scan_orphan_entries(tmp_path))
        # Without dedup we'd see 2 entries (the same one yielded twice).
        # The seen_topics guard collapses this back to 1.
        assert len(results) == 1
        assert results[0]["entry_id"] == "01X1"
