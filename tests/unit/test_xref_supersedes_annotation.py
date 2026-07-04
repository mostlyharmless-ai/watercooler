"""D5/D6 step 1: the xref_supersedes annotation kind.

The durable, append-only authored record of a ratified supersession (earned-edge RFC P3).
Its presence on an entry is what flips a supersession badge afforded→solid — replacing the
mutable T2 ``superseded_ratified`` flag shipped in #1037.
"""

from watercooler.baseline_graph.annotations import (
    VALID_KINDS,
    AnnotationEvent,
    AnnotationState,
    materialize_all_states,
)


def _ev(target, value, kind="xref_supersedes", ts="2026-07-01T00:00:00Z"):
    return AnnotationEvent(
        id=ts, target_id=target, target_type="entry", kind=kind, value=value,
        actor="caleb", timestamp=ts,
    )


def test_kind_is_valid():
    assert {"xref_supersedes", "xref_supersedes_remove"} <= VALID_KINDS


def test_records_successor_on_target_entry():
    states = materialize_all_states([_ev("01A", "01B")])
    assert states["01A"].xref_supersedes == ["01B"]


def test_is_append_only_and_deduped():
    states = materialize_all_states([_ev("01A", "01B", ts="t1"), _ev("01A", "01B", ts="t2")])
    assert states["01A"].xref_supersedes == ["01B"]


def test_remove_reverses():
    states = materialize_all_states(
        [_ev("01A", "01B", ts="t1"), _ev("01A", "01B", kind="xref_supersedes_remove", ts="t2")]
    )
    assert states["01A"].xref_supersedes == []


def test_roundtrips_through_dict():
    st = AnnotationState(xref_supersedes=["01B", "01C"])
    assert AnnotationState.from_dict(st.to_dict()).xref_supersedes == ["01B", "01C"]


def test_independent_from_plain_xref():
    """A ratified supersession must not leak into the generic xrefs list, and vice versa."""
    states = materialize_all_states([_ev("01A", "01B"), _ev("01A", "01X", kind="xref")])
    assert states["01A"].xref_supersedes == ["01B"]
    assert states["01A"].xrefs == ["01X"]
