## Benchmark corpora (historic thread sets)

This folder documents and helps provision **preexisting** Watercooler thread sets
for benchmarking, especially for `tools_guided` runs where we want the agent to
retrieve org memory through Watercooler-native tool usage (not prompt injection).

### SWE-bench coupling points (current code)

SWE-bench’s Watercooler integration is enabled via `--wc-pack` (or `wcbench`
pass-through `--swebench-wc-pack`).

The runner expects the following **directory shape**:

- `--wc-pack <DIR>` contains per-repo pack folders:
  - `<DIR>/<repo_slug>/...`
  - where `repo_slug = repo.replace("/", "__")`
- Each pack folder is a **baseline graph threads_dir** (written via `say()`):
  - `graph/baseline/manifest.json`
  - `graph/baseline/threads/*` (entries/edges/meta)
- The thread topic used inside that threads_dir is:
  - `<repo_slug>-knowledge`

Relevant code:
- `load_knowledge_pack()` in `tests/benchmarks/scripts/run_swebench.py`
- `WatercoolerKnowledgePack` in `tests/benchmarks/adapters/knowledge_pack.py`
- `wcbench` SWE track requirement in `tests/benchmarks/wcbench/tracks/swebench.py`

### Pilot SWE-bench pack set (recommended)

These are the original “pilot” instance IDs used in docs/examples and are a
reasonable default set to commit as a reproducible pack corpus:

- `sympy__sympy-20590`
- `django__django-11099`
- `pytest-dev__pytest-5103`
- `scikit-learn__scikit-learn-13142`

The pack builder (`tests/benchmarks/scripts/build_knowledge_pack.py`) accepts
`--instance-ids ...` and writes the per-repo threads_dir outputs into `--output-dir`.

### Where corpora live (versioned)

We keep versioned corpora under `external/wcbench-corpora/` (recommended to make
this a git submodule if it grows large). Each corpus includes:

- `t1/`: baseline-graph threads_dir(s)
- `t2/`: Graphiti restore assets and/or rebuild scripts
- `t3/`: LeanRAG work_dir snapshot and/or rebuild scripts
- `manifest.json`: metadata + stable `group_id`(s)

