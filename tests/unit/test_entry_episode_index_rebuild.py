"""Durable entry→episode index: rebuild from FalkorDB when the node-local cache
is empty (e.g. wiped by an ephemeral hosted redeploy).

The recovery reads the episode's DURABLE fields — matching what the real write
paths persist (verified against the code, not assumed):
- hosted/hybrid (memory_sync._submit_graphiti_to_hosted): name is the *title*,
  the entry_id lives in source_description ("... | hybrid_handoff | entry:<ULID>").
- legacy/batch (add_episode): entry_id is the name prefix "{entry_id}: {title}".
- neither field carries it (older local-sync episodes) → unrecoverable, re-ingest.

Backend is built without full __init__ (avoids the graphiti_core dep); only the
graph driver is mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from watercooler_memory.backends.graphiti import GraphitiBackend
from watercooler_memory.entry_episode_index import EntryEpisodeIndex, IndexConfig

ULID_A = "01BBBBBBBBBBBBBBBBBBBBBBBB"  # 26-char ULID-shaped
ULID_B = "01CCCCCCCCCCCCCCCCCCCCCCCC"


def _backend(tmp_path, rows):
    be = object.__new__(GraphitiBackend)  # skip __init__ → no graphiti_core needed
    be.entry_episode_index = EntryEpisodeIndex(
        IndexConfig(backend="graphiti", index_path=tmp_path / "idx.json"),
        auto_load=False,
    )
    be._entry_index_rebuild_attempted = False

    class _FakeDriver:
        async def execute_query(self, cypher, **kw):
            return (list(rows), None, None)

    fake = MagicMock()
    fake.clients.driver = _FakeDriver()
    be._create_graphiti_client = MagicMock(return_value=fake)
    return be


def test_rebuild_recovers_from_durable_fields(tmp_path):
    rows = [
        # hosted/hybrid: name = title, entry_id in source_description
        {
            "uuid": "ep-a",
            "name": "Adopt option B",
            "source_description": f"thread:feat | hybrid_handoff | entry:{ULID_A}",
        },
        # legacy/batch: entry_id as the name prefix; no entry id in source_desc
        {
            "uuid": "ep-b",
            "name": f"{ULID_B}: Some title",
            "source_description": "thread:feat | Sync from baseline graph",
        },
        # older local-sync shape: NO entry_id anywhere → unrecoverable
        {
            "uuid": "ep-c",
            "name": "Note: a thought with no entry id",
            "source_description": "thread:feat | Sync from baseline graph | tags:x",
        },
        # missing uuid → skipped even though source_description has an entry id
        {"uuid": "", "name": "x", "source_description": f"entry:{ULID_A}"},
    ]
    be = _backend(tmp_path, rows)

    recovered = be.rebuild_entry_episode_index_from_graph()

    assert recovered == 2
    assert be.entry_episode_index.get_episode(ULID_A) == "ep-a"  # via source_desc
    assert be.entry_episode_index.get_episode(ULID_B) == "ep-b"  # via name prefix


def test_source_description_takes_precedence_over_name(tmp_path):
    # If both carry an id, the durable source_description wins (the hosted shape).
    rows = [
        {
            "uuid": "ep",
            "name": f"{ULID_B}: title",
            "source_description": f"thread:t | hybrid_handoff | entry:{ULID_A}",
        }
    ]
    be = _backend(tmp_path, rows)
    be.rebuild_entry_episode_index_from_graph()
    assert be.entry_episode_index.get_episode(ULID_A) == "ep"
    assert be.entry_episode_index.get_episode(ULID_B) is None


def test_episode_uuids_for_entry_lazily_rebuilds_when_empty(tmp_path):
    be = _backend(
        tmp_path,
        [
            {
                "uuid": "ep-a",
                "name": "Adopt B",
                "source_description": f"thread:t | hybrid_handoff | entry:{ULID_A}",
            }
        ],
    )
    assert len(be.entry_episode_index) == 0  # wiped hosted cache
    uuids = be.episode_uuids_for_entry(ULID_A)
    assert uuids == ["ep-a"]
    assert be._entry_index_rebuild_attempted is True


def test_rebuild_attempted_only_once(tmp_path):
    be = _backend(tmp_path, [])  # empty graph → nothing to recover
    be.episode_uuids_for_entry(ULID_A)
    be.episode_uuids_for_entry(ULID_B)
    assert be._create_graphiti_client.call_count == 1


def test_unrecoverable_episode_left_unmapped(tmp_path):
    # Episode with no entry_id in either durable field stays unmapped (re-ingest).
    rows = [
        {
            "uuid": "ep-c",
            "name": "Just a title",
            "source_description": "thread:t | Sync from baseline graph",
        }
    ]
    be = _backend(tmp_path, rows)
    assert be.rebuild_entry_episode_index_from_graph() == 0


def test_rebuild_handles_query_failure_gracefully(tmp_path):
    be = _backend(tmp_path, [])
    be._create_graphiti_client = MagicMock(side_effect=RuntimeError("no db"))
    assert be.rebuild_entry_episode_index_from_graph() == 0
    assert be.episode_uuids_for_entry(ULID_A) == []


# ---------------------------------------------------------------------------
# Recovery by valid_at == entry timestamp (the baseline-sync episodes, which
# carry the topic but no entry_id; valid_at is the entry's timestamp).
# ---------------------------------------------------------------------------


def test_recover_by_valid_at_timestamp_with_tz_normalization(tmp_path):
    rows = [
        {
            "uuid": "ep-ts",
            "name": "Adopt option B",  # title only, no entry_id
            "source_description": "thread:feat | Sync from baseline graph",
            "valid_at": "2026-06-28T01:17:55.804841+00:00",  # tz-aware
        }
    ]
    be = _backend(tmp_path, rows)
    # The entry timestamp from the baseline graph is tz-naive; must still match.
    recovered = be.rebuild_entry_episode_index_from_graph(
        timestamp_to_entry_id={"2026-06-28T01:17:55.804841": ULID_A}
    )
    assert recovered == 1
    assert be.entry_episode_index.get_episode(ULID_A) == "ep-ts"


def test_durable_source_description_beats_timestamp_hint(tmp_path):
    rows = [
        {
            "uuid": "ep",
            "name": "title",
            "source_description": f"thread:t | hybrid_handoff | entry:{ULID_A}",
            "valid_at": "2026-06-28T01:17:55.804841+00:00",
        }
    ]
    be = _backend(tmp_path, rows)
    be.rebuild_entry_episode_index_from_graph(
        timestamp_to_entry_id={"2026-06-28T01:17:55.804841": ULID_B}  # conflicting
    )
    assert be.entry_episode_index.get_episode(ULID_A) == "ep"  # source_desc wins
    assert be.entry_episode_index.get_episode(ULID_B) is None


def test_baseline_sync_unrecoverable_without_hint(tmp_path):
    rows = [
        {
            "uuid": "ep",
            "name": "title",
            "source_description": "thread:t | Sync from baseline graph",
            "valid_at": "2026-06-28T01:17:55.804841+00:00",
        }
    ]
    be = _backend(tmp_path, rows)
    assert be.rebuild_entry_episode_index_from_graph() == 0  # no hint → no match


def test_timestamp_hint_no_false_match(tmp_path):
    # A hint whose timestamp matches no episode recovers nothing.
    rows = [
        {
            "uuid": "ep",
            "name": "title",
            "source_description": "thread:t | Sync from baseline graph",
            "valid_at": "2026-06-28T01:17:55.804841+00:00",
        }
    ]
    be = _backend(tmp_path, rows)
    n = be.rebuild_entry_episode_index_from_graph(
        timestamp_to_entry_id={"2020-01-01T00:00:00+00:00": ULID_A}
    )
    assert n == 0


def test_rebuild_recovers_later_entry_when_index_already_nonempty(tmp_path):
    # The first (partial) recovery must not strand later baseline-sync entries:
    # a second rebuild with a new hint recovers it even though the index is
    # already non-empty (review #1012).
    rows = [
        {
            "uuid": "ep-a",
            "name": "A",
            "source_description": "thread:t | Sync from baseline graph",
            "valid_at": "2026-06-28T01:00:00+00:00",
        },
        {
            "uuid": "ep-b",
            "name": "B",
            "source_description": "thread:t | Sync from baseline graph",
            "valid_at": "2026-06-28T02:00:00+00:00",
        },
    ]
    be = _backend(tmp_path, rows)
    be.rebuild_entry_episode_index_from_graph(
        timestamp_to_entry_id={"2026-06-28T01:00:00+00:00": ULID_A}
    )
    assert be.entry_episode_index.get_episode(ULID_A) == "ep-a"
    assert len(be.entry_episode_index) >= 1  # index is now non-empty
    be.rebuild_entry_episode_index_from_graph(
        timestamp_to_entry_id={"2026-06-28T02:00:00+00:00": ULID_B}
    )
    assert be.entry_episode_index.get_episode(ULID_B) == "ep-b"


def test_apply_supersession_recovers_missing_decision_when_index_nonempty(tmp_path):
    # The fix at the call site: a later topic-scoped page whose Decision is
    # missing from an already-non-empty index still triggers timestamp recovery.
    from watercooler_mcp.tools.decisions import _apply_supersession

    idx = EntryEpisodeIndex(
        IndexConfig(backend="graphiti", index_path=tmp_path / "i.json"),
        auto_load=False,
    )
    idx.add(ULID_A, "ep-a", "")  # a prior page already populated the index

    backend = MagicMock()
    backend.entry_episode_index = idx

    def _rebuild(timestamp_to_entry_id=None):
        for _ts, eid in (timestamp_to_entry_id or {}).items():
            idx.add(eid, "ep-b", "")
        return len(timestamp_to_entry_id or {})

    backend.rebuild_entry_episode_index_from_graph.side_effect = _rebuild
    backend.episode_uuids_for_entry.side_effect = lambda eid: (
        [idx.get_episode(eid)] if idx.get_episode(eid) else []
    )
    backend.get_edges_by_episodes.return_value = []

    collected = [{"entry_id": ULID_B, "timestamp": "2026-06-28T02:00:00+00:00"}]
    _apply_supersession(collected, backend)

    backend.rebuild_entry_episode_index_from_graph.assert_called_once()
    assert idx.get_episode(ULID_B) == "ep-b"
    # recovered → not no_episode_mapping (no_derived_edges here since edges=[])
    assert collected[0]["supersession"]["reason"] != "no_episode_mapping"
