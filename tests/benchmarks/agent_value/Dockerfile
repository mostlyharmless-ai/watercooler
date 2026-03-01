FROM python:3.12-slim

WORKDIR /app

# System deps for git operations and watercooler
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install watercooler-cloud with bench extras (includes openhands-sdk)
COPY . /app/
RUN pip install --no-cache-dir -e ".[bench]"

ENV PYTHONPATH=/app/src

CMD ["bash", "-lc", "sleep infinity"]
