from __future__ import annotations

import re
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
    # Accept Z suffix.
    if ts.endswith("Z"):
      ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)
  except Exception:
    return _utcnow()


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

  config.entry_episode_index_path = entry_episode_index_path
  if work_dir is not None:
    config.work_dir = work_dir

  backend = get_graphiti_backend(config)
  if backend is None or isinstance(backend, dict):
    raise RuntimeError(f"Graphiti backend init failed: {backend}")
  return backend


def seed_into_graphiti(
  *,
  code_path: Path,
  run_id: str,
  task_id: str,
  artifacts_dir: Path,
  thread_id: str,
  entries: list[SeededEntry],
) -> SeedResult:
  """Seed entries into Graphiti and write entry↔episode mappings to an isolated index."""
  group_id = sanitize_group_id(f"wcbench_{run_id}_{task_id}")
  base = artifacts_dir / "memory" / task_id
  base.mkdir(parents=True, exist_ok=True)

  entry_episode_index_path = base / "entry_episode_index.json"
  graphiti_work_dir = base / "graphiti_work"
  graphiti_work_dir.mkdir(parents=True, exist_ok=True)

  backend = get_graphiti_backend_isolated(
    code_path=code_path,
    entry_episode_index_path=entry_episode_index_path,
    work_dir=graphiti_work_dir,
  )

  entry_to_episode: dict[str, str] = {}
  for e in entries:
    ref_time = _parse_ts(e.timestamp)
    # Graphiti requires a source_description; keep it human/audit-oriented.
    source_description = f"thread:{thread_id} entry_id:{e.entry_id}"
    try:
      asyncio.get_running_loop()
    except RuntimeError:
      # No running loop in this thread (expected).
      pass
    else:
      raise RuntimeError("seed_into_graphiti cannot run inside an active event loop.")

    result = asyncio.run(
      backend.add_episode_direct(  # type: ignore[attr-defined]
        name=e.title,
        episode_body=e.body,
        source_description=source_description,
        reference_time=ref_time,
        group_id=group_id,
        previous_episode_uuids=None,
      )
    )
    episode_uuid = result.get("episode_uuid", "")
    if not episode_uuid:
      raise RuntimeError("Graphiti add_episode_direct returned empty episode_uuid")
    backend.index_entry_as_episode(e.entry_id, episode_uuid, thread_id)  # type: ignore[attr-defined]
    entry_to_episode[e.entry_id] = episode_uuid

  return SeedResult(
    group_id=group_id,
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
) -> Path:
  """Build LeanRAG index for the seeded group_id, using an isolated work_dir."""
  base = artifacts_dir / "memory" / task_id
  leanrag_work_dir = base / "leanrag_work"
  leanrag_work_dir.mkdir(parents=True, exist_ok=True)

  # Fetch all episodes for the group from Graphiti
  graphiti_backend = get_graphiti_backend_isolated(
    code_path=code_path,
    entry_episode_index_path=seed.entry_episode_index_path,
    work_dir=base / "graphiti_work",
  )
  episodes = graphiti_backend.get_group_episodes(seed.group_id)  # type: ignore[attr-defined]

  from watercooler_mcp.memory_sync import episodes_to_chunk_payload
  chunk_payload = episodes_to_chunk_payload(episodes, seed.group_id)

  from watercooler_mcp.memory import load_leanrag_config
  from watercooler_memory.backends.leanrag import LeanRAGBackend

  cfg = load_leanrag_config(code_path=code_path)
  if cfg is None:
    raise RuntimeError("LeanRAG config not available/enabled (load_leanrag_config returned None).")
  cfg.work_dir = leanrag_work_dir
  backend = LeanRAGBackend(cfg)
  _ = backend.index(chunk_payload)
  return leanrag_work_dir

