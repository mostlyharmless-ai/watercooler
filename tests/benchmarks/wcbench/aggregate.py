from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional


def iter_events(path: Path) -> Iterable[dict[str, Any]]:
  if not path.exists():
    return []
  with path.open("r", encoding="utf-8") as f:
    for line in f:
      line = line.strip()
      if not line:
        continue
      try:
        obj = json.loads(line)
        if isinstance(obj, dict):
          yield obj
      except Exception:
        continue


@dataclass
class DerivedMetrics:
  event_type_counts: dict[str, int] = field(default_factory=dict)
  tool_call_counts: dict[str, int] = field(default_factory=dict)
  per_task_tool_calls: dict[str, dict[str, int]] = field(default_factory=dict)
  tests_passed: int = 0
  tests_failed: int = 0
  t2_stale_fact_rate: Optional[float] = None
  t3_provenance_resolve_rate: Optional[float] = None
  t3_source_id_coverage: Optional[float] = None
  t3_multi_hop_quality_rate: Optional[float] = None
  cross_thread_discovery_rate: Optional[float] = None
  tier_preference_distribution: dict[str, float] = field(default_factory=dict)
  citation_accuracy: Optional[float] = None
  recall_at_k_mean: Optional[float] = None
  mrr_mean: Optional[float] = None
  context_rehydration_efficiency_delta: Optional[float] = None
  resolved_over_completed: Optional[float] = None
  resolved_over_submitted: Optional[float] = None


def derive_metrics(events_path: Path) -> DerivedMetrics:
  m = DerivedMetrics()
  t2_total = 0
  t2_stale = 0
  prov_total = 0
  prov_ok = 0
  t3_total = 0
  t3_with_source = 0
  smart_query_count = 0
  smart_primary_tiers: dict[str, int] = {}
  thread_topics_by_task: dict[str, set[str]] = {}
  smart_query_with_cross_topic = 0
  citation_task_count = 0
  citation_task_hits = 0
  phase_steps: dict[str, dict[str, int]] = {}
  submitted = 0
  completed = 0
  resolved = 0
  t3_multihop_total = 0
  t3_multihop_good = 0
  recall_at_k_values: list[float] = []
  mrr_values: list[float] = []

  for e in iter_events(events_path):
    et = str(e.get("event_type") or "unknown")
    m.event_type_counts[et] = m.event_type_counts.get(et, 0) + 1
    task_id = e.get("task_id")
    task_id_str = task_id if isinstance(task_id, str) else ""

    payload = e.get("payload") or {}
    if isinstance(payload, dict):
      if et == "tool_call":
        tool = str(payload.get("tool") or "unknown")
        m.tool_call_counts[tool] = m.tool_call_counts.get(tool, 0) + 1

        if task_id_str:
          per = m.per_task_tool_calls.setdefault(task_id_str, {})
          per[tool] = per.get(tool, 0) + 1
          command = str(payload.get("command") or "")
          if command.startswith("wc-read-thread "):
            try:
              argv = shlex.split(command)
            except Exception:
              argv = command.split()
            if len(argv) >= 2:
              topic = str(argv[1]).strip()
              if topic:
                topics = thread_topics_by_task.setdefault(task_id_str, set())
                topics.add(topic)

      if et == "tool_result":
        tool = str(payload.get("tool") or "unknown")
        meta = payload.get("meta") or {}
        if isinstance(meta, dict):
          if tool == "wc-t2-facts" and bool(meta.get("active_only")):
            t2_total += int(meta.get("total") or 0)
            t2_stale += int(meta.get("stale_count") or 0)
          if tool == "wc-provenance":
            prov_total += 1
            if bool(meta.get("provenance_available")):
              prov_ok += 1
          if tool == "wc-smart-query":
            smart_query_count += 1
            primary_tier = str(meta.get("primary_tier") or "").strip()
            if primary_tier:
              smart_primary_tiers[primary_tier] = smart_primary_tiers.get(primary_tier, 0) + 1
            t3_total += int(meta.get("t3_results") or 0)
            t3_with_source += int(meta.get("t3_with_source") or 0)
            top_evidence = meta.get("top_evidence")
            if isinstance(top_evidence, list):
              cross_topic = 0
              for ev in top_evidence:
                if not isinstance(ev, dict):
                  continue
                source = str(ev.get("source") or "")
                if "|" in source:
                  cross_topic += 1
              if cross_topic > 0:
                smart_query_with_cross_topic += 1

      if et == "test_result":
        passed = bool(payload.get("passed", payload.get("ok")))
        if passed:
          m.tests_passed += 1
        else:
          m.tests_failed += 1
        if str(payload.get("name") or "") == "t3_reverse_provenance":
          t3_multihop_total += 1
          if int(payload.get("resolved_entry_count") or 0) >= 2:
            t3_multihop_good += 1
        # Citation accuracy from test_result events (decision_recall, etc.)
        if bool(payload.get("citation_required")):
          citation_task_count += 1
          gold = set(str(x) for x in (payload.get("citation_gold_ids") or []) if str(x))
          got = set(str(x) for x in (payload.get("citation_observed_ids") or []) if str(x))
          if gold and got.intersection(gold):
            citation_task_hits += 1
        # Rehydration retrieval quality metrics
        if str(payload.get("name") or "") == "rehydration":
          recall_at_k_values.append(float(payload.get("recall_at_5") or 0))
          mrr_values.append(float(payload.get("mrr") or 0))

      if et == "task_start":
        submitted += 1

      if et == "task_end":
        completed += 1
        if bool(payload.get("ok")):
          resolved += 1
        if task_id_str and ":" in task_id_str:
          parent, _, phase = task_id_str.rpartition(":")
          if phase in {"AgentA", "AgentB"}:
            steps = int(payload.get("steps") or 0)
            phases = phase_steps.setdefault(parent, {})
            phases[phase] = steps

        details = payload.get("details")
        if isinstance(details, dict) and bool(details.get("citation_required")):
          citation_task_count += 1
          gold = set(str(x) for x in (details.get("citation_gold_ids") or []) if str(x))
          got = set(str(x) for x in (details.get("citation_observed_ids") or []) if str(x))
          if not gold:
            continue
          if bool(got.intersection(gold)):
            citation_task_hits += 1

  if t2_total > 0:
    m.t2_stale_fact_rate = float(t2_stale) / float(t2_total)
  if prov_total > 0:
    m.t3_provenance_resolve_rate = float(prov_ok) / float(prov_total)
  if t3_total > 0:
    m.t3_source_id_coverage = float(t3_with_source) / float(t3_total)
  if t3_multihop_total > 0:
    m.t3_multi_hop_quality_rate = float(t3_multihop_good) / float(t3_multihop_total)
  if submitted > 0:
    m.resolved_over_submitted = float(resolved) / float(submitted)
  if completed > 0:
    m.resolved_over_completed = float(resolved) / float(completed)
  if smart_query_count > 0:
    m.cross_thread_discovery_rate = float(smart_query_with_cross_topic) / float(smart_query_count)
  if smart_primary_tiers:
    total = float(sum(smart_primary_tiers.values()))
    m.tier_preference_distribution = {
      tier: float(count) / total for tier, count in sorted(smart_primary_tiers.items())
    }
  if citation_task_count > 0:
    m.citation_accuracy = float(citation_task_hits) / float(citation_task_count)
  if recall_at_k_values:
    m.recall_at_k_mean = sum(recall_at_k_values) / len(recall_at_k_values)
  if mrr_values:
    m.mrr_mean = sum(mrr_values) / len(mrr_values)

  deltas: list[float] = []
  for phases in phase_steps.values():
    if "AgentA" not in phases or "AgentB" not in phases:
      continue
    a = phases["AgentA"]
    b = phases["AgentB"]
    if a <= 0:
      continue
    deltas.append(float(b - a) / float(a))
  if deltas:
    m.context_rehydration_efficiency_delta = sum(deltas) / float(len(deltas))

  return m

