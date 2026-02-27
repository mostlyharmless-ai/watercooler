#!/usr/bin/env python3
"""Build portable T2/T3 memory artifacts from a sanitized baseline graph export.

This script is intended for benchmark corpora under:
  external/wcbench-corpora/corpora/<corpus_name>/

It:
  - starts a fresh FalkorDB via docker compose (isolated project name + volume)
  - ingests T1 baseline-graph entries into Graphiti (T2) under a stable group_id
  - writes an isolated entry↔episode index into the corpus (no ~/.watercooler writes)
  - builds a LeanRAG (T3) index from the Graphiti episodes into a corpus-local work_dir
  - dumps FalkorDB state to a portable RDB file for fast restore
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))


def _utcnow() -> datetime:
  return datetime.now(timezone.utc)


def _parse_ts(ts: str) -> datetime:
  if not ts:
    return _utcnow()
  try:
    if ts.endswith("Z"):
      ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)
  except Exception:
    return _utcnow()


def _iter_entries_jsonl(threads_dir: Path) -> list[Path]:
  base = threads_dir / "graph" / "baseline" / "threads"
  if not base.exists():
    raise FileNotFoundError(f"Expected baseline graph at {base}")
  return sorted(base.glob("*/entries.jsonl"))


@dataclass(frozen=True)
class BuildResult:
  group_id: str
  graphiti_database: str
  leanrag_work_dir: str
  falkordb_port: int
  threads_total: int
  entries_total: int
  skipped_entries: int
  started_at: str
  finished_at: str
  elapsed_seconds: float


def _get_container_name_from_compose_ps(rows: list[dict[str, Any]], service: str) -> Optional[str]:
  for r in rows:
    if r.get("Service") != service:
      continue
    name = r.get("Name") or r.get("name")
    if isinstance(name, str) and name.strip():
      return name.strip()
  return None


@dataclass(frozen=True)
class ComposeSpec:
  compose_file: Path
  project_name: str
  env: dict[str, str]


def _run(cmd: list[str], *, cwd: Optional[Path] = None, env: Optional[dict[str, str]] = None) -> str:
  merged_env = os.environ.copy()
  if env:
    merged_env.update(env)
  p = subprocess.run(
    cmd,
    cwd=str(cwd) if cwd else None,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    env=merged_env,
    check=False,
  )
  if p.returncode != 0:
    raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(cmd)}\n\n{p.stdout}")
  return p.stdout


def _docker_compose_cmd(spec: ComposeSpec) -> list[str]:
  return ["docker", "compose", "-f", str(spec.compose_file), "-p", spec.project_name]


def compose_up(spec: ComposeSpec) -> None:
  _run(_docker_compose_cmd(spec) + ["up", "-d", "--remove-orphans"], env=spec.env)


def compose_down(spec: ComposeSpec) -> None:
  _run(_docker_compose_cmd(spec) + ["down", "--remove-orphans", "--volumes"], env=spec.env)


def compose_ps_json(spec: ComposeSpec) -> list[dict[str, Any]]:
  out = _run(_docker_compose_cmd(spec) + ["ps", "--format", "json"], env=spec.env)
  out = (out or "").strip()
  if not out:
    return []

  try:
    parsed = json.loads(out)
    if isinstance(parsed, list):
      return [p for p in parsed if isinstance(p, dict)]
    if isinstance(parsed, dict):
      return [parsed]
  except Exception:
    pass

  rows: list[dict[str, Any]] = []
  for line in out.splitlines():
    line = line.strip()
    if not line:
      continue
    try:
      obj = json.loads(line)
      if isinstance(obj, dict):
        rows.append(obj)
    except Exception:
      continue
  return rows


def compose_ps_quiet_id(spec: ComposeSpec, service: str) -> Optional[str]:
  out = _run(_docker_compose_cmd(spec) + ["ps", "-q", service], env=spec.env)
  cid = (out or "").strip()
  return cid or None


def choose_free_local_port(preferred: list[int]) -> int:
  for port in preferred:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
      s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
      s.bind(("127.0.0.1", port))
      return port
    except OSError:
      continue
    finally:
      try:
        s.close()
      except Exception:
        pass
  raise RuntimeError(f"No free port found in preferred set: {preferred}")


def wait_for_service_healthy(spec: ComposeSpec, service: str, *, timeout_seconds: int = 120) -> None:
  deadline = time.time() + timeout_seconds
  last = ""
  while time.time() < deadline:
    rows = compose_ps_json(spec)
    for r in rows:
      if r.get("Service") != service:
        continue
      health = (r.get("Health") or "").lower()
      state = (r.get("State") or "").lower()
      status = (r.get("Status") or "").lower()
      last = f"state={state} health={health} status={status}"
      if "healthy" in health:
        return
      if state == "running" and "healthy" not in health and "unhealthy" not in health:
        return
    time.sleep(1.0)
  raise TimeoutError(f"Timed out waiting for {service} to become healthy ({last})")


def _redis_config_get(container_id: str, key: str) -> Optional[str]:
  """Read a Redis/FalkorDB config value via redis-cli inside container."""
  p = subprocess.run(
    ["docker", "exec", container_id, "redis-cli", "-p", "6379", "CONFIG", "GET", key],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    check=False,
  )
  if p.returncode != 0:
    return None
  lines = [ln.strip() for ln in (p.stdout or "").splitlines() if ln.strip()]
  # Expected:
  #   1) "dir"
  #   2) "/var/lib/falkordb/data"
  if len(lines) >= 2:
    return lines[1].strip('"')
  return None

def main() -> int:
  parser = argparse.ArgumentParser(
    description="Build Graphiti (T2) + LeanRAG (T3) artifacts from a sanitized baseline graph export.",
  )
  parser.add_argument(
    "--threads-dir",
    required=True,
    help="Path to exported threads_dir containing graph/baseline/threads/*/entries.jsonl",
  )
  parser.add_argument(
    "--out-corpus-dir",
    required=True,
    help="Corpus root directory containing t2/ and t3/ (e.g. external/wcbench-corpora/corpora/<name>)",
  )
  parser.add_argument(
    "--group-id",
    default="watercooler_site_snapshot_v1",
    help="Stable Graphiti group_id used to partition the corpus in a shared DB",
  )
  parser.add_argument(
    "--graphiti-database",
    default="wcbench_watercooler_site_snapshot_v1",
    help="FalkorDB graph/database name for Graphiti",
  )
  parser.add_argument(
    "--leanrag-work-dir-name",
    default="leanrag_watercooler_site_snapshot_v1",
    help="Directory name under t3/ used as LeanRAG work_dir (also becomes FalkorDB graph name).",
  )
  parser.add_argument(
    "--compose-project",
    default="wcbench-corpus-watercooler-site-snapshot-v1",
    help="Docker compose project name (controls isolated volume/container naming).",
  )
  parser.add_argument(
    "--preferred-ports",
    default="6379,6380,6381,6382",
    help="Comma-separated list of preferred localhost ports for FalkorDB.",
  )
  parser.add_argument(
    "--max-entries",
    type=int,
    default=0,
    help="If set >0, ingest at most N entries (smoke test).",
  )
  parser.add_argument(
    "--skip-leanrag",
    action="store_true",
    help="Skip building LeanRAG index (T3).",
  )
  parser.add_argument(
    "--skip-ingest",
    action="store_true",
    help="Skip Graphiti ingest (requires an already-populated FalkorDB).",
  )
  parser.add_argument(
    "--restore-rdb",
    default="",
    help="If set, copy this RDB to /data/dump.rdb and restart FalkorDB before use.",
  )
  parser.add_argument(
    "--skip-dump",
    action="store_true",
    help="Skip FalkorDB RDB dump (still seeds Graphiti + LeanRAG work_dir).",
  )
  parser.add_argument(
    "--keep-compose",
    action="store_true",
    help="Leave docker compose running (default tears down and removes volumes).",
  )
  parser.add_argument(
    "--log-every",
    type=int,
    default=25,
    help="Progress log cadence (entries).",
  )

  args = parser.parse_args()
  threads_dir = Path(args.threads_dir).resolve()
  corpus_dir = Path(args.out_corpus_dir).resolve()
  t2_dir = corpus_dir / "t2"
  t3_dir = corpus_dir / "t3"
  t2_dir.mkdir(parents=True, exist_ok=True)
  t3_dir.mkdir(parents=True, exist_ok=True)

  entry_episode_index_path = t2_dir / "entry_episode_index.json"
  graphiti_work_dir = t2_dir / "graphiti_work"
  graphiti_work_dir.mkdir(parents=True, exist_ok=True)

  leanrag_work_dir = t3_dir / args.leanrag_work_dir_name
  leanrag_work_dir.mkdir(parents=True, exist_ok=True)

  preferred_ports = [int(p.strip()) for p in str(args.preferred_ports).split(",") if p.strip()]
  if not preferred_ports:
    raise ValueError("--preferred-ports must contain at least one integer port")

  compose_file = REPO_ROOT / "tests" / "benchmarks" / "infra" / "docker-compose.memory.yml"
  port = choose_free_local_port(preferred_ports)
  spec = ComposeSpec(
    compose_file=compose_file,
    project_name=str(args.compose_project),
    env={"FALKORDB_PORT": str(port)},
  )

  started = _utcnow()
  print(f"[wcbench] Starting FalkorDB (port={port}, project={spec.project_name})")
  compose_up(spec)
  try:
    wait_for_service_healthy(spec, "falkordb", timeout_seconds=180)

    rows = compose_ps_json(spec)
    container_name = _get_container_name_from_compose_ps(rows, "falkordb")
    if not container_name:
      # Best-effort fallback for older compose json output.
      container_name = f"{spec.project_name}-falkordb-1"
    print(f"[wcbench] FalkorDB container: {container_name}")
    container_id = compose_ps_quiet_id(spec, "falkordb") or container_name

    if args.restore_rdb:
      restore_path = Path(str(args.restore_rdb)).expanduser().resolve()
      if not restore_path.exists():
        raise FileNotFoundError(f"--restore-rdb not found: {restore_path}")
      print(f"[wcbench] Restoring FalkorDB from RDB: {restore_path}")
      # Important: `docker restart` triggers a graceful shutdown which may
      # overwrite the RDB on disk (empty) before restarting. Instead:
      # - disable shutdown save
      # - stop container
      # - copy RDB into configured dir/dbfilename
      # - start container
      rdb_dir = _redis_config_get(container_id, "dir") or "/var/lib/falkordb/data"
      rdb_filename = _redis_config_get(container_id, "dbfilename") or "dump.rdb"
      rdb_path_in_container = f"{rdb_dir.rstrip('/')}/{rdb_filename}"

      subprocess.run(
        ["docker", "exec", container_id, "redis-cli", "-p", "6379", "CONFIG", "SET", "save", ""],
        check=True,
      )
      subprocess.run(["docker", "stop", container_id], check=True)
      subprocess.run(["docker", "cp", str(restore_path), f"{container_id}:{rdb_path_in_container}"], check=True)
      subprocess.run(["docker", "start", container_id], check=True)
      wait_for_service_healthy(spec, "falkordb", timeout_seconds=180)

    # Force this process to talk to the freshly started FalkorDB.
    os.environ["FALKORDB_HOST"] = "localhost"
    os.environ["FALKORDB_PORT"] = str(port)
    os.environ["WATERCOOLER_GRAPHITI_ENABLED"] = "1"
    os.environ["WATERCOOLER_GRAPHITI_DATABASE"] = str(args.graphiti_database)
    os.environ["WATERCOOLER_LEANRAG_ENABLED"] = "1"

    # For corpus builds, prefer localhost OpenAI-compatible endpoints to avoid
    # cloud quota surprises and to keep runs reproducible/offline-ish.
    os.environ.setdefault("LLM_API_BASE", "http://localhost:8000/v1")
    os.environ.setdefault("LLM_API_KEY", "LOCAL_NO_KEY")
    if "LLM_MODEL" not in os.environ:
      try:
        import urllib.request
        data = json.loads(urllib.request.urlopen("http://localhost:8000/v1/models", timeout=2).read())
        ids = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]
        if ids and isinstance(ids[0], str) and ids[0].strip():
          os.environ["LLM_MODEL"] = ids[0].strip()
      except Exception:
        pass

    os.environ.setdefault("EMBEDDING_API_BASE", "http://localhost:8080/v1")
    os.environ.setdefault("EMBEDDING_MODEL", "bge-m3")

    # Graphiti's config validation requires an EMBEDDING_API_KEY even when the
    # local server ignores auth; provide a sentinel if missing.
    os.environ.setdefault("EMBEDDING_API_KEY", "LOCAL_NO_KEY")

    # Make LeanRAG path explicit so load_leanrag_config doesn't depend on user config.
    leanrag_path = REPO_ROOT / "external" / "LeanRAG"
    os.environ["LEANRAG_PATH"] = str(leanrag_path)

    # Build Graphiti backend isolated to this corpus' entry_episode_index.json
    from watercooler_memory.backends.graphiti import GraphitiBackend, GraphitiConfig

    graphiti_cfg = GraphitiConfig.from_unified()
    graphiti_cfg.database = str(args.graphiti_database)
    graphiti_cfg.entry_episode_index_path = entry_episode_index_path
    graphiti_cfg.work_dir = graphiti_work_dir

    graphiti_backend = GraphitiBackend(graphiti_cfg)

    # Ingest baseline graph entries (unless skipped)
    entry_files = _iter_entries_jsonl(threads_dir)
    threads_total = len(entry_files)
    entries_total = 0
    skipped_entries = 0
    max_entries = int(args.max_entries or 0)

    if not args.skip_ingest:
      print(f"[wcbench] Ingesting Graphiti episodes from {threads_total} threads (group_id={args.group_id})")
      async def _ingest() -> int:
        nonlocal entries_total
        nonlocal skipped_entries
        from watercooler_memory.chunker import ChunkerConfig, chunk_text, count_tokens

        chunker_cfg = ChunkerConfig(max_tokens=768, overlap=64)
        idx = getattr(graphiti_backend, "entry_episode_index", None)

        for i, p in enumerate(entry_files):
          thread_id = p.parent.name
          with p.open("r", encoding="utf-8") as f:
            for line in f:
              if max_entries and entries_total >= max_entries:
                break
              line = line.strip()
              if not line:
                continue
              obj = json.loads(line)
              entry_id = str(obj.get("entry_id") or obj.get("id") or "")
              title = str(obj.get("title") or "")
              body = str(obj.get("body") or "")
              ts = str(obj.get("timestamp") or "")
              if not entry_id:
                continue

              try:
                source_description = f"thread:{thread_id} entry_id:{entry_id}"
                ref_time = _parse_ts(ts)

                token_count = count_tokens(body, chunker_cfg.encoding_name)
                chunks = [(body, token_count)]
                if token_count > 1500:
                  chunks = chunk_text(body, chunker_cfg)

                previous: list[str] = []
                total_chunks = len(chunks)
                first_episode_uuid = ""
                for chunk_index, (chunk_body, _) in enumerate(chunks):
                  name = title
                  if total_chunks > 1:
                    name = f"{title} [chunk {chunk_index+1}/{total_chunks}]"
                  result = await graphiti_backend.add_episode_direct(  # type: ignore[attr-defined]
                    name=name,
                    episode_body=chunk_body,
                    source_description=source_description,
                    reference_time=ref_time,
                    group_id=str(args.group_id),
                    previous_episode_uuids=previous,
                  )
                  episode_uuid = str(result.get("episode_uuid") or "")
                  if not episode_uuid:
                    raise RuntimeError("Graphiti add_episode_direct returned empty episode_uuid")
                  if not first_episode_uuid:
                    first_episode_uuid = episode_uuid
                  if total_chunks > 1 and idx is not None:
                    chunk_id = hashlib.sha256(
                      f"{entry_id}:{chunk_index}:{chunk_body[:200]}".encode("utf-8", errors="ignore")
                    ).hexdigest()[:16]
                    idx.add_chunk_mapping(
                      chunk_id=chunk_id,
                      episode_uuid=episode_uuid,
                      entry_id=entry_id,
                      thread_id=thread_id,
                      chunk_index=chunk_index,
                      total_chunks=total_chunks,
                    )
                  previous = [episode_uuid]

                if idx is not None and total_chunks > 1:
                  try:
                    idx.save()
                  except Exception:
                    pass

                if first_episode_uuid and total_chunks == 1:
                  graphiti_backend.index_entry_as_episode(entry_id, first_episode_uuid, thread_id)  # type: ignore[attr-defined]
              except Exception as e:
                skipped_entries += 1
                print(f"[wcbench]  warning: skipped entry {entry_id} ({thread_id}): {e}")

              entries_total += 1
              if args.log_every and entries_total % int(args.log_every) == 0:
                print(f"[wcbench]  ingested {entries_total} entries...")

          if max_entries and entries_total >= max_entries:
            break
          if (i + 1) % 10 == 0:
            print(f"[wcbench]  scanned {i+1}/{threads_total} threads")
        return entries_total

      try:
        asyncio.get_running_loop()
      except RuntimeError:
        # No running loop in this thread (expected).
        pass
      else:
        raise RuntimeError("This script cannot run inside an active event loop.")

      entries_total = asyncio.run(_ingest())

      print(f"[wcbench] Graphiti ingest complete: entries={entries_total} threads={threads_total}")
    else:
      print("[wcbench] Skipping Graphiti ingest (--skip-ingest)")

    # Dump FalkorDB to a portable RDB snapshot and write T2 report BEFORE T3,
    # so a LeanRAG failure doesn't waste the full ingest.
    #
    # IMPORTANT: For T3-only runs (`--skip-ingest`), do NOT overwrite existing T2 artifacts.
    if not args.skip_ingest:
      finished_t2 = _utcnow()
      t2_result = BuildResult(
        group_id=str(args.group_id),
        graphiti_database=str(args.graphiti_database),
        leanrag_work_dir=str(leanrag_work_dir.relative_to(corpus_dir)),
        falkordb_port=int(port),
        threads_total=int(threads_total),
        entries_total=int(entries_total),
        skipped_entries=int(skipped_entries),
        started_at=started.isoformat(),
        finished_at=finished_t2.isoformat(),
        elapsed_seconds=float((finished_t2 - started).total_seconds()),
      )
      (t2_dir / "build_report.json").write_text(json.dumps(asdict(t2_result), indent=2) + "\n", encoding="utf-8")

      if not args.skip_dump:
        print(f"[wcbench] Dumping FalkorDB RDB to {t2_dir / 'falkordb.rdb'}")
        # Prefer server-side SAVE to ensure module data (FalkorDB graphs) is captured.
        subprocess.run(["docker", "exec", container_id, "redis-cli", "-p", "6379", "SAVE"], check=True)
        rdb_dir = _redis_config_get(container_id, "dir") or "/data"
        rdb_filename = _redis_config_get(container_id, "dbfilename") or "dump.rdb"
        rdb_dir = rdb_dir.rstrip("/") or "/data"
        rdb_path_in_container = f"{rdb_dir}/{rdb_filename}"
        subprocess.run(
          ["docker", "cp", f"{container_id}:{rdb_path_in_container}", str(t2_dir / "falkordb.rdb")],
          check=True,
        )

    # Build LeanRAG index (optional)
    if not args.skip_leanrag:
      print(f"[wcbench] Building LeanRAG index into {leanrag_work_dir}")
      episodes = graphiti_backend.get_group_episodes(str(args.group_id))  # type: ignore[attr-defined]

      from watercooler_memory.backends import ChunkPayload

      chunks: list[dict[str, Any]] = []
      for ep in episodes:
        content = getattr(ep, "content", "")
        uuid = getattr(ep, "uuid", "")
        if not content:
          continue
        chunk_id = uuid or hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()
        chunks.append({
          "id": chunk_id,
          "text": content,
          "metadata": {"group_id": str(args.group_id), "source": "graphiti_episode"},
        })
      chunk_payload = ChunkPayload(manifest_version="1.0", chunks=chunks)

      from watercooler_memory.backends.leanrag import LeanRAGBackend, LeanRAGConfig

      cfg = LeanRAGConfig.from_unified()
      cfg.leanrag_path = leanrag_path
      cfg.work_dir = leanrag_work_dir
      backend = LeanRAGBackend(cfg)
      _ = backend.index(chunk_payload)
      print("[wcbench] LeanRAG index complete")

    # Snapshot FalkorDB for T3 consumers too (optional)
    if not args.skip_dump and (t2_dir / "falkordb.rdb").exists():
      try:
        (t3_dir / "falkordb.rdb").write_bytes((t2_dir / "falkordb.rdb").read_bytes())
      except Exception:
        pass

    report_threads_total = threads_total
    report_entries_total = entries_total
    report_skipped_entries = skipped_entries
    if args.skip_ingest:
      # For T3-only runs, reuse the existing T2 ingest counts if available,
      # so `t3/build_report.json` still reflects the corpus size.
      try:
        existing = json.loads((t2_dir / "build_report.json").read_text(encoding="utf-8"))
        report_threads_total = int(existing.get("threads_total", report_threads_total))
        report_entries_total = int(existing.get("entries_total", report_entries_total))
        report_skipped_entries = int(existing.get("skipped_entries", report_skipped_entries))
      except Exception:
        pass

    finished = _utcnow()
    result = BuildResult(
      group_id=str(args.group_id),
      graphiti_database=str(args.graphiti_database),
      leanrag_work_dir=str(leanrag_work_dir.relative_to(corpus_dir)),
      falkordb_port=int(port),
      threads_total=int(report_threads_total),
      entries_total=int(report_entries_total),
      skipped_entries=int(report_skipped_entries),
      started_at=started.isoformat(),
      finished_at=finished.isoformat(),
      elapsed_seconds=float((finished - started).total_seconds()),
    )

    (t3_dir / "build_report.json").write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8")
    print(f"[wcbench] Wrote build reports under {t2_dir} and {t3_dir}")
    return 0
  finally:
    if not args.keep_compose:
      print("[wcbench] Tearing down docker compose (removing volumes)")
      try:
        compose_down(spec)
      except Exception as e:
        print(f"[wcbench] Warning: compose down failed: {e}")


if __name__ == "__main__":
  raise SystemExit(main())

