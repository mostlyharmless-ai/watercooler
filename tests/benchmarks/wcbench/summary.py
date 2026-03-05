from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class TaskSummary:
  task_id: str
  ok: bool
  mode: str
  cost: float = 0.0
  steps: int = 0
  wc_commands: int = 0
  wc_tools_used: dict[str, int] = field(default_factory=dict)
  wc_entry_ids_returned: list[str] = field(default_factory=list)
  bash_commands: int = 0
  test_runs: int = 0
  category: str = ""
  details: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunSummary:
  run_id: str
  track: str
  model: str
  mode: str
  started_at: str
  ended_at: Optional[str] = None
  elapsed_seconds: float = 0.0
  tasks: list[TaskSummary] = field(default_factory=list)

  def to_dict(self) -> dict[str, Any]:
    d = asdict(self)
    d["passed"] = sum(1 for t in self.tasks if t.ok)
    d["total_tasks"] = len(self.tasks)
    return d

  def write_json(self, path: Path) -> None:
    path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

