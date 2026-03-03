#!/usr/bin/env python3
"""Run the canonical Watercooler benchmark matrix with paired deltas.

This runner enforces the benchmark contract:
- Primary axis: SWE-bench subset with identical instances across 4 modes
  (baseline, inject, tools, tools_guided)
- Secondary axis: coordination-under-overlap A/B (baseline vs tools_guided)
- Validation axis: memory_qa semantic correctness checks (T2/T3)

Every primary metric is reported as baseline + delta for WC variants.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Resolve repo root and src
_REPO = Path(__file__).resolve().parents[3]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


@dataclass
class ModeMetrics:
    mode: str
    instances: int
    submitted: int
    completed: int
    resolved: int
    resolve_rate: float | None
    total_cost: float
    total_steps: int
    total_duplicate_commands: int
    total_bash_commands: int
    total_wc_commands: int
    total_test_runs: int
    retrieval_action_proxy_rate: float | None
    cost_per_resolved: float | None
    steps_per_resolved: float | None
    duplicate_command_rate: float | None
    cross_thread_discovery_rate: float | None
    citation_accuracy: float | None
    context_rehydration_efficiency_delta: float | None
    resolved_over_completed: float | None
    resolved_over_submitted: float | None
    tier_preference_distribution: dict[str, float]
    run_id: str
    output_dir: str


@dataclass
class InstanceCalibrationStats:
    instance_id: str
    repo: str
    seen_runs: int
    resolved_runs: int
    unresolved_runs: int
    resolve_rate: float
    avg_cost: float


def _load_env_pipeline(env: dict[str, str]) -> dict[str, str]:
    if (_REPO / ".env.pipeline").exists():
        for line in (_REPO / ".env.pipeline").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def _run_wcbench(
    *,
    run_id: str,
    track: str,
    output_root: Path,
    mode: str | None = None,
    model: str = "minimax/MiniMax-M2.5",
    swebench_dataset: str = "SWE-bench/SWE-bench_Lite",
    swebench_split: str = "test",
    swebench_instance_ids: list[str] | None = None,
    swebench_max_instances: int | None = None,
    swebench_wc_pack: Path | None = None,
    wc_tier_ceiling: str = "T3",
    wc_max_calls: int = 8,
    wc_token_budget: int = 1400,
    wc_code_path: Path | None = None,
    coordination_task_id: str | None = None,
) -> tuple[int, str]:
    env = _load_env_pipeline(os.environ.copy())
    env["PYTHONPATH"] = str(_SRC)

    cmd: list[str] = [
        sys.executable,
        "-m",
        "tests.benchmarks.wcbench",
        "--track",
        track,
        "--run-id",
        run_id,
        "--output-root",
        str(output_root),
        "--model",
        model,
        "--wc-tier-ceiling",
        wc_tier_ceiling,
        "--wc-max-calls",
        str(wc_max_calls),
        "--wc-token-budget",
        str(wc_token_budget),
    ]
    if wc_code_path is not None:
        cmd += ["--wc-code-path", str(wc_code_path)]
    if mode is not None:
        cmd += ["--mode", mode]

    if track == "swebench":
        cmd += ["--swebench-dataset", swebench_dataset, "--swebench-split", swebench_split]
        if swebench_instance_ids:
            cmd += ["--swebench-instance-ids", *swebench_instance_ids]
        if swebench_max_instances is not None:
            cmd += ["--swebench-max-instances", str(swebench_max_instances)]
        if mode and mode != "baseline":
            if swebench_wc_pack is None:
                raise ValueError("SWE matrix requires --swebench-wc-pack for non-baseline modes")
            cmd += ["--swebench-wc-pack", str(swebench_wc_pack)]

    if track == "coordination" and coordination_task_id:
        cmd += ["--coordination-task-id", coordination_task_id]

    p = subprocess.run(
        cmd,
        env=env,
        cwd=str(_REPO),
        capture_output=True,
        text=True,
        check=False,
    )
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _extract_resolved_from_reports(swebench_output_dir: Path) -> tuple[int, int]:
    report_paths = list(swebench_output_dir.glob("logs/run_evaluation/**/report.json"))
    resolved = 0
    total = 0
    for rp in report_paths:
        try:
            data = json.loads(rp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            for val in data.values():
                if isinstance(val, dict) and "resolved" in val:
                    total += 1
                    if bool(val.get("resolved")):
                        resolved += 1
    return resolved, total


def _extract_per_instance_resolved(swebench_output_dir: Path) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for rp in swebench_output_dir.glob("logs/run_evaluation/**/report.json"):
        try:
            data = json.loads(rp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for instance_id, value in data.items():
            if isinstance(value, dict) and "resolved" in value:
                out[str(instance_id)] = bool(value.get("resolved"))
    return out


def _iter_baseline_summaries(logs_root: Path) -> list[Path]:
    candidates = list(logs_root.glob("**/summary.json"))
    baseline_paths: list[Path] = []
    for p in candidates:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and str(data.get("mode", "")) == "baseline":
            baseline_paths.append(p)
    return baseline_paths


def _collect_calibration_stats(logs_root: Path) -> dict[str, InstanceCalibrationStats]:
    acc: dict[str, dict[str, Any]] = {}
    for summary_path in _iter_baseline_summaries(logs_root):
        try:
            swe = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(swe, dict):
            continue
        results = list(swe.get("results", []))
        resolved_map = _extract_per_instance_resolved(summary_path.parent)
        for r in results:
            if not isinstance(r, dict):
                continue
            instance_id = str(r.get("instance_id") or "")
            if not instance_id:
                continue
            repo = str(instance_id.split("-", 1)[0]) if "-" in instance_id else str(r.get("repo") or "")
            row = acc.setdefault(
                instance_id,
                {
                    "repo": repo,
                    "seen_runs": 0,
                    "resolved_runs": 0,
                    "unresolved_runs": 0,
                    "total_cost": 0.0,
                    "cost_count": 0,
                },
            )
            row["seen_runs"] += 1
            resolved = resolved_map.get(instance_id)
            if resolved is True:
                row["resolved_runs"] += 1
            elif resolved is False:
                row["unresolved_runs"] += 1

            try:
                row["total_cost"] += float(r.get("cost", 0.0) or 0.0)
                row["cost_count"] += 1
            except Exception:
                pass

    out: dict[str, InstanceCalibrationStats] = {}
    for instance_id, row in acc.items():
        seen_runs = int(row["seen_runs"])
        resolved_runs = int(row["resolved_runs"])
        unresolved_runs = int(row["unresolved_runs"])
        denom = resolved_runs + unresolved_runs
        resolve_rate = (float(resolved_runs) / float(denom)) if denom > 0 else 0.0
        avg_cost = (float(row["total_cost"]) / float(row["cost_count"])) if int(row["cost_count"]) > 0 else 0.0
        out[instance_id] = InstanceCalibrationStats(
            instance_id=instance_id,
            repo=str(row["repo"]),
            seen_runs=seen_runs,
            resolved_runs=resolved_runs,
            unresolved_runs=unresolved_runs,
            resolve_rate=resolve_rate,
            avg_cost=avg_cost,
        )
    return out


def _select_calibrated_subset(
    stats: dict[str, InstanceCalibrationStats], size: int, min_baseline_resolved: int
) -> list[str]:
    if not stats:
        raise ValueError("No baseline historical stats found under logs/.")
    if size < 4:
        raise ValueError("Calibration size must be >= 4.")

    # Prioritize stable candidates with mixed baseline difficulty.
    sorted_stats = sorted(
        stats.values(),
        key=lambda s: (
            -s.seen_runs,
            abs(s.resolve_rate - 0.35),
            s.avg_cost,
            s.instance_id,
        ),
    )
    positives = [s for s in sorted_stats if s.resolve_rate > 0.0]
    zeros = [s for s in sorted_stats if s.resolve_rate <= 0.0]
    mediums = [s for s in sorted_stats if 0.0 < s.resolve_rate < 0.6]
    highs = [s for s in sorted_stats if s.resolve_rate >= 0.6]

    chosen: list[InstanceCalibrationStats] = []
    # Seed with positives so baseline has a nonzero floor.
    for pool in (mediums, highs, positives):
        for s in pool:
            if s not in chosen:
                chosen.append(s)
            if len([x for x in chosen if x.resolve_rate > 0]) >= min_baseline_resolved:
                break
        if len([x for x in chosen if x.resolve_rate > 0]) >= min_baseline_resolved:
            break

    # Fill remaining slots with mix of unresolved and medium tasks.
    mixed_order = []
    for i in range(max(len(zeros), len(mediums), len(highs))):
        if i < len(zeros):
            mixed_order.append(zeros[i])
        if i < len(mediums):
            mixed_order.append(mediums[i])
        if i < len(highs):
            mixed_order.append(highs[i])
    if not mixed_order:
        mixed_order = sorted_stats

    for s in mixed_order:
        if len(chosen) >= size:
            break
        if s not in chosen:
            chosen.append(s)

    if len(chosen) < size:
        for s in sorted_stats:
            if len(chosen) >= size:
                break
            if s not in chosen:
                chosen.append(s)

    chosen = chosen[:size]
    if len([x for x in chosen if x.resolve_rate > 0]) < min_baseline_resolved:
        raise ValueError(
            f"Unable to satisfy calibration floor: need {min_baseline_resolved} historically solvable tasks."
        )
    return [s.instance_id for s in chosen]


def _derive_task_tags(problem_statement: str, repo: str) -> list[str]:
    text = problem_statement.lower()
    tags: list[str] = []
    if "regression" in text:
        tags.append("regression")
    if "performance" in text or "slow" in text:
        tags.append("performance")
    if "crash" in text or "exception" in text or "error" in text:
        tags.append("error-handling")
    if "test" in text or "failing" in text:
        tags.append("test-fix")
    if "__slots__" in text or "attribute" in text:
        tags.append("data-model")
    if "django" in repo:
        tags.append("framework-django")
    if "sympy" in repo:
        tags.append("framework-sympy")
    if "scikit-learn" in repo:
        tags.append("framework-sklearn")
    return tags[:5]


def _load_instance_context(
    *, dataset: str, split: str, instance_ids: list[str]
) -> dict[str, dict[str, Any]]:
    if not instance_ids:
        return {}
    try:
        from datasets import load_dataset
    except Exception as e:
        return {
            iid: {"instance_id": iid, "repo": iid.split("-", 1)[0], "summary": "", "tags": [], "error": str(e)}
            for iid in instance_ids
        }

    ds = load_dataset(dataset, split=split)
    id_set = set(instance_ids)
    rows = [row for row in ds if str(row.get("instance_id")) in id_set]
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        iid = str(row.get("instance_id"))
        problem = str(row.get("problem_statement") or "")
        summary = " ".join(problem.strip().split())[:240]
        repo = str(row.get("repo") or iid.split("-", 1)[0])
        out[iid] = {
            "instance_id": iid,
            "repo": repo,
            "summary": summary,
            "tags": _derive_task_tags(problem, repo),
        }
    for iid in instance_ids:
        if iid not in out:
            out[iid] = {
                "instance_id": iid,
                "repo": iid.split("-", 1)[0],
                "summary": "",
                "tags": [],
                "error": "not found in dataset split",
            }
    return out


def _build_interpretation(
    swe_modes: dict[str, ModeMetrics], instance_context: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    baseline = swe_modes["baseline"]
    practical: list[str] = []
    caveats: list[str] = []
    if baseline.resolved == 0:
        caveats.append(
            "Baseline resolved zero tasks; baseline-normalized per-resolved deltas are undefined in this run."
        )
    else:
        practical.append(
            f"Baseline resolved {baseline.resolved}/{baseline.instances}; per-resolved efficiency deltas are fully comparable."
        )
    if baseline.completed < baseline.submitted:
        caveats.append(
            f"Baseline evaluator completeness is {baseline.completed}/{baseline.submitted}; "
            "interpretation should be completion-adjusted."
        )

    for mode in ("inject", "tools", "tools_guided"):
        mm = swe_modes.get(mode)
        if mm is None:
            continue
        solve_delta = mm.resolved - baseline.resolved
        practical.append(
            f"{mode}: solved {mm.resolved}/{mm.instances} ({'+' if solve_delta >= 0 else ''}{solve_delta} vs baseline), "
            f"cost={mm.total_cost:.4f} ({mm.total_cost - baseline.total_cost:+.4f} vs baseline)."
        )
        if mm.retrieval_action_proxy_rate is not None:
            practical.append(
                f"{mode}: retrieval-to-action={mm.retrieval_action_proxy_rate:.3f}, meaning most WC retrievals led to edits/tests."
            )
        if mm.duplicate_command_rate is not None and baseline.duplicate_command_rate is not None:
            ddup = mm.duplicate_command_rate - baseline.duplicate_command_rate
            practical.append(
                f"{mode}: duplicate-command-rate={mm.duplicate_command_rate:.3f} ({ddup:+.3f}); lower values imply less rework looping."
            )
        if mm.completed < mm.submitted:
            caveats.append(
                f"{mode} evaluator completeness {mm.completed}/{mm.submitted}; headline solve-rate may be overstated."
            )

    repos = sorted({ctx.get("repo", "") for ctx in instance_context.values() if ctx.get("repo")})
    if repos:
        practical.append(
            "Task mix spans repos: " + ", ".join(repos) + "; this reduces single-repo bias but remains a small-sample signal."
        )
    caveats.append("Sample size is small; treat this as directional evidence, not final benchmark evidence.")
    return {"practical_summary": practical, "caveats": caveats}


def _summarize_swebench_mode(run_root: Path, mode: str, run_id: str) -> ModeMetrics:
    swebench_summary_path = run_root / "artifacts" / "swebench" / "summary.json"
    if not swebench_summary_path.exists():
        raise FileNotFoundError(f"Missing swebench summary: {swebench_summary_path}")
    swe = json.loads(swebench_summary_path.read_text(encoding="utf-8"))
    results = list(swe.get("results", []))
    agg = dict(swe.get("aggregate_metrics", {}) or {})
    instances = int(swe.get("instances", len(results)) or len(results))
    submitted = instances
    resolved, resolved_total = _extract_resolved_from_reports(run_root / "artifacts" / "swebench")
    completed = int(resolved_total)
    resolve_rate = (resolved / resolved_total) if resolved_total > 0 else None

    total_steps = sum(int(r.get("steps", 0) or 0) for r in results if isinstance(r, dict))
    total_cost = float(swe.get("total_cost", 0.0) or 0.0)
    total_duplicates = int(agg.get("total_duplicate_commands", 0) or 0)
    total_bash = int(agg.get("total_bash_commands", 0) or 0)
    total_wc = int(agg.get("total_wc_commands", 0) or 0)
    total_tests = int(agg.get("total_test_runs", 0) or 0)

    wc_instances = 0
    retrieval_hits = 0
    for r in results:
        if not isinstance(r, dict):
            continue
        metrics = dict(r.get("metrics", {}) or {})
        wc_cmds = int(metrics.get("wc_commands", 0) or 0)
        if wc_cmds > 0:
            wc_instances += 1
            if int(metrics.get("file_edits", 0) or 0) > 0 or int(metrics.get("test_runs", 0) or 0) > 0:
                retrieval_hits += 1
    retrieval_action_proxy = (retrieval_hits / wc_instances) if wc_instances > 0 else None

    cost_per_resolved = (total_cost / resolved) if resolved > 0 else None
    steps_per_resolved = (total_steps / resolved) if resolved > 0 else None
    duplicate_rate = (total_duplicates / total_bash) if total_bash > 0 else None
    from tests.benchmarks.wcbench.aggregate import derive_metrics
    derived = derive_metrics(run_root / "events.jsonl")

    return ModeMetrics(
        mode=mode,
        instances=instances,
        submitted=submitted,
        completed=completed,
        resolved=resolved,
        resolve_rate=resolve_rate,
        total_cost=total_cost,
        total_steps=total_steps,
        total_duplicate_commands=total_duplicates,
        total_bash_commands=total_bash,
        total_wc_commands=total_wc,
        total_test_runs=total_tests,
        retrieval_action_proxy_rate=retrieval_action_proxy,
        cost_per_resolved=cost_per_resolved,
        steps_per_resolved=steps_per_resolved,
        duplicate_command_rate=duplicate_rate,
        cross_thread_discovery_rate=derived.cross_thread_discovery_rate,
        citation_accuracy=derived.citation_accuracy,
        context_rehydration_efficiency_delta=derived.context_rehydration_efficiency_delta,
        resolved_over_completed=derived.resolved_over_completed,
        resolved_over_submitted=derived.resolved_over_submitted,
        tier_preference_distribution=dict(derived.tier_preference_distribution),
        run_id=run_id,
        output_dir=str(run_root),
    )


def _delta(curr: float | None, base: float | None) -> float | None:
    if curr is None or base is None:
        return None
    return curr - base


def _fmt(v: float | int | None, digits: int = 4) -> str:
    if v is None:
        return "N/A"
    if isinstance(v, int):
        return str(v)
    return f"{v:.{digits}f}"


def _write_report(
    output_dir: Path,
    *,
    swe_modes: dict[str, ModeMetrics],
    coordination: dict[str, dict] | None,
    memory_qa: dict | None,
    instance_ids: list[str],
    instance_context: dict[str, dict[str, Any]],
    interpretation: dict[str, Any],
    calibration: dict[str, Any] | None,
) -> None:
    baseline = swe_modes.get("baseline")
    if baseline is None:
        raise ValueError("baseline mode results are required for the report")
    lines: list[str] = [
        "# Watercooler Benchmark Report (Intent-Strict)",
        "",
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        "Primary contract: SWE-bench subset matrix with paired deltas vs baseline.",
        "",
        "---",
        "",
        "## 1. Primary axis — SWE subset paired comparison",
        "",
        f"- baseline run_id: `{baseline.run_id}`",
        f"- instances: `{baseline.instances}`",
        f"- evaluator completion: `{baseline.completed}/{baseline.submitted}`",
        "",
        "### Baseline (reference)",
        "",
        f"- resolved: `{baseline.resolved}`",
        f"- resolve_rate: `{_fmt(baseline.resolve_rate)}`",
        f"- resolved_over_completed: `{_fmt(baseline.resolved_over_completed)}`",
        f"- resolved_over_submitted: `{_fmt(baseline.resolved_over_submitted)}`",
        f"- total_cost: `{_fmt(baseline.total_cost)}`",
        f"- cost_per_resolved: `{_fmt(baseline.cost_per_resolved)}`",
        f"- steps_per_resolved: `{_fmt(baseline.steps_per_resolved)}`",
        f"- duplicate_command_rate: `{_fmt(baseline.duplicate_command_rate)}`",
        f"- retrieval_to_action_proxy: `{_fmt(baseline.retrieval_action_proxy_rate)}`",
        f"- cross_thread_discovery_rate: `{_fmt(baseline.cross_thread_discovery_rate)}`",
        f"- citation_accuracy: `{_fmt(baseline.citation_accuracy)}`",
        f"- context_rehydration_efficiency_delta: `{_fmt(baseline.context_rehydration_efficiency_delta)}`",
        f"- tier_preference_distribution: `{baseline.tier_preference_distribution or {}}`",
        "",
    ]
    if baseline.completed < baseline.submitted:
        lines.extend(
            [
                "> [!WARNING]",
                "> Evaluator completeness is below 100%. Use completion-adjusted rates (`resolved_over_completed` and `resolved_over_submitted`) for headline interpretation.",
                "",
            ]
        )

    for mode in ("inject", "tools", "tools_guided"):
        mm = swe_modes.get(mode)
        if mm is None:
            continue
        lines.extend(
            [
                f"### {mode}",
                "",
                f"- run_id: `{mm.run_id}`",
                f"- evaluator completion: `{mm.completed}/{mm.submitted}`",
                f"- resolved: `{mm.resolved}` (delta `{_fmt(_delta(mm.resolved, baseline.resolved), 0)}`)",
                f"- resolve_rate: `{_fmt(mm.resolve_rate)}` (delta `{_fmt(_delta(mm.resolve_rate, baseline.resolve_rate))}`)",
                f"- resolved_over_completed: `{_fmt(mm.resolved_over_completed)}` (delta `{_fmt(_delta(mm.resolved_over_completed, baseline.resolved_over_completed))}`)",
                f"- resolved_over_submitted: `{_fmt(mm.resolved_over_submitted)}` (delta `{_fmt(_delta(mm.resolved_over_submitted, baseline.resolved_over_submitted))}`)",
                f"- total_cost: `{_fmt(mm.total_cost)}` (delta `{_fmt(_delta(mm.total_cost, baseline.total_cost))}`)",
                f"- cost_per_resolved: `{_fmt(mm.cost_per_resolved)}` (delta `{_fmt(_delta(mm.cost_per_resolved, baseline.cost_per_resolved))}`)",
                f"- steps_per_resolved: `{_fmt(mm.steps_per_resolved)}` (delta `{_fmt(_delta(mm.steps_per_resolved, baseline.steps_per_resolved))}`)",
                f"- duplicate_command_rate: `{_fmt(mm.duplicate_command_rate)}` (delta `{_fmt(_delta(mm.duplicate_command_rate, baseline.duplicate_command_rate))}`)",
                f"- retrieval_to_action_proxy: `{_fmt(mm.retrieval_action_proxy_rate)}` (delta `{_fmt(_delta(mm.retrieval_action_proxy_rate, baseline.retrieval_action_proxy_rate))}`)",
                f"- cross_thread_discovery_rate: `{_fmt(mm.cross_thread_discovery_rate)}` (delta `{_fmt(_delta(mm.cross_thread_discovery_rate, baseline.cross_thread_discovery_rate))}`)",
                f"- citation_accuracy: `{_fmt(mm.citation_accuracy)}` (delta `{_fmt(_delta(mm.citation_accuracy, baseline.citation_accuracy))}`)",
                f"- context_rehydration_efficiency_delta: `{_fmt(mm.context_rehydration_efficiency_delta)}` (delta `{_fmt(_delta(mm.context_rehydration_efficiency_delta, baseline.context_rehydration_efficiency_delta))}`)",
                f"- tier_preference_distribution: `{mm.tier_preference_distribution or {}}`",
                "",
            ]
        )

    lines.extend(["---", "", "## 1b. Task context", ""])
    lines.append(
        "These tasks represent concrete bug-fix scenarios from SWE-bench; summaries below describe what each task is asking the agent to do."
    )
    lines.append("")
    for iid in instance_ids:
        ctx = instance_context.get(iid, {})
        summary = str(ctx.get("summary", "") or "")
        repo = str(ctx.get("repo", "") or iid.split("-", 1)[0])
        tags = list(ctx.get("tags", []) or [])
        lines.append(f"- `{iid}` (`{repo}`): {summary or 'No summary available.'}")
        if tags:
            lines.append(f"  tags: `{', '.join(tags)}`")
    lines.append("")

    lines.extend(["## 1c. Practical interpretation", ""])
    for item in interpretation.get("practical_summary", []):
        lines.append(f"- {item}")
    lines.append("")

    lines.extend(["## 1d. Confidence and caveats", ""])
    for item in interpretation.get("caveats", []):
        lines.append(f"- {item}")
    if calibration:
        lines.append(
            f"- Calibration selection: `{calibration.get('selected_count', 0)}` instances, "
            f"target baseline floor `{calibration.get('min_baseline_resolved', 0)}`."
        )
    lines.append("")

    lines.extend(["---", "", "## 2. Secondary axis — coordination-under-overlap (A/B)", ""])
    if coordination:
        b = coordination.get("baseline")
        w = coordination.get("tools_guided")
        if b:
            lines.append(f"- baseline: ok={b.get('ok')} wc_commands={b.get('wc_commands')} test_runs={b.get('test_runs')}")
            lines.append(
                f"  phaseA_steps={b.get('phase_a_steps')} phaseB_steps={b.get('phase_b_steps')} "
                f"handoff_fact_recall={_fmt(b.get('handoff_fact_recall_completeness'))} "
                f"citation_accuracy={_fmt(b.get('citation_accuracy'))}"
            )
        if w:
            lines.append(f"- tools_guided: ok={w.get('ok')} wc_commands={w.get('wc_commands')} test_runs={w.get('test_runs')}")
            lines.append(
                f"  phaseA_steps={w.get('phase_a_steps')} phaseB_steps={w.get('phase_b_steps')} "
                f"handoff_fact_recall={_fmt(w.get('handoff_fact_recall_completeness'))} "
                f"citation_accuracy={_fmt(w.get('citation_accuracy'))}"
            )
        if b and w:
            lines.append(
                f"- wc_command_delta: `{_fmt(float(w.get('wc_commands', 0)) - float(b.get('wc_commands', 0)), 0)}`"
            )
            if b.get("phase_b_steps") is not None and w.get("phase_b_steps") is not None:
                lines.append(
                    f"- phaseB_effort_delta(steps): `{_fmt(float(w.get('phase_b_steps', 0)) - float(b.get('phase_b_steps', 0)), 0)}`"
                )
        lines.append("")
    else:
        lines.append("- status: `skipped`")
        lines.append("")

    lines.extend(["---", "", "## 3. Validation axis — memory_qa (T2/T3 correctness gate)", ""])
    if memory_qa:
        lines.extend(
            [
                f"- status: `{memory_qa.get('status')}`",
                f"- tasks_passed: `{memory_qa.get('tasks_passed')}/{memory_qa.get('tasks_total')}`",
                f"- t2_stale_fact_rate: `{_fmt(memory_qa.get('t2_stale_fact_rate'))}`",
                f"- t3_provenance_resolve_rate: `{_fmt(memory_qa.get('t3_provenance_resolve_rate'))}`",
                f"- t3_source_id_coverage: `{_fmt(memory_qa.get('t3_source_id_coverage'))}`",
                f"- t3_multi_hop_quality_rate: `{_fmt(memory_qa.get('t3_multi_hop_quality_rate'))}`",
                "",
            ]
        )
    else:
        lines.append("- status: `skipped`")
        lines.append("")

    (output_dir / "BENCHMARK_REPORT.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _summarize_coordination(run_root: Path) -> dict:
    summary_path = run_root / "summary.json"
    if not summary_path.exists():
        return {"status": "missing_summary"}
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    tasks = list(data.get("tasks", []))
    if not tasks:
        return {"status": "no_tasks"}
    by_id = {str(t.get("task_id") or ""): t for t in tasks if isinstance(t, dict)}
    phase_a = by_id.get(next((k for k in by_id if k.endswith(":AgentA")), ""), {})
    phase_b = by_id.get(next((k for k in by_id if k.endswith(":AgentB")), ""), {})
    last = tasks[-1]
    phase_a_details = dict(phase_a.get("details", {}) or {})
    phase_b_details = dict(phase_b.get("details", {}) or {})
    return {
        "status": "ok",
        "ok": bool(last.get("ok")),
        "wc_commands": int(last.get("wc_commands", 0) or 0),
        "test_runs": int(last.get("test_runs", 0) or 0),
        "phase_a_steps": int(phase_a.get("steps", 0) or 0),
        "phase_b_steps": int(phase_b.get("steps", 0) or 0),
        "handoff_fact_recall_completeness": phase_a_details.get("handoff_fact_recall_completeness"),
        "citation_accuracy": phase_b_details.get("citation_accuracy"),
        "speed_to_first_correct_patch_test_steps": phase_b_details.get("speed_to_first_correct_patch_test_steps"),
        "run_id": data.get("run_id", ""),
    }


def _run_memory_qa(run_id: str, output_root: Path, wc_code_path: Path) -> dict:
    rc, out = _run_wcbench(
        run_id=run_id,
        track="memory_qa",
        output_root=output_root,
        mode="tools_guided",
        wc_tier_ceiling="T3",
        wc_code_path=wc_code_path,
    )
    run_root = output_root / run_id
    summary_path = run_root / "summary.json"
    if rc != 0 or not summary_path.exists():
        return {"status": "failed", "error": out[-1000:]}
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    tasks = list(data.get("tasks", []))
    passed = sum(1 for t in tasks if t.get("ok"))
    total = len(tasks)
    from tests.benchmarks.wcbench.aggregate import derive_metrics

    derived = derive_metrics(run_root / "events.jsonl")
    return {
        "status": "ok",
        "run_id": run_id,
        "tasks_passed": passed,
        "tasks_total": total,
        "t2_stale_fact_rate": derived.t2_stale_fact_rate,
        "t3_provenance_resolve_rate": derived.t3_provenance_resolve_rate,
        "t3_source_id_coverage": derived.t3_source_id_coverage,
        "t3_multi_hop_quality_rate": derived.t3_multi_hop_quality_rate,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run intent-strict Watercooler benchmark matrix")
    parser.add_argument("--output-dir", type=Path, default=None, help="Root output dir for benchmark artifacts")
    parser.add_argument("--model", type=str, default="minimax/MiniMax-M2.5")
    parser.add_argument("--dataset", type=str, default="SWE-bench/SWE-bench_Lite")
    parser.add_argument("--split", type=str, default="test")
    parser.add_argument("--instance-ids", nargs="+", default=None)
    parser.add_argument("--max-instances", type=int, default=None)
    parser.add_argument("--swebench-wc-pack", type=Path, default=None)
    parser.add_argument("--calibrate", action="store_true", help="Select instance IDs from historical baseline runs")
    parser.add_argument("--calibrate-size", type=int, default=8, help="Number of instances to select when calibrating")
    parser.add_argument(
        "--calibrate-min-baseline-resolved",
        type=int,
        default=2,
        help="Minimum historically solvable instances to include in calibrated subset",
    )
    parser.add_argument(
        "--calibration-source-root",
        type=Path,
        default=_REPO / "logs",
        help="Logs root used to derive calibration statistics",
    )
    parser.add_argument("--wc-tier-ceiling", type=str, default="T3", choices=["T1", "T2", "T3"])
    parser.add_argument("--wc-max-calls", type=int, default=8)
    parser.add_argument("--wc-token-budget", type=int, default=1400)
    parser.add_argument("--wc-code-path", type=Path, default=_REPO)
    parser.add_argument("--skip-coordination", action="store_true")
    parser.add_argument("--skip-memory-qa", action="store_true")
    parser.add_argument("--coordination-task-id", type=str, default="multi-hop-with-citations")
    parser.add_argument(
        "--min-completion-rate",
        type=float,
        default=1.0,
        help="Minimum evaluator completion rate required for headline interpretation (strict default: 1.0).",
    )
    args = parser.parse_args()

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_dir = args.output_dir or (_REPO / "logs" / f"benchmark-{ts}")
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output dir: {output_dir}")

    if args.calibrate and args.instance_ids:
        raise SystemExit("Use either --calibrate or --instance-ids, not both.")

    calibration_payload: dict[str, Any] | None = None
    selected_instance_ids = list(args.instance_ids) if args.instance_ids else []
    if args.calibrate:
        stats = _collect_calibration_stats(args.calibration_source_root)
        selected_instance_ids = _select_calibrated_subset(
            stats,
            size=args.calibrate_size,
            min_baseline_resolved=args.calibrate_min_baseline_resolved,
        )
        calibration_payload = {
            "selected_count": len(selected_instance_ids),
            "selected_instance_ids": selected_instance_ids,
            "min_baseline_resolved": args.calibrate_min_baseline_resolved,
            "source_root": str(args.calibration_source_root),
            "candidate_stats": {
                iid: {
                    "repo": s.repo,
                    "seen_runs": s.seen_runs,
                    "resolved_runs": s.resolved_runs,
                    "unresolved_runs": s.unresolved_runs,
                    "resolve_rate": s.resolve_rate,
                    "avg_cost": s.avg_cost,
                }
                for iid, s in sorted(stats.items())
            },
        }
        (output_dir / "calibration_selected_instances.json").write_text(
            json.dumps(calibration_payload, indent=2), encoding="utf-8"
        )
        print(
            f"[Calibration] selected {len(selected_instance_ids)} instances with baseline floor "
            f"{args.calibrate_min_baseline_resolved}"
        )

    swe_modes: dict[str, ModeMetrics] = {}
    for mode in ("baseline", "inject", "tools", "tools_guided"):
        if mode != "baseline" and args.swebench_wc_pack is None:
            raise SystemExit("Non-baseline modes require --swebench-wc-pack")
        run_id = f"benchmark-swebench-{mode}-{ts}"
        print(f"\n[SWE] running mode={mode} run_id={run_id}")
        rc, out = _run_wcbench(
            run_id=run_id,
            track="swebench",
            output_root=output_dir,
            mode=mode,
            model=args.model,
            swebench_dataset=args.dataset,
            swebench_split=args.split,
            swebench_instance_ids=selected_instance_ids if selected_instance_ids else None,
            swebench_max_instances=args.max_instances,
            swebench_wc_pack=args.swebench_wc_pack,
            wc_tier_ceiling=args.wc_tier_ceiling,
            wc_max_calls=args.wc_max_calls,
            wc_token_budget=args.wc_token_budget,
            wc_code_path=args.wc_code_path,
        )
        if rc != 0:
            raise RuntimeError(f"SWE mode {mode} failed:\n{out[-2000:]}")
        run_root = output_dir / run_id
        swe_modes[mode] = _summarize_swebench_mode(run_root, mode, run_id)
        print(
            f"  resolved={swe_modes[mode].resolved} cost={swe_modes[mode].total_cost:.4f} "
            f"wc_cmds={swe_modes[mode].total_wc_commands} "
            f"completed={swe_modes[mode].completed}/{swe_modes[mode].submitted}"
        )

    completion_rates = {
        mode: (mm.completed / mm.submitted) if mm.submitted > 0 else 0.0
        for mode, mm in swe_modes.items()
    }
    if any(rate < args.min_completion_rate for rate in completion_rates.values()):
        lines = ", ".join(f"{mode}={rate:.3f}" for mode, rate in sorted(completion_rates.items()))
        raise RuntimeError(
            "Evaluator completeness gate failed: "
            f"required >= {args.min_completion_rate:.3f}, got [{lines}]"
        )

    coordination: dict[str, dict] | None = None
    if not args.skip_coordination:
        coordination = {}
        for mode in ("baseline", "tools_guided"):
            run_id = f"benchmark-coordination-{mode}-{ts}"
            print(f"\n[Coordination] running mode={mode} run_id={run_id}")
            rc, out = _run_wcbench(
                run_id=run_id,
                track="coordination",
                output_root=output_dir,
                mode=mode,
                model=args.model,
                wc_tier_ceiling=args.wc_tier_ceiling,
                wc_max_calls=args.wc_max_calls,
                wc_token_budget=args.wc_token_budget,
                wc_code_path=args.wc_code_path,
                coordination_task_id=args.coordination_task_id,
            )
            if rc != 0:
                raise RuntimeError(f"Coordination mode {mode} failed:\n{out[-2000:]}")
            coordination[mode] = _summarize_coordination(output_dir / run_id)

    memory_qa: dict | None = None
    if not args.skip_memory_qa:
        run_id = f"benchmark-memory_qa-{ts}"
        print(f"\n[memory_qa] run_id={run_id}")
        memory_qa = _run_memory_qa(run_id, output_dir, args.wc_code_path)
        if memory_qa.get("status") != "ok":
            raise RuntimeError(f"memory_qa failed: {memory_qa}")

    instance_context = _load_instance_context(
        dataset=args.dataset,
        split=args.split,
        instance_ids=selected_instance_ids,
    )
    interpretation = _build_interpretation(swe_modes, instance_context)
    _write_report(
        output_dir,
        swe_modes=swe_modes,
        coordination=coordination,
        memory_qa=memory_qa,
        instance_ids=selected_instance_ids,
        instance_context=instance_context,
        interpretation=interpretation,
        calibration=calibration_payload,
    )
    json_payload = {
        "instance_ids": selected_instance_ids,
        "instance_context": instance_context,
        "swebench_matrix": {k: vars(v) for k, v in swe_modes.items()},
        "completion_rates": completion_rates,
        "min_completion_rate": args.min_completion_rate,
        "interpretation": interpretation,
        "coordination": coordination,
        "memory_qa": memory_qa,
        "calibration": calibration_payload,
    }
    (output_dir / "benchmark_results.json").write_text(json.dumps(json_payload, indent=2), encoding="utf-8")
    print(f"\nReport: {output_dir / 'BENCHMARK_REPORT.md'}")
    print(f"JSON:   {output_dir / 'benchmark_results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
