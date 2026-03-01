"""Agent value benchmark track.

Answers one question: does an agent with watercooler tools succeed at tasks
that an agent without them fails?

For each task we run two paired agent sessions via OpenHands SDK:
  1. **baseline** -- agent has bash + file editor only
  2. **tools**    -- same agent + watercooler MCP server for search/read

Both agents get the same problem statement and workspace.  After each run
we execute ``test_cmd`` to get a deterministic pass/fail.
Results go to ``COMPARISON.md`` and ``pair_results.json``.

Uses OpenHands Software Agent SDK with native MCP integration.
The watercooler MCP server (``python -m watercooler_mcp``) is spawned as
a stdio MCP server via ``mcp_config``, pointed at seeded thread data
using the ``WATERCOOLER_DIR`` environment variable.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from tests.benchmarks.wcbench.config import RunConfig
from tests.benchmarks.wcbench.events import EventLogger
from tests.benchmarks.wcbench.run_layout import RunLayout
from tests.benchmarks.wcbench.summary import RunSummary, TaskSummary

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TASKS_PATH = Path("tests/benchmarks/agent_value/tasks.json")

# Regex matching tools the agent can use: base tools + watercooler MCP tools.
# filter_tools_regex applies to ALL tools (not just MCP), so we must include
# the built-in TerminalTool and FileEditorTool names too.
_WC_TOOLS_REGEX = (
  "(terminal|file_editor|str_replace_editor"
  "|watercooler_(search|smart_query|read_thread|get_thread_entry"
  "|list_thread_entries|list_threads))"
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
# OpenHands agent helpers
# ---------------------------------------------------------------------------


def _make_llm(model: str) -> Any:
  """Create an OpenHands LLM instance.

  API keys are expected to be in the environment (loaded by setup_api_keys).
  LiteLLM model strings are used directly (e.g. ``minimax/MiniMax-M2.5``).
  """
  from openhands.sdk import LLM

  return LLM(model=model)


def _make_agent(
  llm: Any,
  *,
  with_wc_tools: bool = False,
  threads_dir: Optional[Path] = None,
  code_path: Optional[Path] = None,
) -> Any:
  """Create an OpenHands Agent.

  Args:
    llm: LLM instance.
    with_wc_tools: If True, attach watercooler MCP server via mcp_config.
    threads_dir: Path to seeded thread data (for WATERCOOLER_DIR).
    code_path: Code path for WATERCOOLER_CODE_PATH.
  """
  from openhands.sdk import Agent, Tool
  from openhands.tools.file_editor import FileEditorTool
  from openhands.tools.terminal import TerminalTool

  base_tools = [
    Tool(name=TerminalTool.name),
    Tool(name=FileEditorTool.name),
  ]

  kwargs: dict[str, Any] = {
    "llm": llm,
    "tools": base_tools,
  }

  if with_wc_tools and threads_dir is not None:
    env = {"WATERCOOLER_DIR": str(threads_dir)}
    if code_path is not None:
      env["WATERCOOLER_CODE_PATH"] = str(code_path)

    kwargs["mcp_config"] = {
      "mcpServers": {
        "watercooler": {
          "command": sys.executable,
          "args": ["-m", "watercooler_mcp"],
          "env": env,
        }
      }
    }
    kwargs["filter_tools_regex"] = _WC_TOOLS_REGEX

  return Agent(**kwargs)


def _run_conversation(
  agent: Any,
  workspace_dir: Path,
  problem_statement: str,
  max_steps: int,
) -> dict[str, Any]:
  """Run an OpenHands conversation and extract metrics.

  Returns:
    Dict with keys: ok, steps, cost, wc_commands, wc_tools_used, events.
  """
  from openhands.sdk import Conversation

  conversation = Conversation(
    agent=agent,
    workspace=str(workspace_dir),
    max_iteration_per_run=max_steps,
  )

  try:
    conversation.send_message(problem_statement)
    conversation.run()

    # Extract metrics
    cost = 0.0
    try:
      spend = conversation.conversation_stats.get_combined_metrics()
      cost = float(spend.accumulated_cost)
    except Exception:
      pass

    # Count steps and watercooler tool calls from event log
    steps = 0
    wc_commands = 0
    wc_tools_used: dict[str, int] = {}
    events_data: list[dict[str, Any]] = []

    try:
      for event in conversation.state.events:
        event_dict: dict[str, Any] = {"type": type(event).__name__}

        # Count action events as steps
        if hasattr(event, "tool_name"):
          steps += 1
          tool_name = str(getattr(event, "tool_name", ""))
          event_dict["tool_name"] = tool_name

          # Track watercooler tool usage
          if "watercooler" in tool_name.lower():
            wc_commands += 1
            wc_tools_used[tool_name] = wc_tools_used.get(tool_name, 0) + 1

        events_data.append(event_dict)
    except Exception as exc:
      log.warning("Error extracting events: %s", exc)

    status = "unknown"
    try:
      status = str(conversation.state.execution_status.value)
    except Exception:
      pass

    return {
      "ok": True,
      "status": status,
      "steps": steps,
      "cost": cost,
      "wc_commands": wc_commands,
      "wc_tools_used": wc_tools_used,
      "events": events_data,
    }
  except Exception as exc:
    log.error("Conversation failed: %s", exc)
    return {
      "ok": False,
      "status": "error",
      "steps": 0,
      "cost": 0.0,
      "wc_commands": 0,
      "wc_tools_used": {},
      "events": [],
      "error": str(exc),
    }
  finally:
    try:
      conversation.close()
    except Exception:
      pass


# ---------------------------------------------------------------------------
# Single paired run
# ---------------------------------------------------------------------------


def _run_one(
  *,
  problem_statement: str,
  test_cmd: str,
  cfg: RunConfig,
  mode: str,
  task_id: str,
  category: str,
  threads_dir: Path,
  workspace_dir: Path,
  event_logger: EventLogger,
) -> tuple[TaskSummary, str]:
  """Run one agent (baseline or tools) in a workspace.

  Returns:
    (TaskSummary, test_output)
  """
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

  # Create LLM and agent
  llm = _make_llm(cfg.model)
  agent = _make_agent(
    llm,
    with_wc_tools=(mode == "tools"),
    threads_dir=threads_dir,
    code_path=cfg.wc_code_path,
  )

  # Run the conversation
  result = _run_conversation(
    agent=agent,
    workspace_dir=workspace_dir,
    problem_statement=problem_statement,
    max_steps=cfg.max_steps,
  )

  # Run test command in the workspace
  import subprocess

  try:
    proc = subprocess.run(
      ["bash", "-c", test_cmd],
      cwd=str(workspace_dir),
      capture_output=True,
      text=True,
      timeout=60,
    )
    test_exit = proc.returncode
    test_output = (proc.stdout + proc.stderr).strip()
  except Exception as exc:
    test_exit = 1
    test_output = f"test_cmd execution failed: {exc}"

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
  """Run the agent value benchmark: paired baseline vs tools runs."""
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

    # ---- Create workspace with project context ----
    workspace_base = layout.artifacts_dir / "workspaces" / task_id
    workspace_base.mkdir(parents=True, exist_ok=True)

    # Write project context as README so the agent has domain context
    readme_path = workspace_base / "README.md"
    readme_path.write_text(project_context, encoding="utf-8")

    # ---- Baseline run (no WC tools) ----
    log.info("  [baseline] starting...")
    baseline_workspace = workspace_base / "baseline"
    baseline_workspace.mkdir(parents=True, exist_ok=True)
    (baseline_workspace / "README.md").write_text(project_context, encoding="utf-8")

    baseline_summary, baseline_output = _run_one(
      problem_statement=problem_statement,
      test_cmd=test_cmd,
      cfg=cfg,
      mode="baseline",
      task_id=task_id,
      category=category,
      threads_dir=threads_dir,
      workspace_dir=baseline_workspace,
      event_logger=event_logger,
    )
    run_summary.tasks.append(baseline_summary)

    # ---- Tools run (with WC tools) ----
    log.info("  [tools] starting...")
    tools_workspace = workspace_base / "tools"
    tools_workspace.mkdir(parents=True, exist_ok=True)
    (tools_workspace / "README.md").write_text(project_context, encoding="utf-8")

    tools_summary, tools_output = _run_one(
      problem_statement=problem_statement,
      test_cmd=test_cmd,
      cfg=cfg,
      mode="tools",
      task_id=task_id,
      category=category,
      threads_dir=threads_dir,
      workspace_dir=tools_workspace,
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
