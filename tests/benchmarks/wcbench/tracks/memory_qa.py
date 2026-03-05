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


def _extract_entry_ids(result: Any) -> set[str]:
  """Extract entry_ids from a WcCommandResult."""
  ids = list(getattr(result, "entry_ids", []) or [])
  return set(str(i) for i in ids if str(i))


def _extract_entry_ids_ranked(result: Any) -> list[str]:
  """Extract entry_ids from a WcCommandResult preserving rank order."""
  ids = list(getattr(result, "entry_ids", []) or [])
  return [str(i) for i in ids if str(i)]


def _to_thread_dicts(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
  """Convert raw task JSON entries to the format _seed_threads expects."""
  return [
    {"entry_id": e["entry_id"], "title": e.get("title", ""), "body": e.get("body", "")}
    for e in entries
  ]


def run_memory_qa_track(
  cfg: RunConfig,
  *,
  layout: RunLayout,
  event_logger: EventLogger,
  run_summary: RunSummary,
) -> None:
  if cfg.wc_code_path is None:
    raise ValueError("memory_qa track requires --wc-code-path (RunConfig.wc_code_path)")

  tasks_path = Path(__file__).resolve().parent.parent.parent / "memory_qa" / "tasks.json"
  tasks = json.loads(_read_text(tasks_path))
  if not isinstance(tasks, list):
    raise ValueError("memory_qa tasks.json must be a JSON list")

  # Load API keys (shared helper) for Graphiti / LeanRAG backends.
  try:
    from tests.benchmarks.scripts.run_swebench import setup_api_keys

    setup_api_keys()
  except Exception as exc:
    log.warning("API key setup failed: %s", exc)

  from tests.benchmarks.wcbench.memory_seed import SeededEntry, build_leanrag_index_from_group, seed_into_graphiti
  from tests.benchmarks.scripts.wc_text_tools import WcToolSession
  from tests.benchmarks.wcbench.wc_tools import WcToolAdapter

  for t in tasks:
    task_id = str(t.get("task_id") or "")
    topic = str(t.get("thread_id") or "memory-qa")
    category = str(t.get("category") or "")
    if not task_id:
      continue

    event_logger.emit(
      "task_start",
      run_id=cfg.run_id,
      task_id=task_id,
      payload={"title": task_id, "mode": "memory_qa", "topic": topic, "category": category},
    )

    ok = True
    details: dict[str, Any] = {}
    if category:
      details["category"] = category

    # --- Seeding phase (wrapped separately for seed error distinction) ---
    seed = None
    threads_dir = layout.artifacts_dir / "threads" / task_id
    try:
      seed_threads_list = t.get("seed_threads") or []
      if seed_threads_list:
        # Multi-topic seeding (Category D cross-thread tasks)
        for st in seed_threads_list:
          st_topic = str(st["thread_id"])
          st_entries = list(st.get("entries") or [])
          threads_sub = layout.artifacts_dir / "threads" / task_id
          _seed_threads(
            threads_dir=threads_sub,
            topic=st_topic,
            entries=_to_thread_dicts(st_entries),
            event_logger=event_logger,
            run_id=cfg.run_id,
            task_id=task_id,
          )

        # Flatten all entries for a single Graphiti seed call (same group_id).
        all_entries_raw = [e for st in seed_threads_list for e in (st.get("entries") or [])]
        seeded_entries = [
          SeededEntry(
            entry_id=str(e["entry_id"]),
            title=str(e.get("title") or ""),
            body=str(e.get("body") or ""),
            timestamp=str(e.get("timestamp") or ""),
          )
          for e in all_entries_raw
        ]
        seed = seed_into_graphiti(
          code_path=cfg.wc_code_path,
          run_id=cfg.run_id,
          task_id=task_id,
          artifacts_dir=layout.artifacts_dir,
          thread_id=task_id,  # use task_id as unified thread_id for cross-thread
          entries=seeded_entries,
          entries_raw=all_entries_raw,
        )
        # Use the first thread's topic as default for WcToolSession
        topic = str(seed_threads_list[0]["thread_id"])
      else:
        # Existing single-topic path
        seed_entries_raw = list(t.get("seed_entries") or [])
        seeded_thread_entries = _to_thread_dicts(seed_entries_raw)

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
          entries_raw=seed_entries_raw,
        )

      details["group_id"] = seed.group_id
      details["entry_episode_index_path"] = str(seed.entry_episode_index_path)

      if bool(t.get("t3_index", False)):
        work_dir = build_leanrag_index_from_group(
          code_path=cfg.wc_code_path,
          seed=seed,
          artifacts_dir=layout.artifacts_dir,
          task_id=task_id,
          entries=seeded_entries,
        )
        details["leanrag_work_dir"] = str(work_dir)

    except Exception as seed_exc:
      event_logger.emit(
        "seed_error",
        run_id=cfg.run_id,
        task_id=task_id,
        payload={"error": str(seed_exc)},
      )
      details["seed_failure"] = True
      details["error"] = f"Seed error: {seed_exc}"
      ok = False
      # Record and continue — the test didn't run, it didn't fail on assertions.
      run_summary.tasks.append(
        TaskSummary(
          task_id=task_id,
          ok=ok,
          mode="memory_qa",
          category=category,
          details=details,
        )
      )
      event_logger.emit(
        "task_end",
        run_id=cfg.run_id,
        task_id=task_id,
        payload={"ok": ok, "steps": 0, "details": details},
      )
      continue

    # --- Assertion phase ---
    try:
      tier_ceiling = "T3" if bool(t.get("t3_index", False)) else "T2"
      base_session = WcToolSession(
        threads_dir=threads_dir,
        default_topic=topic,
        code_path=cfg.wc_code_path,
        group_ids=[seed.group_id],
        graphiti_entry_episode_index_path=seed.entry_episode_index_path,
        tier_ceiling=tier_ceiling,
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

        # Use --all to make this deterministic (doesn't depend on semantic indices).
        full = wc.execute(f'wc-t2-facts --all "{q}"')
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

        active = wc.execute(f'wc-t2-facts --all --active-only "{q}"')
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
            "category": category,
            "stale_count": stale_count,
            "returned_active": len(active_facts),
          },
        )

      # T3 reverse provenance: source_id episode UUIDs → entry_id via EntryEpisodeIndex
      if "t3_query" in assertions:
        q = str(assertions["t3_query"])
        expect_entry_ids = list(assertions.get("expect_resolves_to_entry_ids") or [])
        expect_answer_contains = str(assertions.get("expect_answer_contains") or "").strip()
        require_multi_hop_min_entries = int(assertions.get("require_multi_hop_min_entries", 0) or 0)
        # Force T3 so this validates reverse provenance end-to-end.
        sq = wc.execute(f'wc-smart-query --force-tier T3 "{q}"')
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
        unique_resolved = sorted(set(resolved))
        if expect_entry_ids and not any(e in set(expect_entry_ids) for e in resolved):
          raise AssertionError(
            f"Resolved entry_ids did not match expectations. resolved={unique_resolved} expected_any={expect_entry_ids}"
          )
        if require_multi_hop_min_entries > 0 and len(unique_resolved) < require_multi_hop_min_entries:
          raise AssertionError(
            f"T3 multi-hop requirement failed: expected >= {require_multi_hop_min_entries} unique source entries, "
            f"got {len(unique_resolved)}"
          )
        if expect_answer_contains:
          rendered = str(getattr(sq, "output", "") or "").lower()
          if expect_answer_contains.lower() not in rendered:
            raise AssertionError(
              f"T3 answer text missing expected phrase: {expect_answer_contains}"
            )

        event_logger.emit(
          "test_result",
          run_id=cfg.run_id,
          task_id=task_id,
          payload={
            "name": "t3_reverse_provenance",
            "passed": True,
            "category": category,
            "resolved_entry_ids": unique_resolved[:10],
            "resolved_entry_count": len(unique_resolved),
          },
        )

      # Decision recall: gold entry retrieval via T1 search
      if "decision_recall_query" in assertions:
        q = str(assertions["decision_recall_query"])
        expected_ids = list(assertions.get("expect_entry_ids_in_results") or [])
        expect_contains = str(assertions.get("expect_answer_contains") or "")

        result = wc.execute(f'wc-search "{q}"')
        returned_ids = _extract_entry_ids(result)

        if expected_ids and not returned_ids.intersection(set(expected_ids)):
          raise AssertionError(
            f"Decision recall: expected entry_ids {expected_ids} "
            f"not in results {sorted(returned_ids)}"
          )
        if expect_contains and expect_contains.lower() not in (
          getattr(result, "output", "") or ""
        ).lower():
          raise AssertionError(
            f"Decision recall: expected '{expect_contains}' not in output"
          )

        event_logger.emit(
          "test_result",
          run_id=cfg.run_id,
          task_id=task_id,
          payload={
            "name": "decision_recall",
            "passed": True,
            "category": category,
            "citation_required": True,
            "citation_gold_ids": expected_ids,
            "citation_observed_ids": sorted(returned_ids),
          },
        )

      # Rehydration: retrieval quality (Recall@K, MRR)
      if "rehydration_query" in assertions:
        q = str(assertions["rehydration_query"])
        gold_ids = set(assertions.get("expect_entry_ids_in_results") or [])
        gold_count = int(assertions.get("gold_relevant_count") or len(gold_ids))

        result = wc.execute(f'wc-search "{q}"')
        returned_ids = _extract_entry_ids_ranked(result)

        # Recall@5
        top_k = returned_ids[:5]
        hits_at_k = len(gold_ids.intersection(set(top_k)))
        recall_at_5 = hits_at_k / gold_count if gold_count > 0 else 0.0

        # MRR
        mrr = 0.0
        for rank, rid in enumerate(returned_ids, 1):
          if rid in gold_ids:
            mrr = 1.0 / rank
            break

        passed = recall_at_5 > 0  # at least one gold entry in top-5
        if not passed:
          ok = False

        event_logger.emit(
          "test_result",
          run_id=cfg.run_id,
          task_id=task_id,
          payload={
            "name": "rehydration",
            "passed": passed,
            "category": category,
            "recall_at_5": recall_at_5,
            "mrr": mrr,
            "gold_count": gold_count,
            "hits_at_5": hits_at_k,
          },
        )

      # Cross-thread synthesis: multi-topic retrieval via smart-query
      if "cross_thread_query" in assertions:
        q = str(assertions["cross_thread_query"])
        expect_threads = set(assertions.get("expect_threads_cited") or [])
        expect_min = int(assertions.get("expect_min_threads_in_results") or 2)
        expect_contains = str(assertions.get("expect_answer_contains") or "")

        result = wc.execute(f'wc-smart-query "{q}"')
        top = list(getattr(result, "meta", {}).get("top_evidence", []) or [])

        threads_found: set[str] = set()
        for r in top:
          # Try multiple field names for thread topic
          ev_topic = str(r.get("topic") or r.get("thread_topic") or "")
          if ev_topic:
            threads_found.add(ev_topic)
          # Also check the source field for thread topic hints
          source = str(r.get("source") or "")
          if source and "thread:" in source:
            for part in source.split():
              if part.startswith("thread:"):
                threads_found.add(part[7:])

        if len(threads_found) < expect_min:
          raise AssertionError(
            f"Cross-thread: found {len(threads_found)} threads "
            f"({threads_found}), expected >= {expect_min}"
          )
        if expect_threads and not expect_threads.issubset(threads_found):
          raise AssertionError(
            f"Cross-thread: missing expected threads "
            f"{expect_threads - threads_found}"
          )
        if expect_contains and expect_contains.lower() not in (
          getattr(result, "output", "") or ""
        ).lower():
          raise AssertionError(
            f"Cross-thread: expected '{expect_contains}' not in output"
          )

        event_logger.emit(
          "test_result",
          run_id=cfg.run_id,
          task_id=task_id,
          payload={
            "name": "cross_thread",
            "passed": True,
            "category": category,
            "threads_found": sorted(threads_found),
            "threads_expected": sorted(expect_threads),
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
        category=category,
        details=details,
      )
    )
    event_logger.emit(
      "task_end",
      run_id=cfg.run_id,
      task_id=task_id,
      payload={"ok": ok, "steps": 0, "details": details},
    )
