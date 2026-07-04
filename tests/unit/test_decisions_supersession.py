"""Tests for the T2 supersession enrichment in list_decisions (#894).

Exercises the orchestration helper with a fake backend — no live FalkorDB. The
supersession aggregation rule itself is covered in test_supersession.py.
"""

from __future__ import annotations

from watercooler_mcp.tools.decisions import (
    _apply_supersession,
    _unknown_supersession,
)


def _edge(episodes, invalid_at=None):
    return {"episodes": list(episodes), "invalid_at": invalid_at, "fact": "x"}


class _FakeBackend:
    """Mimics the GraphitiBackend surface _apply_supersession depends on.

    `episode_uuids_for_entry` never raises (the real method swallows index
    errors → []). `get_edges_by_episodes` returns edges whose `episodes` list
    intersects the requested UUIDs.
    """

    def __init__(self, episodes_by_entry, edges, *, raise_edges=False):
        self._episodes = episodes_by_entry
        self._edges = edges
        self._raise_edges = raise_edges
        self.episode_query_args: list[list[str]] = []

    def episode_uuids_for_entry(self, entry_id):
        return list(self._episodes.get(entry_id, []))

    def get_edges_by_episodes(self, episode_uuids, limit=2000):
        self.episode_query_args.append(list(episode_uuids))
        if self._raise_edges:
            raise RuntimeError("edge query boom")
        wanted = set(episode_uuids)
        return [e for e in self._edges if wanted & set(e["episodes"])]


class TestUnknownSupersession:
    def test_shape(self):
        out = _unknown_supersession("t2_unavailable")
        assert out["state"] == "unknown"
        assert out["reason"] == "t2_unavailable"
        assert out["active_facts"] == 0 and out["superseded_facts"] == 0
        assert out["as_of"] is None


class TestApplySupersession:
    def test_no_backend_marks_all_unknown(self):
        collected = [{"entry_id": "d1", "topic": "t"}, {"entry_id": "d2", "topic": "t"}]
        _apply_supersession(collected, None)
        assert all(d["supersession"]["state"] == "unknown" for d in collected)
        assert all(d["supersession"]["reason"] == "t2_unavailable" for d in collected)

    def test_happy_path_single_union_query(self):
        # Two decisions across two topics: ONE edges-by-episode query covers the
        # whole page (group-agnostic — no per-thread fetch).
        backend = _FakeBackend(
            episodes_by_entry={"d1": ["epA"], "d2": ["epB"]},
            edges=[
                _edge(["epA"]),  # d1 active
                _edge(["epB"], invalid_at="2026-03-01T00:00:00Z"),  # d2 superseded
            ],
        )
        collected = [
            {"entry_id": "d1", "topic": "topicA"},
            {"entry_id": "d2", "topic": "topicB"},
        ]
        _apply_supersession(collected, backend)

        assert collected[0]["supersession"]["state"] == "in_force"
        assert collected[1]["supersession"]["state"] == "superseded"
        assert collected[1]["supersession"]["as_of"] == "2026-03-01T00:00:00Z"
        # Exactly one query, with the de-duplicated, sorted union of episodes.
        assert backend.episode_query_args == [["epA", "epB"]]

    def test_each_decision_sees_only_its_own_episode_edges(self):
        # The page-wide edge set is attributed per-decision by the pure helper,
        # so a sibling's superseded edge does not leak into another decision.
        backend = _FakeBackend(
            episodes_by_entry={"d1": ["epA"], "d2": ["epB"]},
            edges=[
                _edge(["epA"]),
                _edge(["epB"], invalid_at="2026-03-01T00:00:00Z"),
            ],
        )
        collected = [
            {"entry_id": "d1", "topic": "t"},
            {"entry_id": "d2", "topic": "t"},
        ]
        _apply_supersession(collected, backend)
        assert collected[0]["supersession"]["superseded_facts"] == 0
        assert collected[1]["supersession"]["superseded_facts"] == 1

    def test_unindexed_entry_is_unknown_not_in_force(self):
        backend = _FakeBackend(
            episodes_by_entry={},  # d1 not indexed → no episodes
            edges=[_edge(["ep1"])],
        )
        collected = [{"entry_id": "d1", "topic": "t"}]
        _apply_supersession(collected, backend)
        assert collected[0]["supersession"]["state"] == "unknown"
        assert collected[0]["supersession"]["reason"] == "no_episode_mapping"
        # No episodes in the page → no edge query issued at all.
        assert backend.episode_query_args == []

    def test_edge_query_failure_degrades_all_to_unknown(self):
        backend = _FakeBackend(
            episodes_by_entry={"d1": ["ep1"], "d2": ["ep2"]},
            edges=[],
            raise_edges=True,
        )
        collected = [
            {"entry_id": "d1", "topic": "t"},
            {"entry_id": "d2", "topic": "t"},
        ]
        _apply_supersession(collected, backend)
        assert all(d["supersession"]["state"] == "unknown" for d in collected)
        assert all(d["supersession"]["reason"] == "lookup_error" for d in collected)
