from __future__ import annotations

import argparse
import time
from pathlib import Path

from tests.benchmarks.wcbench.config import RunConfig
from tests.benchmarks.wcbench.orchestrator import run_wcbench


def _default_run_id(prefix: str) -> str:
  return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"


def main() -> None:
  parser = argparse.ArgumentParser(description="wcbench: Watercooler-centric benchmark runner")
  parser.add_argument("--track", choices=["custom", "swebench", "coordination", "memory_qa"], default="custom")
  parser.add_argument("--run-id", default=None)
  parser.add_argument("--model", default="minimax/MiniMax-M2.5")
  parser.add_argument("--mode", choices=["baseline", "inject", "tools", "tools_guided"], default="tools_guided")
  parser.add_argument("--max-steps", type=int, default=25)
  parser.add_argument("--cost-limit", type=float, default=0.50)
  parser.add_argument("--output-root", type=str, default="logs")

  parser.add_argument("--wc-tier-ceiling", choices=["T1", "T2", "T3"], default="T1")
  parser.add_argument("--wc-max-calls", type=int, default=8)
  parser.add_argument("--wc-token-budget", type=int, default=1400)
  parser.add_argument("--wc-guidance-file", type=str, default="tests/benchmarks/guidance/watercooler_usage.md")
  parser.add_argument("--wc-code-path", type=str, default=".", help="Code path for T2/T3 backends (if used)")

  # Custom track args
  parser.add_argument("--custom-tasks", type=str, default="tests/benchmarks/custom/tasks/tasks.json")
  parser.add_argument("--custom-image-tag", type=str, default="watercooler-custom-bench:latest")
  parser.add_argument("--custom-repo-dir", type=str, default="tests/benchmarks/custom/repo")
  parser.add_argument("--custom-only", nargs="+", default=None, help="Run only these custom task_ids")

  # SWE-bench track args (passed through to tests/benchmarks/scripts/run_swebench.py)
  parser.add_argument("--swebench-dataset", type=str, default="SWE-bench/SWE-bench_Lite")
  parser.add_argument("--swebench-split", type=str, default="test")
  parser.add_argument("--swebench-instance-ids", nargs="+", default=None)
  parser.add_argument("--swebench-max-instances", type=int, default=None)
  parser.add_argument("--swebench-wc-pack", type=str, default=None)
  parser.add_argument("--swebench-eval-only", action="store_true")

  # Coordination track args
  parser.add_argument("--coordination-task-id", type=str, default="multi-hop-with-citations")

  args = parser.parse_args()

  run_id = args.run_id or _default_run_id(f"wcbench-{args.track}")

  guidance_file = Path(args.wc_guidance_file) if args.mode == "tools_guided" else None
  cfg = RunConfig(
    run_id=run_id,
    track=args.track,
    model=args.model,
    mode=args.mode,
    max_steps=args.max_steps,
    cost_limit=args.cost_limit,
    wc_tier_ceiling=args.wc_tier_ceiling,
    wc_max_calls=args.wc_max_calls,
    wc_token_budget=args.wc_token_budget,
    wc_guidance_file=guidance_file,
    wc_code_path=Path(args.wc_code_path) if args.wc_code_path else None,
    custom_tasks_path=Path(args.custom_tasks),
    custom_image_tag=args.custom_image_tag,
    custom_repo_dir=Path(args.custom_repo_dir),
    custom_only_task_ids=args.custom_only,
    swebench_dataset=args.swebench_dataset,
    swebench_split=args.swebench_split,
    swebench_instance_ids=args.swebench_instance_ids,
    swebench_max_instances=args.swebench_max_instances,
    swebench_wc_pack=Path(args.swebench_wc_pack) if args.swebench_wc_pack else None,
    swebench_eval_only=bool(args.swebench_eval_only),
    coordination_task_id=args.coordination_task_id,
    output_root=Path(args.output_root),
  )

  run_wcbench(cfg)


if __name__ == "__main__":
  main()

