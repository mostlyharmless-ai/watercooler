"""Self-contained agent runner for the agent_value benchmark.

This script runs **inside** a Docker container launched by the harness
(``agent_value.py``).  It is bind-mounted at ``/runner/container_runner.py``
and invoked as::

    python /runner/container_runner.py /output/runner_config.json

The container image (``wcbench-agent-base``) contains the watercooler-site
codebase at ``/repo``.  The harness writes a JSON config to the bind-mounted
``/output`` volume before exec-ing this script.  Results are written back
to ``/output/result.json`` so the harness can read them after the container
stops.

For **tools** mode, the orphan-branch thread data is bind-mounted at
``/data/threads`` and the MCP server is configured with
``WATERCOOLER_DIR=/data/threads``.

No imports from ``agent_value.py`` — this module is fully standalone.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Regex matching tools the agent can use: base tools + watercooler MCP tools.
# filter_tools_regex applies to ALL tools (not just MCP), so we must include
# the built-in TerminalTool and FileEditorTool names too.
_WC_TOOLS_REGEX = (
  "(terminal|file_editor|str_replace_editor"
  "|watercooler_(search|smart_query|read_thread|get_thread_entry"
  "|list_thread_entries|list_threads))"
)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _load_config(config_path: str) -> dict[str, Any]:
  """Read and validate the runner config JSON written by the harness."""
  path = Path(config_path)
  if not path.exists():
    raise FileNotFoundError(f"Runner config not found: {config_path}")
  cfg = json.loads(path.read_text(encoding="utf-8"))
  for key in ("model", "problem_statement", "mode", "workspace_dir", "result_path"):
    if key not in cfg:
      raise ValueError(f"Missing required config key: {key}")
  return cfg


# ---------------------------------------------------------------------------
# OpenHands helpers
# ---------------------------------------------------------------------------


def _make_llm(model: str) -> Any:
  """Create an OpenHands LLM instance.

  API keys are expected in the environment (passed via container env vars).
  LiteLLM model strings are used directly (e.g. ``minimax/MiniMax-M2.5``).
  """
  from openhands.sdk import LLM

  return LLM(model=model)


def _make_agent(
  llm: Any,
  *,
  mode: str,
  threads_dir: Optional[str] = None,
) -> Any:
  """Create an OpenHands Agent.

  Args:
    llm: LLM instance.
    mode: ``"baseline"`` or ``"tools"``.
    threads_dir: Path to seeded thread data (for WATERCOOLER_DIR).
      Only used when ``mode == "tools"``.
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

  if mode == "tools" and threads_dir:
    env = {
      "WATERCOOLER_DIR": threads_dir,
      "WATERCOOLER_LOG_LEVEL": "DEBUG",
    }
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
  workspace_dir: str,
  problem_statement: str,
  max_steps: int,
  *,
  transcript_dir: Optional[str] = None,
) -> dict[str, Any]:
  """Run an OpenHands conversation and extract metrics.

  Returns:
    Dict with keys: ok, status, steps, cost, wc_commands, wc_tools_used.
  """
  from openhands.sdk import Conversation

  conv_kwargs: dict[str, Any] = {
    "agent": agent,
    "workspace": workspace_dir,
    "max_iteration_per_run": max_steps,
  }
  if transcript_dir:
    Path(transcript_dir).mkdir(parents=True, exist_ok=True)
    conv_kwargs["persistence_dir"] = transcript_dir

  conversation = Conversation(**conv_kwargs)

  try:
    conversation.send_message(problem_statement)
    conversation.run()

    # Extract cost
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
        event_type_name = type(event).__name__
        event_dict: dict[str, Any] = {"type": event_type_name}

        if event_type_name == "ActionEvent":
          steps += 1
          tool_name = str(getattr(event, "tool_name", ""))
          event_dict["tool_name"] = tool_name

          # Extract tool_call arguments for transcript
          tool_args = ""
          try:
            tc = getattr(event, "tool_call", None)
            if tc is not None:
              tool_args = str(getattr(tc, "arguments", ""))[:4000]
          except Exception:
            pass
          event_dict["tool_args"] = tool_args

          # Extract agent reasoning
          thought_text = ""
          try:
            thought = getattr(event, "thought", None)
            if thought:
              thought_text = " ".join(
                str(getattr(t, "text", "")) for t in thought
              )[:2000]
          except Exception:
            pass
          if thought_text:
            event_dict["thought"] = thought_text

          # Track watercooler tool usage
          if "watercooler" in tool_name.lower():
            wc_commands += 1
            wc_tools_used[tool_name] = wc_tools_used.get(tool_name, 0) + 1

        elif event_type_name == "ObservationEvent":
          tool_name = str(getattr(event, "tool_name", ""))
          event_dict["tool_name"] = tool_name
          obs_content = ""
          is_error = False
          try:
            obs = getattr(event, "observation", None)
            if obs is not None:
              obs_content = str(getattr(obs, "content", ""))[:4000]
            is_error = bool(getattr(event, "is_error", False))
          except Exception:
            pass
          event_dict["observation"] = obs_content
          event_dict["is_error"] = is_error

        events_data.append(event_dict)
    except Exception as exc:
      log.warning("Error extracting events: %s", exc)

    # Write consolidated transcript
    if transcript_dir:
      _write_transcript(
        Path(transcript_dir) / "transcript.jsonl", events_data
      )

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
      "error": str(exc),
    }
  finally:
    try:
      conversation.close()
    except Exception:
      pass


def _write_transcript(path: Path, events_data: list[dict[str, Any]]) -> None:
  """Write events_data as newline-delimited JSON (transcript.jsonl)."""
  try:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
      for entry in events_data:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    log.info("Transcript written: %s (%d events)", path, len(events_data))
  except Exception as exc:
    log.warning("Failed to write transcript %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
  """CLI entry point.  Reads config, runs agent, writes result JSON."""
  logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
  )

  if len(sys.argv) < 2:
    print("Usage: container_runner.py <config.json>", file=sys.stderr)
    sys.exit(1)

  cfg = _load_config(sys.argv[1])

  model = cfg["model"]
  mode = cfg["mode"]
  problem_statement = cfg["problem_statement"]
  workspace_dir = cfg["workspace_dir"]
  result_path = cfg["result_path"]
  max_steps = cfg.get("max_steps", 25)
  threads_dir = cfg.get("threads_dir")  # None for baseline
  transcript_dir = cfg.get("transcript_dir")

  log.info(
    "container_runner: model=%s mode=%s workspace=%s threads=%s",
    model, mode, workspace_dir, threads_dir,
  )

  llm = _make_llm(model)
  agent = _make_agent(llm, mode=mode, threads_dir=threads_dir)

  result = _run_conversation(
    agent=agent,
    workspace_dir=workspace_dir,
    problem_statement=problem_statement,
    max_steps=max_steps,
    transcript_dir=transcript_dir,
  )

  # Write result JSON for the harness to read
  result_file = Path(result_path)
  result_file.parent.mkdir(parents=True, exist_ok=True)
  result_file.write_text(
    json.dumps(result, indent=2, ensure_ascii=False),
    encoding="utf-8",
  )
  log.info("Result written to %s", result_path)


if __name__ == "__main__":
  main()
