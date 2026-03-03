#!/usr/bin/env bash
# Build the agent_value benchmark base image.
#
# Both baseline and tools containers use the same image — the only delta is
# that the harness bind-mounts the orphan branch thread data into the tools
# container at runtime.
#
# The build script clones watercooler-site on the HOST (where Git credentials
# are available) and stages a Docker build context with:
#   wc-cloud-src/  — watercooler-cloud source (for pip install)
#   wc-site/       — watercooler-site code (project codebase at /repo)
#
# Usage:
#   bash tests/benchmarks/agent_value/build_base_image.sh
#   bash tests/benchmarks/agent_value/build_base_image.sh v1.2.3
#   bash tests/benchmarks/agent_value/build_base_image.sh main https://github.com/org/repo.git
set -euo pipefail

SITE_COMMIT="${1:-main}"
SITE_REPO="${2:-https://github.com/mostlyharmless-ai/watercooler-site.git}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Prepare staging directory for Docker build context
BUILD_CTX=$(mktemp -d)
trap 'rm -rf "$BUILD_CTX"' EXIT

# Stage watercooler-cloud source (minimal copy for pip install)
echo "==> Staging watercooler-cloud source..."
rsync -a \
  --exclude='.git' \
  --exclude='external' \
  --exclude='logs' \
  --exclude='.claude' \
  --exclude='__pycache__' \
  --exclude='*.egg-info' \
  --exclude='.mypy_cache' \
  --exclude='.ruff_cache' \
  "$REPO_ROOT/" "$BUILD_CTX/wc-cloud-src/"

# Clone watercooler-site at the specified commit (host-side, with auth)
echo "==> Cloning watercooler-site at ${SITE_COMMIT}..."
git clone --single-branch --depth=1 --branch "$SITE_COMMIT" \
  "$SITE_REPO" "$BUILD_CTX/wc-site"
rm -rf "$BUILD_CTX/wc-site/.git" \
       "$BUILD_CTX/wc-site/.next" \
       "$BUILD_CTX/wc-site/node_modules" \
       "$BUILD_CTX/wc-site/.claude"
# Remove .env* files (may contain secrets)
find "$BUILD_CTX/wc-site" -maxdepth 1 -name '.env*' -delete 2>/dev/null || true

# Build the image
echo "==> Building Docker image wcbench-agent-base:wc-site-v1..."
docker build \
  -f "$SCRIPT_DIR/Dockerfile.base" \
  -t wcbench-agent-base:wc-site-v1 \
  "$BUILD_CTX"

echo "==> Done: wcbench-agent-base:wc-site-v1"
