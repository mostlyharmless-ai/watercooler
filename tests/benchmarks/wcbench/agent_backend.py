from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol


@dataclass(frozen=True)
class AgentRunResult:
  """Normalized agent run result for wcbench tracks."""

  model_patch: str
  steps: int
  total_cost: float
  metrics: dict[str, Any]
  raw: dict[str, Any]


class AgentBackend(Protocol):
  """Backend interface for running an agent against a task."""

  def run(
    self,
    *,
    problem_statement: str,
    model_name: str,
    max_steps: int,
    cost_limit: float,
    workdir: str,
    knowledge_context: str,
    wc_session: Any,
    wc_guidance_text: str,
  ) -> AgentRunResult: ...


@dataclass
class RunAgentBackend:
  """Adapter over the existing `run_agent()` implementation.

  This keeps the current proven behavior while allowing wcbench tracks to
  standardize how they invoke agents.
  """

  container: Any

  def run(
    self,
    *,
    problem_statement: str,
    model_name: str,
    max_steps: int,
    cost_limit: float,
    workdir: str,
    knowledge_context: str,
    wc_session: Any,
    wc_guidance_text: str,
  ) -> AgentRunResult:
    from tests.benchmarks.scripts.run_swebench import run_agent

    raw = run_agent(
      container=self.container,
      problem_statement=problem_statement,
      model_name=model_name,
      max_steps=max_steps,
      cost_limit=cost_limit,
      knowledge_context=knowledge_context,
      wc_session=wc_session,
      wc_guidance_text=wc_guidance_text,
      workdir=workdir,
    )

    metrics = dict(raw.get("metrics", {}) or {})
    return AgentRunResult(
      model_patch=str(raw.get("model_patch", "") or raw.get("patch", "") or ""),
      steps=int(raw.get("steps", 0) or 0),
      total_cost=float(raw.get("total_cost", 0.0) or raw.get("cost", 0.0) or 0.0),
      metrics=metrics,
      raw=dict(raw),
    )

