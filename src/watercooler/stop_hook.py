#!/usr/bin/env python3
"""Packaged Stop hook that surfaces candidate Notes and stance advisories.

Reads the Claude Code Stop hook payload from stdin, scans the active
daemon findings logs — ``decision_extractor`` for
``extraction_candidate_note`` entries, plus whichever daemon is the active
``stance_advisory`` producer (``project_coordinator`` or
``decision_stance``, per ``findings_source.resolve_active_findings_sources``)
— for records created since the session started, and prints a brief summary
to stderr so the user sees what landed during the session.

The hook is read-only: it never writes thread state, never blocks the stop
action, and always exits 0. If anything goes wrong (missing files, malformed
payload, permission errors) the hook stays silent — surfacing is best-effort.

Authority-ladder Phase 1c deliverable. Phase 1b (PR #800) is what creates the
candidate Notes that this hook surfaces. Stance-advisory delivery is the
Role Salience Compiler's D11 MVP requirement (see
dev_docs/plans/2026-06-30-feat-role-salience-compiler-plan.md, Phase 3) —
this generalizes the hook's findings source beyond decision-extractor output
so elevated planner/critic/tester advisories (with any ``project_salience``
decoration) surface at end-of-turn without a new delivery transport.

Usage (as hook):
    Configured in ~/.claude/settings.json as a Stop hook. Receives JSON via
    stdin: {"session_id": "...", "transcript_path": "...", "cwd": "...",
    "stop_hook_active": bool}
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Strips ANSI/OSC/DCS escape sequences AND every C0/C1 control byte before
# printing untrusted text to the terminal. Advisory/candidate content
# ultimately originates from watercooler thread entries, but the compiler's
# lint is content-vocabulary-only and the loader permits hand-authored
# bullets with no equivalent check — this is the last line of defense before
# terminal echo, not a substitute for validating at the source (the loader
# now rejects control chars too; see role_loader._validate_project_salience).
#
# Two passes: (1) remove recognizable escape *sequences* — 7-bit CSI
# (``\x1b[…``), 8-bit single-byte CSI (``\x9b…``), OSC/DCS/PM/APC strings up
# to their ST/BEL terminator, and the simple two-char C1 escapes — including
# their bodies, so no printable remnant (``[31m``) survives the introducer;
# then (2) mop up any remaining bare C0/C1 control bytes, including the
# single-byte C1 range ``\x7f-\x9f`` that UTF-8 terminals decode from
# ``\xc2\x9x``. The negated character classes make the regex linear (no
# catastrophic backtracking).
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"          # 7-bit CSI, full sequence + body
    r"|\x9b[0-9;?]*[ -/]*[@-~]"           # 8-bit single-byte CSI, full
    r"|[\x1b\x9d\x90\x9e\x9f][^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)"  # OSC/DCS/PM/APC → ST/BEL
    r"|\x1b[@-Z\\-_]"                      # other two-char C1 escapes
)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _strip_unsafe_terminal_content(text: str) -> str:
    """Neutralize terminal-escape / control-byte injection in untrusted text."""
    return _CONTROL_CHARS_RE.sub("", _ANSI_ESCAPE_RE.sub("", text))

CAT_CANDIDATE_NOTE = "extraction_candidate_note"
CAT_RATE_CAP = "extraction_rejected_rate_cap"
CAT_STANCE_ADVISORY = "stance_advisory"
_SURFACED_CATEGORIES = (CAT_CANDIDATE_NOTE, CAT_RATE_CAP, CAT_STANCE_ADVISORY)
FALLBACK_LOOKBACK_S = 3600.0


_DAEMONS_DIR = Path.home() / ".watercooler" / "daemons"
_ACTIVE_STANCE_PRODUCER_SIDECAR = _DAEMONS_DIR / "active_stance_producer"
# Allowlist the two possible stance producers so a sidecar value can never
# be used to build an out-of-tree findings path (defense in depth — the
# sidecar is written by our own daemon process, but it is still file input).
_KNOWN_STANCE_PRODUCERS = ("project_coordinator", "decision_stance")


@dataclass(frozen=True)
class _Source:
    daemon_name: str
    findings_path: Path


def _source(name: str) -> _Source:
    return _Source(name, _DAEMONS_DIR / name / "findings.jsonl")


def _findings_sources() -> list[Any]:
    """Resolve the findings sources to poll — sidecar fast path, best-effort.

    The daemon-owning process writes the active stance producer to a sidecar
    (``~/.watercooler/daemons/active_stance_producer``); reading it here
    avoids importing the daemons package and building the full pydantic
    config on every turn (~110ms/turn cold). ``decision_extractor`` is always
    polled; the stance producer is appended only when the sidecar names a
    known one (empty file = no local stance producer).

    Falls back to full resolution when the sidecar is absent (daemons never
    started this machine, or an older build), and to decision_extractor-only
    if even that import fails — so a missing optional dependency degrades the
    hook to its pre-Phase-3 behavior rather than silencing it entirely.
    """
    try:
        if _ACTIVE_STANCE_PRODUCER_SIDECAR.is_file():
            names = ["decision_extractor"]
            producer = _ACTIVE_STANCE_PRODUCER_SIDECAR.read_text(
                encoding="utf-8"
            ).strip()
            if producer in _KNOWN_STANCE_PRODUCERS:
                names.append(producer)
            return [_source(n) for n in names]
    except OSError as exc:
        logger.debug("stop_hook: could not read stance-producer sidecar: %s", exc)

    # No sidecar (or unreadable): fall back to full resolution.
    try:
        from watercooler_mcp.daemons.findings_source import (
            resolve_active_findings_sources,
        )

        return resolve_active_findings_sources()
    except Exception as exc:
        # Best-effort degradation, not silence: this is the one place a
        # stance-advisory-delivery outage (e.g. an unexpected exception
        # from the resolver) would otherwise vanish with zero trace.
        logger.debug(
            "stop_hook: falling back to decision_extractor-only findings "
            "source (resolve_active_findings_sources failed: %s)",
            exc,
        )
        return [_source("decision_extractor")]


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


def _load_findings_from(
    findings_path: Path, start_epoch: float, session_repo: str = ""
) -> list[dict[str, Any]]:
    if not findings_path.is_file():
        return []
    results: list[dict[str, Any]] = []
    try:
        with findings_path.open("r", encoding="utf-8") as fh:
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
                if rec.get("category") not in _SURFACED_CATEGORIES:
                    continue
                if not _repo_matches(session_repo, rec.get("repo") or ""):
                    continue
                results.append(rec)
    except OSError:
        return []
    return results


def _load_findings_since(
    start_epoch: float, session_repo: str = ""
) -> list[dict[str, Any]]:
    """Load surfaced-category findings from every active findings source.

    Sources are resolved via ``findings_source.resolve_active_findings_sources``
    — decision_extractor plus whichever daemon is the active stance_advisory
    producer (mutex-resolved, never both).
    """
    results: list[dict[str, Any]] = []
    for source in _findings_sources():
        results.extend(
            _load_findings_from(source.findings_path, start_epoch, session_repo)
        )
    return results


def _elevated_stance(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Elevated (level >= 1) stance_advisory findings.

    Tombstones ("cleared", level 0) are not worth surfacing at turn end —
    only elevated advisories carry an actionable attention signal.
    """
    elevated: list[dict[str, Any]] = []
    for finding in findings:
        if finding.get("category") != CAT_STANCE_ADVISORY:
            continue
        details = finding.get("details", {}) or {}
        if not isinstance(details, dict):
            continue
        advisory = details.get("advisory", {}) or {}
        if not isinstance(advisory, dict):
            continue
        level = advisory.get("level", 0)
        if not isinstance(level, int) or isinstance(level, bool):
            continue
        if level >= 1:
            elevated.append(finding)
    return elevated


def _has_surfaceable_content(findings: list[dict[str, Any]]) -> bool:
    """Whether findings contain anything worth printing.

    A non-empty ``findings`` list can still be all-tombstone (cleared
    stance, level 0) — that's not surfaceable, so the hook must not print
    an empty-looking summary in that case.
    """
    return bool(
        [f for f in findings if f.get("category") in (CAT_CANDIDATE_NOTE, CAT_RATE_CAP)]
    ) or bool(_elevated_stance(findings))


def _format_summary(findings: list[dict[str, Any]]) -> str:
    candidates = [f for f in findings if f.get("category") == CAT_CANDIDATE_NOTE]
    rate_caps = [f for f in findings if f.get("category") == CAT_RATE_CAP]
    stance = _elevated_stance(findings)

    header_parts = [f"{len(candidates)} candidate Note(s) emitted this session"]
    if rate_caps:
        header_parts.append(f"{len(rate_caps)} rate-cap suppression(s)")
    if stance:
        header_parts.append(f"{len(stance)} elevated stance advisory(ies)")
    lines: list[str] = ["[watercooler] " + "; ".join(header_parts)]
    # Every field below is interpolated into a line printed to the user's
    # terminal and originates from untrusted (thread-derived) finding
    # records, so each is passed through _strip_unsafe_terminal_content —
    # not just `summary` — to close terminal-escape injection on every path.
    def _safe(v: Any) -> str:
        return _strip_unsafe_terminal_content(str(v))

    for f in stance:
        details = f.get("details", {}) or {}
        advisory = details.get("advisory", {}) or {}
        role = _safe(advisory.get("role", "?"))
        level = _safe(advisory.get("level", "?"))
        summary = _safe(advisory.get("summary", ""))
        salience = advisory.get("project_salience") or []
        advisory_only = advisory.get("advisory_only", True)
        label = "advisory-only" if advisory_only else "advisory"
        lines.append(f"  • [{role}] L{level} {summary} ({label})")
        for bullet in salience:
            lines.append(f"      · {_safe(bullet)}")
    for f in candidates:
        details = f.get("details", {}) or {}
        topic = _safe(f.get("topic", "?"))
        entry_id = _safe(details.get("entry_id") or "?")
        source = _safe(details.get("source_entry_id") or "?")
        confidence = details.get("confidence")
        reason = _safe(details.get("rejection_reason") or "?")
        conf_str = f" conf={_safe(confidence)}" if confidence is not None else ""
        lines.append(
            f"  • {topic}: candidate {entry_id} ← source {source} "
            f"({reason}{conf_str})"
        )
    for f in rate_caps:
        topic = _safe(f.get("topic", "?"))
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
        if not _has_surfaceable_content(findings):
            return 0
        print(_format_summary(findings), file=sys.stderr)
    except Exception:
        # Best-effort surfacing — never block session stop.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
