"""Unit tests for the T1 stdio↔hybrid orchestration.

Mocks _local + _orphan + _remote so the orchestrator's branching logic
(cache hit vs miss, dry-run, idempotency, error counting) is exercised
in isolation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from watercooler.migration import t1 as t1_mod
from watercooler.migration._local import LocalEntry
from watercooler.migration._remote import RemoteEntry


def _orphan_entry(eid: str, topic: str = "t1") -> dict:
    return {
        "entry_id": eid,
        "_topic": topic,
        "title": "title",
        "body": "body",
        "role": "implementer",
        "entry_type": "Note",
        "agent": "test",
        "timestamp": "2026-01-01T00:00:00Z",
    }


class TestMigrateT1ToHybridDryRun:
    def test_cache_hit_counts_as_pushed_in_dry_run(self, tmp_path: Path) -> None:
        with patch.object(t1_mod, "build_premium_client", return_value=object()), \
             patch.object(t1_mod, "discover_threads_dir", return_value=tmp_path), \
             patch.object(t1_mod, "scan_orphan_entries", return_value=iter([_orphan_entry("E1")])), \
             patch.object(t1_mod, "connect_local_falkor", return_value=object()), \
             patch.object(t1_mod, "list_local_entries", return_value=iter([
                 LocalEntry(entry_id="E1", thread_topic="t1", embedding=[0.0] * 1024),
             ])):
            s = t1_mod.migrate_t1_to_hybrid(
                code_path=str(tmp_path),
                checkpoint_path=tmp_path / "cp.jsonl",
                dry_run=True,
            )
        assert s.dry_run is True
        assert s.cache_hits == 1
        assert s.api_calls == 0
        assert s.pushed == 1
        assert s.errored == 0

    def test_api_path_counted_in_pushed_when_dry_run(self, tmp_path: Path) -> None:
        with patch.object(t1_mod, "build_premium_client", return_value=object()), \
             patch.object(t1_mod, "discover_threads_dir", return_value=tmp_path), \
             patch.object(t1_mod, "scan_orphan_entries", return_value=iter([_orphan_entry("E1")])), \
             patch.object(t1_mod, "connect_local_falkor", return_value=object()), \
             patch.object(t1_mod, "list_local_entries", return_value=iter([])):  # empty cache
            s = t1_mod.migrate_t1_to_hybrid(
                code_path=str(tmp_path),
                checkpoint_path=tmp_path / "cp.jsonl",
                dry_run=True,
            )
        assert s.cache_hits == 0
        assert s.api_calls == 1
        assert s.pushed == 1, "Dry-run pushed must include API-bound entries"


class TestMigrateT1ToHybridLive:
    def test_pushes_via_premium_client(self, tmp_path: Path) -> None:
        captured = {}

        def _fake_upsert(client, *, target_group_id, entry):
            captured["entry_id"] = entry.entry_id
            captured["topic"] = entry.thread_topic
            captured["dim"] = len(entry.embedding)
            captured["target_group_id"] = target_group_id
            return {"success": True, "status": "upserted"}

        with patch.object(t1_mod, "build_premium_client", return_value=object()), \
             patch.object(t1_mod, "discover_threads_dir", return_value=tmp_path), \
             patch.object(t1_mod, "scan_orphan_entries", return_value=iter([_orphan_entry("E1")])), \
             patch.object(t1_mod, "connect_local_falkor", return_value=object()), \
             patch.object(t1_mod, "list_local_entries", return_value=iter([
                 LocalEntry(entry_id="E1", thread_topic="t1", embedding=[0.0] * 1024),
             ])), \
             patch.object(t1_mod, "upsert_remote_embedding", side_effect=_fake_upsert):
            s = t1_mod.migrate_t1_to_hybrid(
                code_path=str(tmp_path),
                target_group_id="my_group",
                checkpoint_path=tmp_path / "cp.jsonl",
                dry_run=False,
            )
        assert s.pushed == 1
        assert s.errored == 0
        assert captured["entry_id"] == "E1"
        assert captured["dim"] == 1024
        assert captured["target_group_id"] == "my_group"

    def test_checkpoint_skip_resume(self, tmp_path: Path) -> None:
        cp = tmp_path / "cp.jsonl"
        cp.write_text("E1\n")  # already pushed previously

        captured_calls = []

        def _fake_upsert(client, *, target_group_id, entry):
            captured_calls.append(entry.entry_id)
            return {"success": True, "status": "upserted"}

        with patch.object(t1_mod, "build_premium_client", return_value=object()), \
             patch.object(t1_mod, "discover_threads_dir", return_value=tmp_path), \
             patch.object(t1_mod, "scan_orphan_entries", return_value=iter([
                 _orphan_entry("E1"), _orphan_entry("E2"),
             ])), \
             patch.object(t1_mod, "connect_local_falkor", return_value=object()), \
             patch.object(t1_mod, "list_local_entries", return_value=iter([
                 LocalEntry(entry_id="E1", thread_topic="t1", embedding=[0.0] * 1024),
                 LocalEntry(entry_id="E2", thread_topic="t1", embedding=[0.1] * 1024),
             ])), \
             patch.object(t1_mod, "upsert_remote_embedding", side_effect=_fake_upsert):
            s = t1_mod.migrate_t1_to_hybrid(
                code_path=str(tmp_path),
                target_group_id="g",
                checkpoint_path=cp,
                dry_run=False,
            )
        assert s.skipped_already_present == 1
        assert s.pushed == 1
        assert captured_calls == ["E2"]

    def test_generate_embedding_exception_does_not_lose_partial_counts(
        self, tmp_path: Path
    ) -> None:
        """Round-12 review MEDIUM: an unguarded raise from generate_embedding
        used to escape the loop entirely, hit cmd_migrate's boundary catch,
        and produce a FRESH MigrationSummary with pushed=0 — wiping all
        the prior successful work from the user-visible summary.

        Now the per-entry try/except matches the rest of the loop: errored++,
        continue, summary keeps its accumulated counts.
        """
        from unittest.mock import MagicMock

        # 2 cache-hit entries (E1, E2) + 1 cache-miss (E3 → triggers
        # generate_embedding which raises). All three should iterate;
        # E1+E2 push successfully, E3 errored. Summary preserves all counts.
        cache_entries = [
            LocalEntry(entry_id="E1", thread_topic="t", embedding=[0.0] * 1024),
            LocalEntry(entry_id="E2", thread_topic="t", embedding=[0.1] * 1024),
            # E3 NOT in cache → triggers generate_embedding
        ]

        def _raise_rate_limit(text):
            raise RuntimeError("rate limit exceeded from embedding API")

        upserts_called = []

        def _fake_upsert(client, *, target_group_id, entry):
            upserts_called.append(entry.entry_id)
            return {"success": True, "status": "upserted"}

        # generate_embedding is imported lazily inside migrate_t1_to_hybrid
        # via `from watercooler.baseline_graph.sync import generate_embedding`,
        # so we patch the source module path rather than t1_mod.
        with patch.object(t1_mod, "build_premium_client", return_value=object()), \
             patch.object(t1_mod, "discover_threads_dir", return_value=tmp_path), \
             patch.object(t1_mod, "scan_orphan_entries", return_value=iter([
                 _orphan_entry("E1"), _orphan_entry("E2"), _orphan_entry("E3"),
             ])), \
             patch.object(t1_mod, "connect_local_falkor", return_value=MagicMock()), \
             patch.object(t1_mod, "list_local_entries", return_value=iter(cache_entries)), \
             patch("watercooler.baseline_graph.sync.generate_embedding",
                   side_effect=_raise_rate_limit), \
             patch.object(t1_mod, "upsert_remote_embedding", side_effect=_fake_upsert):
            s = t1_mod.migrate_t1_to_hybrid(
                code_path=str(tmp_path),
                target_group_id="g",
                checkpoint_path=tmp_path / "cp.jsonl",
                dry_run=False,
            )
        # E1+E2 pushed, E3 errored. Summary preserves the partial work.
        assert upserts_called == ["E1", "E2"]
        assert s.pushed == 2
        assert s.errored == 1
        # Cleanly returned, NOT escaped to the boundary catch.
        # (If the exception escaped, pushed would be 0 from the fresh
        # boundary-summary.)

    def test_remote_error_increments_errored_not_pushed(self, tmp_path: Path) -> None:
        with patch.object(t1_mod, "build_premium_client", return_value=object()), \
             patch.object(t1_mod, "discover_threads_dir", return_value=tmp_path), \
             patch.object(t1_mod, "scan_orphan_entries", return_value=iter([_orphan_entry("E1")])), \
             patch.object(t1_mod, "connect_local_falkor", return_value=object()), \
             patch.object(t1_mod, "list_local_entries", return_value=iter([
                 LocalEntry(entry_id="E1", thread_topic="t1", embedding=[0.0] * 1024),
             ])), \
             patch.object(t1_mod, "upsert_remote_embedding",
                          return_value={"error": "scope_resolution_failed"}):
            s = t1_mod.migrate_t1_to_hybrid(
                code_path=str(tmp_path),
                target_group_id="g",
                checkpoint_path=tmp_path / "cp.jsonl",
                dry_run=False,
            )
        assert s.errored == 1
        assert s.pushed == 0
        assert not s.is_clean()


class TestMigrateT1ToStdio:
    def test_pulls_remote_writes_local(self, tmp_path: Path) -> None:
        captured_writes = []

        def _fake_upsert(client, *, graph_name, entry):
            captured_writes.append((graph_name, entry.entry_id, len(entry.embedding)))

        remote_rows = [
            RemoteEntry(entry_id="E1", thread_topic="t1", embedding=[0.0] * 1024,
                        group_id="g", role="r", entry_type="Note", agent="a", timestamp=""),
            RemoteEntry(entry_id="E2", thread_topic="t1", embedding=[0.1] * 1024,
                        group_id="g", role="r", entry_type="Note", agent="a", timestamp=""),
        ]

        with patch.object(t1_mod, "build_premium_client", return_value=object()), \
             patch.object(t1_mod, "connect_local_falkor", return_value=object()), \
             patch.object(t1_mod, "ensure_local_indexes"), \
             patch.object(t1_mod, "list_remote_embeddings", return_value=iter(remote_rows)), \
             patch.object(t1_mod, "upsert_local_entry", side_effect=_fake_upsert):
            s = t1_mod.migrate_t1_to_stdio(
                code_path=str(tmp_path),
                target_group_id="g",
                checkpoint_path=tmp_path / "cp.jsonl",
                dry_run=False,
            )
        assert s.pushed == 2
        assert s.errored == 0
        assert len(captured_writes) == 2
        assert captured_writes[0] == ("g", "E1", 1024)

    def test_dry_run_doesnt_call_local_upsert(self, tmp_path: Path) -> None:
        with patch.object(t1_mod, "build_premium_client", return_value=object()), \
             patch.object(t1_mod, "list_remote_embeddings", return_value=iter([
                 RemoteEntry(entry_id="E1", thread_topic="t1", embedding=[0.0] * 1024),
             ])), \
             patch.object(t1_mod, "upsert_local_entry") as mock_upsert:
            s = t1_mod.migrate_t1_to_stdio(
                code_path=str(tmp_path),
                target_group_id="g",
                checkpoint_path=tmp_path / "cp.jsonl",
                dry_run=True,
            )
        assert mock_upsert.call_count == 0
        assert s.pushed == 1  # dry-run still counts what would be pushed
        # cache_hits is 0 in to_stdio direction — there's no local cache
        # concept; every embedding comes from hosted T1.
        assert s.cache_hits == 0

    def test_to_stdio_does_not_increment_cache_hits(self, tmp_path: Path) -> None:
        """Round-11 review LOW: cache_hits is meaningless for to_stdio.

        Pre-fix: cache_hits was incremented on every push, making
        cache_hits == pushed. A caller computing a cache-warm rate
        would always see 100% — false signal.
        """
        from unittest.mock import MagicMock

        with patch.object(t1_mod, "build_premium_client", return_value=object()), \
             patch.object(t1_mod, "connect_local_falkor", return_value=MagicMock()), \
             patch.object(t1_mod, "ensure_local_indexes"), \
             patch.object(t1_mod, "list_remote_embeddings", return_value=iter([
                 RemoteEntry(entry_id="E1", thread_topic="t1", embedding=[0.0] * 1024),
                 RemoteEntry(entry_id="E2", thread_topic="t1", embedding=[0.1] * 1024),
                 RemoteEntry(entry_id="E3", thread_topic="t1", embedding=[0.2] * 1024),
             ])), \
             patch.object(t1_mod, "upsert_local_entry"):
            s = t1_mod.migrate_t1_to_stdio(
                code_path=str(tmp_path),
                target_group_id="g",
                checkpoint_path=tmp_path / "cp.jsonl",
                dry_run=False,
            )
        assert s.pushed == 3
        assert s.cache_hits == 0, (
            "to_stdio has no cache concept — cache_hits must stay 0, "
            "not equal to pushed"
        )

    def test_bad_remote_dim_is_errored(self, tmp_path: Path) -> None:
        with patch.object(t1_mod, "build_premium_client", return_value=object()), \
             patch.object(t1_mod, "connect_local_falkor", return_value=object()), \
             patch.object(t1_mod, "ensure_local_indexes"), \
             patch.object(t1_mod, "list_remote_embeddings", return_value=iter([
                 RemoteEntry(entry_id="E1", thread_topic="t1", embedding=[0.0] * 768),  # wrong dim
             ])), \
             patch.object(t1_mod, "upsert_local_entry") as mock_upsert:
            s = t1_mod.migrate_t1_to_stdio(
                code_path=str(tmp_path),
                target_group_id="g",
                checkpoint_path=tmp_path / "cp.jsonl",
                dry_run=False,
            )
        assert mock_upsert.call_count == 0
        assert s.errored == 1
        assert s.pushed == 0


class TestLocalGraphSymmetry:
    """Round-10 review MEDIUM-2: local graph defaults must be symmetric.

    Pre-fix: to_hybrid hardcoded ``"watercooler_cloud"`` (legacy name)
    while to_stdio defaulted to the canonical group_id-derived name.
    Round-trip pull-then-push wrote to one graph, read from a different
    one, found nothing, regenerated everything via API. Silent waste +
    silent embedding divergence.
    """

    def test_to_hybrid_defaults_local_graph_to_target_group_id(
        self, tmp_path: Path
    ) -> None:
        """Without --local-graph-name, read from the canonical group_id graph (NOT 'watercooler_cloud')."""
        captured = {}

        def _fake_list(client, *, graph_name):
            captured["graph_name"] = graph_name
            return iter([])

        with patch.object(t1_mod, "build_premium_client", return_value=object()), \
             patch.object(t1_mod, "discover_threads_dir", return_value=tmp_path), \
             patch.object(t1_mod, "scan_orphan_entries", return_value=iter([])), \
             patch.object(t1_mod, "connect_local_falkor", return_value=object()), \
             patch.object(t1_mod, "list_local_entries", side_effect=_fake_list):
            t1_mod.migrate_t1_to_hybrid(
                code_path=str(tmp_path),
                target_group_id="mostlyharmless_ai_watercooler_cloud",
                checkpoint_path=tmp_path / "cp.jsonl",
                dry_run=True,
            )
        # Must be the canonical name, NOT the legacy "watercooler_cloud" hardcode.
        assert captured["graph_name"] == "mostlyharmless_ai_watercooler_cloud"

    def test_to_hybrid_honors_explicit_local_graph_name_for_legacy_volume(
        self, tmp_path: Path
    ) -> None:
        """--local-graph-name watercooler_cloud lets users target the legacy-named volume."""
        captured = {}

        def _fake_list(client, *, graph_name):
            captured["graph_name"] = graph_name
            return iter([])

        with patch.object(t1_mod, "build_premium_client", return_value=object()), \
             patch.object(t1_mod, "discover_threads_dir", return_value=tmp_path), \
             patch.object(t1_mod, "scan_orphan_entries", return_value=iter([])), \
             patch.object(t1_mod, "connect_local_falkor", return_value=object()), \
             patch.object(t1_mod, "list_local_entries", side_effect=_fake_list):
            t1_mod.migrate_t1_to_hybrid(
                code_path=str(tmp_path),
                target_group_id="mostlyharmless_ai_watercooler_cloud",
                local_graph_name="watercooler_cloud",  # legacy override
                checkpoint_path=tmp_path / "cp.jsonl",
                dry_run=True,
            )
        assert captured["graph_name"] == "watercooler_cloud"


class TestReviewFindings:
    """Pins for the round-1 review findings on PR #678."""

    def test_local_falkor_server_down_is_caught(self, tmp_path: Path) -> None:
        """connect_local_falkor raising ConnectionError must NOT crash migration.

        Pre-fix the except was `RuntimeError`-only; redis.exceptions.ConnectionError
        propagated unhandled when the FalkorDB server was reachable-but-unhealthy.
        """
        from unittest.mock import MagicMock

        def _raise_connection(*a, **kw):
            # Simulate redis.exceptions.ConnectionError (any Exception subclass)
            raise ConnectionError("Connection refused on :6379")

        with patch.object(t1_mod, "build_premium_client", return_value=object()), \
             patch.object(t1_mod, "discover_threads_dir", return_value=tmp_path), \
             patch.object(t1_mod, "scan_orphan_entries", return_value=iter([_orphan_entry("E1")])), \
             patch.object(t1_mod, "connect_local_falkor", side_effect=_raise_connection):
            # Should NOT raise — falls through to API path with empty cache.
            s = t1_mod.migrate_t1_to_hybrid(
                code_path=str(tmp_path),
                target_group_id="g",
                checkpoint_path=tmp_path / "cp.jsonl",
                dry_run=True,
            )
        # Migration still ran; the entry shows as cache-miss.
        assert s.cache_hits == 0
        assert s.api_calls == 1
        assert any("Local FalkorDB unreachable" in n for n in s.notes)

    def test_limit_does_not_count_checkpointed_entries_on_resume(self, tmp_path: Path) -> None:
        """--limit 2 with 1 entry already checkpointed should process 2 MORE entries.

        Pre-fix, total_scanned was incremented before the checkpoint skip
        (and `>` was used), so with N entries already in checkpoint and
        --limit M, the loop exited early without pushing M new ones.
        """
        cp = tmp_path / "cp.jsonl"
        cp.write_text("E1\n")  # E1 already pushed

        captured = []

        def _fake_upsert(client, *, target_group_id, entry):
            captured.append(entry.entry_id)
            return {"success": True, "status": "upserted"}

        orphan = [_orphan_entry(eid) for eid in ["E1", "E2", "E3", "E4", "E5"]]
        cache_entries = [
            LocalEntry(entry_id=e["entry_id"], thread_topic="t1", embedding=[0.0] * 1024)
            for e in orphan
        ]

        with patch.object(t1_mod, "build_premium_client", return_value=object()), \
             patch.object(t1_mod, "discover_threads_dir", return_value=tmp_path), \
             patch.object(t1_mod, "scan_orphan_entries", return_value=iter(orphan)), \
             patch.object(t1_mod, "connect_local_falkor", return_value=object()), \
             patch.object(t1_mod, "list_local_entries", return_value=iter(cache_entries)), \
             patch.object(t1_mod, "upsert_remote_embedding", side_effect=_fake_upsert):
            s = t1_mod.migrate_t1_to_hybrid(
                code_path=str(tmp_path),
                target_group_id="g",
                checkpoint_path=cp,
                dry_run=False,
                limit=2,
            )
        # E1 skipped (in checkpoint), then 2 NEW entries processed.
        assert s.skipped_already_present == 1
        assert s.pushed == 2
        assert captured == ["E2", "E3"]

    def test_to_stdio_local_falkor_down_returns_clean_summary(self, tmp_path: Path) -> None:
        """Round-4 review MEDIUM: to_stdio must not crash when local server is down.

        Pre-fix: connect_local_falkor's ConnectionError propagated unhandled,
        crashing the process with a Python traceback and no MigrationSummary
        on stdout. Scripts parsing the JSON for exit-code decisions broke.
        """
        def _raise_connection(*a, **kw):
            raise ConnectionError("Connection refused on :6379")

        with patch.object(t1_mod, "build_premium_client", return_value=object()), \
             patch.object(t1_mod, "connect_local_falkor", side_effect=_raise_connection):
            s = t1_mod.migrate_t1_to_stdio(
                code_path=str(tmp_path),
                target_group_id="g",
                checkpoint_path=tmp_path / "cp.jsonl",
                dry_run=False,
            )
        # Returns a clean summary, NOT a Python traceback.
        assert s.tier == "t1"
        assert s.direction == "hybrid_to_stdio"
        assert s.errored >= 1
        assert any("Local FalkorDB unreachable" in n for n in s.notes)
        assert any("--local-host" in n for n in s.notes)  # actionable hint
        assert s.exit_code() == 2
        assert s.elapsed_seconds >= 0  # populated even on early return

    def test_to_stdio_closes_local_client_when_ensure_indexes_raises(self, tmp_path: Path) -> None:
        """Round-6 review HIGH-2: leak in early-return branch.

        connect_local_falkor succeeded → client open. ensure_local_indexes
        raised (e.g. bad dim). Pre-fix the except branch returned without
        closing → leaked redis-py pool connection.
        """
        from unittest.mock import MagicMock

        local_client_mock = MagicMock()
        local_client_mock.close = MagicMock()

        with patch.object(t1_mod, "build_premium_client", return_value=object()), \
             patch.object(t1_mod, "connect_local_falkor", return_value=local_client_mock), \
             patch.object(t1_mod, "ensure_local_indexes",
                          side_effect=ValueError("bad dim")):
            s = t1_mod.migrate_t1_to_stdio(
                code_path=str(tmp_path),
                target_group_id="g",
                checkpoint_path=tmp_path / "cp.jsonl",
                dry_run=False,
            )
        # Connection MUST have been closed despite the early return.
        local_client_mock.close.assert_called_once()
        assert s.errored == 1
        assert s.exit_code() == 2

    def test_to_stdio_signals_errored_on_generic_transport_exception(
        self, tmp_path: Path
    ) -> None:
        """Round-7 review MEDIUM-2: catch any transport exception, not just MigrationTransportError.

        The premium_client can raise ConnectionError / TimeoutError /
        generic RuntimeError from the async layer. Pre-fix those
        propagated past the iteration guard, dumping a Python traceback
        to the user with no JSON summary and exit code 1 (Python
        default) instead of the documented exit code 2.
        """
        from unittest.mock import MagicMock

        local_client_mock = MagicMock()

        def _fake_iter():
            yield RemoteEntry(entry_id="E1", thread_topic="t",
                              embedding=[0.0] * 1024, group_id="g")
            # Simulate a network blip from the underlying transport.
            raise ConnectionError("simulated network blip from premium_client")

        with patch.object(t1_mod, "build_premium_client", return_value=object()), \
             patch.object(t1_mod, "connect_local_falkor", return_value=local_client_mock), \
             patch.object(t1_mod, "ensure_local_indexes"), \
             patch.object(t1_mod, "list_remote_embeddings", return_value=_fake_iter()), \
             patch.object(t1_mod, "upsert_local_entry"):
            s = t1_mod.migrate_t1_to_stdio(
                code_path=str(tmp_path),
                target_group_id="g",
                checkpoint_path=tmp_path / "cp.jsonl",
                dry_run=False,
            )
        # E1 was successfully pushed before the failure.
        assert s.pushed == 1
        # Generic exception surfaced as errored + recovery hint.
        assert s.errored == 1
        assert s.exit_code() == 2
        joined = " ".join(s.notes)
        assert "ConnectionError" in joined
        assert "Pull is incomplete" in joined or "re-run" in joined.lower()

    def test_to_stdio_signals_errored_when_remote_pull_truncates_mid_stream(
        self, tmp_path: Path
    ) -> None:
        """Round-6 review HIGH-1: silent partial pull becomes a real error signal.

        list_remote_embeddings used to silently `return` on transport error;
        the for-loop just ended; summary.errored stayed 0. The user
        checkpointed an incomplete pull with no signal to re-run.
        Now MigrationTransportError raises, gets caught by the iteration
        guard, and signals errored + a recovery hint.
        """
        from unittest.mock import MagicMock
        from watercooler.migration._remote import MigrationTransportError

        local_client_mock = MagicMock()

        def _fake_iter():
            # Yield 1 entry, then signal mid-stream transport failure.
            yield RemoteEntry(entry_id="E1", thread_topic="t",
                              embedding=[0.0] * 1024, group_id="g")
            raise MigrationTransportError("simulated network blip after page 1")

        with patch.object(t1_mod, "build_premium_client", return_value=object()), \
             patch.object(t1_mod, "connect_local_falkor", return_value=local_client_mock), \
             patch.object(t1_mod, "ensure_local_indexes"), \
             patch.object(t1_mod, "list_remote_embeddings", return_value=_fake_iter()), \
             patch.object(t1_mod, "upsert_local_entry"):
            s = t1_mod.migrate_t1_to_stdio(
                code_path=str(tmp_path),
                target_group_id="g",
                checkpoint_path=tmp_path / "cp.jsonl",
                dry_run=False,
            )
        # E1 was successfully pushed before the failure.
        assert s.pushed == 1
        # But the mid-stream failure surfaces as errored + a recovery hint.
        assert s.errored == 1
        assert s.exit_code() == 2
        joined = " ".join(s.notes)
        assert "Pull is incomplete" in joined or "aborted" in joined
        assert "re-run" in joined.lower()

    def test_to_hybrid_closes_local_client_when_premium_build_raises(self, tmp_path: Path) -> None:
        """Round-4 review LOW: leaked redis pool connection on bad-config raise.

        Pre-fix: build_premium_client() raising (e.g. no [mcp].url) abandoned
        the local FalkorDB client → leaked a redis-py pool connection. Over
        repeated misconfig invocations this exhausts the local pool.
        """
        from unittest.mock import MagicMock

        local_client_mock = MagicMock()
        local_client_mock.close = MagicMock()

        with patch.object(t1_mod, "discover_threads_dir", return_value=tmp_path), \
             patch.object(t1_mod, "scan_orphan_entries", return_value=iter([])), \
             patch.object(t1_mod, "connect_local_falkor", return_value=local_client_mock), \
             patch.object(t1_mod, "list_local_entries", return_value=iter([])), \
             patch.object(t1_mod, "build_premium_client",
                          side_effect=RuntimeError("Hybrid migration requires [mcp].url")):
            with pytest.raises(RuntimeError, match=r"\[mcp\]\.url"):
                t1_mod.migrate_t1_to_hybrid(
                    code_path=str(tmp_path),
                    target_group_id="g",
                    checkpoint_path=tmp_path / "cp.jsonl",
                    dry_run=False,
                )
        # Connection MUST have been closed despite the raise.
        local_client_mock.close.assert_called_once()

    def test_total_scanned_does_not_overshoot_limit(self, tmp_path: Path) -> None:
        """--limit 1 should report total_scanned=1, not 2 (no off-by-one).

        Pre-fix, increment-before-guard meant total_scanned was always
        limit+1 (the guard fired one iteration too late).
        """
        orphan = [_orphan_entry(eid) for eid in ["E1", "E2", "E3"]]
        cache_entries = [
            LocalEntry(entry_id=e["entry_id"], thread_topic="t1", embedding=[0.0] * 1024)
            for e in orphan
        ]

        with patch.object(t1_mod, "build_premium_client", return_value=object()), \
             patch.object(t1_mod, "discover_threads_dir", return_value=tmp_path), \
             patch.object(t1_mod, "scan_orphan_entries", return_value=iter(orphan)), \
             patch.object(t1_mod, "connect_local_falkor", return_value=object()), \
             patch.object(t1_mod, "list_local_entries", return_value=iter(cache_entries)):
            s = t1_mod.migrate_t1_to_hybrid(
                code_path=str(tmp_path),
                target_group_id="g",
                checkpoint_path=tmp_path / "cp.jsonl",
                dry_run=True,
                limit=1,
            )
        assert s.total_scanned == 1
        assert s.pushed == 1
