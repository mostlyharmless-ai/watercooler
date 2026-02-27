#!/usr/bin/env bash
set -euo pipefail

# Durable wrapper to build T2/T3 artifacts for the sanitized watercooler-site snapshot corpus.
# Safe to run under nohup/setsid; logs go to logs/corpora-build/.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
CORPUS_DIR="${REPO_ROOT}/external/wcbench-corpora/corpora/watercooler_site_snapshot_v1"
THREADS_DIR="${CORPUS_DIR}/t1/threads_dir"

LOG_DIR="${REPO_ROOT}/logs/corpora-build/watercooler_site_snapshot_v1"
mkdir -p "${LOG_DIR}"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_FILE="${LOG_DIR}/build-memory-${TS}.log"

export PYTHONUNBUFFERED=1

echo "Logging to: ${LOG_FILE}"
echo "Corpus dir: ${CORPUS_DIR}"
echo "Threads dir: ${THREADS_DIR}"

python3 "${REPO_ROOT}/tests/benchmarks/scripts/build_corpus_memory_artifacts.py" \
  --threads-dir "${THREADS_DIR}" \
  --out-corpus-dir "${CORPUS_DIR}" \
  --group-id "watercooler_site_snapshot_v1" \
  --graphiti-database "wcbench_watercooler_site_snapshot_v1" \
  --leanrag-work-dir-name "leanrag_watercooler_site_snapshot_v1" \
  --compose-project "wcbench-corpus-watercooler-site-snapshot-v1" \
  --preferred-ports "6379,6380,6381,6382" \
  2>&1 | tee "${LOG_FILE}"

