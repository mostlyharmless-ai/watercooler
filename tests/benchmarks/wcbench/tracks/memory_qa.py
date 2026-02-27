from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from tests.benchmarks.wcbench.config import RunConfig
from tests.benchmarks.wcbench.events import EventLogger
from tests.benchmarks.wcbench.run_layout import RunLayout
from tests.benchmarks.wcbench.summary import RunSummary, TaskSummary

log = logging.getLogger(__name__)


def _read_text(path: Path) -> str:
  return path.read_text(encoding="utf-8")


def _seed_threads(
  *,
  threads_dir: Path,
  topic: str,
  entries: list[dict[str, Any]],
  event_logger: EventLogger,
  run_id: str,
  task_id: str,
) -> None:
  """Seed a minimal baseline thread (T1) under the run layout."""
  from watercooler.commands_graph import say

  threads_dir.mkdir(parents=True, exist_ok=True)
  for e in entries:
    entry_id = str(e["entry_id"])
    say(
      topic,
      threads_dir=threads_dir,
      agent="WCBenchMemoryQA (system)",
      role="planner",
      title=str(e.get("title") or ""),
      body=str(e.get("body") or ""),
      entry_type="Note",
      entry_id=entry_id,
    )
    event_logger.emit(
      "tool_result",
      run_id=run_id,
      task_id=task_id,
      payload={"tool": "watercooler.commands_graph.say", "topic": topic, "entry_id": entry_id},
    )


def _fact_text(f: dict[str, Any]) -> str:
  return (str(f.get("fact") or f.get("content") or "")).strip()


def _assert_contains_any(facts: list[dict[str, Any]], needle: str) -> bool:
  n = needle.lower().strip()
  for f in facts:
    if n in _fact_text(f).lower():
      return True
  return False


def _extract_episode_uuids(source: str) -> list[str]:
  # LeanRAG provenance.source is usually pipe-delimited episode UUIDs.
  raw = [s.strip() for s in source.split("|")]
  return [s for s in raw if s]


def run_memory_qa_track(
  cfg: RunConfig,
  *,
  layout: RunLayout,
  event_logger: EventLogger,
  run_summary: RunSummary,
) -> None:
  if cfg.wc_code_path is None:
    raise SystemExit("memory_qa track requires --wc-code-path (RunConfig.wc_code_path)")

  tasks_path = Path(__file__).resolve().parent.parent.parent / "memory_qa" / "tasks.json"
  tasks = json.loads(_read_text(tasks_path))
  if not isinstance(tasks, list):
    raise SystemExit("memory_qa tasks.json must be a JSON list")

  # Load API keys (shared helper) for Graphiti / LeanRAG backends.
  try:
    from tests.benchmarks.scripts.run_swebench import setup_api_keys

    setup_api_keys()
  except Exception:
    pass

  from tests.benchmarks.wcbench.memory_seed import SeededEntry, build_leanrag_index_from_group, seed_into_graphiti
  from tests.benchmarks.scripts.wc_text_tools import WcToolSession
  from tests.benchmarks.wcbench.wc_tools import WcToolAdapter

  for t in tasks:
    task_id = str(t.get("task_id") or "")
    topic = str(t.get("thread_id") or "memory-qa")
    if not task_id:
      continue

    event_logger.emit(
      "task_start",
      run_id=cfg.run_id,
      task_id=task_id,
      payload={"title": task_id, "mode": "memory_qa", "topic": topic},
    )

    ok = True
    details: dict[str, Any] = {}
    try:
      seed_entries_raw = list(t.get("seed_entries") or [])
      seeded_thread_entries = [
        {"entry_id": e["entry_id"], "title": e.get("title", ""), "body": e.get("body", "")}
        for e in seed_entries_raw
      ]

      threads_dir = layout.artifacts_dir / "threads" / task_id
      _seed_threads(
        threads_dir=threads_dir,
        topic=topic,
        entries=seeded_thread_entries,
        event_logger=event_logger,
        run_id=cfg.run_id,
        task_id=task_id,
      )

      seeded_entries = [
        SeededEntry(
          entry_id=str(e["entry_id"]),
          title=str(e.get("title") or ""),
          body=str(e.get("body") or ""),
          timestamp=str(e.get("timestamp") or ""),
        )
        for e in seed_entries_raw
      ]
      seed = seed_into_graphiti(
        code_path=cfg.wc_code_path,
        run_id=cfg.run_id,
        task_id=task_id,
        artifacts_dir=layout.artifacts_dir,
        thread_id=topic,
        entries=seeded_entries,
      )
      details["group_id"] = seed.group_id
      details["entry_episode_index_path"] = str(seed.entry_episode_index_path)

      if bool(t.get("t3_index", False)):
        work_dir = build_leanrag_index_from_group(
          code_path=cfg.wc_code_path,
          seed=seed,
          artifacts_dir=layout.artifacts_dir,
          task_id=task_id,
        )
        details["leanrag_work_dir"] = str(work_dir)

      tier_ceiling = "T3" if bool(t.get("t3_index", False)) else "T2"
      base_session = WcToolSession(
        threads_dir=threads_dir,
        default_topic=topic,
        code_path=cfg.wc_code_path,
        group_ids=[seed.group_id],
        graphiti_entry_episode_index_path=seed.entry_episode_index_path,
        tier_ceiling=tier_ceiling,  # ensure smart-query can reach T3 when needed
        max_calls=max(8, cfg.wc_max_calls),
        token_budget=cfg.wc_token_budget,
        allow_write=False,
      )
      wc = WcToolAdapter(
        session=base_session,
        event_logger=event_logger,
        run_id=cfg.run_id,
        task_id=task_id,
      )

      assertions = dict(t.get("assertions") or {})

      # T2 active-only assertions (supersession correctness via invalid_at)
      if "t2_query" in assertions:
        q = str(assertions["t2_query"])
        expect_current = str(assertions.get("expect_current_contains") or "")
        expect_stale = str(assertions.get("expect_stale_contains") or "")
        require_stale = bool(assertions.get("require_stale_fact", False))

        full = wc.execute(f'wc-t2-facts "{q}"')
        full_facts = list(getattr(full, "meta", {}).get("facts", []) or [])
        stale_count = int(getattr(full, "meta", {}).get("stale_count", 0) or 0)

        if expect_current and not _assert_contains_any(full_facts, expect_current):
          raise AssertionError(f"T2 facts missing expected current needle: {expect_current}")
        if require_stale and stale_count < 1:
          raise AssertionError("T2 facts did not include any stale facts (invalid_at!=None).")
        if require_stale and expect_stale:
          stale_facts = [
            f for f in full_facts if isinstance(f, dict) and (f.get("invalid_at") is not None)
          ]
          if not _assert_contains_any(stale_facts, expect_stale):
            raise AssertionError(f"T2 facts missing expected stale needle: {expect_stale}")

        active = wc.execute(f'wc-t2-facts --active-only "{q}"')
        active_facts = list(getattr(active, "meta", {}).get("facts", []) or [])
        if any(isinstance(f, dict) and f.get("invalid_at") for f in active_facts):
          raise AssertionError("T2 active-only returned stale facts (invalid_at!=None).")
        if expect_current and not _assert_contains_any(active_facts, expect_current):
          raise AssertionError(f"T2 active-only missing expected current needle: {expect_current}")

        event_logger.emit(
          "test_result",
          run_id=cfg.run_id,
          task_id=task_id,
          payload={
            "name": "t2_active_only",
            "passed": True,
            "stale_count": stale_count,
            "returned_active": len(active_facts),
          },
        )

      # T3 reverse provenance: source_id episode UUIDs → entry_id via EntryEpisodeIndex
      if "t3_query" in assertions:
        q = str(assertions["t3_query"])
        expect_entry_ids = list(assertions.get("expect_resolves_to_entry_ids") or [])
        sq = wc.execute(f'wc-smart-query "{q}"')
        top = list(getattr(sq, "meta", {}).get("top_evidence", []) or [])
        t3 = [r for r in top if isinstance(r, dict) and r.get("tier") == "T3"]
        if not t3:
          raise AssertionError("T3 smart-query returned no T3 evidence in top results.")
        sources = [str(r.get("source") or "") for r in t3 if str(r.get("source") or "")]
        if not sources:
          raise AssertionError("T3 evidence missing provenance.source values.")

        resolved: list[str] = []
        for src in sources:
          for ep_uuid in _extract_episode_uuids(src):
            prov = wc.execute(f"wc-provenance {ep_uuid}")
            if bool(getattr(prov, "meta", {}).get("provenance_available", False)):
              eid = str(getattr(prov, "meta", {}).get("entry_id") or "")
              if eid:
                resolved.append(eid)
        if not resolved:
          raise AssertionError("wc-provenance failed to resolve any episode UUID to entry_id.")
        if expect_entry_ids and not any(e in set(expect_entry_ids) for e in resolved):
          raise AssertionError(
            f"Resolved entry_ids did not match expectations. resolved={sorted(set(resolved))} expected_any={expect_entry_ids}"
          )

        event_logger.emit(
          "test_result",
          run_id=cfg.run_id,
          task_id=task_id,
          payload={
            "name": "t3_reverse_provenance",
            "passed": True,
            "resolved_entry_ids": sorted(set(resolved))[:10],
          },
        )

      details["ok"] = True
    except Exception as e:
      ok = False
      details["ok"] = False
      details["error"] = str(e)
      event_logger.emit(
        "test_result",
        run_id=cfg.run_id,
        task_id=task_id,
        payload={"name": "memory_qa", "passed": False, "error": str(e)},
      )

    run_summary.tasks.append(
      TaskSummary(
        task_id=task_id,
        ok=ok,
        mode="memory_qa",
        cost=0.0,
        steps=0,
        wc_commands=0,
        details=details,
      )
    )
    event_logger.emit(
      "task_end",
      run_id=cfg.run_id,
      task_id=task_id,
      payload={"ok": ok},
    )

