#!/usr/bin/env bash
set -euo pipefail

# Durable wrapper around build_knowledge_pack.py.
# - Skips repo packs that already exist
# - Retries transient failures
# - Designed to run under nohup/setsid so it survives editor restarts

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

MODEL="${MODEL:-minimax/MiniMax-M2.5}"
OUT_DIR="${OUT_DIR:-external/wcbench-corpora/corpora/swebench_repo_packs_v1/t1}"
LOG_DIR="${LOG_DIR:-logs/corpora-build}"
RETRIES="${RETRIES:-3}"
SLEEP_SECONDS="${SLEEP_SECONDS:-15}"

mkdir -p "$LOG_DIR"
mkdir -p "$OUT_DIR"

# Ensure HuggingFace/datasets cache is writable even under restricted sandboxes.
# (Also avoids lockfile permission issues in ~/.cache.)
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$REPO_ROOT/$LOG_DIR/xdg-cache}"
export HF_HOME="${HF_HOME:-$REPO_ROOT/$LOG_DIR/hf-home}"
export HF_DATASETS_CACHE="${HF_DATASETS_CACHE:-$REPO_ROOT/$LOG_DIR/hf-datasets-cache}"
export TRANSFORMERS_CACHE="${TRANSFORMERS_CACHE:-$REPO_ROOT/$LOG_DIR/hf-transformers-cache}"
mkdir -p "$XDG_CACHE_HOME" "$HF_HOME" "$HF_DATASETS_CACHE" "$TRANSFORMERS_CACHE"

ts="$(date -u +%Y%m%d-%H%M%S)"
log_file="$LOG_DIR/swebench_repo_packs_v1-$ts.log"

echo "Writing log to: $log_file"
echo "Model: $MODEL" | tee -a "$log_file"
echo "Out dir: $OUT_DIR" | tee -a "$log_file"
echo "Retries: $RETRIES" | tee -a "$log_file"
echo "" | tee -a "$log_file"

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <instance_id> [<instance_id> ...]" | tee -a "$log_file"
  echo "Example: $0 sympy__sympy-20590" | tee -a "$log_file"
  exit 2
fi

repo_slug_for_instance() {
  local inst="$1"
  # Instance IDs look like: <repo_slug>-<number>
  echo "${inst%-*}"
}

pack_exists() {
  local repo_slug="$1"
  [ -f "$OUT_DIR/$repo_slug/graph/baseline/manifest.json" ]
}

build_one() {
  local inst="$1"
  local repo_slug
  repo_slug="$(repo_slug_for_instance "$inst")"

  if pack_exists "$repo_slug"; then
    echo "SKIP: $repo_slug (already built)" | tee -a "$log_file"
    return 0
  fi

local attempt=1
  while [ "$attempt" -le "$RETRIES" ]; do
    echo "BUILD: $repo_slug from $inst (attempt $attempt/$RETRIES)" | tee -a "$log_file"
    if PYTHONUNBUFFERED=1 python3 tests/benchmarks/scripts/build_knowledge_pack.py \
      --instance-ids "$inst" \
      --model "$MODEL" \
      --output-dir "$OUT_DIR" >>"$log_file" 2>&1; then
      echo "OK: $repo_slug" | tee -a "$log_file"
      return 0
    fi
    echo "FAIL: $repo_slug (attempt $attempt)" | tee -a "$log_file"
    attempt="$((attempt + 1))"
    sleep "$SLEEP_SECONDS"
  done

  echo "ERROR: giving up on $repo_slug after $RETRIES attempts" | tee -a "$log_file"
  return 1
}

for inst in "$@"; do
  build_one "$inst"
done

echo "" | tee -a "$log_file"
echo "DONE" | tee -a "$log_file"

