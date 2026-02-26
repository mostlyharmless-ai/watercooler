from __future__ import annotations

import json
import logging
from pathlib import Path

import docker

from tests.benchmarks.wcbench.config import RunConfig
from tests.benchmarks.wcbench.events import EventLogger
from tests.benchmarks.wcbench.run_layout import RunLayout
from tests.benchmarks.wcbench.summary import RunSummary, TaskSummary

log = logging.getLogger(__name__)


def run_coordination_track(cfg: RunConfig, *, layout: RunLayout, event_logger: EventLogger, run_summary: RunSummary) -> None:
  """CooperBench-like subset: two-phase handoff under overlap.

  Phase 1 ("AgentA"): gather facts + constraints and write a wc-say handoff note.
  Phase 2 ("AgentB"): start from a clean repo state, read the handoff note, implement and test.
  """
  if cfg.custom_tasks_path is None:
    raise SystemExit("coordination track requires RunConfig.custom_tasks_path")

  # Load API keys (shared helper)
  try:
    from tests.benchmarks.scripts.run_swebench import setup_api_keys
    setup_api_keys()
  except Exception:
    pass

  tasks_cfg = json.loads(Path(cfg.custom_tasks_path).read_text(encoding="utf-8"))
  dockerfile_dir = Path(cfg.custom_tasks_path).parent.parent

  tasks = list(tasks_cfg.get("tasks", []))
  target = None
  for t in tasks:
    if t.get("task_id") == cfg.coordination_task_id:
      target = t
      break
  if target is None:
    raise SystemExit(f"coordination task not found: {cfg.coordination_task_id}")

  client = docker.from_env()
  # Reuse the custom bench image tag from config
  from tests.benchmarks.wcbench.tracks.custom import _build_image, _init_git, _seed_threads
  _build_image(client, dockerfile_dir, tag=cfg.custom_image_tag)

  workdir = target.get("workdir", tasks_cfg.get("workdir", "/repo"))
  topic = target["threads_seed_topic"]
  test_cmd = target.get("test_command", "pytest -q")

  threads_dir = layout.artifacts_dir / "threads" / f"coordination-{cfg.coordination_task_id}"
  _seed_threads(
    threads_dir,
    topic,
    target.get("threads_seed_entries", []),
    event_logger=event_logger,
    run_id=cfg.run_id,
    task_id=f"coordination:{cfg.coordination_task_id}",
  )

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

    # Phase 1: AgentA (handoff note)
    phase1_id = f"coordination:{cfg.coordination_task_id}:AgentA"
    event_logger.emit(
      "task_start",
      run_id=cfg.run_id,
      task_id=phase1_id,
      payload={"mode": cfg.mode, "topic": topic, "workdir": workdir},
    )

    wc_session = None
    wc_guidance_text = ""
    if cfg.mode in ("tools", "tools_guided"):
      from tests.benchmarks.scripts.wc_text_tools import WcToolSession
      from tests.benchmarks.wcbench.wc_tools import WcToolAdapter

      base = WcToolSession(
        threads_dir=threads_dir,
        default_topic=topic,
        code_path=cfg.wc_code_path,
        tier_ceiling=cfg.wc_tier_ceiling,
        max_calls=cfg.wc_max_calls,
        token_budget=cfg.wc_token_budget,
        allow_write=True,
      )
      wc_session = WcToolAdapter(session=base, event_logger=event_logger, run_id=cfg.run_id, task_id=phase1_id)
      if cfg.mode == "tools_guided" and cfg.wc_guidance_file and cfg.wc_guidance_file.exists():
        wc_guidance_text = cfg.wc_guidance_file.read_text(encoding="utf-8")

    phase1_statement = (
      "You are AgentA. Your job is to gather the exact facts/constraints needed to solve the task "
      "and write a handoff note using wc-say. Do NOT modify any files.\n\n"
      f"Task:\n{target['problem_statement']}\n\n"
      "Handoff requirements:\n"
      "- Use wc-read-thread / wc-get-entry to retrieve decision trace entries\n"
      "- Compute the exact outputs required\n"
      "- Write one wc-say note summarizing: required outputs + the entry IDs you will cite + which file(s) to edit\n"
      "- Then respond with SUBMIT\n"
    )

    agent_a = backend.run(
      problem_statement=phase1_statement,
      model_name=cfg.model,
      max_steps=max(8, min(cfg.max_steps, 15)),
      cost_limit=min(cfg.cost_limit, 0.25),
      knowledge_context="",
      wc_session=wc_session,
      wc_guidance_text=wc_guidance_text,
      workdir=workdir,
    )

    run_summary.tasks.append(
      TaskSummary(
        task_id=phase1_id,
        ok=True,
        mode=cfg.mode,
        cost=float(agent_a.total_cost),
        steps=int(agent_a.steps),
        wc_commands=int(agent_a.raw.get("wc_commands", 0) or 0),
        wc_tools_used=dict(agent_a.raw.get("wc_tools_used", {}) or {}),
        wc_entry_ids_returned=list(agent_a.raw.get("wc_entry_ids_returned", []) or []),
        bash_commands=int(agent_a.raw.get("bash_commands", 0) or 0),
        test_runs=int(agent_a.raw.get("test_runs", 0) or 0),
        details={"patch_chars": len(agent_a.model_patch or "")},
      )
    )
    event_logger.emit("task_end", run_id=cfg.run_id, task_id=phase1_id, payload={"ok": True})

    # Reset repo to force overlap/handoff usage
    exec_in_container(container, "git reset --hard HEAD", workdir=workdir)
    exec_in_container(container, "git clean -fd", workdir=workdir)

    # Phase 2: AgentB (implementation)
    phase2_id = f"coordination:{cfg.coordination_task_id}:AgentB"
    event_logger.emit(
      "task_start",
      run_id=cfg.run_id,
      task_id=phase2_id,
      payload={"mode": cfg.mode, "topic": topic, "workdir": workdir},
    )

    wc_session_b = None
    wc_guidance_text_b = ""
    if cfg.mode in ("tools", "tools_guided"):
      from tests.benchmarks.scripts.wc_text_tools import WcToolSession
      from tests.benchmarks.wcbench.wc_tools import WcToolAdapter

      base_b = WcToolSession(
        threads_dir=threads_dir,
        default_topic=topic,
        code_path=cfg.wc_code_path,
        tier_ceiling=cfg.wc_tier_ceiling,
        max_calls=cfg.wc_max_calls,
        token_budget=cfg.wc_token_budget,
        allow_write=False,
      )
      wc_session_b = WcToolAdapter(session=base_b, event_logger=event_logger, run_id=cfg.run_id, task_id=phase2_id)
      if cfg.mode == "tools_guided" and cfg.wc_guidance_file and cfg.wc_guidance_file.exists():
        wc_guidance_text_b = cfg.wc_guidance_file.read_text(encoding="utf-8")

    phase2_statement = (
      "You are AgentB. You are resuming work after AgentA handed off notes.\n\n"
      "First, read the handoff note from Watercooler (recommended: wc-read-thread custom-bench-org-knowledge).\n"
      "Then implement the fix with minimal changes and run the specified tests.\n\n"
      f"Task:\n{target['problem_statement']}\n"
    )

    agent_b = backend.run(
      problem_statement=phase2_statement,
      model_name=cfg.model,
      max_steps=max(cfg.max_steps, 20),
      cost_limit=max(cfg.cost_limit, 0.25),
      knowledge_context="",
      wc_session=wc_session_b,
      wc_guidance_text=wc_guidance_text_b,
      workdir=workdir,
    )

    test_exit, test_out = exec_in_container(container, test_cmd, workdir=workdir)
    ok = test_exit == 0

    run_summary.tasks.append(
      TaskSummary(
        task_id=phase2_id,
        ok=ok,
        mode=cfg.mode,
        cost=float(agent_b.total_cost),
        steps=int(agent_b.steps),
        wc_commands=int(agent_b.raw.get("wc_commands", 0) or 0),
        wc_tools_used=dict(agent_b.raw.get("wc_tools_used", {}) or {}),
        wc_entry_ids_returned=list(agent_b.raw.get("wc_entry_ids_returned", []) or []),
        bash_commands=int(agent_b.raw.get("bash_commands", 0) or 0),
        test_runs=int(agent_b.raw.get("test_runs", 0) or 0),
        details={"test_command": test_cmd, "test_output": test_out},
      )
    )

    event_logger.emit(
      "test_result",
      run_id=cfg.run_id,
      task_id=phase2_id,
      payload={"command": test_cmd, "exit_code": test_exit, "passed": ok, "output": test_out[:8000]},
    )
    event_logger.emit("task_end", run_id=cfg.run_id, task_id=phase2_id, payload={"ok": ok})

  finally:
    try:
      container.stop(timeout=5)
    except Exception:
      pass
    try:
      container.remove(force=True)
    except Exception:
      pass

