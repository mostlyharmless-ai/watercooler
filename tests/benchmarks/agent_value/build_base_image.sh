#!/usr/bin/env bash
# Build the agent_value benchmark base image.
#
# Both baseline and tools containers use the same image — the only delta is
# that the harness bind-mounts the orphan branch thread data into the tools
# container at runtime.
#
# Usage:
#   bash tests/benchmarks/agent_value/build_base_image.sh
#   bash tests/benchmarks/agent_value/build_base_image.sh --site-commit v1.2.3
set -euo pipefail

SITE_COMMIT="${1:-main}"

docker build \
  -f tests/benchmarks/agent_value/Dockerfile.base \
  --build-arg SITE_COMMIT="$SITE_COMMIT" \
  -t wcbench-agent-base:wc-site-v1 \
  tests/benchmarks/agent_value/
