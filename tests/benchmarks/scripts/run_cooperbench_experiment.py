#!/usr/bin/env python3
"""Run CooperBench A/B experiment: Redis vs Watercooler channel.

Runs a set of CooperBench cooperative SWE tasks twice — once with the
default Redis-backed MessagingConnector and once with
WatercoolerMessagingConnector — then compares results.

Prerequisites:
  - CooperBench cloned and pip-installed (``pip install -e CooperBench/``)
  - Docker running (for evaluation sandbox)
  - LLM API key set (e.g. ``ANTHROPIC_API_KEY``)

Usage::

    python tests/benchmarks/scripts/run_cooperbench_experiment.py \\
        --cooperbench-dir /path/to/CooperBench \\
        --subset lite \\
        --model claude-sonnet-4-20250514

Output: ``logs/{run_name}/summary.json`` with pass rates for both channels.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="CooperBench A/B: Redis vs Watercooler channel",
    )
    p.add_argument(
        "--cooperbench-dir",
        type=Path,
        default=Path.home()
        / "Work/Personal/MostlyHarmless-AI/repo/CooperBench",
        help="Path to CooperBench clone",
    )
    p.add_argument(
        "--subset",
        choices=["lite", "full"],
        default="lite",
        help="Task subset (lite = first 3 tasks, full = all)",
    )
    p.add_argument(
        "--model",
        default="claude-sonnet-4-20250514",
        help="LLM model identifier",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory (default: logs/<timestamp>)",
    )
    return p.parse_args()


def discover_tasks(cooperbench_dir: Path, subset: str) -> list[Path]:
    """Find CooperBench task directories."""
    dataset_dir = cooperbench_dir / "dataset"
    if not dataset_dir.is_dir():
        print(f"ERROR: dataset dir not found: {dataset_dir}", file=sys.stderr)
        sys.exit(1)

    tasks = sorted(
        d
        for d in dataset_dir.iterdir()
        if d.is_dir() and (d / "Dockerfile").exists()
    )
    if not tasks:
        # Look one level deeper: dataset/{repo}_task/task{id}/
        tasks = sorted(
            t
            for repo_dir in dataset_dir.iterdir()
            if repo_dir.is_dir()
            for t in repo_dir.iterdir()
            if t.is_dir() and (t / "Dockerfile").exists()
        )

    if subset == "lite":
        tasks = tasks[:3]

    print(f"Discovered {len(tasks)} tasks ({subset})")
    return tasks


def run_experiment(
    tasks: list[Path],
    cooperbench_dir: Path,
    model: str,
    output_dir: Path,
) -> dict:
    """Run both channels and collect results.

    This is a skeleton — the actual CooperBench runner integration requires
    the cooperbench package to be importable and the agent adapters to be
    registered.
    """
    results: dict = {
        "model": model,
        "task_count": len(tasks),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "channels": {},
    }

    for channel_name in ["redis", "watercooler"]:
        channel_results = []
        print(f"\n{'='*60}")
        print(f"Channel: {channel_name}")
        print(f"{'='*60}")

        for task_dir in tasks:
            task_name = task_dir.name
            print(f"  Task: {task_name} ... ", end="", flush=True)
            t0 = time.monotonic()

            # Placeholder — real implementation will:
            # 1. Import cooperbench.coop.execute_coop
            # 2. Monkey-patch MessagingConnector if channel == "watercooler"
            # 3. Run the task with the specified model
            # 4. Collect AgentResult from each agent
            task_result = {
                "task": task_name,
                "channel": channel_name,
                "status": "skipped",
                "reason": "experiment runner not yet wired to CooperBench",
                "duration_s": round(time.monotonic() - t0, 2),
            }
            channel_results.append(task_result)
            print(f"{task_result['status']} ({task_result['duration_s']}s)")

        results["channels"][channel_name] = channel_results

    results["finished_at"] = datetime.now(timezone.utc).isoformat()
    return results


def main() -> None:
    args = parse_args()

    if not args.cooperbench_dir.is_dir():
        print(
            f"ERROR: CooperBench not found at {args.cooperbench_dir}",
            file=sys.stderr,
        )
        sys.exit(1)

    output_dir = args.output_dir or Path("logs") / datetime.now(
        timezone.utc
    ).strftime("%Y%m%d-%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)

    tasks = discover_tasks(args.cooperbench_dir, args.subset)
    if not tasks:
        print("ERROR: No tasks found", file=sys.stderr)
        sys.exit(1)

    results = run_experiment(tasks, args.cooperbench_dir, args.model, output_dir)

    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(results, indent=2))
    print(f"\nResults written to {summary_path}")


if __name__ == "__main__":
    main()
