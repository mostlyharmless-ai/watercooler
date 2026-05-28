"""Reset the decision_extractor daemon checkpoint.

Extracted from the ``watercooler_decision_extractor_reset`` MCP tool, which was
removed from the agent-facing surface in the tool-surface consolidation
(thread ``mcp-tool-surface-consolidation-2026-05``). Resetting the extractor
checkpoint is an operator/ops action, not an agent action, so it now lives as
a CLI script — ``scripts/reset_decision_extractor.py`` — that calls the
:func:`reset_decision_extractor_checkpoint` function defined here.
"""

from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# When the checkpoint was written within this window the extractor daemon is
# probably still running and will overwrite a reset on its next tick.
RESET_LIVE_DAEMON_WINDOW_SECONDS = 60.0

_DAEMON_NAME = "decision_extractor"


def reset_decision_extractor_checkpoint(
    *,
    clear_finding_cursor: bool = True,
    clear_source_cursor: bool = True,
    reset_daily_count: bool = True,
    clear_attempt_caps: bool = False,
    force: bool = False,
) -> dict:
    """Reset the decision_extractor cursor so stuck candidates can be retried.

    The extractor persists two sets that grow over time:
    ``processed_finding_ids`` (dedup per-finding) and ``processed_source_keys``
    (dedup per source entry). When the quote-validation rejection rate is high
    these sets accumulate permanent-skip markers that prevent re-evaluation
    even after the source entry changes. This empties them so the next tick
    scans fresh. The existing checkpoint is backed up
    (``checkpoint.json.bak-<timestamp>``) before any mutation.

    WARNING — race with a running daemon: ``BaseDaemon`` holds its checkpoint
    in memory and writes it after every tick. If the extractor daemon ticks
    between this function's ``save_checkpoint`` and its next in-memory write,
    the reset is silently overwritten. Stop the daemon first. As a safety net,
    when the checkpoint was modified within
    ``RESET_LIVE_DAEMON_WINDOW_SECONDS`` this refuses with
    ``status: active_daemon`` unless ``force=True``.

    Args:
        clear_finding_cursor: Empty ``processed_finding_ids`` (default True).
        clear_source_cursor: Empty ``processed_source_keys`` (default True).
        reset_daily_count: Reset ``daily_count`` to today at count 0
            (default True).
        clear_attempt_caps: Also empty ``llm_extraction_attempts`` and
            ``write_failure_attempts`` (default False — these represent genuine
            per-entry cost ceilings; clear only after verifying the underlying
            failure mode is resolved).
        force: Skip the active-daemon check. Use only after confirming the
            daemon is stopped.

    Returns:
        A result dict with ``status`` of ``ok``, ``not_found``,
        ``active_daemon``, or ``error``.
    """
    # Imported inside the function (not at module scope) so the daemon-state
    # helpers resolve through ``watercooler_mcp.daemons.state`` on each call —
    # this keeps them patchable by tests and mirrors the original tool impl.
    from .state import _daemon_dir, load_checkpoint, save_checkpoint

    try:
        # This admin/diagnostic action legitimately operates on the
        # un-namespaced checkpoint root (single-tenant local mode, no
        # auth-derived scope). ``_allow_unscoped=True`` opts out of the
        # WATERCOOLER_FINDINGS_STRICT_NAMESPACE strict-mode check; the audit
        # anchor for this exemption is the ``_allow_unscoped=True`` literal.
        cp_dir = _daemon_dir(_DAEMON_NAME, _allow_unscoped=True)
        cp_path = cp_dir / "checkpoint.json"
        if not cp_path.exists():
            return {
                "status": "not_found",
                "message": f"No checkpoint for {_DAEMON_NAME} at {cp_path}",
            }

        mtime = cp_path.stat().st_mtime
        age_seconds = time.time() - mtime
        if age_seconds < RESET_LIVE_DAEMON_WINDOW_SECONDS and not force:
            return {
                "status": "active_daemon",
                "message": (
                    f"Checkpoint was modified {age_seconds:.1f}s ago; the "
                    f"extractor daemon may be running and will overwrite a "
                    f"reset on its next tick. Stop the daemon first, or pass "
                    f"force=True to override."
                ),
                "checkpoint_age_seconds": round(age_seconds, 1),
                "checkpoint_path": str(cp_path),
            }

        # Microsecond precision on the backup filename so two rapid resets
        # cannot collide and silently clobber the only pre-reset snapshot.
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        bak_path = cp_dir / f"checkpoint.json.bak-{ts}"
        shutil.copy2(cp_path, bak_path)

        checkpoint = load_checkpoint(_DAEMON_NAME, _allow_unscoped=True)
        extras = checkpoint.extras
        before = {
            "processed_finding_ids": len(extras.get("processed_finding_ids", [])),
            "processed_source_keys": len(extras.get("processed_source_keys", [])),
            "daily_count": extras.get("daily_count"),
            "llm_extraction_attempts": len(extras.get("llm_extraction_attempts", {})),
            "write_failure_attempts": len(extras.get("write_failure_attempts", {})),
        }

        if clear_finding_cursor:
            extras["processed_finding_ids"] = []
        if clear_source_cursor:
            extras["processed_source_keys"] = []
        if reset_daily_count:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            extras["daily_count"] = {"date": today, "count": 0}
        if clear_attempt_caps:
            extras["llm_extraction_attempts"] = {}
            extras["write_failure_attempts"] = {}

        save_checkpoint(checkpoint, _allow_unscoped=True)

        return {
            "status": "ok",
            "daemon": _DAEMON_NAME,
            "backup_path": str(bak_path),
            "before": before,
            "cleared": {
                "finding_cursor": clear_finding_cursor,
                "source_cursor": clear_source_cursor,
                "daily_count": reset_daily_count,
                "attempt_caps": clear_attempt_caps,
            },
            "note": (
                "Next extractor tick will re-evaluate all live candidates. "
                "If the extractor daemon was running when this fired, its "
                "in-memory checkpoint will overwrite the reset on next tick — "
                "stop the daemon or restart the MCP server to apply cleanly."
            ),
        }
    except Exception as exc:  # noqa: BLE001 — surfaced as a structured result
        log.error("decision_extractor reset failed: %s", exc)
        return {"status": "error", "message": str(exc)}
