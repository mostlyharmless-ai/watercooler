"""wcbench: Watercooler-centric benchmark orchestration and logging.

This package provides:
- a stable on-disk run layout under logs/<run_id>/
- JSONL event logging for agent/tool traces
- track runners (custom tasks, SWE-bench compatibility, coordination tests)

The primary goal is to measure incremental Watercooler value by holding the
agent runtime fixed and varying only Watercooler access / tier ceilings /
guidance.
"""

