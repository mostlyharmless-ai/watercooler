"""Unit tests for T2 stdio→hybrid orchestration + hybrid→stdio defer."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from watercooler.migration import t2 as t2_mod


class TestMigrateT2ToHybrid:
    def test_dry_run_does_not_call_remote(self) -> None:
        with patch.object(t2_mod, "build_premium_client") as mock_client, \
             patch.object(t2_mod, "call_remote_tool") as mock_call:
            s = t2_mod.migrate_t2_to_hybrid(
                code_path=".",
                target_group_id="my_group",
                dry_run=True,
            )
        assert mock_client.call_count == 0
        assert mock_call.call_count == 0
        assert s.dry_run is True
        assert s.errored == 0
        assert any("DRY-RUN" in n for n in s.notes)

    def test_calls_bulk_index_with_canonical_args(self) -> None:
        captured = {}

        def _fake_call(client, tool_name, arguments):
            captured["tool_name"] = tool_name
            captured["arguments"] = arguments
            return '{"queued": 42, "skipped": 3}'

        with patch.object(t2_mod, "build_premium_client", return_value=object()), \
             patch.object(t2_mod, "call_remote_tool", side_effect=_fake_call):
            s = t2_mod.migrate_t2_to_hybrid(
                code_path=".",
                target_group_id="my_group",
                dry_run=False,
                limit=100,
                threads_filter="topic-a,topic-b",
            )
        assert captured["tool_name"] == "watercooler_bulk_index"
        assert captured["arguments"]["backend"] == "graphiti"
        assert captured["arguments"]["threads"] == "topic-a,topic-b"
        assert captured["arguments"]["max_entries"] == 100
        # Pin: do NOT send `confirm` — the hosted ``_bulk_index_impl``
        # does not accept it, and prior code that did caused every
        # real T2 migration to fail Pydantic validation. See t2.py
        # call site for the in-code commentary.
        assert "confirm" not in captured["arguments"]
        assert s.pushed == 42
        assert s.skipped_already_present == 3
        assert s.errored == 0

    def test_handles_remote_error(self) -> None:
        with patch.object(t2_mod, "build_premium_client", return_value=object()), \
             patch.object(t2_mod, "call_remote_tool",
                          return_value='{"error": "queue_full"}'):
            s = t2_mod.migrate_t2_to_hybrid(
                code_path=".",
                target_group_id="g",
                dry_run=False,
            )
        assert s.errored == 1
        assert any("queue_full" in n for n in s.notes)
        assert not s.is_clean()

    def test_legitimate_queued_zero_is_honoured_not_overwritten(self) -> None:
        """Round-6 review MEDIUM: int(x) or int(y) conflates absent-key with zero.

        Pre-fix: `summary.pushed = int(resp.get("queued", 0)) or int(resp.get("enqueued", 0))`
        would silently fall through to enqueued whenever queued was 0,
        misreporting "zero new tasks queued" as "fifty already-idempotent tasks."
        """
        with patch.object(t2_mod, "build_premium_client", return_value=object()), \
             patch.object(t2_mod, "call_remote_tool",
                          return_value='{"queued": 0, "enqueued": 50, "skipped": 0}'):
            s = t2_mod.migrate_t2_to_hybrid(
                code_path=".",
                target_group_id="g",
                dry_run=False,
            )
        # `queued` is present with value 0; we honour that, NOT silently
        # falling through to `enqueued`.
        assert s.pushed == 0, "Explicit queued=0 must NOT fall through to enqueued"
        assert s.errored == 0

    def test_falls_back_to_enqueued_when_queued_absent(self) -> None:
        """Inverse of the prior test: absent queued → use enqueued."""
        with patch.object(t2_mod, "build_premium_client", return_value=object()), \
             patch.object(t2_mod, "call_remote_tool",
                          return_value='{"enqueued": 42, "skipped": 5}'):
            s = t2_mod.migrate_t2_to_hybrid(
                code_path=".",
                target_group_id="g",
                dry_run=False,
            )
        assert s.pushed == 42

    def test_zero_when_neither_key_present(self) -> None:
        with patch.object(t2_mod, "build_premium_client", return_value=object()), \
             patch.object(t2_mod, "call_remote_tool", return_value='{"skipped": 3}'):
            s = t2_mod.migrate_t2_to_hybrid(
                code_path=".",
                target_group_id="g",
                dry_run=False,
            )
        assert s.pushed == 0

    def test_hosted_response_shape_entries_queued(self) -> None:
        """Pin: hosted ``_bulk_index_hosted_impl`` returns
        ``{entries_queued, entries_skipped, already_indexed, errors}`` —
        the parser must recognise this shape (was the actual bug
        observed during the test-cjh dogfood after PR #678). Map:
        entries_queued → pushed, already_indexed → skipped_already_present,
        entries_skipped → notes, errors → errored.
        """
        with patch.object(t2_mod, "build_premium_client", return_value=object()), \
             patch.object(
                 t2_mod,
                 "call_remote_tool",
                 return_value=(
                     '{"entries_queued": 7, "entries_skipped": 2, '
                     '"already_indexed": 41, "errors": []}'
                 ),
             ):
            s = t2_mod.migrate_t2_to_hybrid(
                code_path=".",
                target_group_id="g",
                dry_run=False,
            )
        assert s.pushed == 7
        assert s.skipped_already_present == 41
        assert s.errored == 0
        # entries_skipped surfaces in notes for diagnostic visibility.
        assert any("skipped 2" in n for n in s.notes)

    def test_hosted_response_with_errors(self) -> None:
        with patch.object(t2_mod, "build_premium_client", return_value=object()), \
             patch.object(
                 t2_mod,
                 "call_remote_tool",
                 return_value=(
                     '{"entries_queued": 0, "entries_skipped": 0, '
                     '"already_indexed": 0, '
                     '"errors": [{"reason": "x"}, {"reason": "y"}]}'
                 ),
             ):
            s = t2_mod.migrate_t2_to_hybrid(
                code_path=".",
                target_group_id="g",
                dry_run=False,
            )
        assert s.errored == 2
        assert any("2 error" in n for n in s.notes)
        assert not s.is_clean()

    def test_hosted_all_already_indexed_clear_message(self) -> None:
        """When every entry is already indexed (idempotent re-run),
        the user gets a clear ``All N entries already indexed`` note
        rather than the misleading ``enqueued tasks`` message."""
        with patch.object(t2_mod, "build_premium_client", return_value=object()), \
             patch.object(
                 t2_mod,
                 "call_remote_tool",
                 return_value=(
                     '{"entries_queued": 0, "entries_skipped": 0, '
                     '"already_indexed": 70, "errors": []}'
                 ),
             ):
            s = t2_mod.migrate_t2_to_hybrid(
                code_path=".",
                target_group_id="g",
                dry_run=False,
            )
        assert s.pushed == 0
        assert s.skipped_already_present == 70
        assert s.errored == 0
        joined = " ".join(s.notes)
        assert "already indexed" in joined
        # The misleading "enqueued tasks; hosted memory queue worker drains"
        # message must NOT appear when 0 entries were actually queued.
        assert "enqueued tasks" not in joined

    def test_handles_unparseable_response(self) -> None:
        with patch.object(t2_mod, "build_premium_client", return_value=object()), \
             patch.object(t2_mod, "call_remote_tool",
                          return_value="<html>404</html>"):
            s = t2_mod.migrate_t2_to_hybrid(
                code_path=".",
                target_group_id="g",
                dry_run=False,
            )
        assert s.errored == 1
        assert any("Unparseable" in n for n in s.notes)


class TestMigrateT2ToStdio:
    def test_returns_not_implemented_pointing_to_runbook(self) -> None:
        """Round-3 review LOW-2: distinguish 'not yet built' from 'real error'.

        Previously this set errored=1, conflating "feature deferred" with
        "partial migration failure." Now the deferred state is signalled
        via not_implemented + a distinct exit code (64).
        """
        s = t2_mod.migrate_t2_to_stdio(code_path=".", dry_run=False)
        assert s.tier == "t2"
        assert s.direction == "hybrid_to_stdio"
        assert s.errored == 0, "deferred is not the same as errored"
        assert s.not_implemented is True
        assert not s.is_clean()
        assert s.exit_code() == 64  # EX_USAGE
        # Notes should point users at the canonical alternative path.
        joined = " ".join(s.notes)
        assert "OPS_T2_REBUILD" in joined
        assert "watercooler_t2_dump" in joined
