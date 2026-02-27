#!/usr/bin/env python3
"""Minimal custom benchmark runner for Watercooler validation.

This harness is intentionally small: it runs a planted-bug repository in Docker,
and optionally enables prompt-only Watercooler usage via the `wc-*` text-command
interface (host-side dispatcher).

It exists to test Watercooler’s differentiators (decision constraints, durability,
multi-step recall) without SWE-bench’s constraints.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any

import docker

# Add project root + src/ for imports (src-layout package)
import sys

_repo_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_repo_root))
sys.path.insert(0, str(_repo_root / "src"))

log = logging.getLogger(__name__)


def _seed_threads(threads_dir: Path, topic: str, entries: list[dict[str, Any]]) -> None:
    """Seed a minimal Watercooler thread using the core graph writer."""
    from ulid import ULID
    from watercooler.commands_graph import say

    threads_dir.mkdir(parents=True, exist_ok=True)
    for e in entries:
        say(
            topic,
            threads_dir=threads_dir,
            agent="CustomBench (system)",
            role=e.get("role", "planner"),
            title=e["title"],
            body=e["body"],
            entry_type=e.get("entry_type", "Note"),
            entry_id=str(ULID()),
        )


def _build_image(client: docker.DockerClient, dockerfile_dir: Path, tag: str) -> None:
    log.info(f"Building custom benchmark image: {tag}")
    image, logs_iter = client.images.build(path=str(dockerfile_dir), tag=tag)
    # Drain logs to avoid silent failures (but don’t spam).
    for _ in logs_iter:
        pass
    _ = image


def _init_git(container: docker.models.containers.Container, workdir: str) -> None:
    from tests.benchmarks.scripts.run_swebench import exec_in_container

    # Ensure repo is diffable without requiring image-time git setup.
    exec_in_container(container, "git init", workdir=workdir)
    exec_in_container(container, "git config user.email bench@example.com", workdir=workdir)
    exec_in_container(container, "git config user.name Bench", workdir=workdir)
    exec_in_container(container, "git add -A", workdir=workdir)
    exec_in_container(container, "git commit -m 'base' --no-gpg-sign", workdir=workdir)


def run_task(
    client: docker.DockerClient,
    image_tag: str,
    task: dict[str, Any],
    model_name: str,
    max_steps: int,
    cost_limit: float,
    mode: str,
    output_dir: Path,
    guidance_text: str,
) -> dict[str, Any]:
    from tests.benchmarks.scripts.run_swebench import exec_in_container, run_agent
    from tests.benchmarks.scripts.wc_text_tools import WcToolSession

    task_id = task["task_id"]
    workdir = task.get("workdir", "/repo")

    threads_dir = output_dir / "threads" / task_id
    topic = task["threads_seed_topic"]
    _seed_threads(threads_dir, topic, task.get("threads_seed_entries", []))

    wc_session = None
    wc_guidance_text = ""
    if mode in ("tools", "tools_guided"):
        wc_session = WcToolSession(
            threads_dir=threads_dir,
            default_topic=topic,
            tier_ceiling="T1",
            max_calls=5,
            token_budget=900,
        )
        if mode == "tools_guided":
            wc_guidance_text = guidance_text

    container = client.containers.run(
        image_tag,
        command="sleep infinity",
        detach=True,
        remove=False,
    )

    try:
        _init_git(container, workdir=workdir)
        agent_result = run_agent(
            container=container,
            problem_statement=task["problem_statement"],
            model_name=model_name,
            max_steps=max_steps,
            cost_limit=cost_limit,
            knowledge_context="",
            wc_session=wc_session,
            wc_guidance_text=wc_guidance_text,
            workdir=workdir,
        )

        test_cmd = task.get("test_command", "pytest -q")
        test_exit, test_out = exec_in_container(container, test_cmd, workdir=workdir)

        return {
            "task_id": task_id,
            "mode": mode,
            "model": model_name,
            "steps": agent_result.get("steps", 0),
            "cost": agent_result.get("total_cost", 0.0),
            "patch_chars": len(agent_result.get("patch", "")),
            "metrics": {
                "bash_commands": agent_result.get("bash_commands", 0),
                "wc_commands": agent_result.get("wc_commands", 0),
                "wc_tools_used": agent_result.get("wc_tools_used", {}),
                "wc_entry_ids_returned": agent_result.get("wc_entry_ids_returned", []),
                "test_runs": agent_result.get("test_runs", 0),
            },
            "tests": {
                "exit_code": test_exit,
                "passed": test_exit == 0,
                "output": test_out,
            },
        }
    finally:
        container.stop(timeout=5)
        container.remove(force=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Run custom Watercooler benchmark tasks")
    parser.add_argument("--model", default="minimax/MiniMax-M2.5", help="LiteLLM model string")
    parser.add_argument("--mode", default="tools_guided", choices=["baseline", "tools", "tools_guided"])
    parser.add_argument("--max-steps", type=int, default=25)
    parser.add_argument("--cost-limit", type=float, default=0.50)
    parser.add_argument("--tasks", type=str, default=str(Path(__file__).parent / "tasks" / "tasks.json"))
    parser.add_argument("--output-dir", type=str, default="logs/custom-bench")
    parser.add_argument("--guidance-file", type=str, default=str(Path(__file__).resolve().parents[1] / "guidance" / "watercooler_usage.md"))
    args = parser.parse_args()

    # Load API keys from ~/.watercooler/credentials.toml (shared helper)
    try:
        from tests.benchmarks.scripts.run_swebench import setup_api_keys
        setup_api_keys()
    except Exception as e:
        log.warning(f"Failed to load API keys: {e}")

    tasks_path = Path(args.tasks)
    cfg = json.loads(tasks_path.read_text(encoding="utf-8"))
    image_tag = cfg.get("image_tag", "watercooler-custom-bench:latest")
    dockerfile_dir = Path(__file__).parent

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    guidance_text = ""
    gp = Path(args.guidance_file)
    if gp.exists():
        guidance_text = gp.read_text(encoding="utf-8")

    client = docker.from_env()
    _build_image(client, dockerfile_dir, tag=image_tag)

    results: list[dict[str, Any]] = []
    start = time.time()
    for task in cfg["tasks"]:
        results.append(
            run_task(
                client=client,
                image_tag=image_tag,
                task=task,
                model_name=args.model,
                max_steps=args.max_steps,
                cost_limit=args.cost_limit,
                mode=args.mode,
                output_dir=output_dir,
                guidance_text=guidance_text,
            )
        )

    summary = {
        "image_tag": image_tag,
        "mode": args.mode,
        "model": args.model,
        "tasks": len(results),
        "passed": sum(1 for r in results if r["tests"]["passed"]),
        "elapsed_seconds": time.time() - start,
        "results": results,
    }

    out_path = output_dir / "summary.json"
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log.info(f"Custom benchmark complete. Summary: {out_path}")


if __name__ == "__main__":
    main()

