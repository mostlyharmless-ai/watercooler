from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional


EventType = Literal[
  "run_start",
  "run_end",
  "task_start",
  "task_end",
  "agent_message",
  "tool_call",
  "tool_result",
  "shell_command",
  "shell_result",
  "test_result",
]


def _utc_iso() -> str:
  # ISO8601-ish without timezone dependency. Consumers can treat as UTC.
  return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class EventLogger:
  """Append-only JSONL event logger.

  The event log is the primary audit trail used for process metrics and
  guidance tuning.
  """

  path: Path

  def emit(
    self,
    event_type: EventType,
    *,
    run_id: str,
    payload: dict[str, Any],
    task_id: Optional[str] = None,
  ) -> None:
    record: dict[str, Any] = {
      "ts": _utc_iso(),
      "event_type": event_type,
      "run_id": run_id,
      "task_id": task_id,
      "payload": payload,
    }
    self.path.parent.mkdir(parents=True, exist_ok=True)
    with self.path.open("a", encoding="utf-8") as f:
      f.write(json.dumps(record, ensure_ascii=False) + "\n")

