"""Agent value benchmark track.

Answers one question: does an agent with watercooler tools succeed at tasks
that an agent without them fails?

Both agents get the **watercooler-site codebase** (baked into the Docker image
at ``/repo``).  The tools agent additionally gets the real **thread history**
from the ``watercooler/threads`` orphan branch, bind-mounted at
``/data/threads``.

For each task we run two paired agent sessions in **separate Docker containers**:
  1. **baseline** -- agent has bash + file editor only; no thread data
  2. **tools**    -- same agent + watercooler MCP server; thread graph at /data/threads

Both containers use the same image (``wcbench-agent-base:wc-site-v1`` by default).
To make runs reproducible, pin ``--agent-value-image`` to an immutable image tag.
After the agent finishes we exec ``test_cmd`` *inside the same container*
to get a deterministic pass/fail.  Results go to ``COMPARISON.md`` and
``pair_results.json``.

Container isolation guarantees the baseline agent has zero access to thread
data — ``find / -name '*.jsonl'`` will find nothing watercooler-related.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
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

# The container_runner.py script on the host, bind-mounted into containers.
_HOST_RUNNER_PATH = Path("tests/benchmarks/agent_value/container_runner.py")
_CONTAINER_RUNNER_PATH = "/runner/container_runner.py"

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
# Orphan branch clone (replaces per-task thread seeding)
# ---------------------------------------------------------------------------


def _clone_orphan_threads(
  repo_url: str,
  branch: str,
  dest: Path,
) -> Path:
  """Shallow-clone the orphan threads branch to a host-side directory.

  The clone is shared across all tasks in a single run.  The harness
  bind-mounts it read-only into tools containers at ``/data/threads``.

  After cloning, ``.md`` projections under ``threads/`` are deleted —
  they contain human-readable answer text that must not leak to agents.
  The MCP server reads only from ``graph/baseline/``.

  Args:
    repo_url: Git remote URL (e.g. watercooler-site on GitHub).
    branch: Orphan branch name (e.g. ``watercooler/threads``).
    dest: Host-side directory to clone into.

  Returns:
    Path to the cloned directory.
  """
  if dest.exists():
    log.info("Removing existing orphan branch clone for fresh run: %s", dest)
    shutil.rmtree(dest)
  dest.parent.mkdir(parents=True, exist_ok=True)
  log.info("Cloning orphan branch %s from %s -> %s", branch, repo_url, dest)
  subprocess.run(
    [
      "git", "clone",
      "--single-branch", "--depth=1",
      "--branch", branch,
      repo_url, str(dest),
    ],
    check=True,
    capture_output=True,
  )

  # Remove .git (not needed at runtime, saves space).
  git_dir = dest / ".git"
  if git_dir.exists():
    shutil.rmtree(git_dir)

  # Strip .md projections — they contain human-readable answers.
  threads_md_dir = dest / "threads"
  if threads_md_dir.exists():
    for md_file in threads_md_dir.rglob("*.md"):
      md_file.unlink()
    log.info("Stripped .md projections from %s", threads_md_dir)

  return dest


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
  workdir: str = "/repo",
  timeout: int = 300,
) -> tuple[int, str]:
  """Execute a command in a running container. Returns (exit_code, output)."""
  def _run_exec() -> Any:
    return container.exec_run(
      ["bash", "-c", cmd],
      workdir=workdir,
      demux=True,
    )

  try:
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_run_exec)
    try:
      result = future.result(timeout=timeout)
    except FutureTimeoutError:
      future.cancel()
      try:
        container.kill()
      except Exception:
        pass
      return (
        124,
        (
          f"exec_in_container timeout after {timeout}s "
          f"(workdir={workdir}, cmd={cmd[:200]!r})"
        ),
      )
    finally:
      executor.shutdown(wait=False, cancel_futures=True)
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
  host_output_dir: Path,
  host_runner_path: Path,
  api_env: dict[str, str],
  event_logger: EventLogger,
  layout: RunLayout,
) -> tuple[TaskSummary, str]:
  """Run one agent mode (baseline or tools) in an isolated Docker container.

  The container image has the watercooler-site codebase baked in at ``/repo``.
  No host-side workspace dir is needed — each container starts fresh.

  Args:
    host_threads_dir: Orphan branch clone on the host; mounted read-only
      into the tools container at ``/data/threads``.  NOT mounted for baseline.
    host_output_dir: Output dir on the host; mounted rw at ``/output``.
      The harness writes ``runner_config.json`` here before exec, and reads
      ``result.json`` after the agent finishes.
    host_runner_path: Path to container_runner.py on the host; mounted ro
      at ``/runner/container_runner.py``.
    api_env: API key env vars to inject into the container.

  Returns:
    (TaskSummary, test_output)
  """
  import docker

  image_name = cfg.agent_value_image
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

  transcript_dir = "/output/transcripts"
  runner_cfg: dict[str, Any] = {
    "model": cfg.model,
    "problem_statement": problem_statement,
    "max_steps": cfg.max_steps,
    "mode": mode,
    "workspace_dir": "/repo",
    "result_path": "/output/result.json",
    "transcript_dir": transcript_dir,
  }
  if mode == "tools":
    runner_cfg["threads_dir"] = "/data/threads"

  config_path = host_output_dir / "runner_config.json"
  config_path.write_text(json.dumps(runner_cfg, indent=2), encoding="utf-8")

  # ---- Build volume mounts ----
  volumes: dict[str, dict[str, str]] = {
    str(host_output_dir.resolve()): {"bind": "/output", "mode": "rw"},
    str(host_runner_path.resolve()): {
      "bind": _CONTAINER_RUNNER_PATH, "mode": "ro",
    },
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
      image_name,
      command="sleep infinity",
      detach=True,
      remove=False,
      volumes=volumes,
      environment=api_env,
    )
    log.info(
      "  [%s] container %s started (image=%s)",
      mode, container.short_id, image_name,
    )

    # ---- Run the agent via container_runner.py ----
    runner_cmd = f"python {_CONTAINER_RUNNER_PATH} /output/runner_config.json"
    exit_code, runner_output = _exec_in_container(
      container, runner_cmd, workdir="/repo", timeout=600,
    )
    if exit_code == 124:
      result["status"] = "timeout"
    log.info("  [%s] container_runner exit=%d", mode, exit_code)
    if exit_code != 0:
      log.warning("  [%s] container_runner failed:\n%s", mode, runner_output[:2000])

    # ---- Run test_cmd inside the container ----
    test_exit, test_output = _exec_in_container(
      container, test_cmd, workdir="/repo", timeout=60,
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
      "runner_exit_code": exit_code if "exit_code" in locals() else -1,
      "runner_output": runner_output[:4000] if "runner_output" in locals() else "",
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

  Architecture:
    - Both containers use the same image with watercooler-site code at /repo.
      Reproducibility is image-tag based (pin cfg.agent_value_image).
    - The harness clones the orphan branch (thread history) once per run.
    - Baseline containers: /repo (code) only.
    - Tools containers: /repo (code) + /data/threads (orphan branch, ro).
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

  # ---- Clone orphan branch (once for the entire run) ----
  threads_clone_dir = layout.artifacts_dir / "threads-clone"
  _clone_orphan_threads(
    repo_url=cfg.agent_value_site_repo,
    branch=cfg.agent_value_threads_ref,
    dest=threads_clone_dir,
  )

  # Resolve host-side runner script path
  host_runner_path = _HOST_RUNNER_PATH.resolve()
  if not host_runner_path.exists():
    raise FileNotFoundError(
      f"container_runner.py not found: {host_runner_path}"
    )

  # Prepend project context to the problem statement so agents know
  # what project they're working in (matches the old README.md seeding).
  def _full_problem(ps: str) -> str:
    if project_context:
      return f"{project_context}\n\n---\n\n{ps}"
    return ps

  pair_results: list[_PairResult] = []

  for task in tasks:
    task_id = task["task_id"]
    category = task.get("category", "")
    test_cmd = task.get("test_cmd", "true")
    problem_statement = _full_problem(task["problem_statement"])

    log.info("=== Agent value task: %s (%s) ===", task_id, category)

    # ---- Baseline run (no WC tools, no thread data) ----
    log.info("  [baseline] starting container...")

    baseline_output = layout.artifacts_dir / "output" / task_id / "baseline"
    baseline_summary, baseline_test_output = _run_one_in_container(
      problem_statement=problem_statement,
      test_cmd=test_cmd,
      cfg=cfg,
      mode="baseline",
      task_id=task_id,
      category=category,
      host_threads_dir=threads_clone_dir,
      host_output_dir=baseline_output,
      host_runner_path=host_runner_path,
      api_env=api_env,
      event_logger=event_logger,
      layout=layout,
    )
    run_summary.tasks.append(baseline_summary)

    # ---- Tools run (with WC tools, thread data mounted ro) ----
    log.info("  [tools] starting container...")

    tools_output = layout.artifacts_dir / "output" / task_id / "tools"
    tools_summary, tools_test_output = _run_one_in_container(
      problem_statement=problem_statement,
      test_cmd=test_cmd,
      cfg=cfg,
      mode="tools",
      task_id=task_id,
      category=category,
      host_threads_dir=threads_clone_dir,
      host_output_dir=tools_output,
      host_runner_path=host_runner_path,
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
