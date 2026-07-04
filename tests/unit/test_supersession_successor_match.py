"""Unit tests for the #991 successor-matching heuristic.

Pure-function tests for ``_match_superseded_successors`` — no live FalkorDB needed,
so they run in the standard CI gate (unlike the live enrichment integration test).
"""

from watercooler_memory.backends.graphiti import _match_superseded_successors


def _edge(uuid, src, name, valid_at, invalid_at=None, superseded_by=None, created_at=None):
    return {"uuid": uuid, "src": src, "name": name, "valid_at": valid_at,
            "invalid_at": invalid_at, "superseded_by": superseded_by, "created_at": created_at}


T0, T1, T2 = "2023-06-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00", "2024-06-01T00:00:00+00:00"


def test_links_superseded_to_active_at_the_boundary_instant():
    edges = [
        _edge("old", "alex", "IS_LEAD_ENGINEER_OF", T1, invalid_at=T2, created_at=T0),
        _edge("new", "alex", "IS_LEAD_ENGINEER_OF", T2),  # active successor
    ]
    pairs = _match_superseded_successors(edges)
    # Carries the determining basis (cite-evidence) + the superseded edge's own created_at
    # (temporal provenance — here T0 predates invalid_at T2, i.e. a genuine transition).
    assert pairs == [{"superseded": "old", "successor": "new", "superseded_at": T2,
                      "name": "IS_LEAD_ENGINEER_OF", "basis": "same_source_and_name",
                      "superseded_created_at": T0}]


def test_falls_back_across_different_name_and_source():
    """Real extraction: superseded MANAGES / successor IS_MANAGER_OF, dup source node."""
    edges = [
        _edge("old", "riley_a", "MANAGES", T1, invalid_at=T2),
        _edge("new", "riley_b", "IS_MANAGER_OF", T2),  # diff name + diff source node
    ]
    pairs = _match_superseded_successors(edges)
    assert len(pairs) == 1 and pairs[0]["superseded"] == "old" and pairs[0]["successor"] == "new"
    assert pairs[0]["basis"] == "temporal_only"  # neither source nor name matched


def test_prefers_same_source_then_same_name_when_multiple_candidates():
    edges = [
        _edge("old", "s1", "R", T1, invalid_at=T2),
        _edge("other_src", "s2", "R", T2),        # same instant, wrong source
        _edge("same_src_wrong_name", "s1", "Q", T2),
        _edge("same_src_same_name", "s1", "R", T2),  # best: same source + same name
    ]
    pairs = _match_superseded_successors(edges)
    assert pairs[0]["successor"] == "same_src_same_name"
    assert pairs[0]["basis"] == "same_source_and_name"


def test_skips_when_no_active_edge_at_the_instant():
    edges = [
        _edge("old", "s", "R", T1, invalid_at=T2),
        _edge("unrelated_active", "s", "R", T1),  # active but valid_at != T2
    ]
    assert _match_superseded_successors(edges) == []


def test_skips_already_linked_edges_idempotent():
    edges = [
        _edge("old", "s", "R", T1, invalid_at=T2, superseded_by="new"),
        _edge("new", "s", "R", T2),
    ]
    assert _match_superseded_successors(edges) == []


def test_active_edges_are_never_treated_as_superseded():
    edges = [_edge("a", "s", "R", T1), _edge("b", "s", "R", T2)]
    assert _match_superseded_successors(edges) == []
