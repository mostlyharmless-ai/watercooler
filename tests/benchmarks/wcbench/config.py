from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, Sequence


WcbenchMode = Literal["baseline", "inject", "tools", "tools_guided"]
WcTierCeiling = Literal["T1", "T2", "T3"]
Track = Literal["custom", "swebench", "coordination", "memory_qa", "agent_value"]


@dataclass(frozen=True)
class RunConfig:
  run_id: str
  track: Track

  # Agent/model
  model: str = "minimax/MiniMax-M2.5"
  mode: WcbenchMode = "baseline"
  max_steps: int = 25
  cost_limit: float = 0.50

  # Watercooler tools
  wc_tier_ceiling: WcTierCeiling = "T1"
  wc_max_calls: int = 8
  wc_token_budget: int = 1400
  wc_guidance_file: Optional[Path] = None
  wc_code_path: Optional[Path] = None

  # Track-specific
  custom_tasks_path: Optional[Path] = None
  custom_image_tag: str = "watercooler-custom-bench:latest"
  custom_repo_dir: Optional[Path] = None
  custom_only_task_ids: Optional[Sequence[str]] = None

  swebench_dataset: str = "SWE-bench/SWE-bench_Lite"
  swebench_split: str = "test"
  swebench_instance_ids: Optional[Sequence[str]] = None
  swebench_max_instances: Optional[int] = None
  swebench_wc_pack: Optional[Path] = None
  swebench_eval_only: bool = False

  coordination_task_id: str = "multi-hop-with-citations"

  # Agent value track
  agent_value_tasks_path: Optional[Path] = None
  agent_value_only_task_ids: Optional[Sequence[str]] = None
  agent_value_site_repo: str = "https://github.com/mostlyharmless-ai/watercooler-site.git"
  # Git ref for the orphan branch clone.  Accepts a branch name or tag.
  # Pin to a tag for reproducible runs; bump when thread data evolves.
  agent_value_threads_ref: str = "agent-value-bench/v1"
  # Pin an image/tag to control the exact watercooler-site code under /repo.
  agent_value_image: str = "wcbench-agent-base:wc-site-v1"

  # Output
  output_root: Path = Path("logs")

