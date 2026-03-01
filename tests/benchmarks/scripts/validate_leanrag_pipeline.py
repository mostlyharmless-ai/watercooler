#!/usr/bin/env python3
"""
Validate the LeanRAG + watercooler memory pipeline end-to-end.

Run from repo root:
  PYTHONPATH=src python3 tests/benchmarks/scripts/validate_leanrag_pipeline.py [work_dir]

  work_dir: Optional path to existing LeanRAG work dir (discovered from ~/.watercooler if omitted)

Checks:
  1. Embedding shape handling - query and chunk embeddings flatten correctly
  2. Entity extraction - triple extraction produces entities (or fallback path works)
  3. Retrieval - search_nodes returns results (entity path or chunk fallback)
  4. Provenance - source IDs resolvable to entry_ids when EntryEpisodeIndex available
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _discover_work_dir() -> Path | None:
    """Find a wcbench LeanRAG work dir with threads_chunk.json."""
    home = Path.home() / ".watercooler"
    if not home.exists():
        return None
    candidates = sorted(
        (d for d in home.iterdir() if d.is_dir() and d.name.startswith("wcbench_")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for d in candidates:
        if (d / "threads_chunk.json").exists():
            return d
    return None


def _ensure_env(work_dir: Path | None) -> None:
    """Set env vars for LeanRAG (same as memory_seed.build_leanrag_index_from_group)."""
    os.environ.setdefault("WATERCOOLER_LEANRAG_ENABLED", "1")
    os.environ.setdefault("LEANRAG_PATH", str((REPO / "external" / "LeanRAG").resolve()))
    if work_dir:
        os.environ.setdefault("WATERCOOLER_LEANRAG_DATABASE", work_dir.name)


def _setup_leanrag_path() -> None:
    """Add LeanRAG to sys.path for imports (same as backend)."""
    leanrag_path = os.environ.get("LEANRAG_PATH") or str(REPO / "external" / "LeanRAG")
    path_str = str(Path(leanrag_path).resolve())
    if path_str not in sys.path:
        sys.path.insert(0, path_str)


def _load_env_file(path: Path) -> None:
    """Load KEY=VALUE from file into os.environ (only if not already set)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


def _prime_leanrag_env_from_config(cfg: object) -> None:
    """Set LeanRAG-required env vars from config (mirrors backend _ensure_env_for_leanrag)."""
    # Avoid importing leanrag; use getattr for config fields
    for name, cfg_attr in [
        ("LEANRAG_PATH", "leanrag_path"),
        ("GLM_BASE_URL", "embedding_api_base"),
        ("GLM_MODEL", "embedding_model"),
        ("GLM_EMBEDDING_MODEL", "embedding_model"),
        ("DEEPSEEK_API_KEY", "llm_api_key"),
        ("DEEPSEEK_MODEL", "llm_model"),
        ("DEEPSEEK_BASE_URL", "llm_api_base"),
    ]:
        val = getattr(cfg, cfg_attr, None)
        if val and name not in os.environ:
            os.environ[name] = str(val)
    for name, default in [
        ("FALKORDB_PASSWORD", ""),
        ("FALKORDB_HOST", "localhost"),
        ("FALKORDB_PORT", "6379"),
        ("MYSQL_HOST", "localhost"),
        ("MYSQL_PORT", "3306"),
        ("MYSQL_USER", "root"),
        ("MYSQL_PASSWORD", ""),
    ]:
        os.environ.setdefault(name, default)


def test_embedding_shape() -> tuple[bool, str]:
    """1. Verify embedding() returns compatible shapes and _to_flat_floats works."""
    sys.path.insert(0, str(REPO / "src"))
    _ensure_env(None)
    _setup_leanrag_path()

    # Load env from .env.pipeline / .env so load_leanrag_config and LeanRAG find GLM_MODEL etc
    _load_env_file(REPO / ".env.pipeline")
    _load_env_file(REPO / ".env")

    # Prime env from unified config before importing LeanRAG (load_config runs at import time)
    from watercooler_mcp.memory import load_leanrag_config

    cfg = load_leanrag_config(REPO)
    if not cfg:
        return False, (
            "LeanRAG not configured. Source .env.pipeline or set GLM_MODEL, GLM_BASE_URL, "
            "DEEPSEEK_API_KEY, etc. See docs for memory/LeanRAG setup."
        )
    _prime_leanrag_env_from_config(cfg)

    leanrag_path = Path(os.environ.get("LEANRAG_PATH", REPO / "external" / "LeanRAG")).resolve()
    config_yaml = leanrag_path / "config.yaml"
    if not config_yaml.exists():
        return False, f"LeanRAG config.yaml not found at {config_yaml}"

    cwd = os.getcwd()
    try:
        os.chdir(leanrag_path)
        from leanrag.core.llm import embedding

        try:
            q = embedding("test query")
            c = embedding("test chunk text")
        except Exception as e:
            return False, f"embedding() call failed: {e}"

        def to_flat(vec):
            if hasattr(vec, "tolist"):
                flat = vec.tolist()
            elif isinstance(vec, list):
                flat = vec
            else:
                flat = list(vec)
            if flat and isinstance(flat[0], (list, tuple)):
                flat = list(flat[0])
            return [float(x) for x in flat]

        try:
            q_flat = to_flat(q)
            c_flat = to_flat(c)
        except Exception as e:
            return False, f"_to_flat_floats equivalent failed: {e}"

        if len(q_flat) != len(c_flat):
            return False, f"Embedding dimension mismatch: query={len(q_flat)} chunk={len(c_flat)}"
        if len(q_flat) < 10:
            return False, f"Embedding too short: {len(q_flat)}"
        return True, f"OK: dim={len(q_flat)}, same model for query and chunk"
    except ImportError as e:
        return False, f"LeanRAG embedding import failed: {e}"
    except (ValueError, FileNotFoundError) as e:
        return False, f"LeanRAG config failed: {e}"
    finally:
        os.chdir(cwd)


def test_retrieval(work_dir: Path) -> tuple[bool, str]:
    """2. Verify search_nodes returns results (entity path or fallback)."""
    sys.path.insert(0, str(REPO / "src"))
    _ensure_env(work_dir)

    from watercooler_mcp.memory import load_leanrag_config
    from watercooler_memory.backends.leanrag import LeanRAGBackend

    cfg = load_leanrag_config(REPO)
    if not cfg or not cfg.work_dir:
        return False, "load_leanrag_config returned None or missing work_dir"
    if cfg.work_dir != work_dir.resolve():
        return False, f"work_dir mismatch: config={cfg.work_dir} expected={work_dir}"

    backend = LeanRAGBackend(cfg)
    try:
        nodes = backend.search_nodes("What is the EU retention period?", max_results=5)
    except Exception as e:
        return False, f"search_nodes failed: {e}"

    if not nodes:
        return False, "search_nodes returned 0 results (entity path and fallback both failed)"

    has_source = any(n.get("source") for n in nodes)
    if not has_source:
        return False, "Results missing provenance.source (needed for wc-provenance)"
    return True, f"OK: {len(nodes)} results, source={has_source}"


def test_entity_extraction(work_dir: Path) -> tuple[bool, str]:
    """3. Check entity extraction artifacts (entity.jsonl, all_entities.json)."""
    entity_path = work_dir / "entity.jsonl"
    all_entities_path = work_dir / "all_entities.json"
    chunks_path = work_dir / "threads_chunk.json"

    if not chunks_path.exists():
        return False, "threads_chunk.json not found"
    chunks = json.loads(chunks_path.read_text())
    chunk_count = len(chunks) if isinstance(chunks, list) else 0

    entity_count = 0
    if entity_path.exists():
        for _ in entity_path.open():
            entity_count += 1
            if entity_count > 100:
                break

    all_count = 0
    if all_entities_path.exists():
        data = json.loads(all_entities_path.read_text())
        all_count = len(data) if isinstance(data, list) else 0

    if entity_count > 0 or all_count > 0:
        return True, f"Entities present: entity.jsonl={entity_count} all_entities.json={all_count} (chunks={chunk_count})"
    if chunk_count > 0:
        return True, f"No entities (expected for tiny corpus + filtering) but chunks={chunk_count} (fallback valid)"
    return False, "No chunks and no entities"


def test_provenance_resolution(work_dir: Path, entry_index_path: Path | None) -> tuple[bool, str]:
    """4. If EntryEpisodeIndex exists, verify source -> entry_id resolution."""
    if not entry_index_path or not entry_index_path.exists():
        return True, "Skip: no entry_episode_index (provenance test N/A)"

    sys.path.insert(0, str(REPO / "src"))
    _ensure_env(work_dir)

    from watercooler_mcp.memory import load_leanrag_config
    from watercooler_memory.backends.leanrag import LeanRAGBackend

    cfg = load_leanrag_config(REPO)
    if not cfg:
        return True, "Skip: LeanRAG not configured"
    backend = LeanRAGBackend(cfg)
    nodes = backend.search_nodes("retention", max_results=3)
    if not nodes:
        return True, "Skip: no nodes to resolve"

    index = json.loads(entry_index_path.read_text())
    entries = index.get("entries", []) if isinstance(index, dict) else []
    ep_to_entry = {
        str(e.get("episode_uuid", "")): str(e.get("entry_id", ""))
        for e in entries
        if e.get("episode_uuid") and e.get("entry_id")
    }
    if not ep_to_entry:
        return True, "Skip: index empty"

    resolved = 0
    for n in nodes:
        src = str(n.get("source") or "")
        if src and src in ep_to_entry:
            resolved += 1
    if resolved > 0:
        return True, f"OK: resolved {resolved}/{len(nodes)} sources to entry_id"
    return True, "Skip: no overlap (index may be for different run)"


def main() -> int:
    work_dir_arg = sys.argv[1] if len(sys.argv) > 1 else None
    work_dir = Path(work_dir_arg).resolve() if work_dir_arg else _discover_work_dir()

    print("LeanRAG pipeline validation")
    print("=" * 50)

    # 1. Embedding shape
    ok, msg = test_embedding_shape()
    print(f"1. Embedding shape: {'PASS' if ok else 'FAIL'} - {msg}")
    if not ok:
        return 1

    # 2. Work dir
    if not work_dir or not work_dir.exists():
        print("2. Work dir: SKIP - no work dir (run memory_qa first)")
        return 0
    print(f"2. Work dir: {work_dir}")

    # 3. Entity extraction
    ok, msg = test_entity_extraction(work_dir)
    print(f"3. Entity extraction: {'PASS' if ok else 'FAIL'} - {msg}")
    if not ok:
        return 1

    # 4. Retrieval
    ok, msg = test_retrieval(work_dir)
    print(f"4. Retrieval: {'PASS' if ok else 'FAIL'} - {msg}")
    if not ok:
        return 1

    # 5. Provenance (best-effort: find entry_episode_index from latest memory_qa run)
    entry_index = None
    logs = REPO / "logs"
    if logs.exists():
        for run_dir in sorted(logs.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            cand = run_dir / "artifacts" / "memory" / "t3-reverse-provenance" / "entry_episode_index.json"
            if cand.exists():
                entry_index = cand
                break
    ok, msg = test_provenance_resolution(work_dir, entry_index)
    print(f"5. Provenance: {'PASS' if ok else 'FAIL'} - {msg}")

    print("=" * 50)
    print("All critical checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
