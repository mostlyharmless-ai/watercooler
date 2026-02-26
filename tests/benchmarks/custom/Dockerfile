FROM python:3.11-slim

WORKDIR /repo

# Minimal test dependencies
RUN pip install --no-cache-dir pytest

COPY repo/ /repo/

ENV PYTHONPATH=/repo/src

CMD ["bash", "-lc", "sleep infinity"]

