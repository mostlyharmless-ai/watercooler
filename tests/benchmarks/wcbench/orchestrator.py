from __future__ import annotations

import time
import os
import re
from pathlib import Path

from tests.benchmarks.wcbench.config import RunConfig
from tests.benchmarks.wcbench.events import EventLogger
from tests.benchmarks.wcbench.run_layout import make_run_layout
from tests.benchmarks.wcbench.summary import RunSummary


_COMPOSE_PROJECT_RE = re.compile(r"[^a-z0-9_-]+")


def _sanitize_compose_project_name(run_id: str) -> str:
  """Docker compose project names must be [a-z0-9][a-z0-9_-]*."""
  s = (run_id or "").strip().lower()
  s = _COMPOSE_PROJECT_RE.sub("-", s)
  s = re.sub(r"-+", "-", s).strip("-_")
  if not s or not s[0].isalnum():
    s = f"wcbench-{s}" if s else "wcbench"
  return s[:63]


def run_wcbench(cfg: RunConfig) -> None:
  """Run a benchmark suite using a stable run layout + JSONL events."""
  layout = make_run_layout(cfg.output_root, cfg.run_id)
  logger = EventLogger(layout.events_path)

  started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
  t0 = time.time()
  compose_spec = None
  if cfg.wc_tier_ceiling in ("T2", "T3"):
    from tests.benchmarks.wcbench.infra import (
      ComposeSpec,
      choose_free_local_port,
      compose_up,
      wait_for_service_healthy,
    )

    compose_file = Path("tests/benchmarks/infra/docker-compose.memory.yml").resolve()
    falkor_port = choose_free_local_port([int(os.environ.get("FALKORDB_PORT", "6379")), 16379, 26379])
    os.environ["FALKORDB_PORT"] = str(falkor_port)
    compose_spec = ComposeSpec(
      compose_file=compose_file,
      project_name=_sanitize_compose_project_name(f"wcbench-{cfg.run_id}"),
      env={"FALKORDB_PORT": str(falkor_port)},
    )
    logger.emit(
      "tool_call",
      run_id=cfg.run_id,
      payload={"tool": "docker compose up", "compose_file": str(compose_file)},
    )
    compose_up(compose_spec)
    wait_for_service_healthy(compose_spec, "falkordb", timeout_seconds=120)

  summary = RunSummary(
    run_id=cfg.run_id,
    track=cfg.track,
    model=cfg.model,
    mode=cfg.mode,
    started_at=started_at,
  )

  try:
    logger.emit(
      "run_start",
      run_id=cfg.run_id,
      payload={
        "track": cfg.track,
        "mode": cfg.mode,
        "model": cfg.model,
        "wc_tier_ceiling": cfg.wc_tier_ceiling,
        "wc_max_calls": cfg.wc_max_calls,
        "wc_token_budget": cfg.wc_token_budget,
      },
    )

    if cfg.track == "custom":
      from tests.benchmarks.wcbench.tracks.custom import run_custom_track

      run_custom_track(cfg, layout=layout, event_logger=logger, run_summary=summary)
    elif cfg.track == "swebench":
      from tests.benchmarks.wcbench.tracks.swebench import run_swebench_track

      run_swebench_track(cfg, layout=layout, event_logger=logger, run_summary=summary)
    elif cfg.track == "coordination":
      from tests.benchmarks.wcbench.tracks.coordination import run_coordination_track

      run_coordination_track(cfg, layout=layout, event_logger=logger, run_summary=summary)
    elif cfg.track == "memory_qa":
      from tests.benchmarks.wcbench.tracks.memory_qa import run_memory_qa_track

      run_memory_qa_track(cfg, layout=layout, event_logger=logger, run_summary=summary)
    elif cfg.track == "agent_value":
      from tests.benchmarks.wcbench.tracks.agent_value import run_agent_value_track

      run_agent_value_track(cfg, layout=layout, event_logger=logger, run_summary=summary)
    else:
      raise SystemExit(f"Track not implemented yet: {cfg.track}")

    summary.elapsed_seconds = time.time() - t0
    summary.ended_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    summary.write_json(layout.summary_path)
    try:
      from tests.benchmarks.wcbench.report import write_report

      write_report(layout, summary)
    except Exception:
      pass

    logger.emit(
      "run_end",
      run_id=cfg.run_id,
      payload={
        "elapsed_seconds": summary.elapsed_seconds,
        "passed": sum(1 for t in summary.tasks if t.ok),
        "total_tasks": len(summary.tasks),
        "summary_path": str(layout.summary_path),
        "report_path": str(layout.report_path),
      },
    )
  finally:
    if compose_spec is not None:
      from tests.benchmarks.wcbench.infra import compose_down

      try:
        logger.emit(
          "tool_call",
          run_id=cfg.run_id,
          payload={"tool": "docker compose down"},
        )
      except Exception:
        pass
      try:
        compose_down(compose_spec)
      except Exception:
        pass

