from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.benchmarks.wcbench.config import RunConfig
from tests.benchmarks.wcbench.events import EventLogger
from tests.benchmarks.wcbench.run_layout import RunLayout
from tests.benchmarks.wcbench.summary import RunSummary, TaskSummary


def _extract_resolved(output_dir: Path) -> tuple[int, int]:
  resolved = 0
  total = 0
  for report_path in output_dir.glob("logs/run_evaluation/**/report.json"):
    try:
      report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
      continue
    if not isinstance(report, dict):
      continue
    for value in report.values():
      if isinstance(value, dict) and "resolved" in value:
        total += 1
        if bool(value.get("resolved")):
          resolved += 1
  return resolved, total


def run_swebench_track(cfg: RunConfig, *, layout: RunLayout, event_logger: EventLogger, run_summary: RunSummary) -> None:
  """Run SWE-bench via the existing script, but keep artifacts under this run layout."""
  output_dir = layout.artifacts_dir / "swebench"
  output_dir.mkdir(parents=True, exist_ok=True)

  cmd: list[str] = [
    sys.executable,
    "tests/benchmarks/scripts/run_swebench.py",
    "--model",
    cfg.model,
    "--dataset",
    cfg.swebench_dataset,
    "--split",
    cfg.swebench_split,
    "--max-steps",
    str(cfg.max_steps),
    "--cost-limit",
    str(cfg.cost_limit),
    "--output-dir",
    str(output_dir),
  ]

  if cfg.swebench_instance_ids:
    cmd += ["--instance-ids", *list(cfg.swebench_instance_ids)]
  if cfg.swebench_max_instances is not None:
    cmd += ["--max-instances", str(cfg.swebench_max_instances)]
  if cfg.swebench_eval_only:
    cmd += ["--eval-only"]

  # Watercooler integration (pass-through to the SWE-bench script)
  cmd += ["--wc-tier-ceiling", cfg.wc_tier_ceiling]
  if cfg.wc_code_path is not None:
    cmd += ["--wc-code-path", str(cfg.wc_code_path)]

  if cfg.mode == "baseline":
    cmd += ["--wc-mode", "baseline"]
  else:
    if cfg.swebench_wc_pack is None:
      raise ValueError("SWE-bench wc modes require --swebench-wc-pack")
    cmd += ["--wc-pack", str(cfg.swebench_wc_pack)]
    cmd += ["--wc-mode", cfg.mode]
    cmd += ["--wc-max-calls", str(cfg.wc_max_calls)]
    cmd += ["--wc-token-budget", str(cfg.wc_token_budget)]
    if cfg.mode == "tools_guided" and cfg.wc_guidance_file is not None:
      cmd += ["--wc-guidance-file", str(cfg.wc_guidance_file)]

  event_logger.emit(
    "shell_command",
    run_id=cfg.run_id,
    payload={"cmd": cmd, "cwd": str(Path.cwd())},
  )

  p = subprocess.run(
    cmd,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    check=False,
  )

  event_logger.emit(
    "shell_result",
    run_id=cfg.run_id,
    payload={
      "returncode": p.returncode,
      "output_head": (p.stdout or "")[:8000],
      "output_dir": str(output_dir),
    },
  )

  summary_path = output_dir / "summary.json"
  swe_summary = {}
  if summary_path.exists():
    try:
      swe_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:
      swe_summary = {}

  # Represent the full SWE-bench run as a single task summary entry.
  results = list(swe_summary.get("results", []))
  instances = int(swe_summary.get("instances", 0) or 0)
  total_cost = float(swe_summary.get("total_cost", 0.0) or 0.0)
  total_wc = int((swe_summary.get("aggregate_metrics", {}) or {}).get("total_wc_commands", 0) or 0)
  total_bash = int((swe_summary.get("aggregate_metrics", {}) or {}).get("total_bash_commands", 0) or 0)
  total_tests = int((swe_summary.get("aggregate_metrics", {}) or {}).get("total_test_runs", 0) or 0)
  total_duplicates = int((swe_summary.get("aggregate_metrics", {}) or {}).get("total_duplicate_commands", 0) or 0)
  total_steps = sum(int((r or {}).get("steps", 0) or 0) for r in results if isinstance(r, dict))
  resolved, resolved_total = _extract_resolved(output_dir)

  wc_instances = 0
  retrieval_action_hits = 0
  for r in results:
    if not isinstance(r, dict):
      continue
    metrics = dict(r.get("metrics", {}) or {})
    if int(metrics.get("wc_commands", 0) or 0) > 0:
      wc_instances += 1
      if int(metrics.get("file_edits", 0) or 0) > 0 or int(metrics.get("test_runs", 0) or 0) > 0:
        retrieval_action_hits += 1
  retrieval_action_proxy_rate = float(retrieval_action_hits) / float(wc_instances) if wc_instances > 0 else None

  run_summary.tasks.append(
    TaskSummary(
      task_id="swebench",
      ok=(p.returncode == 0),
      mode=cfg.mode,
      cost=total_cost,
      steps=total_steps,
      wc_commands=total_wc,
      bash_commands=total_bash,
      test_runs=total_tests,
      details={
        "instances": instances,
        "resolved": resolved,
        "resolved_total": resolved_total,
        "resolve_rate": (float(resolved) / float(resolved_total)) if resolved_total > 0 else None,
        "cost_per_resolved": (float(total_cost) / float(resolved)) if resolved > 0 else None,
        "steps_per_resolved": (float(total_steps) / float(resolved)) if resolved > 0 else None,
        "duplicate_command_rate": (float(total_duplicates) / float(total_bash)) if total_bash > 0 else None,
        "retrieval_to_action_proxy_rate": retrieval_action_proxy_rate,
        "output_dir": str(output_dir),
        "swebench_summary_path": str(summary_path),
      },
    )
  )

  if p.returncode != 0:
    raise RuntimeError(f"SWE-bench runner failed (exit={p.returncode}); see {output_dir}")

