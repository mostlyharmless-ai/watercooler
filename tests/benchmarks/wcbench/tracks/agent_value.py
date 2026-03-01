"""Agent value benchmark track.

Answers one question: does an agent with watercooler tools succeed at tasks
that an agent without them fails?

For each task we run two paired agent sessions in separate Docker containers:
  1. **baseline** -- agent has bash + file editor only
  2. **tools**    -- same agent + WcToolSession for watercooler search/read

Both agents get the same problem statement and workspace.  After each run
we execute ``test_cmd`` in the container to get a deterministic pass/fail.
Results go to ``COMPARISON.md`` and ``pair_results.json``.

Uses the proven RunAgentBackend (litellm agent loop + Docker exec) -- same
infrastructure as the custom and swebench tracks.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import docker

from tests.benchmarks.wcbench.config import RunConfig
from tests.benchmarks.wcbench.events import EventLogger
from tests.benchmarks.wcbench.run_layout import RunLayout
from tests.benchmarks.wcbench.summary import RunSummary, TaskSummary

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TASKS_PATH = Path("tests/benchmarks/agent_value/tasks.json")
_WORKDIR = "/workspace"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class _PairResult:
  """Result of a single baseline+tools paired run."""

  task_id: str
  category: str
  baseline_ok: bool
  tools_ok: bool
  baseline_steps: int = 0
  tools_steps: int = 0
  baseline_cost: float = 0.0
  tools_cost: float = 0.0
  wc_calls: int = 0
  wc_tools_used: dict[str, int] = field(default_factory=dict)
  test_cmd: str = ""
  baseline_test_output: str = ""
  tools_test_output: str = ""


# ---------------------------------------------------------------------------
# Thread seeding (same pattern as custom track)
# ---------------------------------------------------------------------------


def _seed_threads(
  threads_dir: Path,
  task: dict[str, Any],
  *,
  event_logger: EventLogger,
  run_id: str,
  task_id: str,
) -> list[str]:
  """Seed watercooler threads from task definition.

  Returns:
    List of topic slugs that were seeded.
  """
  from ulid import ULID
  from watercooler.commands_graph import say

  threads_dir.mkdir(parents=True, exist_ok=True)
  topics: list[str] = []

  for thread_seed in task.get("seed_threads", []):
    topic = thread_seed["thread_id"]
    topics.append(topic)
    for e in thread_seed.get("entries", []):
      entry_id = str(e.get("entry_id") or ULID())
      say(
        topic,
        threads_dir=threads_dir,
        agent="WCBenchAgentValue (system)",
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

  return topics


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------


def _start_container(
  client: docker.DockerClient,
  image_tag: str,
) -> docker.models.containers.Container:
  """Start a detached container with ``sleep infinity``."""
  return client.containers.run(
    image_tag,
    command="sleep infinity",
    detach=True,
    remove=False,
  )


def _setup_workspace(
  container: docker.models.containers.Container,
  workdir: str,
  project_context: str,
) -> None:
  """Create workspace dir with a README giving project context."""
  from tests.benchmarks.scripts.run_swebench import exec_in_container

  exec_in_container(container, f"mkdir -p {workdir}", workdir="/")
  # Write project context as README so the agent has domain context
  readme_content = project_context.replace("'", "'\\''")
  exec_in_container(
    container,
    f"printf '%s\\n' '{readme_content}' > {workdir}/README.md",
    workdir=workdir,
  )


def _init_git(
  container: docker.models.containers.Container,
  workdir: str,
) -> None:
  """Initialise a git repo in the workspace."""
  from tests.benchmarks.scripts.run_swebench import exec_in_container

  exec_in_container(container, "git init", workdir=workdir)
  exec_in_container(container, "git config user.email bench@example.com", workdir=workdir)
  exec_in_container(container, "git config user.name Bench", workdir=workdir)
  exec_in_container(container, "git add -A", workdir=workdir)
  exec_in_container(container, "git commit -m 'base' --no-gpg-sign", workdir=workdir)


def _destroy_container(container: docker.models.containers.Container) -> None:
  try:
    container.stop(timeout=5)
  except Exception:
    pass
  try:
    container.remove(force=True)
  except Exception:
    pass


# ---------------------------------------------------------------------------
# Single agent run (baseline or tools)
# ---------------------------------------------------------------------------


def _run_one(
  *,
  client: docker.DockerClient,
  image_tag: str,
  project_context: str,
  problem_statement: str,
  test_cmd: str,
  cfg: RunConfig,
  mode: str,
  task_id: str,
  category: str,
  threads_dir: Path,
  default_topic: str,
  event_logger: EventLogger,
) -> tuple[TaskSummary, int, str]:
  """Run one agent (baseline or tools) in a fresh Docker container.

  Returns:
    (TaskSummary, test_exit_code, test_output)
  """
  from tests.benchmarks.scripts.run_swebench import exec_in_container
  from tests.benchmarks.wcbench.agent_backend import RunAgentBackend

  tagged_task_id = f"{task_id}:{mode}"

  container = _start_container(client, image_tag)
  try:
    _setup_workspace(container, _WORKDIR, project_context)
    _init_git(container, _WORKDIR)

    event_logger.emit(
      "task_start",
      run_id=cfg.run_id,
      task_id=tagged_task_id,
      payload={
        "title": f"{task_id} ({mode})",
        "mode": mode,
        "category": category,
      },
    )

    # Build WC tools session for tools mode, None for baseline
    wc_session = None
    if mode == "tools":
      from tests.benchmarks.scripts.wc_text_tools import WcToolSession
      from tests.benchmarks.wcbench.wc_tools import WcToolAdapter

      base_session = WcToolSession(
        threads_dir=threads_dir,
        default_topic=default_topic,
        code_path=cfg.wc_code_path,
        tier_ceiling=cfg.wc_tier_ceiling,
        max_calls=cfg.wc_max_calls,
        token_budget=cfg.wc_token_budget,
      )
      wc_session = WcToolAdapter(
        session=base_session,
        event_logger=event_logger,
        run_id=cfg.run_id,
        task_id=tagged_task_id,
      )

    backend = RunAgentBackend(container=container)
    agent_run = backend.run(
      problem_statement=problem_statement,
      model_name=cfg.model,
      max_steps=cfg.max_steps,
      cost_limit=cfg.cost_limit,
      knowledge_context="",
      wc_session=wc_session,
      wc_guidance_text="",
      workdir=_WORKDIR,
    )

    # Run test command
    test_exit, test_output = exec_in_container(
      container, test_cmd, workdir=_WORKDIR,
    )
    ok = test_exit == 0

    summary = TaskSummary(
      task_id=tagged_task_id,
      ok=ok,
      mode=mode,
      cost=float(agent_run.total_cost),
      steps=int(agent_run.steps),
      wc_commands=int(
        agent_run.raw.get("wc_commands", agent_run.metrics.get("wc_commands", 0)) or 0
      ),
      wc_tools_used=dict(
        agent_run.raw.get("wc_tools_used", agent_run.metrics.get("wc_tools_used", {})) or {}
      ),
      wc_entry_ids_returned=list(
        agent_run.raw.get("wc_entry_ids_returned", agent_run.metrics.get("wc_entry_ids_returned", [])) or []
      ),
      bash_commands=int(
        agent_run.raw.get("bash_commands", agent_run.metrics.get("bash_commands", 0)) or 0
      ),
      test_runs=1,
      category=category,
      details={
        "test_cmd": test_cmd,
        "test_output": test_output[:4000],
        "paired_with": f"{task_id}:{'baseline' if mode == 'tools' else 'tools'}",
      },
    )

    event_logger.emit(
      "test_result",
      run_id=cfg.run_id,
      task_id=tagged_task_id,
      payload={
        "command": test_cmd,
        "exit_code": test_exit,
        "passed": ok,
        "output": test_output[:4000],
      },
    )
    event_logger.emit(
      "task_end",
      run_id=cfg.run_id,
      task_id=tagged_task_id,
      payload={
        "ok": ok,
        "mode": mode,
        "cost": summary.cost,
        "steps": summary.steps,
        "wc_commands": summary.wc_commands,
        "wc_tools_used": summary.wc_tools_used,
      },
    )

    return summary, test_exit, test_output

  finally:
    _destroy_container(container)


# ---------------------------------------------------------------------------
# Comparison report
# ---------------------------------------------------------------------------


def _write_comparison_report(
  report_path: Path,
  results: list[_PairResult],
  *,
  run_id: str,
  model: str,
) -> None:
  """Write COMPARISON.md summarizing baseline vs tools delta."""
  n = len(results) or 1  # avoid /0
  baseline_pass = sum(1 for r in results if r.baseline_ok)
  tools_pass = sum(1 for r in results if r.tools_ok)
  tools_only = sum(1 for r in results if not r.baseline_ok and r.tools_ok)
  baseline_only = sum(1 for r in results if r.baseline_ok and not r.tools_ok)
  both_pass = sum(1 for r in results if r.baseline_ok and r.tools_ok)
  neither = sum(1 for r in results if not r.baseline_ok and not r.tools_ok)

  lines: list[str] = [
    "# Agent Value Benchmark -- COMPARISON",
    "",
    f"- **run_id**: `{run_id}`",
    f"- **model**: `{model}`",
    f"- **tasks**: {len(results)}",
    "",
    "## Summary",
    "",
    "| Metric | Value |",
    "|--------|-------|",
    f"| Baseline pass rate | {baseline_pass}/{len(results)} ({100*baseline_pass//n}%) |",
    f"| Tools pass rate | {tools_pass}/{len(results)} ({100*tools_pass//n}%) |",
    f"| Both pass | {both_pass} |",
    f"| **Tools-only wins** | **{tools_only}** |",
    f"| Baseline-only wins | {baseline_only} |",
    f"| Neither pass | {neither} |",
    f"| Absolute delta | {'+' if tools_pass >= baseline_pass else ''}{tools_pass - baseline_pass} tasks |",
    "",
  ]

  # Per-category
  categories: dict[str, list[_PairResult]] = {}
  for r in results:
    categories.setdefault(r.category, []).append(r)

  if categories:
    lines += [
      "## Per-category breakdown",
      "",
      "| Category | Baseline | Tools | Delta |",
      "|----------|----------|-------|-------|",
    ]
    for cat in sorted(categories):
      cr = categories[cat]
      b = sum(1 for r in cr if r.baseline_ok)
      t = sum(1 for r in cr if r.tools_ok)
      cn = len(cr)
      lines.append(f"| {cat} | {b}/{cn} | {t}/{cn} | {'+' if t-b >= 0 else ''}{t-b} |")
    lines.append("")

  # Per-task
  lines += [
    "## Per-task results",
    "",
    "| Task | Category | Baseline | Tools | WC calls | Verdict |",
    "|------|----------|----------|-------|----------|---------|",
  ]
  for r in results:
    verdict = (
      "BOTH" if r.baseline_ok and r.tools_ok else
      "TOOLS-WIN" if not r.baseline_ok and r.tools_ok else
      "BASE-WIN" if r.baseline_ok and not r.tools_ok else
      "NEITHER"
    )
    lines.append(
      f"| {r.task_id} | {r.category} "
      f"| {'PASS' if r.baseline_ok else 'FAIL'} "
      f"| {'PASS' if r.tools_ok else 'FAIL'} "
      f"| {r.wc_calls} | {verdict} |"
    )
  lines.append("")

  report_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Main track runner
# ---------------------------------------------------------------------------


def run_agent_value_track(
  cfg: RunConfig,
  *,
  layout: RunLayout,
  event_logger: EventLogger,
  run_summary: RunSummary,
) -> None:
  """Run the agent value benchmark: paired baseline vs tools runs in Docker."""
  tasks_path = cfg.agent_value_tasks_path or DEFAULT_TASKS_PATH
  if not tasks_path.exists():
    raise FileNotFoundError(f"Agent value tasks file not found: {tasks_path}")

  tasks_cfg = json.loads(tasks_path.read_text(encoding="utf-8"))
  tasks = list(tasks_cfg.get("tasks", []))
  project_context = str(tasks_cfg.get("project_context", ""))

  if cfg.agent_value_only_task_ids:
    allow = set(cfg.agent_value_only_task_ids)
    tasks = [t for t in tasks if t.get("task_id") in allow]

  if not tasks:
    log.warning("No agent_value tasks to run")
    return

  # Load API keys from ~/.watercooler/credentials.toml
  try:
    from tests.benchmarks.scripts.run_swebench import setup_api_keys
    setup_api_keys()
  except Exception as exc:
    log.warning("API key setup failed: %s", exc)

  client = docker.from_env()
  image_tag = cfg.agent_value_image_tag
  pair_results: list[_PairResult] = []

  for task in tasks:
    task_id = task["task_id"]
    category = task.get("category", "")
    test_cmd = task.get("test_cmd", "true")
    problem_statement = task["problem_statement"]

    log.info("=== Agent value task: %s (%s) ===", task_id, category)

    # ---- Seed threads on host ----
    threads_dir = layout.artifacts_dir / "threads" / task_id
    topics = _seed_threads(
      threads_dir, task,
      event_logger=event_logger,
      run_id=cfg.run_id,
      task_id=task_id,
    )
    default_topic = topics[0] if topics else "agent-value"

    # ---- Baseline run (no WC tools) ----
    log.info("  [baseline] starting...")
    baseline_summary, _, baseline_output = _run_one(
      client=client,
      image_tag=image_tag,
      project_context=project_context,
      problem_statement=problem_statement,
      test_cmd=test_cmd,
      cfg=cfg,
      mode="baseline",
      task_id=task_id,
      category=category,
      threads_dir=threads_dir,
      default_topic=default_topic,
      event_logger=event_logger,
    )
    run_summary.tasks.append(baseline_summary)

    # ---- Tools run (with WC tools) ----
    log.info("  [tools] starting...")
    tools_summary, _, tools_output = _run_one(
      client=client,
      image_tag=image_tag,
      project_context=project_context,
      problem_statement=problem_statement,
      test_cmd=test_cmd,
      cfg=cfg,
      mode="tools",
      task_id=task_id,
      category=category,
      threads_dir=threads_dir,
      default_topic=default_topic,
      event_logger=event_logger,
    )
    run_summary.tasks.append(tools_summary)

    # ---- Record pair ----
    pair = _PairResult(
      task_id=task_id,
      category=category,
      baseline_ok=baseline_summary.ok,
      tools_ok=tools_summary.ok,
      baseline_steps=baseline_summary.steps,
      tools_steps=tools_summary.steps,
      baseline_cost=baseline_summary.cost,
      tools_cost=tools_summary.cost,
      wc_calls=tools_summary.wc_commands,
      wc_tools_used=dict(tools_summary.wc_tools_used),
      test_cmd=test_cmd,
      baseline_test_output=baseline_output[:2000],
      tools_test_output=tools_output[:2000],
    )
    pair_results.append(pair)

    log.info(
      "  Result: baseline=%s  tools=%s  wc_calls=%d",
      "PASS" if pair.baseline_ok else "FAIL",
      "PASS" if pair.tools_ok else "FAIL",
      pair.wc_calls,
    )

  # ---- Write COMPARISON.md ----
  comparison_path = layout.root / "COMPARISON.md"
  _write_comparison_report(
    comparison_path, pair_results,
    run_id=cfg.run_id, model=cfg.model,
  )
  log.info("COMPARISON.md -> %s", comparison_path)

  # ---- Write pair_results.json ----
  pairs_json_path = layout.root / "pair_results.json"
  pairs_json_path.write_text(
    json.dumps(
      [
        {
          "task_id": p.task_id,
          "category": p.category,
          "baseline_ok": p.baseline_ok,
          "tools_ok": p.tools_ok,
          "baseline_steps": p.baseline_steps,
          "tools_steps": p.tools_steps,
          "baseline_cost": p.baseline_cost,
          "tools_cost": p.tools_cost,
          "wc_calls": p.wc_calls,
          "wc_tools_used": p.wc_tools_used,
        }
        for p in pair_results
      ],
      indent=2,
    ),
    encoding="utf-8",
  )
  log.info("pair_results.json -> %s", pairs_json_path)
