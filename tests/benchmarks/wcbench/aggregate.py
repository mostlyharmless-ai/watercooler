from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, DefaultDict, Iterable, Optional


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


def derive_metrics(events_path: Path) -> DerivedMetrics:
  m = DerivedMetrics()
  t2_total = 0
  t2_stale = 0
  prov_total = 0
  prov_ok = 0
  t3_total = 0
  t3_with_source = 0

  for e in iter_events(events_path):
    et = str(e.get("event_type") or "unknown")
    m.event_type_counts[et] = m.event_type_counts.get(et, 0) + 1

    payload = e.get("payload") or {}
    if isinstance(payload, dict):
      if et == "tool_call":
        tool = str(payload.get("tool") or "unknown")
        m.tool_call_counts[tool] = m.tool_call_counts.get(tool, 0) + 1

        task_id = e.get("task_id")
        if isinstance(task_id, str) and task_id:
          per = m.per_task_tool_calls.setdefault(task_id, {})
          per[tool] = per.get(tool, 0) + 1

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
            t3_total += int(meta.get("t3_results") or 0)
            t3_with_source += int(meta.get("t3_with_source") or 0)

      if et == "test_result":
        passed = bool(payload.get("passed", payload.get("ok")))
        if passed:
          m.tests_passed += 1
        else:
          m.tests_failed += 1

  if t2_total > 0:
    m.t2_stale_fact_rate = float(t2_stale) / float(t2_total)
  if prov_total > 0:
    m.t3_provenance_resolve_rate = float(prov_ok) / float(prov_total)
  if t3_total > 0:
    m.t3_source_id_coverage = float(t3_with_source) / float(t3_total)

  return m

