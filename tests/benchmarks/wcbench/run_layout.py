from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunLayout:
  """Standard on-disk layout for a single benchmark run."""

  root: Path

  @property
  def events_path(self) -> Path:
    return self.root / "events.jsonl"

  @property
  def summary_path(self) -> Path:
    return self.root / "summary.json"

  @property
  def report_path(self) -> Path:
    return self.root / "report.md"

  @property
  def repro_dir(self) -> Path:
    return self.root / "repro"

  @property
  def artifacts_dir(self) -> Path:
    return self.root / "artifacts"


def make_run_layout(output_root: Path, run_id: str) -> RunLayout:
  root = output_root / run_id
  root.mkdir(parents=True, exist_ok=True)
  (root / "repro").mkdir(parents=True, exist_ok=True)
  (root / "artifacts").mkdir(parents=True, exist_ok=True)
  return RunLayout(root=root)

