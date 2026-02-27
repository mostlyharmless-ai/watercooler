"""Integration tests for T2 bi-temporal edge invalidation (supersession).

Verifies that Graphiti's automatic edge-invalidation mechanism works end-to-end
in watercooler-cloud:
  - Mutually exclusive facts → exactly one edge gets invalid_at set
  - Additive (non-contradicting) facts → both edges remain active
  - Temporal semantics → earlier fact is superseded when role changes

These tests require a live FalkorDB instance and a configured LLM provider.

Markers: @pytest.mark.integration @pytest.mark.integration_graphiti
Usage:   pytest tests/integration/test_t2_supersession.py -v -m integration_graphiti
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

import pytest

# Skip the entire module if live backend dependencies are absent
pytestmark = [
  pytest.mark.integration,
  pytest.mark.integration_graphiti,
]


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def graphiti_backend(tmp_path_factory: pytest.TempPathFactory) -> Generator:
  """Live GraphitiBackend with temp work dir.

  Mirrors the fixture in test_backend_smoke.py.
  """
  from watercooler_memory.backends.graphiti import (
    GraphitiBackend,
    GraphitiConfig,
    ConfigError,
  )

  if "OPENAI_API_KEY" not in os.environ:
    pytest.skip("OPENAI_API_KEY not set — required for Graphiti entity extraction")

  tmp_path = tmp_path_factory.mktemp("pytest__supersession_graphiti")
  config = GraphitiConfig(work_dir=tmp_path / "graphiti_work", test_mode=True)
  try:
    backend = GraphitiBackend(config)
  except ConfigError as e:
    if "No module named" in str(e):
      pytest.skip(f"Graphiti dependencies not installed: {e}")
    raise
  yield backend


def _uid() -> str:
  """Short random suffix for test entity isolation."""
  return uuid.uuid4().hex[:8]


def _add_episode_sync(backend, name: str, body: str, ref_time: datetime, group_id: str) -> str:
  """Synchronous wrapper around add_episode_direct for use in tests."""
  result = asyncio.run(
    backend.add_episode_direct(
      name=name,
      episode_body=body,
      source_description="integration-test",
      reference_time=ref_time,
      group_id=group_id,
    )
  )
  return result["episode_uuid"]


def _poll_facts(backend, query: str, group_id: str, timeout: float = 60.0) -> list[dict]:
  """Poll search_memory_facts until at least one result is returned.

  Graphiti indexing is asynchronous; this retries until the graph is ready
  or the timeout is reached.
  """
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    facts = backend.search_memory_facts(
      query=query,
      group_ids=[group_id],
      max_facts=20,
    )
    if facts:
      return facts
    time.sleep(3)
  return backend.search_memory_facts(
    query=query,
    group_ids=[group_id],
    max_facts=20,
  )


# ---------------------------------------------------------------------------
# Test 1 — Basic supersession (mutually exclusive facts)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_basic_supersession_one_fact_invalidated(graphiti_backend):
  """Contradicting episodes → at least one fact has invalid_at set.

  Structural invariants only — no brittle NL-phrasing assertions:
    - At least one fact for the entity pair exists
    - At least one fact has invalid_at not-None (the superseded one)
    - At least one fact has invalid_at = None (currently valid)

  Uses >= rather than == because Graphiti may create additional edges
  (e.g. entity-type edges) that get superseded alongside the primary fact.
  """
  uid = _uid()
  entity = f"Alex_{uid}"
  group_id = f"pytest__supersession_basic_{uid}"

  T1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
  T2 = datetime(2024, 6, 1, tzinfo=timezone.utc)

  # Ingest two contradicting facts about the same entity
  _add_episode_sync(
    graphiti_backend,
    name=f"episode-t1-{uid}",
    body=f"{entity}'s preferred programming language is Python",
    ref_time=T1,
    group_id=group_id,
  )
  _add_episode_sync(
    graphiti_backend,
    name=f"episode-t2-{uid}",
    body=f"{entity} switched to Rust as her preferred programming language",
    ref_time=T2,
    group_id=group_id,
  )

  # Allow Graphiti time to run entity extraction + contradiction detection
  facts = _poll_facts(graphiti_backend, entity, group_id)
  assert facts, f"No facts found for {entity} in group {group_id}"

  # Structural invariants
  invalid_count = sum(1 for f in facts if f.get("invalid_at") is not None)
  active_count = sum(1 for f in facts if f.get("invalid_at") is None)

  # At least one fact should be superseded
  assert invalid_count >= 1, (
    f"Expected at least one superseded fact (invalid_at set), got 0. "
    f"Facts: {[(f.get('fact', '')[:60], f.get('invalid_at')) for f in facts]}"
  )
  # At least one fact should still be active
  assert active_count >= 1, (
    f"Expected at least one active fact (invalid_at=None), got 0. "
    f"Facts: {[(f.get('fact', '')[:60], f.get('invalid_at')) for f in facts]}"
  )

  # Bonus: verify get_entity_edge path also sees invalid_at for a superseded edge
  superseded_facts = [f for f in facts if f.get("invalid_at") is not None]
  if superseded_facts:
    edge_uuid = superseded_facts[0]["uuid"]
    edge = graphiti_backend.get_entity_edge(edge_uuid)
    assert edge["invalid_at"] is not None, (
      f"get_entity_edge({edge_uuid!r}) should also expose invalid_at"
    )


# ---------------------------------------------------------------------------
# Test 2 — Non-contradicting facts (additive)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_additive_facts_both_remain_active(graphiti_backend):
  """Additive (non-contradicting) facts → both invalid_at=None after ingestion."""
  uid = _uid()
  entity = f"Alex_{uid}"
  group_id = f"pytest__supersession_additive_{uid}"

  T1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
  T2 = datetime(2024, 3, 1, tzinfo=timezone.utc)

  _add_episode_sync(
    graphiti_backend,
    name=f"episode-role-{uid}",
    body=f"{entity} is a software developer",
    ref_time=T1,
    group_id=group_id,
  )
  _add_episode_sync(
    graphiti_backend,
    name=f"episode-team-{uid}",
    body=f"{entity} joined the infrastructure team",
    ref_time=T2,
    group_id=group_id,
  )

  facts = _poll_facts(graphiti_backend, entity, group_id)
  assert facts, f"No facts found for {entity} in group {group_id}"

  # For additive facts, Graphiti should NOT invalidate any existing edge.
  # We assert the structural invariant (both facts active) rather than
  # invalid_count == 0: LLM contradiction detection is probabilistic, so a
  # superseded fact is possible if the model decides the episodes conflict.
  # The meaningful signal is that at least two facts survive as active.
  active_count = sum(1 for f in facts if f.get("invalid_at") is None)
  assert active_count >= 2, (
    f"Expected both facts to remain active, got {active_count} active. "
    f"Facts: {[(f.get('fact', '')[:60], f.get('invalid_at')) for f in facts]}"
  )


# ---------------------------------------------------------------------------
# Test 3 — Temporal semantics (older fact superseded by promotion)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_temporal_semantics_older_fact_superseded(graphiti_backend):
  """Temporal ordering: episode at T_old is superseded when T_new contradicts it.

  At least one fact gains invalid_at (the older role); at least one remains active
  (the newer role). Uses >= rather than == because Graphiti may produce additional
  edges whose supersession state is also valid.
  """
  uid = _uid()
  entity = f"Bob_{uid}"
  group_id = f"pytest__supersession_temporal_{uid}"

  T_old = datetime(2023, 1, 1, tzinfo=timezone.utc)
  T_new = datetime(2024, 6, 1, tzinfo=timezone.utc)

  # Older episode: junior developer
  _add_episode_sync(
    graphiti_backend,
    name=f"episode-junior-{uid}",
    body=f"{entity}'s role is junior developer",
    ref_time=T_old,
    group_id=group_id,
  )
  # Newer episode: promoted to senior
  _add_episode_sync(
    graphiti_backend,
    name=f"episode-senior-{uid}",
    body=f"{entity} was promoted to senior developer",
    ref_time=T_new,
    group_id=group_id,
  )

  facts = _poll_facts(graphiti_backend, entity, group_id)
  assert facts, f"No facts found for {entity} in group {group_id}"

  invalid_count = sum(1 for f in facts if f.get("invalid_at") is not None)
  active_count = sum(1 for f in facts if f.get("invalid_at") is None)

  assert invalid_count >= 1, (
    f"Expected the junior-developer fact to be superseded (invalid_at set). "
    f"Facts: {[(f.get('fact', '')[:60], f.get('invalid_at')) for f in facts]}"
  )
  assert active_count >= 1, (
    f"Expected the senior-developer fact to remain active (invalid_at=None). "
    f"Facts: {[(f.get('fact', '')[:60], f.get('invalid_at')) for f in facts]}"
  )

  # Verify active_only filter works: only active facts returned
  active_facts = graphiti_backend.search_memory_facts(
    query=entity,
    group_ids=[group_id],
    max_facts=20,
    active_only=True,
  )
  for f in active_facts:
    assert f.get("invalid_at") is None, (
      f"active_only=True returned a superseded fact: {f.get('fact', '')[:80]!r}, "
      f"invalid_at={f.get('invalid_at')!r}"
    )
