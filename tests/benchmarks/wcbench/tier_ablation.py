"""Tier ablation runner for memory_qa benchmark.

Runs the memory_qa track at multiple tier ceilings (T1, T2, T3) and produces
a per-category comparison table showing marginal value of each tier.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import replace
from pathlib import Path

from tests.benchmarks.wcbench.config import RunConfig, WcTierCeiling
from tests.benchmarks.wcbench.orchestrator import run_wcbench
from tests.benchmarks.wcbench.run_layout import make_run_layout
from tests.benchmarks.wcbench.summary import RunSummary, TaskSummary

TIER_CEILINGS: list[WcTierCeiling] = ["T1", "T2", "T3"]


def _group_by_category(summary: RunSummary) -> dict[str, dict[str, int]]:
  """Group task results by category, returning {category: {passed, total}}."""
  categories: dict[str, dict[str, int]] = {}
  for t in summary.tasks:
    cat = t.category or t.details.get("category", "") or "uncategorized"
    if cat not in categories:
      categories[cat] = {"passed": 0, "total": 0}
    categories[cat]["total"] += 1
    if t.ok:
      categories[cat]["passed"] += 1
  return categories


def _write_comparison_table(
  results: dict[str, dict[str, dict[str, int]]],
  output_dir: Path,
) -> Path:
  """Write a TIER_ABLATION.md comparison table."""
  # Collect all categories across tiers.
  all_categories: set[str] = set()
  for tier_results in results.values():
    all_categories.update(tier_results.keys())

  lines: list[str] = [
    "## Tier Ablation Results",
    "",
    "| Category | " + " | ".join(TIER_CEILINGS) + " | Best tier | T3 vs T1 |",
    "|" + "|".join(["---"] * (len(TIER_CEILINGS) + 3)) + "|",
  ]

  for cat in sorted(all_categories):
    row: list[str] = [cat]
    tier_pass_rates: dict[str, float] = {}
    for tier in TIER_CEILINGS:
      cat_data = results.get(tier, {}).get(cat, {"passed": 0, "total": 0})
      passed = cat_data["passed"]
      total = cat_data["total"]
      row.append(f"{passed}/{total}")
      tier_pass_rates[tier] = passed / total if total > 0 else 0.0

    # Best tier: the first tier that achieves the highest pass rate.
    best_rate = max(tier_pass_rates.values()) if tier_pass_rates else 0.0
    best_tier = "—"
    for tier in TIER_CEILINGS:
      if tier_pass_rates.get(tier, 0.0) == best_rate and best_rate > 0:
        best_tier = tier
        # If later tiers tie, note with "+"
        later = [t for t in TIER_CEILINGS if t != tier and tier_pass_rates.get(t, 0.0) == best_rate]
        if later:
          best_tier = f"{tier}+"
        break
    row.append(best_tier)

    # T3 vs T1 delta.
    t1_rate = tier_pass_rates.get("T1", 0.0)
    t3_rate = tier_pass_rates.get("T3", 0.0)
    delta = t3_rate - t1_rate
    if delta > 0:
      row.append(f"+{delta*100:.0f}%")
    elif delta < 0:
      row.append(f"{delta*100:.0f}%")
    else:
      row.append("0%")

    lines.append("| " + " | ".join(row) + " |")

  lines.append("")

  # Overall summary.
  lines.append("### Overall")
  lines.append("")
  for tier in TIER_CEILINGS:
    total_passed = sum(cd["passed"] for cd in results.get(tier, {}).values())
    total_tasks = sum(cd["total"] for cd in results.get(tier, {}).values())
    lines.append(f"- **{tier}**: {total_passed}/{total_tasks} passed")
  lines.append("")

  out_path = output_dir / "TIER_ABLATION.md"
  out_path.write_text("\n".join(lines), encoding="utf-8")
  return out_path


def run_tier_ablation(
  base_cfg: RunConfig,
  *,
  output_root: Path | None = None,
) -> Path:
  """Run memory_qa at each tier ceiling and produce comparison table.

  Args:
    base_cfg: Base configuration. Must have track="memory_qa".
    output_root: Override output directory. Defaults to base_cfg.output_root.

  Returns:
    Path to the generated TIER_ABLATION.md file.
  """
  if base_cfg.track != "memory_qa":
    raise ValueError(f"tier_ablation requires track='memory_qa', got '{base_cfg.track}'")

  root = output_root or base_cfg.output_root
  ablation_dir = root / f"{base_cfg.run_id}-ablation"
  ablation_dir.mkdir(parents=True, exist_ok=True)

  results: dict[str, dict[str, dict[str, int]]] = {}

  for ceiling in TIER_CEILINGS:
    sub_run_id = f"{base_cfg.run_id}-{ceiling}"
    cfg = replace(
      base_cfg,
      run_id=sub_run_id,
      wc_tier_ceiling=ceiling,
      output_root=root,
    )
    try:
      run_wcbench(cfg)
    except Exception:
      logging.getLogger(__name__).error("Tier %s run failed for %s", ceiling, sub_run_id, exc_info=True)
      raise
    sub_layout = make_run_layout(root, sub_run_id)
    try:
      raw_summary = json.loads(sub_layout.summary_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
      raise FileNotFoundError(
        f"Tier {ceiling} run {sub_run_id} did not produce a summary file at {sub_layout.summary_path}"
      )
    summary = RunSummary(
      run_id=str(raw_summary.get("run_id", sub_run_id)),
      track=str(raw_summary.get("track", cfg.track)),
      model=str(raw_summary.get("model", cfg.model)),
      mode=str(raw_summary.get("mode", cfg.mode)),
      started_at=str(raw_summary.get("started_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))),
      ended_at=str(raw_summary.get("ended_at", "")) or None,
      elapsed_seconds=float(raw_summary.get("elapsed_seconds", 0.0) or 0.0),
      tasks=[],
    )
    for task in list(raw_summary.get("tasks", [])):
      task_dict = dict(task) if isinstance(task, dict) else {}
      summary.tasks.append(
        TaskSummary(
          task_id=str(task_dict.get("task_id", "")),
          ok=bool(task_dict.get("ok", False)),
          mode=str(task_dict.get("mode", "")),
          cost=float(task_dict.get("cost", 0.0) or 0.0),
          steps=int(task_dict.get("steps", 0) or 0),
          wc_commands=int(task_dict.get("wc_commands", 0) or 0),
          wc_tools_used=dict(task_dict.get("wc_tools_used", {}) or {}),
          wc_entry_ids_returned=list(task_dict.get("wc_entry_ids_returned", []) or []),
          bash_commands=int(task_dict.get("bash_commands", 0) or 0),
          test_runs=int(task_dict.get("test_runs", 0) or 0),
          category=str(task_dict.get("category", "")),
          details=dict(task_dict.get("details", {}) or {}),
        )
      )

    results[ceiling] = _group_by_category(summary)

  return _write_comparison_table(results, ablation_dir)
