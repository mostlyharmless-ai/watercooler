FROM python:3.12-slim

WORKDIR /app

# System deps for git operations and watercooler
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install watercooler-cloud with bench extras (includes openhands-sdk)
COPY . /app/
RUN pip install --no-cache-dir -e ".[bench]"

# Strip data the agent must not have access to:
# - corpora .md projections (contain answer text)
# - tasks.json answer key
# - prior run logs (may contain DECISION.md from earlier runs)
# - .claude conversation history
# - .git history (contains all file content)
# - test fixtures and local thread data (stale .jsonl files)
RUN rm -rf /app/external/wcbench-corpora \
           /app/tests/benchmarks/agent_value/tasks.json \
           /app/tests/benchmarks/memory_qa/tasks.json \
           /app/docs/plans \
           /app/logs \
           /app/.claude \
           /app/.git \
           /app/tests/integration/.cli-threads \
           /app/watercooler/threads

ENV PYTHONPATH=/app/src

CMD ["bash", "-lc", "sleep infinity"]
