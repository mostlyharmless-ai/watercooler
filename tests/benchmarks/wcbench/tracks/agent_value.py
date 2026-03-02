"""Agent value benchmark track.

Answers one question: does an agent with watercooler tools succeed at tasks
that an agent without them fails?

For each task we run two paired agent sessions in **separate Docker containers**:
  1. **baseline** -- agent has bash + file editor only; no thread data mounted
  2. **tools**    -- same agent + watercooler MCP server; thread graph mounted ro

Both containers get the same problem statement and workspace.  After the agent
finishes we exec ``test_cmd`` *inside the same container* to get a deterministic
pass/fail.  Results go to ``COMPARISON.md`` and ``pair_results.json``.

Container isolation guarantees the baseline agent has zero access to thread
data — ``find / -name '*.jsonl'`` will find nothing watercooler-related.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.benchmarks.wcbench.config import RunConfig
from tests.benchmarks.wcbench.events import EventLogger
from tests.benchmarks.wcbench.run_layout import RunLayout
from tests.benchmarks.wcbench.summary import RunSummary, TaskSummary

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TASKS_PATH = Path("tests/benchmarks/agent_value/tasks.json")

IMAGE_NAME = "wcbench-agent-value:latest"
CONTAINER_RUNNER_PATH = "/app/tests/benchmarks/agent_value/container_runner.py"

# API key env vars to forward into agent containers.
_API_KEY_VARS = (
  "ANTHROPIC_API_KEY",
  "OPENAI_API_KEY",
  "DEEPSEEK_API_KEY",
  "GROQ_API_KEY",
  "MINIMAX_API_KEY",
)


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
# Docker container helpers
# ---------------------------------------------------------------------------


def _collect_api_env() -> dict[str, str]:
  """Collect API key env vars that are set in the current process."""
  env: dict[str, str] = {}
  for var in _API_KEY_VARS:
    val = os.environ.get(var)
    if val:
      env[var] = val
  return env


def _exec_in_container(
  container: Any,
  cmd: str,
  *,
  workdir: str = "/app",
  timeout: int = 300,
) -> tuple[int, str]:
  """Execute a command in a running container. Returns (exit_code, output)."""
  try:
    result = container.exec_run(
      ["bash", "-c", cmd],
      workdir=workdir,
      demux=True,
    )
    stdout = result.output[0].decode("utf-8", errors="replace") if result.output[0] else ""
    stderr = result.output[1].decode("utf-8", errors="replace") if result.output[1] else ""
    output = stdout
    if stderr:
      output += "\n" + stderr
    # Truncate very long output
    if len(output) > 30000:
      output = (
        output[:10000]
        + f"\n... ({len(output) - 20000} chars truncated) ...\n"
        + output[-10000:]
      )
    return result.exit_code, output
  except Exception as e:
    return -1, f"exec_in_container error: {e}"


# ---------------------------------------------------------------------------
# Single containerized run
# ---------------------------------------------------------------------------


def _run_one_in_container(
  *,
  problem_statement: str,
  test_cmd: str,
  cfg: RunConfig,
  mode: str,
  task_id: str,
  category: str,
  host_threads_dir: Path,
  host_workspace_dir: Path,
  host_output_dir: Path,
  api_env: dict[str, str],
  event_logger: EventLogger,
  layout: RunLayout,
) -> tuple[TaskSummary, str]:
  """Run one agent mode (baseline or tools) in an isolated Docker container.

  Args:
    host_threads_dir: Seeded thread graph on the host; mounted read-only
      into the tools container at ``/data/threads``.  NOT mounted for baseline.
    host_workspace_dir: Agent workspace on the host; mounted rw at ``/workspace``.
    host_output_dir: Output dir on the host; mounted rw at ``/output``.
      The harness writes ``runner_config.json`` here before exec, and reads
      ``result.json`` after the agent finishes.
    api_env: API key env vars to inject into the container.

  Returns:
    (TaskSummary, test_output)
  """
  import docker

  tagged_task_id = f"{task_id}:{mode}"

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

  # ---- Write runner config to host output dir ----
  host_output_dir.mkdir(parents=True, exist_ok=True)
  host_workspace_dir.mkdir(parents=True, exist_ok=True)

  transcript_dir = "/output/transcripts"
  runner_cfg: dict[str, Any] = {
    "model": cfg.model,
    "problem_statement": problem_statement,
    "max_steps": cfg.max_steps,
    "mode": mode,
    "workspace_dir": "/workspace",
    "result_path": "/output/result.json",
    "transcript_dir": transcript_dir,
  }
  if mode == "tools":
    runner_cfg["threads_dir"] = "/data/threads"

  config_path = host_output_dir / "runner_config.json"
  config_path.write_text(json.dumps(runner_cfg, indent=2), encoding="utf-8")

  # ---- Build volume mounts ----
  volumes: dict[str, dict[str, str]] = {
    str(host_workspace_dir.resolve()): {"bind": "/workspace", "mode": "rw"},
    str(host_output_dir.resolve()): {"bind": "/output", "mode": "rw"},
  }
  if mode == "tools":
    volumes[str(host_threads_dir.resolve())] = {
      "bind": "/data/threads", "mode": "ro",
    }

  # ---- Create and run container ----
  client = docker.from_env()
  container = None
  test_exit = 1
  test_output = ""
  result: dict[str, Any] = {
    "ok": False, "status": "error", "steps": 0,
    "cost": 0.0, "wc_commands": 0, "wc_tools_used": {},
  }

  try:
    container = client.containers.run(
      IMAGE_NAME,
      command="sleep infinity",
      detach=True,
      remove=False,
      volumes=volumes,
      environment=api_env,
    )
    log.info(
      "  [%s] container %s started (image=%s)",
      mode, container.short_id, IMAGE_NAME,
    )

    # ---- Run the agent via container_runner.py ----
    runner_cmd = f"python {CONTAINER_RUNNER_PATH} /output/runner_config.json"
    exit_code, runner_output = _exec_in_container(
      container, runner_cmd, workdir="/app", timeout=600,
    )
    log.info("  [%s] container_runner exit=%d", mode, exit_code)
    if exit_code != 0:
      log.warning("  [%s] container_runner failed:\n%s", mode, runner_output[:2000])

    # ---- Run test_cmd inside the container ----
    test_exit, test_output = _exec_in_container(
      container, test_cmd, workdir="/workspace", timeout=60,
    )
    log.info("  [%s] test_cmd exit=%d", mode, test_exit)

    # ---- Read result.json from host output dir ----
    result_path = host_output_dir / "result.json"
    if result_path.exists():
      try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
      except Exception as exc:
        log.warning("  [%s] Failed to parse result.json: %s", mode, exc)
    else:
      log.warning("  [%s] result.json not found at %s", mode, result_path)

  except Exception as exc:
    log.error("  [%s] Container run failed: %s", mode, exc)
    test_output = f"Container error: {exc}"
  finally:
    if container is not None:
      try:
        container.stop(timeout=5)
      except Exception:
        pass
      try:
        container.remove(force=True)
      except Exception:
        pass

  ok = test_exit == 0

  summary = TaskSummary(
    task_id=tagged_task_id,
    ok=ok,
    mode=mode,
    cost=float(result.get("cost", 0.0)),
    steps=int(result.get("steps", 0)),
    wc_commands=int(result.get("wc_commands", 0)),
    wc_tools_used=dict(result.get("wc_tools_used", {})),
    wc_entry_ids_returned=[],
    bash_commands=0,
    test_runs=1,
    category=category,
    details={
      "test_cmd": test_cmd,
      "test_output": test_output[:4000],
      "agent_status": result.get("status", "unknown"),
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
      "agent_status": result.get("status", "unknown"),
    },
  )

  return summary, test_output


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
  """Run the agent value benchmark: paired baseline vs tools runs.

  Each mode runs in a separate Docker container for true isolation.
  The baseline container has NO access to thread data.
  """
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

  api_env = _collect_api_env()

  pair_results: list[_PairResult] = []

  for task in tasks:
    task_id = task["task_id"]
    category = task.get("category", "")
    test_cmd = task.get("test_cmd", "true")
    problem_statement = task["problem_statement"]

    log.info("=== Agent value task: %s (%s) ===", task_id, category)

    # ---- Seed threads on host filesystem ----
    host_threads_dir = layout.artifacts_dir / "threads" / task_id
    topics = _seed_threads(
      host_threads_dir, task,
      event_logger=event_logger,
      run_id=cfg.run_id,
      task_id=task_id,
    )

    # Delete .md projections — they contain answer text in human-readable
    # form. The MCP server reads only from graph JSON (meta.json,
    # entries.jsonl, edges.jsonl). The .md files are write-only projections
    # that must not exist where the agent could find them.
    for md_file in host_threads_dir.rglob("*.md"):
      md_file.unlink()
    log.info("Stripped .md projections from %s", host_threads_dir)

    # ---- Prepare host-side workspace and output dirs per mode ----
    baseline_workspace = layout.artifacts_dir / "workspaces" / task_id / "baseline"
    baseline_workspace.mkdir(parents=True, exist_ok=True)
    (baseline_workspace / "README.md").write_text(project_context, encoding="utf-8")

    baseline_output = layout.artifacts_dir / "output" / task_id / "baseline"
    baseline_output.mkdir(parents=True, exist_ok=True)

    # ---- Baseline run (no WC tools, no thread data mounted) ----
    log.info("  [baseline] starting container...")

    baseline_summary, baseline_test_output = _run_one_in_container(
      problem_statement=problem_statement,
      test_cmd=test_cmd,
      cfg=cfg,
      mode="baseline",
      task_id=task_id,
      category=category,
      host_threads_dir=host_threads_dir,
      host_workspace_dir=baseline_workspace,
      host_output_dir=baseline_output,
      api_env=api_env,
      event_logger=event_logger,
      layout=layout,
    )
    run_summary.tasks.append(baseline_summary)

    # ---- Tools run (with WC tools, thread data mounted ro) ----
    log.info("  [tools] starting container...")

    tools_workspace = layout.artifacts_dir / "workspaces" / task_id / "tools"
    tools_workspace.mkdir(parents=True, exist_ok=True)
    (tools_workspace / "README.md").write_text(project_context, encoding="utf-8")

    tools_output = layout.artifacts_dir / "output" / task_id / "tools"
    tools_output.mkdir(parents=True, exist_ok=True)

    tools_summary, tools_test_output = _run_one_in_container(
      problem_statement=problem_statement,
      test_cmd=test_cmd,
      cfg=cfg,
      mode="tools",
      task_id=task_id,
      category=category,
      host_threads_dir=host_threads_dir,
      host_workspace_dir=tools_workspace,
      host_output_dir=tools_output,
      api_env=api_env,
      event_logger=event_logger,
      layout=layout,
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
      baseline_test_output=baseline_test_output[:2000],
      tools_test_output=tools_test_output[:2000],
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
