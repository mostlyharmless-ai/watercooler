#!/usr/bin/env python3
"""Run CooperBench A/B experiment: Redis vs Watercooler channel.

Runs CooperBench cooperative SWE tasks with the watercooler messaging adapter
swapped in for the default Redis-based MessagingConnector.  Optionally runs
the same tasks with Redis first as a baseline for A/B comparison.

Prerequisites:
  - CooperBench installed: ``cd CooperBench && uv sync``
  - Docker running (for sandbox evaluation)
  - LLM API key: ANTHROPIC_API_KEY or OPENAI_API_KEY
  - Redis on a reachable port (for baseline runs only)

Usage::

    # Single task, watercooler channel only (fastest validation)
    python tests/benchmarks/scripts/run_cooperbench_experiment.py \
        --repo samuelcolvin_dirty_equals_task --task 43 --features 2,3 \
        --channel watercooler --model openai/gpt-4o \
        --backend docker

    # A/B comparison: Redis baseline then watercooler
    python tests/benchmarks/scripts/run_cooperbench_experiment.py \
        --repo samuelcolvin_dirty_equals_task --task 43 --features 2,3 \
        --channel both --model openai/gpt-4o \
        --backend docker --redis-url redis://localhost:6380

    # Flash subset (50 pairs across 11 repos)
    python tests/benchmarks/scripts/run_cooperbench_experiment.py \
        --subset flash --model openai/gpt-4o --backend docker

Output: ``logs/{run_name}/summary.json``
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


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

    # Task selection: either --subset OR --repo/--task/--features
    task_group = p.add_argument_group("task selection")
    task_group.add_argument(
        "--subset",
        choices=["flash", "lite"],
        default=None,
        help="Run a predefined subset (flash=50 pairs, lite=100 pairs)",
    )
    task_group.add_argument("--repo", type=str, help="Single repo name (e.g., samuelcolvin_dirty_equals_task)")
    task_group.add_argument("--task", type=int, help="Task ID within repo")
    task_group.add_argument("--features", type=str, help="Comma-separated feature IDs (e.g., 2,3)")

    p.add_argument("--model", default="openai/gpt-4o", help="LLM model (litellm format)")
    p.add_argument("--agent", default="mini_swe_agent", help="Agent framework name")
    p.add_argument("--backend", default="docker", choices=["docker", "modal", "gcp"])
    p.add_argument("--channel", default="watercooler", choices=["redis", "watercooler", "both"])
    p.add_argument("--redis-url", default="redis://localhost:6380", help="Redis URL (for baseline)")
    p.add_argument("--threads-dir", type=Path, default=None, help="Watercooler threads dir (default: tmpdir)")
    p.add_argument("--output-dir", type=Path, default=None)
    p.add_argument("--force", action="store_true", help="Re-run even if results exist")
    return p.parse_args()


def load_subset(cooperbench_dir: Path, subset_name: str) -> list[dict]:
    """Load task pairs from a subset JSON file."""
    subset_file = cooperbench_dir / "dataset" / "subsets" / f"{subset_name}.json"
    if not subset_file.exists():
        print(f"ERROR: subset file not found: {subset_file}", file=sys.stderr)
        sys.exit(1)
    with open(subset_file) as f:
        data = json.load(f)
    # Flatten to list of (repo, task_id, features) tuples
    tasks = []
    for entry in data["tasks"]:
        for pair in entry["pairs"]:
            tasks.append({
                "repo": entry["repo"],
                "task_id": entry["task_id"],
                "features": pair,
            })
    return tasks


def _import_watercooler_adapter():
    """Import WatercoolerMessagingConnector, handling sys.path."""
    # Must be imported before CWD changes to CooperBench
    from tests.benchmarks.adapters.cooperbench_adapter import (
        WatercoolerMessagingConnector,
    )
    return WatercoolerMessagingConnector


# Import eagerly so it's available after CWD changes
_WatercoolerMessagingConnector = None


def make_watercooler_connector_class(threads_dir: Path):
    """Create a MessagingConnector-compatible class backed by watercooler.

    Returns a class whose __init__ signature matches CooperBench's
    ``MessagingConnector(agent_id, agents, url)`` so it can be swapped in
    via monkey-patch without changing CooperBench code.
    """
    global _WatercoolerMessagingConnector
    if _WatercoolerMessagingConnector is None:
        _WatercoolerMessagingConnector = _import_watercooler_adapter()
    WatercoolerMessagingConnector = _WatercoolerMessagingConnector

    class _WatercoolerBridge:
        """Bridges CooperBench's (agent_id, agents, url) interface to
        WatercoolerMessagingConnector(agent_id, agents, threads_dir)."""

        def __init__(self, agent_id: str, agents: list[str], url: str = ""):
            # Extract run namespace from URL fragment for topic isolation
            topic = "cooperbench-collab"
            if "#" in url:
                _, ns = url.split("#", 1)
                topic = f"cooperbench-{ns.replace(':', '-')}"

            self._inner = WatercoolerMessagingConnector(
                agent_id=agent_id,
                agents=agents,
                threads_dir=threads_dir,
                topic=topic,
            )

        def setup(self, env=None):
            return self._inner.setup(env)

        def send(self, recipient: str, content: str) -> None:
            return self._inner.send(recipient, content)

        def receive(self) -> list[dict]:
            return self._inner.receive()

        def broadcast(self, content: str) -> None:
            return self._inner.broadcast(content)

        def peek(self) -> int:
            return self._inner.peek()

    return _WatercoolerBridge


def run_single_task(
    repo: str,
    task_id: int,
    features: list[int],
    model: str,
    agent_name: str,
    backend: str,
    channel: str,
    redis_url: str,
    threads_dir: Path,
    run_name: str,
    force: bool,
    cooperbench_dir: Path,
) -> dict:
    """Run a single CooperBench task with the specified channel."""
    from cooperbench.runner.coop import execute_coop

    original_cwd = os.getcwd()
    os.chdir(cooperbench_dir)

    try:
        if channel == "watercooler":
            # Monkey-patch: replace MessagingConnector in the adapter module
            bridge_cls = make_watercooler_connector_class(threads_dir)
            with patch(
                "cooperbench.agents.mini_swe_agent.connectors.messaging.MessagingConnector",
                bridge_cls,
            ), patch(
                "cooperbench.agents.mini_swe_agent.adapter.MessagingConnector",
                bridge_cls,
            ):
                result = execute_coop(
                    repo_name=repo,
                    task_id=task_id,
                    features=features,
                    run_name=run_name,
                    agent_name=agent_name,
                    model_name=model,
                    redis_url=redis_url,  # still passed but intercepted by bridge
                    force=force,
                    backend=backend,
                    messaging_enabled=True,
                )
        else:
            # Redis baseline — use CooperBench as-is
            result = execute_coop(
                repo_name=repo,
                task_id=task_id,
                features=features,
                run_name=run_name,
                agent_name=agent_name,
                model_name=model,
                redis_url=redis_url,
                force=force,
                backend=backend,
                messaging_enabled=True,
            )
        return result or {}
    except Exception as e:
        return {"error": str(e), "status": "Error"}
    finally:
        os.chdir(original_cwd)


def run_experiment(
    tasks: list[dict],
    channels: list[str],
    model: str,
    agent_name: str,
    backend: str,
    redis_url: str,
    threads_dir: Path,
    output_dir: Path,
    force: bool,
    cooperbench_dir: Path,
) -> dict:
    """Run task pairs across specified channels and collect results."""
    experiment = {
        "model": model,
        "agent": agent_name,
        "backend": backend,
        "task_count": len(tasks),
        "channels": channels,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "results": [],
    }

    for channel in channels:
        print(f"\n{'=' * 60}")
        print(f"Channel: {channel}")
        print(f"{'=' * 60}")

        run_name = f"{channel}-{agent_name}-{model.split('/')[-1]}"

        for i, task in enumerate(tasks):
            repo = task["repo"]
            task_id = task["task_id"]
            features = task["features"]
            label = f"{repo}/task{task_id}/f{'_f'.join(str(f) for f in features)}"
            print(f"  [{i + 1}/{len(tasks)}] {label} ... ", end="", flush=True)

            t0 = time.monotonic()
            result = run_single_task(
                repo=repo,
                task_id=task_id,
                features=features,
                model=model,
                agent_name=agent_name,
                backend=backend,
                channel=channel,
                redis_url=redis_url,
                threads_dir=threads_dir,
                run_name=run_name,
                force=force,
                cooperbench_dir=cooperbench_dir,
            )
            duration = round(time.monotonic() - t0, 1)

            # Extract key info
            agents_info = result.get("results", {})
            statuses = {
                aid: adata.get("status", "Unknown")
                for aid, adata in agents_info.items()
            } if isinstance(agents_info, dict) else {}

            entry = {
                "channel": channel,
                "repo": repo,
                "task_id": task_id,
                "features": features,
                "duration_s": duration,
                "total_cost": result.get("total_cost", 0),
                "total_steps": result.get("total_steps", 0),
                "agent_statuses": statuses,
                "error": result.get("error"),
                "log_dir": result.get("log_dir"),
            }
            experiment["results"].append(entry)

            status_str = ", ".join(f"{k}={v}" for k, v in statuses.items()) or result.get("error", "???")
            print(f"{status_str} ({duration}s)")

    experiment["finished_at"] = datetime.now(timezone.utc).isoformat()

    # Summary stats
    for channel in channels:
        ch_results = [r for r in experiment["results"] if r["channel"] == channel]
        submitted = sum(
            1 for r in ch_results
            if r["agent_statuses"]
            and all(s == "Submitted" for s in r["agent_statuses"].values())
        )
        experiment[f"{channel}_submitted"] = submitted
        experiment[f"{channel}_total"] = len(ch_results)

    return experiment


def setup_api_keys():
    """Load API keys from watercooler credentials if not in env."""
    creds_path = Path.home() / ".watercooler" / "credentials.toml"
    if not creds_path.exists():
        return

    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    with open(creds_path, "rb") as f:
        creds = tomllib.load(f)

    mapping = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "groq": "GROQ_API_KEY",
        "minimax": "MINIMAX_API_KEY",
    }
    for section, env_var in mapping.items():
        if env_var not in os.environ and section in creds:
            key = creds[section].get("api_key")
            if key and not key.startswith("#"):
                os.environ[env_var] = key


def main() -> None:
    args = parse_args()

    if not args.cooperbench_dir.is_dir():
        print(f"ERROR: CooperBench not found at {args.cooperbench_dir}", file=sys.stderr)
        sys.exit(1)

    # Ensure CooperBench is importable
    cb_src = args.cooperbench_dir / "src"
    if str(cb_src) not in sys.path:
        sys.path.insert(0, str(cb_src))

    # Also ensure watercooler-cloud is importable
    wc_root = Path(__file__).resolve().parents[3]
    wc_src = wc_root / "src"
    if str(wc_src) not in sys.path:
        sys.path.insert(0, str(wc_src))
    if str(wc_root) not in sys.path:
        sys.path.insert(0, str(wc_root))

    # Load API keys from credentials
    setup_api_keys()

    # Import watercooler adapter now (before CWD changes to CooperBench)
    if args.channel in ("watercooler", "both"):
        global _WatercoolerMessagingConnector
        _WatercoolerMessagingConnector = _import_watercooler_adapter()

    # Determine tasks
    if args.subset:
        tasks = load_subset(args.cooperbench_dir, args.subset)
        print(f"Loaded {len(tasks)} task pairs from {args.subset} subset")
    elif args.repo and args.task is not None and args.features:
        features = [int(f) for f in args.features.split(",")]
        tasks = [{"repo": args.repo, "task_id": args.task, "features": features}]
    else:
        print("ERROR: specify --subset OR --repo/--task/--features", file=sys.stderr)
        sys.exit(1)

    # Determine channels
    if args.channel == "both":
        channels = ["redis", "watercooler"]
    else:
        channels = [args.channel]

    output_dir = args.output_dir or Path("logs") / datetime.now(timezone.utc).strftime(
        "%Y%m%d-%H%M%S"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    # Threads dir for watercooler channel — persistent by default
    if args.threads_dir:
        threads_dir = args.threads_dir
    else:
        threads_dir = output_dir / "watercooler-threads"
    threads_dir.mkdir(parents=True, exist_ok=True)

    print(f"Model: {args.model}")
    print(f"Agent: {args.agent}")
    print(f"Backend: {args.backend}")
    print(f"Channels: {channels}")
    print(f"Tasks: {len(tasks)}")
    if "watercooler" in channels:
        print(f"Threads dir: {threads_dir}")
    print()

    results = run_experiment(
        tasks=tasks,
        channels=channels,
        model=args.model,
        agent_name=args.agent,
        backend=args.backend,
        redis_url=args.redis_url,
        threads_dir=threads_dir,
        output_dir=output_dir,
        force=args.force,
        cooperbench_dir=args.cooperbench_dir,
    )

    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(results, indent=2))
    print(f"\nResults written to {summary_path}")

    # Print summary
    for ch in channels:
        sub = results.get(f"{ch}_submitted", 0)
        tot = results.get(f"{ch}_total", 0)
        print(f"  {ch}: {sub}/{tot} submitted")


if __name__ == "__main__":
    main()
