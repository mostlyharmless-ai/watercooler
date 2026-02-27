## Benchmarks in `watercooler-cloud`

Benchmark code lives in this repository because it is primarily intended to
validate Watercooler’s behavior and value claims.

### What belongs in git

- **Runner / harness code**: `tests/benchmarks/**`
- **Adapters and benchmark fixtures** (small, deterministic)
- **Guidance prompts** used by the runners: `tests/benchmarks/guidance/**`

### What does not belong in git

Generated artifacts should never be committed. These include:

- Run outputs and logs: `logs/**`
- SWE-bench evaluator reports (model-prefixed JSON files)
- Gold validation outputs

The repository `.gitignore` is configured to ignore these.

### Where outputs go

- SWE-bench runs write to `--output-dir` (default under `logs/…`).
- The SWE-bench evaluator is executed with its CWD set to `--output-dir` so its
  report JSON lands next to the run artifacts, not in the repo root.

### Canonical runner: `wcbench`

The canonical entrypoint for Watercooler benchmarks is:

```bash
python3 -m tests.benchmarks.wcbench --help
```

It standardizes all outputs under:

- `logs/<run_id>/events.jsonl`: append-only trace (run/task/tool/test events)
- `logs/<run_id>/summary.json`: structured results
- `logs/<run_id>/report.md`: human-readable rollup for tuning + comparison
- `logs/<run_id>/artifacts/**`: track-specific outputs (threads, SWE-bench artifacts, etc.)

#### Custom (Watercooler-first) track

```bash
python3 -m tests.benchmarks.wcbench \
  --track custom \
  --mode tools_guided \
  --model minimax/MiniMax-M2.5 \
  --run-id wcbench-custom-tools-guided
```

Useful dev flag to iterate on a single custom task:

```bash
python3 -m tests.benchmarks.wcbench \
  --track custom \
  --mode tools_guided \
  --run-id wcbench-custom-multihop \
  --custom-only multi-hop-with-citations
```

#### SWE-bench track (delegates to the existing runner)

```bash
python3 -m tests.benchmarks.wcbench \
  --track swebench \
  --mode baseline \
  --model minimax/MiniMax-M2.5 \
  --run-id wcbench-swebench-baseline \
  --swebench-instance-ids sympy__sympy-20590
```

#### Coordination-under-overlap track (CooperBench-like subset)

This is a two-phase handoff scenario (AgentA → AgentB) that forces a clean repo
reset between phases and measures how effectively the second agent uses the
handoff note (via Watercooler) to complete the task.

```bash
python3 -m tests.benchmarks.wcbench \
  --track coordination \
  --mode tools_guided \
  --coordination-task-id multi-hop-with-citations \
  --run-id wcbench-coordination-smoke
```

#### Memory QA track (T2 supersession + T3 reverse provenance)

This track runs **deterministic** (non-agent) checks that exercise the intended
semantics of the higher memory tiers:

- **T2**: temporal validity (`valid_at`/`invalid_at`) and **active-only** filtering
- **T3**: reverse provenance (`source_id` episode UUIDs → T1 `entry_id`)

```bash
python3 -m tests.benchmarks.wcbench \
  --track memory_qa \
  --wc-tier-ceiling T3 \
  --run-id wcbench-memory-qa-smoke
```

Tasks are defined in `tests/benchmarks/memory_qa/tasks.json` and are isolated
per-run/per-task using a dedicated `group_id` plus a per-task
`entry_episode_index.json` under `logs/<run_id>/artifacts/`.

#### T2/T3 infra (FalkorDB)

When `--wc-tier-ceiling` is `T2` or `T3`, `wcbench` starts a local FalkorDB via
Docker Compose using `tests/benchmarks/infra/docker-compose.memory.yml`.

Notes:
- If port `6379` is already in use, `wcbench` will automatically pick an
  available port (e.g. `16379`) and set `FALKORDB_PORT` for the run.
- Infra lifecycle is recorded in `events.jsonl` (compose up/down).

### `wc-*` text commands for T2/T3 semantics

In addition to the baseline commands (`wc-search`, `wc-smart-query`,
`wc-read-thread`, `wc-get-entry`, `wc-say`), the prompt-only dispatcher supports:

- `wc-t2-facts [--active-only] [--start-time <iso>] [--end-time <iso>] "<query>"`
  - Queries Graphiti facts (T2). `--active-only` filters out results with
    `invalid_at != None`.
- `wc-provenance <episode_uuid>`
  - Resolves a Graphiti episode UUID back to a T1 `entry_id` (via `EntryEpisodeIndex`).

`wc-smart-query` also prints `source=` for T3 evidence when available.

### Legacy runners

The existing scripts remain available (useful for debugging), but `wcbench`
should be treated as the stable interface moving forward:

- `tests/benchmarks/custom/runner.py`
- `tests/benchmarks/scripts/run_swebench.py`

