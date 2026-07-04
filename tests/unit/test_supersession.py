"""Unit tests for the pure supersession-summary rule (#894).

Exercises summarize_supersession without any live T2 backend — it operates on
already-serialized edge dicts.
"""

from watercooler_memory.supersession import (
    STATE_IN_FORCE,
    STATE_PARTIALLY_SUPERSEDED,
    STATE_SUPERSEDED,
    STATE_UNKNOWN,
    summarize_supersession,
)


def _edge(episodes, invalid_at=None):
    return {"episodes": list(episodes), "invalid_at": invalid_at, "fact": "x"}


class TestSummarizeSupersession:
    def test_no_episode_mapping_is_unknown(self):
        # Entry not indexed to any episode -> unknown, never a false in_force.
        out = summarize_supersession([_edge(["ep1"])], episode_uuids=[])
        assert out["state"] == STATE_UNKNOWN
        assert out["reason"] == "no_episode_mapping"
        assert out["active_facts"] == 0 and out["superseded_facts"] == 0

    def test_no_derived_edges_is_unknown(self):
        # The entry has an episode, but no edge references it -> unknown.
        edges = [_edge(["other-ep"]), _edge(["another"], invalid_at="2026-01-01T00:00:00Z")]
        out = summarize_supersession(edges, episode_uuids=["ep1"])
        assert out["state"] == STATE_UNKNOWN
        assert out["reason"] == "no_derived_edges"

    def test_all_active_is_in_force(self):
        edges = [_edge(["ep1"]), _edge(["ep1"]), _edge(["ep1"])]
        out = summarize_supersession(edges, episode_uuids=["ep1"])
        assert out["state"] == STATE_IN_FORCE
        assert out["active_facts"] == 3
        assert out["superseded_facts"] == 0
        assert out["as_of"] is None

    def test_all_superseded_is_superseded(self):
        edges = [
            _edge(["ep1"], invalid_at="2026-02-01T00:00:00Z"),
            _edge(["ep1"], invalid_at="2026-03-01T00:00:00Z"),
        ]
        out = summarize_supersession(edges, episode_uuids=["ep1"])
        assert out["state"] == STATE_SUPERSEDED
        assert out["active_facts"] == 0
        assert out["superseded_facts"] == 2
        # as_of is the LATEST supersession instant.
        assert out["as_of"] == "2026-03-01T00:00:00Z"

    def test_mixed_is_partially_superseded(self):
        edges = [
            _edge(["ep1"]),
            _edge(["ep1"], invalid_at="2026-02-01T00:00:00Z"),
        ]
        out = summarize_supersession(edges, episode_uuids=["ep1"])
        assert out["state"] == STATE_PARTIALLY_SUPERSEDED
        assert out["active_facts"] == 1
        assert out["superseded_facts"] == 1
        assert out["as_of"] == "2026-02-01T00:00:00Z"

    def test_edges_for_other_episodes_are_ignored(self):
        # Only edges referencing the entry's episode count; sibling-entry edges
        # in the same thread/group must not leak into this entry's summary.
        edges = [
            _edge(["ep1"]),  # ours, active
            _edge(["ep2"], invalid_at="2026-02-01T00:00:00Z"),  # sibling, superseded
            _edge(["ep3", "ep2"], invalid_at="2026-02-02T00:00:00Z"),  # not ours
        ]
        out = summarize_supersession(edges, episode_uuids=["ep1"])
        assert out["state"] == STATE_IN_FORCE
        assert out["active_facts"] == 1
        assert out["superseded_facts"] == 0

    def test_entry_mapped_to_multiple_episodes(self):
        # An entry can map to >1 episode; an edge referencing ANY of them counts.
        edges = [
            _edge(["epA"]),
            _edge(["epB"], invalid_at="2026-02-01T00:00:00Z"),
            _edge(["epC"]),  # not ours
        ]
        out = summarize_supersession(edges, episode_uuids=["epA", "epB"])
        assert out["state"] == STATE_PARTIALLY_SUPERSEDED
        assert out["active_facts"] == 1
        assert out["superseded_facts"] == 1

    def test_empty_edges_with_episode_is_unknown(self):
        out = summarize_supersession([], episode_uuids=["ep1"])
        assert out["state"] == STATE_UNKNOWN
        assert out["reason"] == "no_derived_edges"

    def test_missing_episodes_field_on_edge_is_tolerated(self):
        # A malformed edge dict (no 'episodes') must not raise; it simply can't
        # match and is ignored.
        edges = [{"invalid_at": None}, _edge(["ep1"])]
        out = summarize_supersession(edges, episode_uuids=["ep1"])
        assert out["state"] == STATE_IN_FORCE
        assert out["active_facts"] == 1
