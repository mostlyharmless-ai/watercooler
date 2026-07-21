"""F1 candidate lifecycle — disposition state machine + TTL sweep.

Commons cluster Decision 01KXQ32Q7Z41F0P7A1JHN0S527, plan
workflow-packs-prepare-work-discovery-2026-05-29:85 (rereview-required test set
from :84): resolution is a state-machine fold over numeric thread index (stable
total-order fallback), `promoted`/`rejected` are absorbing, a genuine
`Promoted-From:` entry is synthetic-resolved (#886), `expired` alone is dormant
and directly promotable, the TTL sweep is Learning-only and ball-preserving,
and owner resolution prefers the immutable emission stamp.
"""

from __future__ import annotations

import itertools
import random
from datetime import datetime, timedelta, timezone

import pytest

from watercooler.promotion import (
    CandidateMetadata,
    PromotionError,
    candidate_expires_at,
    candidate_has_terminal_disposition,
    format_candidate_expiry_body,
    parse_candidate_body,
    plan_candidate_expiries,
    resolve_candidate_state,
    validate_candidate_for_promotion,
)

CAND = "01AAAAAAAAAAAAAAAAAAAAAAAA"
OTHER = "01BBBBBBBBBBBBBBBBBBBBBBBB"

_NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)


def _candidate_entry(
    entry_id: str = CAND,
    *,
    index: int = 1,
    timestamp: str = "2026-06-01T00:00:00+00:00",
    candidate_type: str = "Learning",
    owner_stamp: str | None = None,
) -> dict:
    owner_line = f"Disposition-Owner: {owner_stamp}\n" if owner_stamp else ""
    return {
        "id": entry_id,
        "entry_id": entry_id,
        "index": index,
        "entry_type": "Note",
        "timestamp": timestamp,
        "title": f"Learning candidate {entry_id}",
        "body": (
            "Spec: learnings\n"
            f"Candidate-Type: {candidate_type}\n"
            "Candidate-Status: needs_human_confirmation\n"
            "Surface-Kind: learning\n"
            "Authority: none\n"
            f"{owner_line}"
            "Confidence: 4/5\n\n"
            "## Candidate learning\nAlways verify before asserting.\n\n"
            "## Root cause\nSpeculation presented as observation.\n\n"
            "## Fix\nCite the determining evidence.\n\n"
            "## Evidence (verbatim)\n> quoted line\n"
        ),
    }


def _disposition(kind: str, target: str = CAND, *, index: int, entry_id: str = "") -> dict:
    eid = entry_id or f"01DISP{index:020d}"
    return {
        "id": eid,
        "entry_id": eid,
        "index": index,
        "entry_type": "Note",
        "timestamp": f"2026-06-{min(28, index + 1):02d}T00:00:00+00:00",
        "body": (
            "Spec: candidate-disposition\n"
            f"CandidateDisposition: {kind}\n"
            f"Disposition-Target: {target}\n"
        ),
    }


def _promoted_entry(target: str = CAND, *, index: int) -> dict:
    eid = f"01PROM{index:020d}"
    return {
        "id": eid,
        "entry_id": eid,
        "index": index,
        "entry_type": "Note",
        "timestamp": f"2026-06-{min(28, index + 1):02d}T00:00:00+00:00",
        "body": (
            "Spec: learnings-promoted\n"
            f"Promoted-From: {target}\n"
            "Authority-Basis: human_promoted\n\n"
            "## Lesson\nAlways verify before asserting.\n"
        ),
    }


def _meta(entries_body_source: dict | None = None) -> CandidateMetadata:
    src = entries_body_source or _candidate_entry()
    return parse_candidate_body(src["body"], CAND, "topic-x")


# ---------------------------------------------------------------------------
# State-machine fold
# ---------------------------------------------------------------------------


class TestResolveCandidateState:
    def test_no_evidence_is_pending(self):
        assert resolve_candidate_state(CAND, [_candidate_entry()]).state == "pending"

    def test_shuffled_input_ordering_is_identical(self):
        entries = [
            _candidate_entry(index=1),
            _disposition("expired", index=3),
            _promoted_entry(index=5),
            _disposition("keep_exploring", index=7),
        ]
        states = set()
        for perm in itertools.permutations(entries):
            states.add(resolve_candidate_state(CAND, list(perm)).state)
        assert states == {"promoted"}

    def test_promoted_and_rejected_absorb(self):
        entries = [
            _candidate_entry(index=1),
            _disposition("rejected", index=2),
            _disposition("keep_exploring", index=3),
            _disposition("expired", index=4),
        ]
        assert resolve_candidate_state(CAND, entries).state == "rejected"

    def test_rejected_then_keep_exploring_does_not_reopen(self):
        entries = [
            _candidate_entry(index=1),
            _disposition("rejected", index=2),
            _disposition("keep_exploring", index=3),
        ]
        assert candidate_has_terminal_disposition(CAND, entries) is True

    def test_promoted_entry_without_disposition_is_resolved(self):
        # #886 synthetic resolved state: the paired disposition write failed.
        entries = [_candidate_entry(index=1), _promoted_entry(index=2)]
        st = resolve_candidate_state(CAND, entries)
        assert st.state == "promoted"
        assert st.evidence_source == "promoted_entry"

    def test_expired_then_promotion_commit_without_disposition_is_promoted(self):
        entries = [
            _candidate_entry(index=1),
            _disposition("expired", index=2),
            _promoted_entry(index=3),
        ]
        assert resolve_candidate_state(CAND, entries).state == "promoted"

    def test_expired_is_dormant_not_terminal(self):
        entries = [_candidate_entry(index=1), _disposition("expired", index=2)]
        assert resolve_candidate_state(CAND, entries).state == "expired"
        assert candidate_has_terminal_disposition(CAND, entries) is False

    def test_fold_fallback_orders_unindexed_entries_stably(self):
        # No numeric index anywhere: order falls back to (timestamp, entry_id).
        cand = _candidate_entry(index=1)
        del cand["index"]
        rej = _disposition("rejected", index=2)
        del rej["index"]
        rej["timestamp"] = "2026-06-02T00:00:00+00:00"
        keep = _disposition("keep_exploring", index=3)
        del keep["index"]
        keep["timestamp"] = "2026-06-03T00:00:00+00:00"
        entries = [keep, cand, rej]
        for perm in itertools.permutations(entries):
            assert resolve_candidate_state(CAND, list(perm)).state == "rejected"

    def test_dispositions_for_other_candidates_are_ignored(self):
        entries = [
            _candidate_entry(index=1),
            _disposition("rejected", target=OTHER, index=2),
        ]
        assert resolve_candidate_state(CAND, entries).state == "pending"


# ---------------------------------------------------------------------------
# validate_candidate_for_promotion transitions
# ---------------------------------------------------------------------------


class TestValidateTransitions:
    def test_expired_to_promoted_is_accepted(self):
        entries = [_candidate_entry(index=1), _disposition("expired", index=2)]
        # Must not raise: expired is dormant and directly promotable.
        validate_candidate_for_promotion(
            _meta(), "Learning", "github:caleb",
            existing_thread_entries=entries,
        )

    def test_rejected_blocks_with_disposition_message(self):
        entries = [_candidate_entry(index=1), _disposition("rejected", index=2)]
        with pytest.raises(PromotionError, match="kind='rejected'"):
            validate_candidate_for_promotion(
                _meta(), "Learning", "github:caleb",
                existing_thread_entries=entries,
            )

    def test_promoted_entry_blocks_with_886_message(self):
        entries = [_candidate_entry(index=1), _promoted_entry(index=2)]
        with pytest.raises(PromotionError, match="Promoted-From"):
            validate_candidate_for_promotion(
                _meta(), "Learning", "github:caleb",
                existing_thread_entries=entries,
            )

    def test_stale_keep_exploring_after_rejected_still_blocks(self):
        entries = [
            _candidate_entry(index=1),
            _disposition("rejected", index=2),
            _disposition("keep_exploring", index=3),
        ]
        with pytest.raises(PromotionError):
            validate_candidate_for_promotion(
                _meta(), "Learning", "github:caleb",
                existing_thread_entries=entries,
            )


# ---------------------------------------------------------------------------
# TTL computation + sweep planner
# ---------------------------------------------------------------------------


class TestTtlSweep:
    def test_expires_at_is_emission_plus_ttl(self):
        out = candidate_expires_at("2026-06-01T00:00:00+00:00", 30)
        assert out is not None and out.startswith("2026-07-01T00:00:00")

    def test_expires_at_handles_z_suffix_and_garbage(self):
        assert candidate_expires_at("2026-06-01T00:00:00Z", 30) is not None
        assert candidate_expires_at("not-a-timestamp", 30) is None
        assert candidate_expires_at("", 30) is None

    def test_overdue_learning_candidate_is_planned(self):
        entries = [_candidate_entry(index=1, timestamp="2026-06-01T00:00:00+00:00")]
        plans = plan_candidate_expiries("topic-x", entries, now=_NOW, ttl_days=30)
        assert [p.candidate_entry_id for p in plans] == [CAND]
        body = plans[0].body
        assert "CandidateDisposition: expired" in body
        assert f"Disposition-Target: {CAND}" in body

    def test_fresh_candidate_is_not_planned(self):
        fresh = (_NOW - timedelta(days=5)).isoformat()
        entries = [_candidate_entry(index=1, timestamp=fresh)]
        assert plan_candidate_expiries("topic-x", entries, now=_NOW, ttl_days=30) == []

    def test_sweep_is_idempotent(self):
        # An already-expired candidate is not pending → planned exactly never again.
        entries = [
            _candidate_entry(index=1, timestamp="2026-06-01T00:00:00+00:00"),
            _disposition("expired", index=2),
        ]
        assert plan_candidate_expiries("topic-x", entries, now=_NOW, ttl_days=30) == []

    def test_excluded_candidate_types_are_untouched(self):
        # Decision/Supersession candidate shapes are governance candidates —
        # never swept by the learning-lifecycle TTL.
        entries = [
            _candidate_entry(index=1, candidate_type="Decision",
                             timestamp="2026-01-01T00:00:00+00:00"),
            _candidate_entry(entry_id=OTHER, index=2, candidate_type="Supersession",
                             timestamp="2026-01-01T00:00:00+00:00"),
        ]
        assert plan_candidate_expiries("topic-x", entries, now=_NOW, ttl_days=30) == []

    def test_terminal_candidates_are_not_planned(self):
        entries = [
            _candidate_entry(index=1, timestamp="2026-06-01T00:00:00+00:00"),
            _disposition("rejected", index=2),
        ]
        assert plan_candidate_expiries("topic-x", entries, now=_NOW, ttl_days=30) == []

    def test_owner_stamp_carried_into_expiry_body(self):
        entries = [
            _candidate_entry(index=1, timestamp="2026-06-01T00:00:00+00:00",
                             owner_stamp="Claude Code (caleb)")
        ]
        plans = plan_candidate_expiries("topic-x", entries, now=_NOW, ttl_days=30)
        assert "Disposition-Owner: Claude Code (caleb)" in plans[0].body

    def test_planner_handles_prefixed_graph_node_ids(self):
        # entries.jsonl stores ids as "entry:<ULID>" — the planner must emit the
        # BARE ulid so the expiry Note's Disposition-Target matches every
        # consumer (review #1130 P1: an expiry keyed on the prefixed form never
        # joins to the candidate in the pending listing).
        node = _candidate_entry(index=1, timestamp="2026-06-01T00:00:00+00:00")
        node["id"] = f"entry:{CAND}"
        del node["entry_id"]
        plans = plan_candidate_expiries("topic-x", [node], now=_NOW, ttl_days=30)
        assert [p.candidate_entry_id for p in plans] == [CAND]

    def test_planner_falls_back_to_thread_ball_for_owner(self):
        # Review #1130 P1: historical (pre-stamp) candidates record the source
        # thread's current ball-holder as the disposition owner on expiry.
        entries = [_candidate_entry(index=1, timestamp="2026-06-01T00:00:00+00:00")]
        plans = plan_candidate_expiries(
            "topic-x", entries, now=_NOW, ttl_days=30, thread_ball="Jay"
        )
        assert "Disposition-Owner: Jay" in plans[0].body

    def test_planner_stamp_beats_thread_ball(self):
        entries = [
            _candidate_entry(index=1, timestamp="2026-06-01T00:00:00+00:00",
                             owner_stamp="Claude Code (caleb)")
        ]
        plans = plan_candidate_expiries(
            "topic-x", entries, now=_NOW, ttl_days=30, thread_ball="Jay"
        )
        assert "Disposition-Owner: Claude Code (caleb)" in plans[0].body
        assert "Disposition-Owner: Jay" not in plans[0].body

    def test_planner_is_deterministic(self):
        entries = [
            _candidate_entry(index=1, timestamp="2026-06-01T00:00:00+00:00"),
            _candidate_entry(entry_id=OTHER, index=2,
                             timestamp="2026-06-02T00:00:00+00:00"),
        ]
        shuffled = entries[:]
        random.Random(7).shuffle(shuffled)
        a = plan_candidate_expiries("topic-x", entries, now=_NOW, ttl_days=30)
        b = plan_candidate_expiries("topic-x", shuffled, now=_NOW, ttl_days=30)
        assert {p.candidate_entry_id for p in a} == {p.candidate_entry_id for p in b}


# ---------------------------------------------------------------------------
# Expiry body ↔ resolver round-trip
# ---------------------------------------------------------------------------


class TestExpiryBodyRoundTrip:
    def test_expiry_body_is_read_as_dormant_expired(self):
        body = format_candidate_expiry_body(
            CAND, "topic-x", ttl_days=30,
            emitted_at="2026-06-01T00:00:00+00:00",
        )
        entries = [
            _candidate_entry(index=1),
            {"id": "01EXP00000000000000000000X", "entry_id": "01EXP00000000000000000000X",
             "index": 2, "entry_type": "Note",
             "timestamp": "2026-07-01T00:00:00+00:00", "body": body},
        ]
        st = resolve_candidate_state(CAND, entries)
        assert st.state == "expired"
        assert candidate_has_terminal_disposition(CAND, entries) is False


# ---------------------------------------------------------------------------
# Listing: include_expired + owner precedence
# ---------------------------------------------------------------------------


class TestPendingListing:
    @staticmethod
    def _collect(entries, **kw):
        from watercooler_mcp.tools.decisions import _collect_pending_for_topic

        return _collect_pending_for_topic("topic-x", entries, **kw)

    def test_expired_excluded_by_default_included_on_flag(self):
        entries = [_candidate_entry(index=1), _disposition("expired", index=2)]
        assert self._collect(entries) == []
        rows = self._collect(entries, include_expired=True)
        assert len(rows) == 1 and rows[0]["state"] == "expired"

    def test_pending_row_carries_lifecycle_fields(self):
        rows = self._collect([_candidate_entry(index=1)])
        assert rows[0]["state"] == "pending"
        assert rows[0]["expires_at"] is not None

    def test_terminal_candidates_never_listed_even_with_flag(self):
        entries = [_candidate_entry(index=1), _disposition("rejected", index=2)]
        assert self._collect(entries, include_expired=True) == []

    def test_owner_emission_stamp_wins_over_ball(self):
        rows = self._collect(
            [_candidate_entry(index=1, owner_stamp="Jay")],
            thread_ball="Claude Code (caleb)",
        )
        assert rows[0]["disposition_owner"] == "Jay"
        assert rows[0]["owner_source"] == "emission_stamp"

    def test_owner_falls_back_to_ball_holder(self):
        rows = self._collect(
            [_candidate_entry(index=1)], thread_ball="Claude Code (caleb)"
        )
        assert rows[0]["disposition_owner"] == "Claude Code (caleb)"
        assert rows[0]["owner_source"] == "ball_holder"

    def test_owner_unavailable_without_stamp_or_ball(self):
        rows = self._collect([_candidate_entry(index=1)])
        assert rows[0]["disposition_owner"] is None
        assert rows[0]["owner_source"] == "unavailable"

    def test_expires_at_honors_non_default_ttl(self):
        # Review #1130 P1: the listing's expires_at must reflect the EFFECTIVE
        # TTL, never a hard-coded 30 — a configured override changes the date.
        rows = self._collect(
            [_candidate_entry(index=1, timestamp="2026-06-01T00:00:00+00:00")],
            ttl_days=7,
        )
        assert rows[0]["expires_at"].startswith("2026-06-08T00:00:00")

    def test_expires_at_default_resolves_from_config(self, monkeypatch):
        # With no explicit ttl_days the collector consults the config-backed
        # resolver (patched here) rather than a literal 30.
        from watercooler_mcp.tools import decisions as d

        monkeypatch.setattr(d, "_effective_candidate_ttl_days", lambda: 10)
        rows = self._collect(
            [_candidate_entry(index=1, timestamp="2026-06-01T00:00:00+00:00")]
        )
        assert rows[0]["expires_at"].startswith("2026-06-11T00:00:00")


class TestCliTtlBounds:
    def test_cli_rejects_out_of_bounds_ttl(self, capsys):
        # Review #1130 P1: --ttl-days must honor the schema bounds (1..365) —
        # an unbounded override could instantly expire the whole queue.
        from watercooler.cli import main

        for bad in (0, -5, 366):
            with pytest.raises(SystemExit) as exc:
                main(["sweep-expired-candidates", "--ttl-days", str(bad),
                      "--threads-dir", "/nonexistent", "--dry-run"])
            assert exc.value.code == 1
        assert "between 1 and 365" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Sweep write path preserves ball AND status (graph-backed)
# ---------------------------------------------------------------------------


class TestSweepWritePreservesBall:
    def test_ack_write_leaves_ball_and_status_unchanged(self, tmp_path):
        from ulid import ULID

        from watercooler.commands_graph import ack, append_entry, get_thread_from_graph

        threads_dir = tmp_path / ".watercooler"
        threads_dir.mkdir()
        append_entry(
            "topic-x", threads_dir=threads_dir, agent="Jay", role="planner",
            title="seed", body="seed", ball="Jay", status="OPEN",
            entry_id=str(ULID()),
        )
        before = get_thread_from_graph(threads_dir, "topic-x")
        assert before["ball"] == "Jay"

        body = format_candidate_expiry_body(
            CAND, "topic-x", ttl_days=30,
            emitted_at="2026-06-01T00:00:00+00:00",
        )
        ack(
            "topic-x", threads_dir=threads_dir,
            agent="Candidate Lifecycle Sweep", role="scribe",
            title=f"CandidateDisposition: expired {CAND}",
            entry_type="Note", body=body, entry_id=str(ULID()),
        )
        after = get_thread_from_graph(threads_dir, "topic-x")
        assert after["ball"] == before["ball"] == "Jay"
        assert after["status"] == before["status"]
