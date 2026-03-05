from __future__ import annotations

import re
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import asyncio


def _utcnow() -> datetime:
  return datetime.now(timezone.utc)


def sanitize_group_id(text: str) -> str:
  """Sanitize to a Graphiti/FalkorDB-friendly group_id token."""
  text = text.strip().lower()
  text = re.sub(r"[^a-z0-9_]+", "_", text)
  text = re.sub(r"_+", "_", text).strip("_")
  return text or "wcbench"


@dataclass(frozen=True)
class SeededEntry:
  entry_id: str
  title: str
  body: str
  timestamp: str = ""  # ISO8601; optional


@dataclass(frozen=True)
class SeedResult:
  group_id: str
  thread_id: str
  entry_to_episode: dict[str, str]
  entry_episode_index_path: Path


def _parse_ts(ts: str) -> datetime:
  if not ts:
    return _utcnow()
  try:
    return _parse_ts_strict(ts)
  except Exception:
    return _utcnow()


def _parse_ts_strict(ts: str) -> datetime:
  """Parse an ISO8601 timestamp or raise ValueError."""
  raw = (ts or "").strip()
  if not raw:
    raise ValueError("timestamp is empty")
  if raw.endswith("Z"):
    raw = raw[:-1] + "+00:00"
  try:
    return datetime.fromisoformat(raw)
  except Exception as exc:
    raise ValueError(f"invalid timestamp: {ts}") from exc


_FACT_PREFIX_RE = re.compile(r"^\s*Fact:\s*", re.IGNORECASE)


def _extract_fact_sentences(text: str) -> list[str]:
  """Extract one or more fact-like sentences from a seeded entry body.

  This is intentionally simple/deterministic for memory_qa seeding:
  - Only considers text after a leading 'Fact:' prefix (if present)
  - Splits on '.' and returns non-empty clauses with a trailing '.'
  """
  if not text:
    return []
  body = _FACT_PREFIX_RE.sub("", text.strip(), count=1)
  parts = [p.strip() for p in body.split(".")]
  facts: list[str] = []
  for p in parts:
    if not p:
      continue
    facts.append(p + ".")
  return facts


def _relation_name_for_fact(fact: str) -> str:
  f = fact.lower()
  if "role is" in f:
    return "ROLE"
  if "retain data" in f or "retention" in f:
    return "RETENTION"
  return "FACT"


def _subject_name_for_fact(fact: str) -> str:
  # Extremely small heuristic: prefer the first token up to possessive "'s".
  # This is good enough for the deterministic memory_qa fixtures (e.g. "Dana's role is ...").
  m = re.match(r"^\s*([A-Za-z0-9_-]+)('s)?\b", fact.strip())
  return (m.group(1) if m else "subject").strip() or "subject"


def _object_name_for_fact(fact: str) -> str:
  # Extremely small heuristic: take the last word token before '.'.
  tokens = re.findall(r"[A-Za-z0-9_-]+", fact)
  return (tokens[-1] if tokens else "object").strip() or "object"


def _ensure_message_format(text: str) -> str:
  """Ensure Graphiti's EpisodeType.message parsing receives 'actor: content'."""
  s = (text or "").strip()
  if re.match(r"^[A-Za-z0-9_-]+:\\s", s):
    return s
  return f"user: {s}"


def get_graphiti_backend_isolated(
  *,
  code_path: Path,
  entry_episode_index_path: Path,
  work_dir: Optional[Path] = None,
) -> Any:
  """Create a Graphiti backend with an isolated EntryEpisodeIndex path."""
  from watercooler_mcp.memory import get_graphiti_backend, load_graphiti_config

  config = load_graphiti_config(code_path=code_path)
  if config is None:
    raise RuntimeError("Graphiti config not available/enabled (load_graphiti_config returned None).")

  # Allow benchmark runs to override Graphiti's LLM/embedding endpoints via env.
  # This keeps the default config behavior for normal use while enabling local,
  # reproducible testing without hitting hosted provider rate limits.
  llm_api_base = os.environ.get("LLM_API_BASE") or os.environ.get("OPENAI_API_BASE")
  if llm_api_base:
    config.llm_api_base = llm_api_base
  if os.environ.get("LLM_MODEL"):
    config.llm_model = os.environ["LLM_MODEL"]
  if os.environ.get("LLM_API_KEY"):
    config.llm_api_key = os.environ["LLM_API_KEY"]

  embedding_api_base = os.environ.get("EMBEDDING_API_BASE")
  if embedding_api_base:
    config.embedding_api_base = embedding_api_base
  if os.environ.get("EMBEDDING_MODEL"):
    config.embedding_model = os.environ["EMBEDDING_MODEL"]
  if os.environ.get("EMBEDDING_API_KEY"):
    config.embedding_api_key = os.environ["EMBEDDING_API_KEY"]

  config.entry_episode_index_path = entry_episode_index_path
  if work_dir is not None:
    config.work_dir = work_dir

  backend = get_graphiti_backend(config)
  if backend is None or isinstance(backend, dict):
    raise RuntimeError(f"Graphiti backend init failed: {backend}")
  return backend


def _validate_seed_entries(entries: list[SeededEntry]) -> None:
  """Pre-flight validation: reject empty bodies and invalid timestamps."""
  for e in entries:
    if not (e.body or "").strip():
      raise ValueError(f"Seed entry {e.entry_id} has empty body")
    if e.timestamp:
      try:
        _parse_ts_strict(e.timestamp)
      except ValueError as exc:
        raise ValueError(
          f"Seed entry {e.entry_id} has invalid timestamp: {e.timestamp}"
        ) from exc


def seed_into_graphiti(
  *,
  code_path: Path,
  run_id: str,
  task_id: str,
  artifacts_dir: Path,
  thread_id: str,
  entries: list[SeededEntry],
  entries_raw: list[dict[str, Any]] | None = None,
) -> SeedResult:
  """Seed entries into Graphiti and write entry↔episode mappings to an isolated index."""
  _validate_seed_entries(entries)
  raw_group_id = sanitize_group_id(f"wcbench_{run_id}_{task_id}")
  base = artifacts_dir / "memory" / task_id
  base.mkdir(parents=True, exist_ok=True)

  entry_episode_index_path = base / "entry_episode_index.json"
  graphiti_work_dir = base / "graphiti_work"
  graphiti_work_dir.mkdir(parents=True, exist_ok=True)

  try:
    asyncio.get_running_loop()
  except RuntimeError:
    # No running loop in this thread (expected).
    pass
  else:
    raise RuntimeError("seed_into_graphiti cannot run inside an active event loop.")

  backend = get_graphiti_backend_isolated(
    code_path=code_path,
    entry_episode_index_path=entry_episode_index_path,
    work_dir=graphiti_work_dir,
  )

  async def _ingest() -> dict[str, str]:
    from graphiti_core.edges import EntityEdge
    from graphiti_core.nodes import EntityNode

    entry_to_episode: dict[str, str] = {}
    graphiti = backend._create_graphiti_client()  # type: ignore[attr-defined]
    node_cache: dict[str, EntityNode] = {}
    latest_edge_uuid_by_key: dict[tuple[str, str], str] = {}

    # Match GraphitiBackend's internal sanitization rules (length caps, prefixes).
    group_id = backend._sanitize_thread_id(raw_group_id)  # type: ignore[attr-defined]
    for e in entries:
      ref_time = _parse_ts(e.timestamp)
      # Graphiti requires a source_description; keep it human/audit-oriented.
      source_description = f"thread:{thread_id} entry_id:{e.entry_id}"
      result = await backend.add_episode_direct(  # type: ignore[attr-defined]
        name=e.title,
        episode_body=_ensure_message_format(e.body),
        source_description=source_description,
        reference_time=ref_time,
        group_id=group_id,
        previous_episode_uuids=None,
      )
      episode_uuid = result.get("episode_uuid", "")
      if not episode_uuid:
        raise RuntimeError("Graphiti add_episode_direct returned empty episode_uuid")
      backend.index_entry_as_episode(e.entry_id, episode_uuid, thread_id)  # type: ignore[attr-defined]
      entry_to_episode[e.entry_id] = episode_uuid

      # Deterministic fact seeding for memory_qa:
      # Create explicit EntityEdge records with valid_at/invalid_at so the
      # benchmark can validate active-only semantics without relying on LLM
      # extraction quality.
      facts = _extract_fact_sentences(e.body)
      supersedes = "supersedes" in (e.body or "").lower()
      for fact in facts:
        rel = _relation_name_for_fact(fact)
        subj = _subject_name_for_fact(fact)
        obj = _object_name_for_fact(fact)

        async def _get_or_create_entity(name: str) -> EntityNode:
          if name in node_cache:
            return node_cache[name]
          n = EntityNode(name=name, group_id=group_id, created_at=ref_time)
          await n.generate_name_embedding(graphiti.embedder)
          await n.save(graphiti.driver)
          node_cache[name] = n
          return n

        subj_node = await _get_or_create_entity(subj)
        obj_node = await _get_or_create_entity(obj)

        edge = EntityEdge(
          group_id=group_id,
          source_node_uuid=subj_node.uuid,
          target_node_uuid=obj_node.uuid,
          created_at=ref_time,
          name=rel,
          fact=fact,
          episodes=[episode_uuid],
          valid_at=ref_time,
          invalid_at=None,
          expired_at=None,
          attributes={},
        )
        await edge.generate_embedding(graphiti.embedder)
        await edge.save(graphiti.driver)

        key = (subj, rel)
        if supersedes and key in latest_edge_uuid_by_key:
          old_uuid = latest_edge_uuid_by_key[key]
          # Mark the previous edge as stale/superseded at this reference time.
          await graphiti.driver.execute_query(
            """
            MATCH (s:Entity)-[e:RELATES_TO {uuid: $uuid}]->(t:Entity)
            SET e.invalid_at = $invalid_at
            RETURN e.uuid AS uuid
            """,
            uuid=old_uuid,
            invalid_at=ref_time,
          )
        latest_edge_uuid_by_key[key] = edge.uuid

    # Handle explicit supersedes_entry_id from task JSON.
    if entries_raw:
      for raw_entry in entries_raw:
        supersedes_id = raw_entry.get("supersedes_entry_id")
        if not supersedes_id:
          continue
        superseded_episode = entry_to_episode.get(supersedes_id)
        if not superseded_episode:
          continue
        # Find the superseding entry's timestamp for invalid_at.
        superseding_id = str(raw_entry.get("entry_id") or "")
        superseding_ts = _utcnow()
        for se in entries:
          if se.entry_id == superseding_id:
            superseding_ts = _parse_ts(se.timestamp)
            break
        # FalkorDB stores episodes as a list (SET e = $edge_data keeps Python list).
        # Use list-based ANY() predicate for membership check.
        invalid_at_str = superseding_ts.isoformat()
        await graphiti.driver.execute_query(
          """
          MATCH (s:Entity)-[e:RELATES_TO]->(t:Entity)
          WHERE ANY(ep IN e.episodes WHERE ep = $ep_uuid) AND e.invalid_at IS NULL
          SET e.invalid_at = $invalid_at
          RETURN count(e) AS invalidated
          """,
          ep_uuid=superseded_episode,
          invalid_at=invalid_at_str,
        )
    return entry_to_episode

  entry_to_episode = asyncio.run(_ingest())

  return SeedResult(
    group_id=backend._sanitize_thread_id(raw_group_id),  # type: ignore[attr-defined]
    thread_id=thread_id,
    entry_to_episode=entry_to_episode,
    entry_episode_index_path=entry_episode_index_path,
  )


def build_leanrag_index_from_group(
  *,
  code_path: Path,
  seed: SeedResult,
  artifacts_dir: Path,
  task_id: str,
  entries: list[SeededEntry],
) -> Path:
  """Build LeanRAG index for the seeded group_id, using an isolated work_dir."""
  # Ensure TierOrchestrator can see T3 as available. It checks load_leanrag_config(),
  # which requires these env vars.
  os.environ["WATERCOOLER_LEANRAG_ENABLED"] = "1"
  os.environ["LEANRAG_PATH"] = str((code_path / "external" / "LeanRAG").resolve())
  os.environ["WATERCOOLER_LEANRAG_DATABASE"] = f"wcbench_{seed.group_id}"

  # Build into the canonical location that load_leanrag_config() uses so wc-smart-query
  # can query the same index we just built.
  leanrag_work_dir = (Path.home() / ".watercooler" / os.environ["WATERCOOLER_LEANRAG_DATABASE"]).resolve()
  leanrag_work_dir.mkdir(parents=True, exist_ok=True)

  # Build a deterministic chunk payload directly from the seeded entries.
  #
  # We use episode UUIDs as chunk IDs so LeanRAG provenance can reference the
  # Graphiti episode_uuid, which we can then reverse-resolve to entry_id via
  # EntryEpisodeIndex (wc-provenance).
  from watercooler_memory.backends import ChunkPayload

  chunks = []
  for e in entries:
    ep_uuid = seed.entry_to_episode.get(e.entry_id, "")
    if not ep_uuid:
      continue
    text = (e.body or "").strip()
    if not text:
      continue
    chunks.append(
      {
        "id": ep_uuid,
        "text": text,
        "metadata": {"group_id": seed.group_id, "source": "memory_qa_seed"},
      }
    )
  chunk_payload = ChunkPayload(manifest_version="1.0", chunks=chunks)

  from watercooler_memory.backends.leanrag import LeanRAGBackend
  from watercooler_memory.backends.leanrag import LeanRAGConfig

  cfg = LeanRAGConfig.from_unified()
  if cfg.leanrag_path is None:
    default_path = (code_path / "external" / "LeanRAG").resolve()
    if not default_path.exists():
      raise RuntimeError(
        "LeanRAG path is not configured and external/LeanRAG does not exist. "
        "Set LEANRAG_PATH or ensure the submodule is present."
      )
    cfg.leanrag_path = default_path
  cfg.work_dir = leanrag_work_dir
  backend = LeanRAGBackend(cfg)
  _ = backend.index(chunk_payload)
  return leanrag_work_dir

