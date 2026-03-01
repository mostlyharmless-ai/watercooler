#!/usr/bin/env python3
"""
Diagnose T3 memory_qa failure: trace the full path from wc-smart-query to LeanRAG.

Run from repo root with:
  PYTHONPATH=src python3 tests/benchmarks/scripts/diagnose_t3.py [work_dir]

  work_dir: Optional path to LeanRAG work dir (default: discover from ~/.watercooler/wcbench_*)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _discover_work_dir() -> Path | None:
    """Find the most recent wcbench LeanRAG work dir."""
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


def step(name: str, fn, *args, **kwargs):
    print(f"\n--- {name} ---")
    try:
        result = fn(*args, **kwargs)
        s = repr(result)
        print(f"OK: {s[:300]}{'...' if len(s) > 300 else ''}")
        return result
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        raise


def main() -> None:
    work_dir_arg = sys.argv[1] if len(sys.argv) > 1 else None
    if work_dir_arg:
        WORK_DIR = Path(work_dir_arg).resolve()
    else:
        WORK_DIR = _discover_work_dir()
    if not WORK_DIR:
        print("No wcbench work dir found. Run memory_qa first or pass path: diagnose_t3.py <path>")
        sys.exit(1)

    print("T3 diagnostic: tracing wc-smart-query --force-tier T3 path")
    print(f"REPO={REPO}")
    print(f"WORK_DIR={WORK_DIR}")
    print(f"WORK_DIR exists={WORK_DIR.exists()}")
    if WORK_DIR.exists():
        for p in sorted(WORK_DIR.iterdir()):
            size = p.stat().st_size if p.is_file() else "-"
            print(f"  {p.name}: {size}")

    # 1. Env as set by memory_seed.build_leanrag_index_from_group
    os.environ.setdefault("WATERCOOLER_LEANRAG_ENABLED", "1")
    os.environ.setdefault("LEANRAG_PATH", str((REPO / "external" / "LeanRAG").resolve()))
    os.environ.setdefault("WATERCOOLER_LEANRAG_DATABASE", WORK_DIR.name)

    sys.path.insert(0, str(REPO / "src"))

    # 2. load_tier_config (as wc_text_tools does)
    threads_dir = REPO / "tests" / "integration" / ".cli-threads"

    from watercooler_memory.tier_strategy import load_tier_config, Tier, TierOrchestrator

    config = step("load_tier_config", load_tier_config, threads_dir=threads_dir, code_path=REPO)
    print(f"  t1_enabled={config.t1_enabled}, t2_enabled={config.t2_enabled}, t3_enabled={config.t3_enabled}")
    config.t3_enabled = True
    config.max_tiers = 3

    # 3. TierOrchestrator + _detect_available_tiers
    orch = step("TierOrchestrator(config)", TierOrchestrator, config)
    print(f"  available_tiers={[t.value for t in orch.available_tiers]}")

    if Tier.T3 not in orch.available_tiers:
        print("\n*** T3 NOT in available_tiers - orchestrator will short-circuit on force_tier=T3 ***")
        return

    # 4. load_leanrag_config (what _detect_available_tiers uses)
    from watercooler_mcp.memory import load_leanrag_config

    lr_config = step("load_leanrag_config(REPO)", load_leanrag_config, REPO)
    if lr_config:
        print(f"  work_dir={lr_config.work_dir}")
        print(f"  work_dir exists={lr_config.work_dir.exists() if lr_config.work_dir else 'N/A'}")
        if lr_config.work_dir and (lr_config.work_dir / "threads_chunk.json").exists():
            chunks = json.loads((lr_config.work_dir / "threads_chunk.json").read_text())
            print(f"  threads_chunk.json: {len(chunks)} chunks")
            for i, c in enumerate(chunks[:2]):
                hc = str(c.get("hash_code", ""))
                txt = str(c.get("text", ""))
                print(f"    [{i}] hash_code={hc[:50]}... text={txt[:70]}...")

    # 5. Direct LeanRAGBackend.search_nodes (bypass orchestrator)
    cfg = load_leanrag_config(REPO)
    if not cfg or not cfg.work_dir:
        print("\n*** LeanRAG config missing work_dir - cannot query ***")
        return

    from watercooler_memory.backends.leanrag import LeanRAGBackend

    backend = LeanRAGBackend(cfg)
    query = "What is the EU retention period?"
    nodes = step("backend.search_nodes(query)", backend.search_nodes, query, max_results=5)
    print(f"  nodes count={len(nodes)}")
    for i, n in enumerate(nodes[:3]):
        print(f"  [{i}] id={n.get('id')} backend={n.get('backend')} source={n.get('source')}")

    # 6. Full orchestrator.query with force_tier=T3
    result = step("orchestrator.query(force_tier=T3)", orch.query, query, group_ids=None, force_tier=Tier.T3)
    print(f"  result_count={result.result_count} primary_tier={result.primary_tier}")
    print(f"  evidence count={len(result.evidence)}")
    t3_ev = [e for e in result.evidence if e.tier == Tier.T3]
    print(f"  T3 evidence count={len(t3_ev)}")
    for i, e in enumerate(t3_ev[:3]):
        src = e.provenance.get("source") if e.provenance else None
        print(f"  T3[{i}] id={e.id} source={src}")


if __name__ == "__main__":
    main()
