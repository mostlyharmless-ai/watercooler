"""Integration tests for watercooler_promote_candidate MCP tool.

These tests cover the MCP-layer glue between the pure `watercooler.promotion`
library and the canonical write path (`_say_impl`). The pure body-formatting
and validation logic is covered by `tests/unit/test_promotion.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from watercooler.baseline_graph import storage
from watercooler_mcp.tools.promotion import (
    _parse_entry_id,
    _promote_candidate_impl,
)


_CANDIDATE_BODY = """\
Spec: decision-extractor
[automated: decision_extractor]
Candidate-Type: Decision
Candidate-Status: needs_human_confirmation
Surface-Kind: decision
Promotable: true
Authority: none
Confidence: 4/5
Failed-Gates: g6_temporal
Quote-Evidence-Status: verified
Source-Entry: 01HZA7T0BC3D4E5F6G7H8J9K0M

## Candidate Decision
We will adopt PostgreSQL for session storage.

## Why this is a candidate, not a Decision
g6_temporal: unclear timing.

## Evidence
> We decided to use PostgreSQL.
"""


_TOPIC = "feature-storage"
# Valid Crockford base32 (no I, L, O, U).
_CANDIDATE_ID = "01HZA8T0BC3D4E5F6G7H8J9K0M"
_SOURCE_ID = "01HZA7T0BC3D4E5F6G7H8J9K0M"


@pytest.fixture()
def candidate_entry() -> dict[str, Any]:
    return {
        "id": f"entry:{_CANDIDATE_ID}",
        "entry_id": _CANDIDATE_ID,
        "agent": "ExtractDecisionsDaemon",
        "timestamp": "2026-05-30T00:00:00Z",
        "role": "implementer",
        "entry_type": "Note",
        "title": "Candidate Decision: PostgreSQL",
        "summary": "Candidate decision needing review.",
        "body": _CANDIDATE_BODY,
        "index": 5,
        "thread_topic": _TOPIC,
    }


# ---------------------------------------------------------------------------
# _parse_entry_id helper
# ---------------------------------------------------------------------------


class TestParseEntryId:
    def test_parses_canonical_say_response(self):
        response = (
            "✅ Entry added to 'feature-storage'\n"
            "Title: Promoted Decision\n"
            "Role: implementer | Type: Decision\n"
            "Ball flipped to: jay\n"
            "Status: OPEN\n"
            "Entry-ID: 01HXYZABCDEF1234567890QRST\n"
            "Ball: jay. Next: continue."
        )
        assert _parse_entry_id(response) == "01HXYZABCDEF1234567890QRST"

    def test_returns_none_when_no_entry_id(self):
        assert _parse_entry_id("❌ Something went wrong\n") is None

    def test_returns_none_on_lowercase_id(self):
        # ULIDs are uppercase Crockford base32; lowercase letters never appear.
        assert _parse_entry_id("Entry-ID: 01abcdefghijklmnopqrstuvwx\n") is None


# ---------------------------------------------------------------------------
# End-to-end promotion glue
# ---------------------------------------------------------------------------


class TestPromoteCandidateImpl:
    def _say_response(self, entry_id: str, entry_type: str = "Decision") -> str:
        return (
            "✅ Entry added to 'feature-storage'\n"
            "Title: Promoted Decision\n"
            f"Role: implementer | Type: {entry_type}\n"
            "Ball flipped to: jay\n"
            "Status: OPEN\n"
            f"Entry-ID: {entry_id}\n"
            "Ball: jay. Next: continue."
        )

    def _patched_context(self, tmp_path: Path):
        threads_dir = tmp_path / "threads"
        graph_dir = storage.ensure_graph_dir(threads_dir)
        # Topic exists on the graph so get_graph_dir + writer.get_entry_node_from_graph
        # don't blow up on missing thread.
        storage.ensure_thread_graph_dir(graph_dir, _TOPIC)
        ctx = type(
            "Ctx", (), {"client_id": "test-client", "threads_dir": threads_dir}
        )()
        ctx.threads_dir = threads_dir
        return ctx, threads_dir

    def test_full_promotion_writes_decision_then_disposition(
        self, tmp_path, candidate_entry
    ):
        ctx, threads_dir = self._patched_context(tmp_path)
        # Test IDs must be valid Crockford base32 (no I, L, O, U) for
        # _parse_entry_id's regex to accept them.
        decision_id = "01HZA8T2BC3D4E5F6G7H8J9K0M"
        disposition_id = "01HZA8T3BC3D4E5F6G7H8J9K1N"

        # We intercept _say_impl to verify it's called twice with the right
        # arguments (Decision first, Note second). The actual write machinery
        # is exercised by other tests; here we cover the glue.
        say_calls: list[dict[str, Any]] = []

        def fake_say(*, topic, title, body, ctx, role, entry_type, code_path, agent_func, **kw):
            say_calls.append(
                {
                    "topic": topic,
                    "title": title,
                    "body": body,
                    "entry_type": entry_type,
                    "role": role,
                    "agent_func": agent_func,
                    "authority_fields": kw.get("authority_fields"),
                }
            )
            # Return the appropriate entry_id based on what's being written.
            if entry_type == "Decision":
                return self._say_response(decision_id, entry_type="Decision")
            return self._say_response(disposition_id, entry_type="Note")

        thread_context = type(
            "ThreadCtx",
            (),
            {"threads_dir": threads_dir, "code_repo": "test/repo", "code_branch": "main"},
        )()

        with (
            patch(
                "watercooler_mcp.tools.promotion._say_impl",
                side_effect=fake_say,
            ),
            patch(
                "watercooler_mcp.validation._require_context",
                return_value=(None, thread_context),
            ),
            patch(
                "watercooler_mcp.tools.promotion.get_entry_node_from_graph",
                return_value=candidate_entry,
            ),
        ):
            result = _promote_candidate_impl(
                candidate_entry_id=_CANDIDATE_ID,
                topic=_TOPIC,
                target_type="Decision",
                human_authorized_by="caleb",
                ctx=ctx,
                code_path=str(tmp_path),
                agent_func="Claude Code:claude-opus-4-7:implementer",
            )

        # Two writes happened in the expected order.
        assert len(say_calls) == 2
        assert say_calls[0]["entry_type"] == "Decision"
        assert say_calls[1]["entry_type"] == "Note"

        # Decision body carries promotion provenance.
        decision_body = say_calls[0]["body"]
        assert "Promoted-From: 01HZA8T0BC3D4E5F6G7H8J9K0M" in decision_body
        assert "Source-Entry: 01HZA7T0BC3D4E5F6G7H8J9K0M" in decision_body
        assert "Authority-Source: human" in decision_body
        assert "Authority-Basis: human_promoted" in decision_body
        assert "Human-Authorized-By: caleb" in decision_body

        # Decision also persists structured authority metadata (queryable graph
        # fields), not only body markers (#879). actor_class stays "agent" because an
        # agent executed the promotion under human instruction.
        decision_authority = say_calls[0]["authority_fields"]
        assert decision_authority is not None
        assert decision_authority["actor_class"] == "agent"
        assert decision_authority["decision_origin"] == "human_promoted"
        assert decision_authority["authority_basis"] == "human_promoted"
        assert decision_authority["human_authorized_by"] == "caleb"
        assert decision_authority["source_entry_id"] == _CANDIDATE_ID

        # Disposition body references the just-written Decision (not the
        # placeholder).
        disposition_body = say_calls[1]["body"]
        assert f"Promoted-To: {decision_id}" in disposition_body
        assert "(promoted_entry_id pending)" not in disposition_body
        assert f"Disposition-Target: {_CANDIDATE_ID}" in disposition_body
        assert "CandidateDisposition: promoted" in disposition_body

        # Final response surfaces both entry IDs.
        assert f"Decision Entry-ID: {decision_id}" in result
        assert f"CandidateDisposition Entry-ID: {disposition_id}" in result
        assert "Authorized by: caleb" in result

    def test_existing_promoted_entry_blocks_re_promotion(
        self, tmp_path, candidate_entry
    ):
        """#886 end-to-end: the MCP tool loads thread entries and forwards them
        to the double-promotion guard, so a prior promoted entry carrying
        ``Promoted-From`` (with no matching disposition) blocks re-promotion
        instead of writing a duplicate promoted entry."""
        ctx, threads_dir = self._patched_context(tmp_path)
        prior_decision = {
            "entry_id": "01HZA8T9BC3D4E5F6G7H8J9K0M",
            "entry_type": "Decision",
            "body": (
                "Spec: decision-extractor-promoted\n"
                f"Promoted-From: {_CANDIDATE_ID}\n"
                "Authority-Source: human\n"
                "Authority-Basis: human_promoted\n"
                "## Decision\nWe will adopt PostgreSQL for session storage.\n"
            ),
        }
        thread_context = type(
            "ThreadCtx",
            (),
            {"threads_dir": threads_dir, "code_repo": "test/repo", "code_branch": "main"},
        )()
        say_calls: list[str] = []

        def fake_say(*, entry_type, **kw):
            say_calls.append(entry_type)
            return self._say_response("01HZA8T8BC3D4E5F6G7H8J9K0M", entry_type)

        with (
            patch(
                "watercooler_mcp.tools.promotion._say_impl",
                side_effect=fake_say,
            ),
            patch(
                "watercooler_mcp.validation._require_context",
                return_value=(None, thread_context),
            ),
            patch(
                "watercooler_mcp.tools.promotion.get_entry_node_from_graph",
                return_value=candidate_entry,
            ),
            patch(
                "watercooler.baseline_graph.writer.get_entries_for_thread",
                return_value=[prior_decision],
            ),
        ):
            result = _promote_candidate_impl(
                candidate_entry_id=_CANDIDATE_ID,
                topic=_TOPIC,
                target_type="Decision",
                human_authorized_by="caleb",
                ctx=ctx,
                code_path=str(tmp_path),
                agent_func="Claude Code:claude-opus-4-7:implementer",
            )

        # No writes happened — the guard refused before the promoted-entry write.
        assert say_calls == []
        assert "❌" in result
        assert "already has a promoted entry" in result

    def test_thread_entries_load_failure_fails_closed(
        self, tmp_path, candidate_entry
    ):
        """#886: if thread entries can't be loaded the double-promotion guard
        can't run, so promotion is refused (fail closed) rather than risking a
        duplicate Decision — a flaky read is exactly when a prior write may have
        half-failed. No Decision is written."""
        ctx, threads_dir = self._patched_context(tmp_path)
        thread_context = type(
            "ThreadCtx",
            (),
            {"threads_dir": threads_dir, "code_repo": "test/repo", "code_branch": "main"},
        )()
        say_calls: list[str] = []

        def fake_say(*, entry_type, **kw):
            say_calls.append(entry_type)
            return self._say_response("01HZA8T8BC3D4E5F6G7H8J9K0M", entry_type)

        with (
            patch(
                "watercooler_mcp.tools.promotion._say_impl",
                side_effect=fake_say,
            ),
            patch(
                "watercooler_mcp.validation._require_context",
                return_value=(None, thread_context),
            ),
            patch(
                "watercooler_mcp.tools.promotion.get_entry_node_from_graph",
                return_value=candidate_entry,
            ),
            patch(
                "watercooler.baseline_graph.writer.get_entries_for_thread",
                side_effect=OSError("graph read failed"),
            ),
        ):
            result = _promote_candidate_impl(
                candidate_entry_id=_CANDIDATE_ID,
                topic=_TOPIC,
                target_type="Decision",
                human_authorized_by="caleb",
                ctx=ctx,
                code_path=str(tmp_path),
                agent_func="Claude Code:claude-opus-4-7:implementer",
            )

        assert say_calls == []
        assert "❌" in result
        assert "could not load thread entries" in result

    def test_forged_candidate_quotes_do_not_launder_source_support(
        self, tmp_path, candidate_entry
    ):
        """#887 security: a candidate that self-asserts ``Quote-Evidence-Status:
        verified`` but whose quotes do NOT appear in the live source must NOT
        render source/record_state support on the promoted Decision. The warrant
        is re-derived from live re-validation, not the candidate's own markers."""
        ctx, threads_dir = self._patched_context(tmp_path)
        thread_context = type(
            "ThreadCtx",
            (),
            {"threads_dir": threads_dir, "code_repo": "test/repo", "code_branch": "main"},
        )()
        forged_source = {
            "entry_id": _SOURCE_ID,
            "entry_type": "Decision",
            "body": "Unrelated source content that never contains the claimed quote.",
        }

        def fake_lookup(_threads_dir, entry_id, topic=None):
            return candidate_entry if entry_id == _CANDIDATE_ID else forged_source

        captured: dict[str, str] = {}

        def fake_say(*, entry_type, body, **kw):
            if entry_type == "Decision":
                captured["decision_body"] = body
            return self._say_response("01HZA8T8BC3D4E5F6G7H8J9K0M", entry_type)

        with (
            patch(
                "watercooler_mcp.tools.promotion._say_impl", side_effect=fake_say
            ),
            patch(
                "watercooler_mcp.validation._require_context",
                return_value=(None, thread_context),
            ),
            patch(
                "watercooler_mcp.tools.promotion.get_entry_node_from_graph",
                side_effect=fake_lookup,
            ),
        ):
            _promote_candidate_impl(
                candidate_entry_id=_CANDIDATE_ID,
                topic=_TOPIC,
                target_type="Decision",
                human_authorized_by="caleb",
                ctx=ctx,
                code_path=str(tmp_path),
                agent_func="Claude Code:claude-opus-4-7:implementer",
            )

        body = captured["decision_body"]
        # Laundering blocked: no substantive source/record_state support.
        assert "Quote-Reverified-At-Promotion: not_reverified" in body
        assert "- source:" not in body
        assert "- record_state:" not in body
        # Human ownership (user tether) is still recorded — the promotion is real.
        assert "Human-Authorized-By: caleb" in body

    def test_short_matching_quote_has_honest_audit_note(self, tmp_path, candidate_entry):
        """A matching quote below the promotion floor withholds source support but
        must not be reported as failing to confirm against the live source."""
        ctx, threads_dir = self._patched_context(tmp_path)
        thread_context = type(
            "ThreadCtx",
            (),
            {"threads_dir": threads_dir, "code_repo": "test/repo", "code_branch": "main"},
        )()
        short_candidate = {
            **candidate_entry,
            "body": _CANDIDATE_BODY.replace(
                "> We decided to use PostgreSQL.", "> We agree."
            ),
        }
        matching_short_source = {
            "entry_id": _SOURCE_ID,
            "entry_type": "Decision",
            "body": "Meeting notes: ok. We agree. Misc trailing text.",
        }

        def fake_lookup(_threads_dir, entry_id, topic=None):
            return short_candidate if entry_id == _CANDIDATE_ID else matching_short_source

        captured: dict[str, str] = {}

        def fake_say(*, entry_type, body, **kw):
            if entry_type == "Decision":
                captured["decision_body"] = body
            return self._say_response("01HZA8T8BC3D4E5F6G7H8J9K0M", entry_type)

        with (
            patch(
                "watercooler_mcp.tools.promotion._say_impl", side_effect=fake_say
            ),
            patch(
                "watercooler_mcp.validation._require_context",
                return_value=(None, thread_context),
            ),
            patch(
                "watercooler_mcp.tools.promotion.get_entry_node_from_graph",
                side_effect=fake_lookup,
            ),
        ):
            _promote_candidate_impl(
                candidate_entry_id=_CANDIDATE_ID,
                topic=_TOPIC,
                target_type="Decision",
                human_authorized_by="caleb",
                ctx=ctx,
                code_path=str(tmp_path),
                agent_func="Claude Code:claude-opus-4-7:implementer",
            )

        body = captured["decision_body"]
        assert "Quote-Reverification-Reason: quote_below_minimum_length" in body
        assert "matched the live source entry at promotion" in body
        assert "too short to count as durable source support" in body
        assert "did NOT confirm against the live source" not in body
        assert "- source:" not in body
        assert "- record_state:" not in body

    def test_revalidated_candidate_quotes_grant_source_support(
        self, tmp_path, candidate_entry
    ):
        """#887: when the candidate's quote IS present in the live source, live
        re-validation passes and the promoted Decision shows source support."""
        ctx, threads_dir = self._patched_context(tmp_path)
        thread_context = type(
            "ThreadCtx",
            (),
            {"threads_dir": threads_dir, "code_repo": "test/repo", "code_branch": "main"},
        )()
        honest_source = {
            "entry_id": _SOURCE_ID,
            "entry_type": "Decision",
            "body": "Earlier discussion. We decided to use PostgreSQL. And more.",
        }
        source_lookup_topics: list = []

        def fake_lookup(_threads_dir, entry_id, topic=None):
            if entry_id == _CANDIDATE_ID:
                return candidate_entry
            # Record how the source was looked up — it must be topic-less so a
            # source entry on a *different* thread than the candidate resolves
            # (cross-thread slow-path search).
            source_lookup_topics.append(topic)
            return honest_source

        captured: dict[str, str] = {}

        def fake_say(*, entry_type, body, **kw):
            if entry_type == "Decision":
                captured["decision_body"] = body
            return self._say_response("01HZA8T8BC3D4E5F6G7H8J9K0M", entry_type)

        with (
            patch(
                "watercooler_mcp.tools.promotion._say_impl", side_effect=fake_say
            ),
            patch(
                "watercooler_mcp.validation._require_context",
                return_value=(None, thread_context),
            ),
            patch(
                "watercooler_mcp.tools.promotion.get_entry_node_from_graph",
                side_effect=fake_lookup,
            ),
        ):
            _promote_candidate_impl(
                candidate_entry_id=_CANDIDATE_ID,
                topic=_TOPIC,
                target_type="Decision",
                human_authorized_by="caleb",
                ctx=ctx,
                code_path=str(tmp_path),
                agent_func="Claude Code:claude-opus-4-7:implementer",
            )

        body = captured["decision_body"]
        assert "Quote-Reverified-At-Promotion: reverified" in body
        assert "- source:" in body
        # The live source is a Decision, so record_state is granted from the
        # live type (not a self-asserted marker).
        assert "- record_state:" in body
        # The source was resolved without a topic → cross-thread capable.
        assert source_lookup_topics == [None]

    def test_record_state_withheld_when_live_source_is_not_record_type(
        self, tmp_path, candidate_entry
    ):
        """#887: even when the quote re-validates, record_state is withheld if the
        LIVE source entry is not a record-state type (e.g. a plain Note) — the
        candidate cannot self-assert Source-Entry-Type to manufacture it."""
        ctx, threads_dir = self._patched_context(tmp_path)
        thread_context = type(
            "ThreadCtx",
            (),
            {"threads_dir": threads_dir, "code_repo": "test/repo", "code_branch": "main"},
        )()
        note_source = {
            "entry_id": _SOURCE_ID,
            "entry_type": "Note",  # live source is a Note, not a Decision
            "body": "Earlier discussion. We decided to use PostgreSQL. And more.",
        }

        def fake_lookup(_threads_dir, entry_id, topic=None):
            return candidate_entry if entry_id == _CANDIDATE_ID else note_source

        captured: dict[str, str] = {}

        def fake_say(*, entry_type, body, **kw):
            if entry_type == "Decision":
                captured["decision_body"] = body
            return self._say_response("01HZA8T8BC3D4E5F6G7H8J9K0M", entry_type)

        with (
            patch(
                "watercooler_mcp.tools.promotion._say_impl", side_effect=fake_say
            ),
            patch(
                "watercooler_mcp.validation._require_context",
                return_value=(None, thread_context),
            ),
            patch(
                "watercooler_mcp.tools.promotion.get_entry_node_from_graph",
                side_effect=fake_lookup,
            ),
        ):
            _promote_candidate_impl(
                candidate_entry_id=_CANDIDATE_ID,
                topic=_TOPIC,
                target_type="Decision",
                human_authorized_by="caleb",
                ctx=ctx,
                code_path=str(tmp_path),
                agent_func="Claude Code:claude-opus-4-7:implementer",
            )

        body = captured["decision_body"]
        # Quote matched → source granted; live type is Note → record_state withheld.
        assert "- source:" in body
        assert "- record_state:" not in body

    def test_missing_candidate_returns_error(self, tmp_path):
        ctx, _ = self._patched_context(tmp_path)
        thread_context = type(
            "ThreadCtx",
            (),
            {"threads_dir": tmp_path / "threads", "code_repo": "test/repo"},
        )()
        with (
            patch(
                "watercooler_mcp.validation._require_context",
                return_value=(None, thread_context),
            ),
            patch(
                "watercooler_mcp.tools.promotion.get_entry_node_from_graph",
                return_value=None,
            ),
        ):
            result = _promote_candidate_impl(
                candidate_entry_id=_CANDIDATE_ID,
                topic=_TOPIC,
                target_type="Decision",
                human_authorized_by="caleb",
                ctx=ctx,
                code_path=str(tmp_path),
                agent_func="Claude Code:claude-opus-4-7:implementer",
            )

        assert "❌" in result
        assert "not found" in result

    def test_empty_candidate_id_returns_error(self, tmp_path):
        ctx, _ = self._patched_context(tmp_path)
        result = _promote_candidate_impl(
            candidate_entry_id="",
            topic=_TOPIC,
            target_type="Decision",
            human_authorized_by="caleb",
            ctx=ctx,
            code_path=str(tmp_path),
            agent_func="Claude Code:claude-opus-4-7:implementer",
        )
        assert "❌" in result
        assert "candidate_entry_id is required" in result

    def test_promotion_error_returns_error_response(self, tmp_path, candidate_entry):
        ctx, _ = self._patched_context(tmp_path)
        thread_context = type(
            "ThreadCtx",
            (),
            {"threads_dir": tmp_path / "threads", "code_repo": "test/repo"},
        )()
        with (
            patch(
                "watercooler_mcp.validation._require_context",
                return_value=(None, thread_context),
            ),
            patch(
                "watercooler_mcp.tools.promotion.get_entry_node_from_graph",
                return_value=candidate_entry,
            ),
        ):
            result = _promote_candidate_impl(
                candidate_entry_id=_CANDIDATE_ID,
                topic=_TOPIC,
                target_type="Closure",  # not supported
                human_authorized_by="caleb",
                ctx=ctx,
                code_path=str(tmp_path),
                agent_func="Claude Code:claude-opus-4-7:implementer",
        )

        assert "❌" in result
        assert "not supported" in result


class TestPromoteCandidateCLI:
    """The CLI promote path mirrors the MCP tool's refusal behavior — both fail
    closed on unreadable thread state and both refuse a duplicate promotion.
    These pin the CLI half (stderr + exit code) so the two symmetric callers
    cannot silently desync (#886 agent-native parity)."""

    def _argv(self, threads_dir: Path) -> list[str]:
        return [
            "promote-candidate",
            _CANDIDATE_ID,
            "--topic",
            _TOPIC,
            "--human-authorized-by",
            "caleb",
            "--threads-dir",
            str(threads_dir),
            "--no-sync",
        ]

    def test_load_failure_fails_closed_exit_2(
        self, tmp_path, candidate_entry, capsys
    ):
        from watercooler import cli

        with (
            patch(
                "watercooler.baseline_graph.writer.get_entry_node_from_graph",
                return_value=candidate_entry,
            ),
            patch(
                "watercooler.baseline_graph.writer.get_entries_for_thread",
                side_effect=OSError("graph read failed"),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            cli.main(self._argv(tmp_path))
        assert exc.value.code == 2
        assert "could not load thread entries" in capsys.readouterr().err

    def test_existing_promoted_decision_refused_exit_2(
        self, tmp_path, candidate_entry, capsys
    ):
        from watercooler import cli

        prior_decision = {
            "entry_id": "01HZA8T9BC3D4E5F6G7H8J9K0M",
            "entry_type": "Decision",
            "body": (
                "Spec: decision-extractor-promoted\n"
                f"Promoted-From: {_CANDIDATE_ID}\n"
                "Authority-Basis: human_promoted\n"
                "## Decision\nWe will adopt PostgreSQL for session storage.\n"
            ),
        }
        with (
            patch(
                "watercooler.baseline_graph.writer.get_entry_node_from_graph",
                return_value=candidate_entry,
            ),
            patch(
                "watercooler.baseline_graph.writer.get_entries_for_thread",
                return_value=[prior_decision],
            ),
            pytest.raises(SystemExit) as exc,
        ):
            cli.main(self._argv(tmp_path))
        assert exc.value.code == 2
        assert "already has a promoted entry" in capsys.readouterr().err

    def test_forged_candidate_quotes_do_not_launder_source_support(
        self, tmp_path, candidate_entry, monkeypatch
    ):
        """#887 CLI parity: the CLI promote path re-validates quotes against the
        live source too, so a forged candidate cannot launder source support onto
        the promoted Decision via the CLI."""
        from watercooler import cli

        monkeypatch.setenv("WATERCOOLER_ALLOW_LOCAL_ONLY", "1")
        forged_source = {
            "entry_id": _SOURCE_ID,
            "entry_type": "Decision",
            "body": "Unrelated source content that never contains the claimed quote.",
        }

        def fake_lookup(_threads_dir, entry_id, topic=None):
            return candidate_entry if entry_id == _CANDIDATE_ID else forged_source

        captured: dict[str, str] = {}

        def fake_say(_topic, **kw):
            if kw.get("entry_type") == "Decision":
                captured["decision_body"] = kw.get("body", "")
            return "ok"

        with (
            patch(
                "watercooler.baseline_graph.writer.get_entry_node_from_graph",
                side_effect=fake_lookup,
            ),
            patch(
                "watercooler.baseline_graph.writer.get_entries_for_thread",
                return_value=[],
            ),
            patch("watercooler.commands_graph.say", side_effect=fake_say),
            pytest.raises(SystemExit) as exc,
        ):
            cli.main(self._argv(tmp_path))
        assert exc.value.code == 0  # promotion succeeded

        body = captured["decision_body"]
        assert "Quote-Reverified-At-Promotion: not_reverified" in body
        assert "- source:" not in body

    def test_short_matching_quote_has_honest_audit_note(
        self, tmp_path, candidate_entry, monkeypatch
    ):
        from watercooler import cli

        monkeypatch.setenv("WATERCOOLER_ALLOW_LOCAL_ONLY", "1")
        short_candidate = {
            **candidate_entry,
            "body": _CANDIDATE_BODY.replace(
                "> We decided to use PostgreSQL.", "> We agree."
            ),
        }
        matching_short_source = {
            "entry_id": _SOURCE_ID,
            "entry_type": "Decision",
            "body": "Meeting notes: ok. We agree. Misc trailing text.",
        }

        def fake_lookup(_threads_dir, entry_id, topic=None):
            return short_candidate if entry_id == _CANDIDATE_ID else matching_short_source

        captured: dict[str, str] = {}

        def fake_say(_topic, **kw):
            if kw.get("entry_type") == "Decision":
                captured["decision_body"] = kw.get("body", "")
            return "ok"

        with (
            patch(
                "watercooler.baseline_graph.writer.get_entry_node_from_graph",
                side_effect=fake_lookup,
            ),
            patch(
                "watercooler.baseline_graph.writer.get_entries_for_thread",
                return_value=[],
            ),
            patch("watercooler.commands_graph.say", side_effect=fake_say),
            pytest.raises(SystemExit) as exc,
        ):
            cli.main(self._argv(tmp_path))
        assert exc.value.code == 0

        body = captured["decision_body"]
        assert "Quote-Reverification-Reason: quote_below_minimum_length" in body
        assert "matched the live source entry at promotion" in body
        assert "too short to count as durable source support" in body
        assert "did NOT confirm against the live source" not in body
        assert "- source:" not in body
