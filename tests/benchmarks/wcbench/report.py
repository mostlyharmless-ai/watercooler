from __future__ import annotations

from pathlib import Path

from tests.benchmarks.wcbench.aggregate import derive_metrics
from tests.benchmarks.wcbench.run_layout import RunLayout
from tests.benchmarks.wcbench.summary import RunSummary


def _fmt_tool_counts(counts: dict[str, int], *, limit: int = 10) -> str:
  items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
  if not items:
    return "none"
  return ", ".join(f"{k}={v}" for k, v in items)


def write_report(layout: RunLayout, summary: RunSummary) -> None:
  derived = derive_metrics(layout.events_path)

  lines: list[str] = []
  lines.append(f"## wcbench report: `{summary.run_id}`")
  lines.append("")
  lines.append(f"- **track**: `{summary.track}`")
  lines.append(f"- **mode**: `{summary.mode}`")
  lines.append(f"- **model**: `{summary.model}`")
  lines.append(f"- **elapsed_seconds**: `{summary.elapsed_seconds:.2f}`")
  lines.append(f"- **tasks_passed**: `{sum(1 for t in summary.tasks if t.ok)}/{len(summary.tasks)}`")
  lines.append(f"- **tests_passed/failed**: `{derived.tests_passed}/{derived.tests_failed}`")
  lines.append("")
  lines.append("### Tooling + trace metrics")
  lines.append("")
  lines.append(f"- **event_types**: {_fmt_tool_counts(derived.event_type_counts)}")
  lines.append(f"- **tool_calls**: {_fmt_tool_counts(derived.tool_call_counts)}")
  lines.append("")
  lines.append("### Memory metrics (T2/T3)")
  lines.append("")
  lines.append(f"- **t2_stale_fact_rate**: `{derived.t2_stale_fact_rate}`")
  lines.append(f"- **t3_provenance_resolve_rate**: `{derived.t3_provenance_resolve_rate}`")
  lines.append(f"- **t3_source_id_coverage**: `{derived.t3_source_id_coverage}`")
  lines.append("")
  lines.append("### Per-task summary")
  lines.append("")

  for t in summary.tasks:
    lines.append(f"#### `{t.task_id}`")
    lines.append("")
    lines.append(f"- **ok**: `{t.ok}`")
    lines.append(f"- **steps**: `{t.steps}`")
    lines.append(f"- **cost**: `{t.cost}`")
    lines.append(f"- **bash_commands**: `{t.bash_commands}`")
    lines.append(f"- **wc_commands**: `{t.wc_commands}`")
    lines.append(f"- **wc_tools_used**: {_fmt_tool_counts(t.wc_tools_used)}")
    if t.wc_entry_ids_returned:
      lines.append(f"- **wc_entry_ids_returned(sample)**: `{', '.join(t.wc_entry_ids_returned[:5])}`")
    tool_calls = derived.per_task_tool_calls.get(t.task_id, {})
    lines.append(f"- **infra/tool_call_counts**: {_fmt_tool_counts(tool_calls)}")
    lines.append("")

  layout.report_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

