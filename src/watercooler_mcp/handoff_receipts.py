"""Plan v20 Phase 5: local handoff receipts for hybrid-mode T2 submission.

In ``local_hybrid``, the local MCP server does not execute T2 work — it
submits to the hosted premium endpoint and records a *handoff receipt* so
there is a local audit trail of Stage A (submission) independent of the
hosted Stage B (execution) outcome. This lets operators confirm "the local
side tried to submit N entries" without needing hosted backend access.

The receipts file is a bounded, append-only JSONL at
``~/.watercooler/handoff_receipts.jsonl``.

Contrast with :mod:`watercooler_mcp.memory_queue.queue` receipts, which
record *terminal* state on the executing side (hosted Stage B or local
``stdio``). Handoff receipts record *submission* state on the local side
in hybrid mode.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_HANDOFF_RECEIPTS_FILE = (
    Path.home() / ".watercooler" / "handoff_receipts.jsonl"
)
_MAX_HANDOFF_RECEIPTS = 1000

_lock = threading.Lock()


def _receipts_file() -> Path:
    """Resolve the configured receipts file path (env override supported)."""
    override = os.environ.get("WATERCOOLER_HANDOFF_RECEIPTS_FILE")
    if override:
        return Path(override)
    return DEFAULT_HANDOFF_RECEIPTS_FILE


_MAX_ERROR_DETAIL_CHARS = 500


def summarize_remote_error(payload: Dict[str, Any]) -> str:
    """Flatten a remote error payload into a receipt-sized error string.

    ``premium_client.call_tool_text`` attaches ``message``, ``status_code``
    and ``remote_error`` (the remote HTTP body, e.g. a
    ``repo_claim_mismatch`` explanation) to its ``remote_call_failed``
    envelope. Receipts that record only ``payload["error"]`` lose all of
    that; this helper preserves it in one bounded string.
    """
    err = str(payload.get("error") or payload.get("status") or "rejected")
    parts = [err]
    status = payload.get("status_code")
    if status:
        parts.append(f"http={status}")
    detail = payload.get("remote_error") or payload.get("message") or ""
    detail = str(detail).strip()
    if detail and detail != err:
        parts.append(detail[:_MAX_ERROR_DETAIL_CHARS])
    return ": ".join(parts)


def append_handoff_receipt(
    *,
    backend: str,
    stage: str,
    entry_id: str = "",
    topic: str = "",
    group_id: str = "",
    remote_task_id: str = "",
    submission_status: str = "",
    error: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Append a Stage-A handoff receipt.

    Args:
        backend: ``graphiti`` / ``leanrag`` / ``t1_semantic``.
        stage: ``"submitted"`` (Stage A success) or ``"submit_failed"``.
        entry_id: Watercooler entry ID, when available.
        topic: Thread topic, when available.
        group_id: Project group id (canonical ``<org>_<repo>``).
        remote_task_id: Returned by the hosted side on a successful submit.
        submission_status: Raw status string from the hosted response (``queued``,
            ``submitted``, etc.).
        error: Short error message if submit failed.
        extra: Optional free-form metadata for future expansion.
    """
    path = _receipts_file()
    record: Dict[str, Any] = {
        "ts": time.time(),
        "backend": backend,
        "stage": stage,
        "entry_id": entry_id,
        "topic": topic,
        "group_id": group_id,
        "remote_task_id": remote_task_id,
        "submission_status": submission_status,
        "error": error,
    }
    if extra:
        record["extra"] = extra

    line = json.dumps(record, separators=(",", ":")) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with open(path, "a") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            # Round 15 (MEDIUM): pass the just-written line through to
            # the trim step. If the post-append re-read somehow misses
            # it (rare OS read-cache quirk on network filesystems), the
            # explicit inclusion here still guarantees the new receipt
            # survives the truncation.
            _trim_if_needed(path, just_written=line)
    except OSError as e:
        logger.debug("HANDOFF: could not write receipt: %s", e)


def _trim_if_needed(path: Path, just_written: str = "") -> None:
    """Keep ``handoff_receipts.jsonl`` bounded to ``_MAX_HANDOFF_RECEIPTS``.

    Called with ``_lock`` held by :func:`append_handoff_receipt`.
    """
    try:
        with open(path, "r") as f:
            lines = f.readlines()
    except OSError:
        return
    # Defensive against an OS read-cache race: if we just wrote a line
    # and the re-read doesn't include it, tack it on before deciding
    # what to keep. If it's already there this is a no-op.
    if just_written and (not lines or lines[-1] != just_written):
        lines.append(just_written)
    if len(lines) <= _MAX_HANDOFF_RECEIPTS:
        return
    trimmed = lines[-_MAX_HANDOFF_RECEIPTS:]
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w") as f:
            f.writelines(trimmed)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        logger.debug("HANDOFF: trim failed: %s", e)
        # Round 18 (LOW): clean up the orphan ``.tmp`` when
        # ``os.replace`` fails (e.g., cross-device on NFS). The live
        # file is unaffected; this only prevents accumulation on
        # repeated trim attempts.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def recent_receipts(limit: int = 50) -> List[Dict[str, Any]]:
    """Return the most recent handoff receipts (newest first)."""
    path = _receipts_file()
    if not path.exists():
        return []
    try:
        with _lock:
            with open(path, "r") as f:
                lines = f.readlines()
    except OSError:
        return []

    out: List[Dict[str, Any]] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out


def summary() -> Dict[str, Any]:
    """Return a summary (counts + last-seen per backend/stage) for diagnostics."""
    path = _receipts_file()
    if not path.exists():
        return {"total": 0, "by_stage": {}, "by_backend": {}}

    try:
        with _lock:
            with open(path, "r") as f:
                lines = f.readlines()
    except OSError:
        return {"total": 0, "by_stage": {}, "by_backend": {}}

    total = 0
    by_stage: Dict[str, int] = {}
    by_backend: Dict[str, int] = {}
    last_ts: Optional[float] = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        total += 1
        stage = rec.get("stage", "unknown")
        backend = rec.get("backend", "unknown")
        by_stage[stage] = by_stage.get(stage, 0) + 1
        by_backend[backend] = by_backend.get(backend, 0) + 1
        ts = rec.get("ts")
        if isinstance(ts, (int, float)):
            if last_ts is None or ts > last_ts:
                last_ts = float(ts)

    return {
        "total": total,
        "by_stage": by_stage,
        "by_backend": by_backend,
        "last_receipt_at": last_ts,
    }
