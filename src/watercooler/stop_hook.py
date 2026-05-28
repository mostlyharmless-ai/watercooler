#!/usr/bin/env python3
"""Packaged Stop hook that surfaces candidate Notes emitted during a session.

Reads the Claude Code Stop hook payload from stdin, scans the
``ExtractDecisionsDaemon`` findings log for ``extraction_candidate_note``
entries created since the session started, and prints a brief summary to
stderr so the user sees what soft-gate extractions landed during the session.

The hook is read-only: it never writes thread state, never blocks the stop
action, and always exits 0. If anything goes wrong (missing files, malformed
payload, permission errors) the hook stays silent — surfacing is best-effort.

Authority-ladder Phase 1c deliverable. Phase 1b (PR #800) is what creates the
candidate Notes that this hook surfaces.

Usage (as hook):
    Configured in ~/.claude/settings.json as a Stop hook. Receives JSON via
    stdin: {"session_id": "...", "transcript_path": "...", "cwd": "...",
    "stop_hook_active": bool}
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

FINDINGS_PATH = (
    Path.home() / ".watercooler" / "daemons" / "decision_extractor" / "findings.jsonl"
)
CAT_CANDIDATE_NOTE = "extraction_candidate_note"
CAT_RATE_CAP = "extraction_rejected_rate_cap"
FALLBACK_LOOKBACK_S = 3600.0


def _read_payload() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _session_start_epoch(transcript_path: str) -> float:
    """Derive session start from transcript file. Falls back to lookback window.

    Claude Code transcripts begin with metadata records (``permission-mode``,
    ``file-history-snapshot``) that carry no ``timestamp``. Scan forward to the
    first timestamped record rather than trusting the first line — and never
    use the file mtime, which at Stop-hook time is near the session *end*.
    """
    fallback = time.time() - FALLBACK_LOOKBACK_S
    if not transcript_path:
        return fallback
    path = Path(transcript_path)
    if not path.is_file():
        return fallback
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("timestamp")
                if not isinstance(ts, str):
                    continue
                # ISO 8601, e.g. "2026-05-19T18:30:00.123Z"
                try:
                    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
    except OSError:
        return fallback
    return fallback


def _session_repo(cwd: str) -> str:
    """Resolve the session's repo root from the Stop payload ``cwd``.

    Returns an empty string when ``cwd`` is absent or unresolvable, in which
    case repo scoping is skipped (best-effort surfacing).
    """
    if not cwd:
        return ""
    try:
        return str(Path(cwd).resolve())
    except OSError:
        return ""


def _repo_matches(session_repo: str, rec_repo: str) -> bool:
    """Whether a finding belongs to the active checkout.

    Local daemons for multiple repos append to one shared findings log, so a
    finding is only shown when its ``repo`` matches the session's repo root.
    When either side is missing (legacy findings, or no ``cwd`` in the
    payload) the finding is included — it cannot be scoped, and silence is
    worse than over-surfacing for a best-effort hook.
    """
    if not session_repo or not rec_repo:
        return True
    try:
        session = Path(session_repo).resolve()
        rec = Path(rec_repo).resolve()
    except OSError:
        return True
    return session == rec or rec in session.parents or session in rec.parents


def _load_findings_since(
    start_epoch: float, session_repo: str = ""
) -> list[dict[str, Any]]:
    if not FINDINGS_PATH.is_file():
        return []
    results: list[dict[str, Any]] = []
    try:
        with FINDINGS_PATH.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                created_at = rec.get("created_at")
                if not isinstance(created_at, (int, float)):
                    continue
                if created_at < start_epoch:
                    continue
                if rec.get("category") not in (CAT_CANDIDATE_NOTE, CAT_RATE_CAP):
                    continue
                if not _repo_matches(session_repo, rec.get("repo") or ""):
                    continue
                results.append(rec)
    except OSError:
        return []
    return results


def _format_summary(findings: list[dict[str, Any]]) -> str:
    candidates = [f for f in findings if f.get("category") == CAT_CANDIDATE_NOTE]
    rate_caps = [f for f in findings if f.get("category") == CAT_RATE_CAP]

    lines: list[str] = []
    lines.append(
        f"[watercooler] {len(candidates)} candidate Note(s) emitted this session"
        + (f"; {len(rate_caps)} rate-cap suppression(s)" if rate_caps else "")
    )
    for f in candidates:
        details = f.get("details", {}) or {}
        topic = f.get("topic", "?")
        entry_id = details.get("entry_id") or "?"
        source = details.get("source_entry_id") or "?"
        confidence = details.get("confidence")
        reason = details.get("rejection_reason") or "?"
        conf_str = f" conf={confidence}" if confidence is not None else ""
        lines.append(
            f"  • {topic}: candidate {entry_id} ← source {source} "
            f"({reason}{conf_str})"
        )
    for f in rate_caps:
        topic = f.get("topic", "?")
        lines.append(f"  • {topic}: rate-cap suppression Note posted")
    lines.append(
        "  Review with: watercooler search 'Candidate-Status: needs_human_confirmation'"
    )
    return "\n".join(lines)


def main() -> int:
    try:
        payload = _read_payload()
        start_epoch = _session_start_epoch(payload.get("transcript_path", ""))
        session_repo = _session_repo(payload.get("cwd", ""))
        findings = _load_findings_since(start_epoch, session_repo)
        if not findings:
            return 0
        print(_format_summary(findings), file=sys.stderr)
    except Exception:
        # Best-effort surfacing — never block session stop.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
