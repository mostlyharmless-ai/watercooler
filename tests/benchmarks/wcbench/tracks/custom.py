from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import docker

from tests.benchmarks.wcbench.config import RunConfig
from tests.benchmarks.wcbench.events import EventLogger
from tests.benchmarks.wcbench.run_layout import RunLayout
from tests.benchmarks.wcbench.summary import TaskSummary, RunSummary

log = logging.getLogger(__name__)


def _read_text(path: Path) -> str:
  return path.read_text(encoding="utf-8")


def _seed_threads(threads_dir: Path, topic: str, entries: list[dict[str, Any]], *, event_logger: EventLogger, run_id: str, task_id: str) -> None:
  """Seed a minimal Watercooler thread using the core graph writer."""
  from ulid import ULID
  from watercooler.commands_graph import say

  threads_dir.mkdir(parents=True, exist_ok=True)
  for e in entries:
    entry_id = str(e.get("entry_id") or ULID())
    say(
      topic,
      threads_dir=threads_dir,
      agent="WCBenchCustom (system)",
      role=e.get("role", "planner"),
      title=e["title"],
      body=e["body"],
      entry_type=e.get("entry_type", "Note"),
      entry_id=entry_id,
    )
    event_logger.emit(
      "tool_result",
      run_id=run_id,
      task_id=task_id,
      payload={
        "tool": "watercooler.commands_graph.say",
        "topic": topic,
        "entry_id": entry_id,
        "title": e.get("title", ""),
      },
    )


def _build_image(client: docker.DockerClient, dockerfile_dir: Path, tag: str) -> None:
  log.info(f"Building custom benchmark image: {tag}")
  image, logs_iter = client.images.build(path=str(dockerfile_dir), tag=tag)
  for _ in logs_iter:
    pass
  _ = image


def _init_git(container: docker.models.containers.Container, workdir: str) -> None:
  from tests.benchmarks.scripts.run_swebench import exec_in_container

  exec_in_container(container, "git init", workdir=workdir)
  exec_in_container(container, "git config user.email bench@example.com", workdir=workdir)
  exec_in_container(container, "git config user.name Bench", workdir=workdir)
  exec_in_container(container, "git add -A", workdir=workdir)
  exec_in_container(container, "git commit -m 'base' --no-gpg-sign", workdir=workdir)


def _load_guidance(cfg: RunConfig) -> str:
  if cfg.mode != "tools_guided":
    return ""
  if cfg.wc_guidance_file is None:
    return ""
  if not cfg.wc_guidance_file.exists():
    return ""
  return _read_text(cfg.wc_guidance_file)


def run_custom_track(cfg: RunConfig, *, layout: RunLayout, event_logger: EventLogger, run_summary: RunSummary) -> None:
  """Run the existing custom tasks under the standardized wcbench run layout."""
  if cfg.custom_tasks_path is None:
    raise SystemExit("custom track requires RunConfig.custom_tasks_path")
  if cfg.custom_repo_dir is None:
    raise SystemExit("custom track requires RunConfig.custom_repo_dir")

  # Load API keys from ~/.watercooler/credentials.toml (shared helper)
  try:
    from tests.benchmarks.scripts.run_swebench import setup_api_keys
    setup_api_keys()
  except Exception:
    pass

  tasks_cfg = json.loads(_read_text(cfg.custom_tasks_path))
  dockerfile_dir = cfg.custom_tasks_path.parent.parent  # tests/benchmarks/custom

  client = docker.from_env()
  _build_image(client, dockerfile_dir, tag=cfg.custom_image_tag)

  guidance_text = _load_guidance(cfg)

  tasks = list(tasks_cfg.get("tasks", []))
  if cfg.custom_only_task_ids:
    allow = set(cfg.custom_only_task_ids)
    tasks = [t for t in tasks if t.get("task_id") in allow]

  for task in tasks:
    task_id = task["task_id"]
    workdir = task.get("workdir", tasks_cfg.get("workdir", "/repo"))
    topic = task["threads_seed_topic"]

    # Seed threads under the run layout
    threads_dir = layout.artifacts_dir / "threads" / task_id
    _seed_threads(
      threads_dir,
      topic,
      task.get("threads_seed_entries", []),
      event_logger=event_logger,
      run_id=cfg.run_id,
      task_id=task_id,
    )

    raw_phases = task.get("phases")
    has_phases = isinstance(raw_phases, list) and len(raw_phases) > 0
    phases = raw_phases if has_phases else [
        {
          "phase_id": "single",
          "max_steps": int(task.get("max_steps", cfg.max_steps) or cfg.max_steps),
          "cost_limit": float(task.get("cost_limit", cfg.cost_limit) or cfg.cost_limit),
          "reset_repo_after": False,
        }
      ]

    # Run agent inside Docker container
    from tests.benchmarks.scripts.run_swebench import exec_in_container
    from tests.benchmarks.wcbench.agent_backend import RunAgentBackend

    container = client.containers.run(
      cfg.custom_image_tag,
      command="sleep infinity",
      detach=True,
      remove=False,
    )

    try:
      _init_git(container, workdir=workdir)

      backend = RunAgentBackend(container=container)
      test_cmd = task.get("test_command", "pytest -q")
      for i, phase in enumerate(phases):
        phase_id = str(phase.get("phase_id") or f"phase{i+1}")
        phase_task_id = f"{task_id}:{phase_id}" if has_phases else task_id
        phase_max_steps = int(phase.get("max_steps", cfg.max_steps) or cfg.max_steps)
        phase_cost_limit = float(phase.get("cost_limit", cfg.cost_limit) or cfg.cost_limit)

        event_logger.emit(
          "task_start",
          run_id=cfg.run_id,
          task_id=phase_task_id,
          payload={
            "title": task.get("title", ""),
            "mode": cfg.mode,
            "workdir": workdir,
            "topic": topic,
            "parent_task_id": task_id,
            "phase_id": phase_id,
          },
        )

        # Tools session (host-side dispatcher)
        wc_session = None
        wc_guidance_text = ""
        if cfg.mode in ("tools", "tools_guided"):
          from tests.benchmarks.scripts.wc_text_tools import WcToolSession
          from tests.benchmarks.wcbench.wc_tools import WcToolAdapter

          base_session = WcToolSession(
            threads_dir=threads_dir,
            default_topic=topic,
            code_path=cfg.wc_code_path,
            tier_ceiling=cfg.wc_tier_ceiling,
            max_calls=cfg.wc_max_calls,
            token_budget=cfg.wc_token_budget,
            allow_write=bool(task.get("wc_allow_write", False)),
          )
          wc_session = WcToolAdapter(
            session=base_session,
            event_logger=event_logger,
            run_id=cfg.run_id,
            task_id=phase_task_id,
          )
          if cfg.mode == "tools_guided":
            wc_guidance_text = guidance_text

        phase_statement = (
          task["problem_statement"]
          + f"\n\n[PHASE={phase_id}] Follow only the instructions for this phase."
        )
        agent_run = backend.run(
          problem_statement=phase_statement,
          model_name=cfg.model,
          max_steps=phase_max_steps,
          cost_limit=phase_cost_limit,
          knowledge_context="",
          wc_session=wc_session,
          wc_guidance_text=wc_guidance_text,
          workdir=workdir,
        )

        is_last = i == (len(phases) - 1)
        test_exit = 0
        test_out = ""
        ok = True
        if is_last:
          test_exit, test_out = exec_in_container(container, test_cmd, workdir=workdir)
          ok = test_exit == 0

        task_summary = TaskSummary(
          task_id=phase_task_id,
          ok=ok,
          mode=cfg.mode,
          cost=float(agent_run.total_cost),
          steps=int(agent_run.steps),
          wc_commands=int(agent_run.raw.get("wc_commands", agent_run.metrics.get("wc_commands", 0)) or 0),
          wc_tools_used=dict(agent_run.raw.get("wc_tools_used", agent_run.metrics.get("wc_tools_used", {})) or {}),
          wc_entry_ids_returned=list(agent_run.raw.get("wc_entry_ids_returned", agent_run.metrics.get("wc_entry_ids_returned", [])) or []),
          bash_commands=int(agent_run.raw.get("bash_commands", agent_run.metrics.get("bash_commands", 0)) or 0),
          test_runs=int(agent_run.raw.get("test_runs", agent_run.metrics.get("test_runs", 0)) or 0),
          details={
            "parent_task_id": task_id,
            "phase_id": phase_id,
            "patch_chars": len(agent_run.model_patch or ""),
            "test_command": test_cmd if is_last else "",
            "test_output": test_out,
            "reset_repo_after": bool(phase.get("reset_repo_after", False)),
          },
        )
        run_summary.tasks.append(task_summary)

        if is_last:
          event_logger.emit(
            "test_result",
            run_id=cfg.run_id,
            task_id=phase_task_id,
            payload={
              "command": test_cmd,
              "exit_code": test_exit,
              "passed": ok,
              "output": test_out[:8000],
            },
          )

        event_logger.emit(
          "task_end",
          run_id=cfg.run_id,
          task_id=phase_task_id,
          payload={
            "ok": ok,
            "cost": task_summary.cost,
            "steps": task_summary.steps,
            "wc_commands": task_summary.wc_commands,
            "wc_tools_used": task_summary.wc_tools_used,
            "wc_entry_ids_returned": task_summary.wc_entry_ids_returned[:10],
          },
        )

        if bool(phase.get("reset_repo_after", False)):
          exec_in_container(container, "git reset --hard HEAD", workdir=workdir)
          exec_in_container(container, "git clean -fd", workdir=workdir)

    finally:
      try:
        container.stop(timeout=5)
      except Exception:
        pass
      try:
        container.remove(force=True)
      except Exception:
        pass

