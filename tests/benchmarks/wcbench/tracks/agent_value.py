"""Agent value benchmark track.

Answers one question: does an agent with watercooler tools succeed at tasks
that an agent without them fails?

For each task we run two paired agent sessions via the OpenHands SDK:
  1. **baseline** — agent has terminal + file editor only
  2. **tools**   — same agent + watercooler MCP server for search/read

Both agents get the same problem statement and workspace.  After each run
we execute ``test_cmd`` to get a deterministic pass/fail.  The COMPARISON.md
report shows the baseline-vs-tools delta.
"""

from __future__ import annotations

import json
import logging
import os
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
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_TASKS_PATH = Path("tests/benchmarks/agent_value/tasks.json")

# Tools regex: only expose watercooler read/search tools (no writes)
_WC_TOOLS_REGEX = (
  "watercooler_(search|smart_query|read_thread|get_thread_entry"
  "|list_thread_entries|list_threads|get_thread_entry_range)"
)


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
# Thread seeding (reuses core graph writer, same pattern as custom track)
# ---------------------------------------------------------------------------


def _seed_threads(
  threads_dir: Path,
  task: dict[str, Any],
  *,
  event_logger: EventLogger,
  run_id: str,
  task_id: str,
) -> None:
  """Seed watercooler threads from task definition."""
  from ulid import ULID
  from watercooler.commands_graph import say

  threads_dir.mkdir(parents=True, exist_ok=True)

  seed_threads = task.get("seed_threads", [])
  for thread_seed in seed_threads:
    topic = thread_seed["thread_id"]
    entries = thread_seed.get("entries", [])
    for e in entries:
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


# ---------------------------------------------------------------------------
# OpenHands agent runner
# ---------------------------------------------------------------------------


def _run_openhands_agent(
  *,
  problem_statement: str,
  workspace: Path,
  model: str,
  max_steps: int,
  mcp_config: Optional[dict[str, Any]] = None,
  filter_tools_regex: Optional[str] = None,
) -> dict[str, Any]:
  """Run an OpenHands agent and return normalized results.

  Returns:
    dict with keys: ok (bool), steps (int), cost (float),
    wc_calls (int), wc_tools_used (dict), test_output (str)
  """
  from openhands.sdk import LLM, Agent, Conversation, Tool
  from openhands.tools.file_editor import FileEditorTool
  from openhands.tools.terminal import TerminalTool

  api_key = os.environ.get("LLM_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or ""
  if not api_key:
    # Try loading from credentials.toml
    try:
      from tests.benchmarks.scripts.run_swebench import setup_api_keys
      setup_api_keys()
      api_key = os.environ.get("LLM_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or ""
    except Exception:
      pass

  llm = LLM(model=model, api_key=api_key)

  agent_kwargs: dict[str, Any] = {
    "llm": llm,
    "tools": [
      Tool(name=TerminalTool.name),
      Tool(name=FileEditorTool.name),
    ],
  }
  if mcp_config is not None:
    agent_kwargs["mcp_config"] = mcp_config
  if filter_tools_regex is not None:
    agent_kwargs["filter_tools_regex"] = filter_tools_regex

  agent = Agent(**agent_kwargs)

  workspace.mkdir(parents=True, exist_ok=True)
  conversation = Conversation(agent=agent, workspace=str(workspace))
  conversation.send_message(problem_statement)

  try:
    conversation.run()
  except Exception as exc:
    log.warning("OpenHands agent run failed: %s", exc)

  # Extract metrics from conversation (best-effort)
  result: dict[str, Any] = {
    "steps": getattr(conversation, "steps", 0) or 0,
    "cost": getattr(conversation, "total_cost", 0.0) or 0.0,
    "wc_calls": 0,
    "wc_tools_used": {},
  }

  # Count watercooler tool calls from conversation history
  history = getattr(conversation, "history", []) or getattr(conversation, "messages", []) or []
  wc_tools: dict[str, int] = {}
  for msg in history:
    tool_calls = getattr(msg, "tool_calls", None) or []
    for tc in tool_calls:
      fn_name = ""
      if hasattr(tc, "function"):
        fn_name = getattr(tc.function, "name", "") or ""
      elif isinstance(tc, dict):
        fn_name = tc.get("function", {}).get("name", "") or tc.get("name", "")
      if "watercooler" in fn_name.lower():
        wc_tools[fn_name] = wc_tools.get(fn_name, 0) + 1

  result["wc_calls"] = sum(wc_tools.values())
  result["wc_tools_used"] = wc_tools
  return result


# ---------------------------------------------------------------------------
# Test execution
# ---------------------------------------------------------------------------


def _run_test_cmd(workspace: Path, test_cmd: str) -> tuple[int, str]:
  """Run test command in workspace, return (exit_code, output)."""
  import subprocess

  try:
    proc = subprocess.run(
      ["bash", "-c", test_cmd],
      cwd=str(workspace),
      capture_output=True,
      text=True,
      timeout=30,
    )
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode, output
  except subprocess.TimeoutExpired:
    return 1, "TIMEOUT"
  except Exception as exc:
    return 1, f"ERROR: {exc}"


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
  lines: list[str] = []
  lines.append("# Agent Value Benchmark — COMPARISON")
  lines.append("")
  lines.append(f"- **run_id**: `{run_id}`")
  lines.append(f"- **model**: `{model}`")
  lines.append(f"- **tasks**: {len(results)}")
  lines.append("")

  # Aggregate
  baseline_pass = sum(1 for r in results if r.baseline_ok)
  tools_pass = sum(1 for r in results if r.tools_ok)
  both_pass = sum(1 for r in results if r.baseline_ok and r.tools_ok)
  tools_only = sum(1 for r in results if not r.baseline_ok and r.tools_ok)
  baseline_only = sum(1 for r in results if r.baseline_ok and not r.tools_ok)
  neither = sum(1 for r in results if not r.baseline_ok and not r.tools_ok)

  lines.append("## Summary")
  lines.append("")
  lines.append(f"| Metric | Value |")
  lines.append(f"|--------|-------|")
  lines.append(f"| Baseline pass rate | {baseline_pass}/{len(results)} ({100*baseline_pass/max(len(results),1):.0f}%) |")
  lines.append(f"| Tools pass rate | {tools_pass}/{len(results)} ({100*tools_pass/max(len(results),1):.0f}%) |")
  lines.append(f"| Both pass | {both_pass} |")
  lines.append(f"| **Tools-only wins** | **{tools_only}** |")
  lines.append(f"| Baseline-only wins | {baseline_only} |")
  lines.append(f"| Neither pass | {neither} |")
  lines.append(f"| Absolute delta | +{tools_pass - baseline_pass} tasks |")
  lines.append("")

  # Per-category
  categories: dict[str, list[_PairResult]] = {}
  for r in results:
    categories.setdefault(r.category, []).append(r)

  if categories:
    lines.append("## Per-category breakdown")
    lines.append("")
    lines.append("| Category | Baseline | Tools | Delta |")
    lines.append("|----------|----------|-------|-------|")
    for cat in sorted(categories):
      cat_results = categories[cat]
      b = sum(1 for r in cat_results if r.baseline_ok)
      t = sum(1 for r in cat_results if r.tools_ok)
      n = len(cat_results)
      lines.append(f"| {cat} | {b}/{n} | {t}/{n} | {'+' if t-b >= 0 else ''}{t-b} |")
    lines.append("")

  # Per-task detail
  lines.append("## Per-task results")
  lines.append("")
  lines.append("| Task | Category | Baseline | Tools | WC calls | Verdict |")
  lines.append("|------|----------|----------|-------|----------|---------|")
  for r in results:
    verdict = "BOTH" if r.baseline_ok and r.tools_ok else (
      "TOOLS-WIN" if not r.baseline_ok and r.tools_ok else (
        "BASE-WIN" if r.baseline_ok and not r.tools_ok else "NEITHER"
      )
    )
    b_icon = "PASS" if r.baseline_ok else "FAIL"
    t_icon = "PASS" if r.tools_ok else "FAIL"
    lines.append(f"| {r.task_id} | {r.category} | {b_icon} | {t_icon} | {r.wc_calls} | {verdict} |")
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

  if cfg.agent_value_only_task_ids:
    allow = set(cfg.agent_value_only_task_ids)
    tasks = [t for t in tasks if t.get("task_id") in allow]

  if not tasks:
    log.warning("No agent_value tasks to run")
    return

  # Load API keys
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

    # --- Seed threads ---
    threads_dir = layout.artifacts_dir / "threads" / task_id
    _seed_threads(
      threads_dir,
      task,
      event_logger=event_logger,
      run_id=cfg.run_id,
      task_id=task_id,
    )

    # --- Baseline run (no watercooler tools) ---
    baseline_task_id = f"{task_id}:baseline"
    baseline_workspace = layout.artifacts_dir / "workspaces" / task_id / "baseline"

    event_logger.emit(
      "task_start",
      run_id=cfg.run_id,
      task_id=baseline_task_id,
      payload={
        "title": task.get("title", ""),
        "mode": "baseline",
        "category": category,
      },
    )

    log.info("  Running baseline agent (no WC tools)...")
    baseline_result = _run_openhands_agent(
      problem_statement=problem_statement,
      workspace=baseline_workspace,
      model=cfg.model,
      max_steps=cfg.max_steps,
    )

    baseline_exit, baseline_output = _run_test_cmd(baseline_workspace, test_cmd)
    baseline_ok = baseline_exit == 0

    baseline_summary = TaskSummary(
      task_id=baseline_task_id,
      ok=baseline_ok,
      mode="baseline",
      cost=float(baseline_result.get("cost", 0.0)),
      steps=int(baseline_result.get("steps", 0)),
      category=category,
      details={
        "test_cmd": test_cmd,
        "test_output": baseline_output[:4000],
        "paired_with": f"{task_id}:tools",
      },
    )
    run_summary.tasks.append(baseline_summary)

    event_logger.emit(
      "test_result",
      run_id=cfg.run_id,
      task_id=baseline_task_id,
      payload={
        "command": test_cmd,
        "exit_code": baseline_exit,
        "passed": baseline_ok,
        "output": baseline_output[:4000],
      },
    )
    event_logger.emit(
      "task_end",
      run_id=cfg.run_id,
      task_id=baseline_task_id,
      payload={
        "ok": baseline_ok,
        "mode": "baseline",
        "cost": baseline_summary.cost,
        "steps": baseline_summary.steps,
      },
    )

    # --- Tools run (with watercooler MCP server) ---
    tools_task_id = f"{task_id}:tools"
    tools_workspace = layout.artifacts_dir / "workspaces" / task_id / "tools"

    event_logger.emit(
      "task_start",
      run_id=cfg.run_id,
      task_id=tools_task_id,
      payload={
        "title": task.get("title", ""),
        "mode": "tools",
        "category": category,
      },
    )

    # Build MCP config pointing watercooler server at seeded threads
    mcp_config = {
      "mcpServers": {
        "watercooler": {
          "command": sys.executable,
          "args": ["-m", "watercooler_mcp"],
          "env": {
            "WATERCOOLER_THREADS_DIR": str(threads_dir),
            "WATERCOOLER_CODE_PATH": str(cfg.wc_code_path or Path.cwd()),
          },
        }
      }
    }

    log.info("  Running tools agent (with WC MCP)...")
    tools_result = _run_openhands_agent(
      problem_statement=problem_statement,
      workspace=tools_workspace,
      model=cfg.model,
      max_steps=cfg.max_steps,
      mcp_config=mcp_config,
      filter_tools_regex=_WC_TOOLS_REGEX,
    )

    tools_exit, tools_output = _run_test_cmd(tools_workspace, test_cmd)
    tools_ok = tools_exit == 0

    tools_summary = TaskSummary(
      task_id=tools_task_id,
      ok=tools_ok,
      mode="tools",
      cost=float(tools_result.get("cost", 0.0)),
      steps=int(tools_result.get("steps", 0)),
      wc_commands=int(tools_result.get("wc_calls", 0)),
      wc_tools_used=dict(tools_result.get("wc_tools_used", {})),
      category=category,
      details={
        "test_cmd": test_cmd,
        "test_output": tools_output[:4000],
        "paired_with": f"{task_id}:baseline",
      },
    )
    run_summary.tasks.append(tools_summary)

    event_logger.emit(
      "test_result",
      run_id=cfg.run_id,
      task_id=tools_task_id,
      payload={
        "command": test_cmd,
        "exit_code": tools_exit,
        "passed": tools_ok,
        "output": tools_output[:4000],
      },
    )
    event_logger.emit(
      "task_end",
      run_id=cfg.run_id,
      task_id=tools_task_id,
      payload={
        "ok": tools_ok,
        "mode": "tools",
        "cost": tools_summary.cost,
        "steps": tools_summary.steps,
        "wc_calls": tools_summary.wc_commands,
        "wc_tools_used": tools_summary.wc_tools_used,
      },
    )

    # --- Record pair result ---
    pair_results.append(
      _PairResult(
        task_id=task_id,
        category=category,
        baseline_ok=baseline_ok,
        tools_ok=tools_ok,
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
    )

    log.info(
      "  Result: baseline=%s, tools=%s, wc_calls=%d",
      "PASS" if baseline_ok else "FAIL",
      "PASS" if tools_ok else "FAIL",
      tools_summary.wc_commands,
    )

  # --- Write COMPARISON.md ---
  comparison_path = layout.root / "COMPARISON.md"
  _write_comparison_report(
    comparison_path,
    pair_results,
    run_id=cfg.run_id,
    model=cfg.model,
  )
  log.info("COMPARISON.md written to %s", comparison_path)

  # Also write pair results as JSON for programmatic consumption
  pairs_json_path = layout.root / "pair_results.json"
  pairs_data = []
  for pr in pair_results:
    pairs_data.append({
      "task_id": pr.task_id,
      "category": pr.category,
      "baseline_ok": pr.baseline_ok,
      "tools_ok": pr.tools_ok,
      "baseline_steps": pr.baseline_steps,
      "tools_steps": pr.tools_steps,
      "baseline_cost": pr.baseline_cost,
      "tools_cost": pr.tools_cost,
      "wc_calls": pr.wc_calls,
      "wc_tools_used": pr.wc_tools_used,
    })
  pairs_json_path.write_text(
    json.dumps(pairs_data, indent=2), encoding="utf-8"
  )
