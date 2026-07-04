"""Tests for the decision_origin surfacing + filter on list_decisions (#897a).

Exercises the pure record-builder and filter helpers directly (no live graph).
The decision_origin filter scopes to promoted Decisions (`human_promoted`) so the
deferred early_supersession_hazard denominator can be built without counting legacy
or hand-authored Decisions as promoted.
"""

from __future__ import annotations

from watercooler.promotion import build_promotion_authority_fields
from watercooler_mcp.tools.decisions import (
    _build_decision_record,
    _decision_matches_filters,
)


def _node(**overrides):
    node = {
        "id": "entry:01DECISION0000000000000AAA",
        "title": "A Decision",
        "timestamp": "2026-06-07T00:00:00Z",
        "agent": "Someone",
        "role": "planner",
        "body": "",
    }
    node.update(overrides)
    return node


def _record(node):
    return _build_decision_record(
        node=node, topic="t", xrefs=[], tags=[], source=None, extracted=False
    )


class TestDecisionOriginSurfacing:
    """_build_decision_record surfaces decision_origin, None-safe."""

    def test_human_promoted_origin_surfaced(self):
        rec = _record(_node(decision_origin="human_promoted"))
        assert rec["decision_origin"] == "human_promoted"

    def test_agent_authored_origin_surfaced(self):
        rec = _record(_node(decision_origin="agent_authored"))
        assert rec["decision_origin"] == "agent_authored"

    def test_legacy_unstamped_origin_is_none(self):
        # A node with no decision_origin (legacy) surfaces None, not a guess.
        rec = _record(_node())
        assert rec["decision_origin"] is None


class TestDecisionOriginFilter:
    """_decision_matches_filters scopes correctly on decision_origin."""

    _BASE = dict(
        topic=None,
        confidence_min=0,
        since_dt=None,
        until_dt=None,
        source_entry_id=None,
        only_extracted=False,
    )

    def test_no_filter_matches_any_origin(self):
        for origin in ("human_promoted", "agent_authored", None):
            rec = _record(_node(decision_origin=origin))
            assert _decision_matches_filters(rec, **self._BASE, decision_origin=None)

    def test_filter_matches_exact_origin(self):
        rec = _record(_node(decision_origin="human_promoted"))
        assert _decision_matches_filters(
            rec, **self._BASE, decision_origin="human_promoted"
        )

    def test_filter_excludes_mismatched_origin(self):
        rec = _record(_node(decision_origin="agent_authored"))
        assert not _decision_matches_filters(
            rec, **self._BASE, decision_origin="human_promoted"
        )

    def test_filter_excludes_legacy_none_origin(self):
        # The load-bearing case: legacy/unstamped Decisions must NOT be counted as
        # human_promoted, or the deferred hazard denominator over-counts.
        rec = _record(_node())  # decision_origin absent -> None
        assert not _decision_matches_filters(
            rec, **self._BASE, decision_origin="human_promoted"
        )


class TestPromotionStampsHumanPromoted:
    """Both MCP and CLI promote paths share build_promotion_authority_fields, which
    always stamps decision_origin='human_promoted' — so neither channel is invisible
    to a decision_origin='human_promoted' filter (#897a denominator integrity)."""

    def test_authority_fields_always_stamp_human_promoted(self):
        fields = build_promotion_authority_fields(human_authorized_by="github:alice")
        assert fields["decision_origin"] == "human_promoted"

    def test_stamp_survives_to_record_origin(self):
        # End-to-end at the read side: a node carrying the promotion authority fields
        # surfaces as decision_origin='human_promoted' and matches the filter.
        fields = build_promotion_authority_fields(human_authorized_by="github:bob")
        rec = _record(_node(decision_origin=fields["decision_origin"]))
        assert _decision_matches_filters(
            rec,
            topic=None,
            confidence_min=0,
            since_dt=None,
            until_dt=None,
            source_entry_id=None,
            only_extracted=False,
            decision_origin="human_promoted",
        )
