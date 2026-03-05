from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from tests.benchmarks.wcbench.events import EventLogger


@dataclass
class WcToolAdapter:
  """Wrap `WcToolSession` to add structured event logging."""

  session: Any
  event_logger: Optional[EventLogger]
  run_id: str
  task_id: str

  def execute(self, command: str) -> Any:
    if self.event_logger is not None:
      self.event_logger.emit(
        "tool_call",
        run_id=self.run_id,
        task_id=self.task_id,
        payload={"tool": "wc-text-tools", "command": command},
      )

    result = self.session.execute(command)

    tool = getattr(result, "tool", "wc-unknown")
    ok = bool(getattr(result, "ok", True))
    entry_ids = list(getattr(result, "entry_ids", []) or [])
    output = getattr(result, "output", "") or ""
    meta = getattr(result, "meta", {}) or {}
    meta_payload: dict[str, Any] = {}
    if isinstance(meta, dict):
      # Keep tool_result lightweight; include only metric-relevant keys.
      for k in (
        "active_only",
        "returned",
        "total",
        "stale_count",
        "tiers_queried",
        "primary_tier",
        "result_count",
        "t3_results",
        "t3_with_source",
        "top_evidence",
        "provenance_available",
        "query",
      ):
        if k in meta:
          meta_payload[k] = meta.get(k)

    if self.event_logger is not None:
      self.event_logger.emit(
        "tool_result",
        run_id=self.run_id,
        task_id=self.task_id,
        payload={
          "tool": tool,
          "ok": ok,
          "entry_ids": entry_ids[:10],
          "output_head": output[:2000],
          "meta": meta_payload,
        },
      )

    return result

