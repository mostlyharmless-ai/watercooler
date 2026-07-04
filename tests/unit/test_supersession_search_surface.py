"""D1 regression: superseded_by surfaces through the watercooler_search formatter.

Entry-6 of ``t2-supercession-testing-proposal`` requires the earned ``superseded_by``
link (written by the enrichment daemon, carried by graphiti under ``attributes``) to be
exposed by the ``watercooler_search`` graphiti formatter — the whitelisted fact dict
otherwise silently drops it. Pure-function test of ``_supersession_fields``.
"""

from watercooler_mcp.tools.graph import _supersession_fields


def test_absent_when_no_attributes():
    assert _supersession_fields({"uuid": "e1", "fact": "x"}) == {}


def test_absent_when_attributes_lack_superseded_by():
    assert _supersession_fields({"attributes": {"other": 1}}) == {}


def test_lifts_superseded_by_and_at_from_attributes():
    r = {"attributes": {"superseded_by": "succ-uuid", "superseded_at": "2024-06-01T00:00:00Z"}}
    assert _supersession_fields(r) == {
        "superseded_by": "succ-uuid",
        "superseded_at": "2024-06-01T00:00:00Z",
    }


def test_superseded_by_without_at_still_surfaces():
    assert _supersession_fields({"attributes": {"superseded_by": "succ"}}) == {
        "superseded_by": "succ"
    }


def test_tolerates_none_attributes():
    assert _supersession_fields({"attributes": None}) == {}
