#!/usr/bin/env python3
"""Run repeated long-budget baseline-vs-with-WC swebench experiment.

This script executes the phase-3 design:
- fixed calibrated instance set (12-16 recommended)
- repeated arms: baseline, tools, tools_guided
- completion-gated headline interpretation
- executive verdict table (No signal / Directional / Demonstrated)
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[3]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
  sys.path.insert(0, str(_SRC))
if str(_REPO) not in sys.path:
  sys.path.insert(0, str(_REPO))

from tests.benchmarks.scripts.run_full_benchmark import (
  _collect_calibration_stats,
  _select_calibrated_subset,
)
from tests.benchmarks.wcbench.aggregate import derive_metrics



@dataclass
class ArmRun:
  arm: str
  repeat: int
  run_id: str
  submitted: int
  completed: int
  resolved: int
  total_cost: float
  total_steps: int
  cross_thread_discovery_rate: float | None
  citation_accuracy: float | None
  context_rehydration_efficiency_delta: float | None
  resolved_over_completed: float | None
  resolved_over_submitted: float | None


def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
  p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)
  return p.returncode, (p.stdout or "") + (p.stderr or "")


def _load_summary(path: Path) -> dict[str, Any]:
  return json.loads(path.read_text(encoding="utf-8"))


def _extract_resolved_from_reports(swebench_output_dir: Path) -> tuple[int, int]:
  report_paths = list(swebench_output_dir.glob("logs/run_evaluation/**/report.json"))
  resolved = 0
  total = 0
  for rp in report_paths:
    try:
      data = json.loads(rp.read_text(encoding="utf-8"))
    except Exception:
      continue
    if isinstance(data, dict):
      for val in data.values():
        if isinstance(val, dict) and "resolved" in val:
          total += 1
          if bool(val.get("resolved")):
            resolved += 1
  return resolved, total


def _summarize_single_arm(run_root: Path, arm: str, repeat: int, run_id: str) -> ArmRun:
  swe_summary_path = run_root / "artifacts" / "swebench" / "summary.json"
  swe = _load_summary(swe_summary_path)
  results = list(swe.get("results", []))
  resolved, completed = _extract_resolved_from_reports(run_root / "artifacts" / "swebench")
  submitted = int(swe.get("instances", len(results)) or len(results))
  total_cost = float(swe.get("total_cost", 0.0) or 0.0)
  total_steps = sum(int(r.get("steps", 0) or 0) for r in results if isinstance(r, dict))
  derived = derive_metrics(run_root / "events.jsonl")
  return ArmRun(
    arm=arm,
    repeat=repeat,
    run_id=run_id,
    submitted=submitted,
    completed=completed,
    resolved=resolved,
    total_cost=total_cost,
    total_steps=total_steps,
    cross_thread_discovery_rate=derived.cross_thread_discovery_rate,
    citation_accuracy=derived.citation_accuracy,
    context_rehydration_efficiency_delta=derived.context_rehydration_efficiency_delta,
    resolved_over_completed=derived.resolved_over_completed,
    resolved_over_submitted=derived.resolved_over_submitted,
  )


def _median(vals: list[float]) -> float | None:
  if not vals:
    return None
  return float(statistics.median(vals))


def _mean(vals: list[float]) -> float | None:
  if not vals:
    return None
  return float(statistics.fmean(vals))


def _fmt(v: float | int | None, digits: int = 4) -> str:
  if v is None:
    return "N/A"
  if isinstance(v, int):
    return str(v)
  return f"{v:.{digits}f}"


def _claim_status(*, baseline_resolved_med: float, best_resolved_med: float, improved_value_metrics: int) -> str:
  if best_resolved_med > baseline_resolved_med and improved_value_metrics >= 2:
    return "Demonstrated"
  if best_resolved_med >= baseline_resolved_med and improved_value_metrics >= 1:
    return "Directional"
  return "No signal"


def main() -> int:
  parser = argparse.ArgumentParser(description="Run repeated value-aligned long-budget experiment")
  parser.add_argument("--output-dir", type=Path, default=None)
  parser.add_argument("--model", type=str, default="minimax/MiniMax-M2.5")
  parser.add_argument("--dataset", type=str, default="SWE-bench/SWE-bench_Lite")
  parser.add_argument("--split", type=str, default="test")
  parser.add_argument("--swebench-wc-pack", type=Path, required=True)
  parser.add_argument("--calibration-source-root", type=Path, default=_REPO / "logs")
  parser.add_argument("--calibrate-size", type=int, default=12)
  parser.add_argument("--calibrate-min-baseline-resolved", type=int, default=3)
  parser.add_argument("--repeats", type=int, default=3)
  parser.add_argument("--min-completion-rate", type=float, default=1.0)
  parser.add_argument("--wc-tier-ceiling", type=str, choices=["T1", "T2", "T3"], default="T3")
  parser.add_argument("--wc-max-calls", type=int, default=10)
  parser.add_argument("--wc-token-budget", type=int, default=1800)
  parser.add_argument("--wc-code-path", type=Path, default=_REPO)
  parser.add_argument("--max-instances", type=int, default=None)
  args = parser.parse_args()

  ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
  out_dir = args.output_dir or (_REPO / "logs" / f"value-aligned-experiment-{ts}")
  out_dir.mkdir(parents=True, exist_ok=True)

  # Step 1: calibrate fixed instance IDs using existing helper script.
  stats = _collect_calibration_stats(args.calibration_source_root)
  instance_ids = _select_calibrated_subset(
    stats,
    size=args.calibrate_size,
    min_baseline_resolved=args.calibrate_min_baseline_resolved,
  )
  if not instance_ids:
    raise RuntimeError("Calibration produced no instance IDs.")

  # Step 2: repeated arm runs on identical fixed IDs.
  arms = ("baseline", "tools", "tools_guided")
  all_runs: list[ArmRun] = []
  completion_gate_violations: list[str] = []
  for repeat in range(1, args.repeats + 1):
    for arm in arms:
      run_id = f"value-{arm}-r{repeat}-{ts}"
      run_cmd = [
        sys.executable,
        "-m",
        "tests.benchmarks.wcbench",
        "--track",
        "swebench",
        "--run-id",
        run_id,
        "--output-root",
        str(out_dir),
        "--model",
        args.model,
        "--mode",
        arm,
        "--swebench-dataset",
        args.dataset,
        "--swebench-split",
        args.split,
        "--swebench-instance-ids",
        *instance_ids,
        "--wc-tier-ceiling",
        args.wc_tier_ceiling,
        "--wc-max-calls",
        str(args.wc_max_calls),
        "--wc-token-budget",
        str(args.wc_token_budget),
        "--wc-code-path",
        str(args.wc_code_path),
      ]
      if args.max_instances is not None:
        run_cmd += ["--swebench-max-instances", str(args.max_instances)]
      if arm != "baseline":
        run_cmd += ["--swebench-wc-pack", str(args.swebench_wc_pack)]
      rc, out = _run(run_cmd, _REPO)
      if rc != 0:
        raise RuntimeError(f"Arm run failed arm={arm} repeat={repeat}:\n{out[-4000:]}")
      run_root = out_dir / run_id
      run = _summarize_single_arm(run_root, arm, repeat, run_id)
      completion_rate = (run.completed / run.submitted) if run.submitted > 0 else 0.0
      if completion_rate < args.min_completion_rate:
        completion_gate_violations.append(
          f"{arm} repeat={repeat}: {run.completed}/{run.submitted} < {args.min_completion_rate:.3f}"
        )
      all_runs.append(run)

  by_arm: dict[str, list[ArmRun]] = {arm: [r for r in all_runs if r.arm == arm] for arm in arms}
  summary: dict[str, dict[str, float | None]] = {}
  for arm in arms:
    rows = by_arm[arm]
    summary[arm] = {
      "resolved_median": _median([float(r.resolved) for r in rows]),
      "resolved_mean": _mean([float(r.resolved) for r in rows]),
      "completed_submitted_median": _median(
        [float(r.completed) / float(r.submitted) if r.submitted > 0 else 0.0 for r in rows]
      ),
      "resolved_over_completed_median": _median(
        [r.resolved_over_completed for r in rows if r.resolved_over_completed is not None]
      ),
      "resolved_over_submitted_median": _median(
        [r.resolved_over_submitted for r in rows if r.resolved_over_submitted is not None]
      ),
      "cross_thread_discovery_rate_median": _median(
        [r.cross_thread_discovery_rate for r in rows if r.cross_thread_discovery_rate is not None]
      ),
      "citation_accuracy_median": _median(
        [r.citation_accuracy for r in rows if r.citation_accuracy is not None]
      ),
      "context_rehydration_efficiency_delta_median": _median(
        [
          r.context_rehydration_efficiency_delta
          for r in rows
          if r.context_rehydration_efficiency_delta is not None
        ]
      ),
      "total_cost_median": _median([r.total_cost for r in rows]),
      "total_steps_median": _median([float(r.total_steps) for r in rows]),
    }

  baseline = summary["baseline"]
  best_arm = max(
    ("tools", "tools_guided"),
    key=lambda arm: float(summary[arm]["resolved_median"] or 0.0),
  )
  best = summary[best_arm]
  improved_value_metrics = 0
  for key in (
    "cross_thread_discovery_rate_median",
    "citation_accuracy_median",
    "context_rehydration_efficiency_delta_median",
  ):
    b = baseline.get(key)
    c = best.get(key)
    if b is None or c is None:
      continue
    # Lower context_rehydration_efficiency_delta is better; others higher is better.
    if key == "context_rehydration_efficiency_delta_median":
      if float(c) < float(b):
        improved_value_metrics += 1
    else:
      if float(c) > float(b):
        improved_value_metrics += 1

  status = _claim_status(
    baseline_resolved_med=float(baseline["resolved_median"] or 0.0),
    best_resolved_med=float(best["resolved_median"] or 0.0),
    improved_value_metrics=improved_value_metrics,
  )

  result_payload = {
    "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "instance_ids": instance_ids,
    "repeats": args.repeats,
    "arms": list(arms),
    "rows": [asdict(r) for r in all_runs],
    "summary": summary,
    "min_completion_rate": args.min_completion_rate,
    "completion_gate_violations": completion_gate_violations,
    "best_arm": best_arm,
    "improved_value_metrics_count": improved_value_metrics,
    "claim_status": status,
  }
  (out_dir / "value_experiment_results.json").write_text(json.dumps(result_payload, indent=2), encoding="utf-8")

  lines: list[str] = [
    "# Value-Aligned Benchmark Executive Verdict",
    "",
    f"- generated_at: `{result_payload['generated_at']}`",
    f"- repeats_per_arm: `{args.repeats}`",
    f"- fixed_instance_count: `{len(instance_ids)}`",
    f"- min_completion_rate: `{args.min_completion_rate:.3f}`",
    "",
    "## Executive table",
    "",
    "| Arm | Resolved (median) | Resolved (mean) | Completed/Submitted (median) | Rehydration delta (median) | Discovery rate (median) | Citation accuracy (median) | Cost (median) | Steps (median) |",
    "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
  ]
  for arm in arms:
    s = summary[arm]
    lines.append(
      "| "
      + " | ".join(
        [
          arm,
          _fmt(s.get("resolved_median"), 2),
          _fmt(s.get("resolved_mean"), 2),
          _fmt(s.get("completed_submitted_median"), 3),
          _fmt(s.get("context_rehydration_efficiency_delta_median"), 3),
          _fmt(s.get("cross_thread_discovery_rate_median"), 3),
          _fmt(s.get("citation_accuracy_median"), 3),
          _fmt(s.get("total_cost_median"), 4),
          _fmt(s.get("total_steps_median"), 1),
        ]
      )
      + " |"
    )

  lines.extend(
    [
      "",
      "## Completion gate",
      "",
      (
        "- status: `pass`"
        if not completion_gate_violations
        else "- status: `warning` (some runs below threshold; use completion-adjusted rates)"
      ),
      *[f"- violation: `{v}`" for v in completion_gate_violations],
      "",
      "## Verdict",
      "",
      f"- best_with_wc_arm: `{best_arm}`",
      f"- improved_value_metrics_count: `{improved_value_metrics}`",
      f"- claim_status: `{status}`",
      "",
      "## Fixed instance IDs",
      "",
      *[f"- `{iid}`" for iid in instance_ids],
      "",
    ]
  )
  (out_dir / "EXECUTIVE_VERDICT.md").write_text("\n".join(lines), encoding="utf-8")
  print(out_dir / "EXECUTIVE_VERDICT.md")
  print(out_dir / "value_experiment_results.json")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
