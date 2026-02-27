from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class ComposeSpec:
  compose_file: Path
  project_name: str
  env: dict[str, str]


def _run(cmd: list[str], *, cwd: Optional[Path] = None, env: Optional[dict[str, str]] = None) -> str:
  merged_env = os.environ.copy()
  if env:
    merged_env.update(env)
  p = subprocess.run(
    cmd,
    cwd=str(cwd) if cwd else None,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    env=merged_env,
    check=False,
  )
  if p.returncode != 0:
    raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(cmd)}\n\n{p.stdout}")
  return p.stdout


def _docker_compose_cmd(spec: ComposeSpec) -> list[str]:
  # Prefer modern `docker compose` plugin.
  return ["docker", "compose", "-f", str(spec.compose_file), "-p", spec.project_name]


def compose_up(spec: ComposeSpec) -> None:
  _run(_docker_compose_cmd(spec) + ["up", "-d", "--remove-orphans"], env=spec.env)


def compose_down(spec: ComposeSpec) -> None:
  _run(_docker_compose_cmd(spec) + ["down", "--remove-orphans", "--volumes"], env=spec.env)


def compose_ps_json(spec: ComposeSpec) -> list[dict[str, Any]]:
  out = _run(_docker_compose_cmd(spec) + ["ps", "--format", "json"], env=spec.env)
  out = (out or "").strip()
  if not out:
    return []

  # docker compose versions vary:
  # - some emit a JSON array
  # - some emit one JSON object per line
  try:
    parsed = json.loads(out)
    if isinstance(parsed, list):
      return [p for p in parsed if isinstance(p, dict)]
    if isinstance(parsed, dict):
      return [parsed]
  except Exception:
    pass

  rows: list[dict[str, Any]] = []
  for line in out.splitlines():
    line = line.strip()
    if not line:
      continue
    try:
      obj = json.loads(line)
      if isinstance(obj, dict):
        rows.append(obj)
    except Exception:
      continue
  return rows


def choose_free_local_port(preferred: list[int]) -> int:
  """Pick the first available localhost TCP port from a list."""
  for port in preferred:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
      s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
      s.bind(("127.0.0.1", port))
      return port
    except OSError:
      continue
    finally:
      try:
        s.close()
      except Exception:
        pass
  raise RuntimeError(f"No free port found in preferred set: {preferred}")


def wait_for_service_healthy(spec: ComposeSpec, service: str, *, timeout_seconds: int = 120) -> None:
  """Wait for docker compose service to report healthy/running."""
  deadline = time.time() + timeout_seconds
  last = ""
  while time.time() < deadline:
    rows = compose_ps_json(spec)
    for r in rows:
      if r.get("Service") != service:
        continue
      # Example fields: State, Health, Status (varies by docker versions)
      health = (r.get("Health") or "").lower()
      state = (r.get("State") or "").lower()
      status = (r.get("Status") or "").lower()
      last = f"state={state} health={health} status={status}"
      if "healthy" in health:
        return
      if state == "running" and "healthy" not in health and "unhealthy" not in health:
        # Some docker versions omit Health; accept running once it stabilizes.
        return
    time.sleep(1.0)
  raise TimeoutError(f"Timed out waiting for {service} to become healthy ({last})")

