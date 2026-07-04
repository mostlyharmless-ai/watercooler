"""Unit tests for the earned-edge supersession write path (Phase 1).

Covers the ``Edge.supersedes()`` factory and the earned-edge payload fields.
Pure model tests — no Graphiti/FalkorDB backend required.
"""

from watercooler_memory.schema import Edge, EdgeType


def test_supersedes_direction_is_new_to_old():
    """source→target reads new→old, so 'what superseded X' is the incoming edge to X."""
    edge = Edge.supersedes("superseding_new", "superseded_old")
    assert edge.edge_type is EdgeType.SUPERSEDES
    assert edge.source_id == "superseding_new"
    assert edge.target_id == "superseded_old"


def test_supersedes_is_earned_and_afforded_by_default():
    """A freshly-inferred supersession is earned + probabilistic until ratified."""
    edge = Edge.supersedes("new", "old")
    assert edge.basis == "earned"
    assert edge.ratification_status == "afforded"


def test_supersedes_carries_evidence_confidence_and_window():
    edge = Edge.supersedes(
        "new",
        "old",
        event_time="2026-06-01T00:00:00Z",
        invalid_at="2026-06-02T00:00:00Z",
        confidence=0.8,
        evidence=["entry:01OLD", "episode:abc"],
        source_entry_id="01SRC",
    )
    assert edge.valid_from == "2026-06-01T00:00:00Z"
    assert edge.valid_until == "2026-06-02T00:00:00Z"
    assert edge.confidence == 0.8
    assert edge.evidence == ["entry:01OLD", "episode:abc"]
    assert edge.source_entry_id == "01SRC"


def test_supersedes_copies_evidence_list():
    """Evidence must be copied, not aliased, so caller mutation can't leak in."""
    src = ["entry:01OLD"]
    edge = Edge.supersedes("new", "old", evidence=src)
    src.append("entry:LEAK")
    assert edge.evidence == ["entry:01OLD"]


def test_supersedes_can_be_marked_ratified():
    edge = Edge.supersedes("new", "old", ratification_status="ratified")
    assert edge.ratification_status == "ratified"


def test_authored_edges_leave_earned_payload_at_defaults():
    """contains/follows must not accidentally look like earned edges."""
    for edge in (Edge.contains("parent", "child"), Edge.follows("a", "b")):
        assert edge.basis == "authored"
        assert edge.evidence == []
        assert edge.ratification_status is None
        assert edge.valid_until is None
        assert edge.confidence is None
        assert edge.source_entry_id is None


def test_evidence_defaults_to_independent_lists():
    """Two edges must not share the same default evidence list instance."""
    a = Edge.supersedes("n1", "o1")
    b = Edge.supersedes("n2", "o2")
    a.evidence.append("entry:X")
    assert b.evidence == []
