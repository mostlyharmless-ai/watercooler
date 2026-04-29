"""coordinator_leads — read-time enrichment for coordinator_lead findings.

Overlays S1 (ThreadAuditor hygiene), S2 (decision-detector booster), and
S3 (PulseSnapshot dimension scores) onto serialized coordinator_lead findings
returned by watercooler_daemon_findings when enrich=True.

All overlays are ephemeral (computed at read time, not persisted on the stored
finding).  Missing overlays are silently omitted — callers detect by key
absence.  Any accessor that raises is logged at DEBUG and skipped; the base
lead survives with the remaining overlays applied.

Namespace isolation is mandatory: every load_findings() call passes
``namespace=namespace`` to prevent cross-tenant leakage in hosted deployments.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from watercooler_mcp.daemons.manager import DaemonManager
    from watercooler_mcp.daemons.hosted_coordinator import HostedDaemonCoordinator

from watercooler_mcp.daemons.state import load_findings

log = logging.getLogger(__name__)

# Maximum number of findings to load per source daemon during enrichment.
# Deliberately separate from state._MAX_FINDINGS_LINES (the JSONL rotation
# threshold) — enrichment only needs the most recent N unacknowledged findings,
# not the full file capacity.
_ENRICH_FINDINGS_CAP = 2_000


def enrich_leads(
    leads: list[dict[str, Any]],
    *,
    namespace: str,
    repo_key: str,
    runtime: "DaemonManager | HostedDaemonCoordinator | None" = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Overlay S1/S2/S3 fields on coordinator_lead findings (post-serialization).

    Takes a list of serialized Finding dicts (Finding.to_dict() output) and
    returns a new list with enrichment fields merged onto coordinator_lead items.
    Non-coordinator-lead findings are passed through unchanged, preserving
    original ordering. ``refined_coordinator_lead`` findings are passed through
    unchanged — their ``suggested_action`` is already present in ``details``
    directly (not nested under ``details["lead"]``).

    Overlay keys are omitted when the signal source is unavailable — key
    absence is not an error.  Partial enrichment (some keys present, others
    absent) is expected in cross-process or pre-first-tick scenarios.

    Hosted-mode behaviour: when runtime is a HostedDaemonCoordinator, S1, S2,
    and S3 are silently skipped (thread_auditor and decision_detector are not
    registered in hosted scopes; hosted S3 is deferred pending a scope-aware
    repo_key accessor).  Callers detect skipped overlays by key absence.

    Args:
      leads: Serialized Finding dicts from watercooler_daemon_findings.
      namespace: Daemon state namespace — never optional.  Prevents cross-tenant
        leakage when multiple users share a hosted deployment.
      repo_key: Resolved repo key for S3 dimension-score lookup.  Must come from
        derive_repo_key(git_root) — a SHA-1 of the filesystem path, not a string.
      runtime: In-process daemon runtime, obtained via get_daemon_runtime().
        None forces cross-process fallbacks (S3 checkpoint read; S1/S2 skipped).

    Returns:
      Tuple of (enriched list, enrichment_stats dict).
      enriched list: new list (does not mutate input), preserving original ordering.
      enrichment_stats: ``{"attempted": int, "succeeded": int, "skipped": int,
        "mode": "local"|"hosted"}`` — counts of S signals attempted, succeeded, and
        skipped (hosted mode).  Useful for observability without log-diving.
    """
    from watercooler_mcp.daemons.hosted_coordinator import HostedDaemonCoordinator

    is_hosted = isinstance(runtime, HostedDaemonCoordinator)
    mode = "hosted" if is_hosted else "local"

    if not any(ld.get("category") == "coordinator_lead" for ld in leads):
        return leads, {"attempted": 0, "succeeded": 0, "skipped": 0, "mode": mode}

    # Track per-signal success for enrichment_stats.
    # s1_ok/s2_ok: True when at least one lead received an overlay from that signal.
    # s3_ok: True when dimension scores were loaded (scoped to repo_key, not per-lead).
    s1_ok = s2_ok = s3_ok = False

    # S1: ThreadAuditor hygiene — LOCAL ONLY
    # Single global read; bucket by topic in Python (avoids N×full-file-parse).
    hygiene_by_topic: dict[str, list[str]] = {}
    if not is_hosted:
        try:
            all_hygiene = load_findings(
                "thread_auditor",
                namespace=namespace,
                limit=_ENRICH_FINDINGS_CAP,
                unacknowledged_only=True,
            )
            for f in all_hygiene:
                hygiene_by_topic.setdefault(f.topic, []).append(f.category)
        except Exception:
            log.debug("enrich_leads: S1 hygiene read failed", exc_info=True)

    # S2: Decision-candidate booster — LOCAL ONLY
    # Same single-global-read pattern as S1.
    decisions_by_topic: dict[str, list[Any]] = {}
    if not is_hosted:
        try:
            all_decisions = load_findings(
                "decision_detector",
                category="decision_candidate",
                namespace=namespace,
                limit=_ENRICH_FINDINGS_CAP,
                unacknowledged_only=True,
            )
            for f in all_decisions:
                decisions_by_topic.setdefault(f.topic, []).append(f)
        except Exception:
            log.debug("enrich_leads: S2 decision read failed", exc_info=True)

    # S3: PulseSnapshot dimension scores — LOCAL now; hosted deferred
    dimension_scores: dict[str, Any] | None = None
    if not is_hosted:
        try:
            dimension_scores = _load_dimension_scores(runtime, namespace, repo_key)
        except Exception:
            log.debug("enrich_leads: S3 dimension scores failed", exc_info=True)

    # Apply overlays: rebuild list in original order, enriching coordinator_lead items.
    result: list[dict[str, Any]] = []
    for lead in leads:
        if lead.get("category") != "coordinator_lead":
            result.append(lead)
            continue

        topic = lead.get("topic", "")
        overlay: dict[str, Any] = {}

        # S1
        tags = hygiene_by_topic.get(topic)
        if tags is not None:
            overlay["hygiene_tags"] = sorted(set(tags))
            s1_ok = True

        # S2
        dec_findings = decisions_by_topic.get(topic)
        if dec_findings:
            s2_ok = True
            overlay["pending_decision_candidates"] = len(dec_findings)
            # Align with AdvisoryAction schema: phase, tool, arguments, reason.
            overlay["suggested_action_override"] = {
                "phase": "pre",
                "tool": "watercooler_daemon_findings",
                "arguments": {
                    "daemon": "decision_detector",
                    "category": "decision_candidate",
                    "topic": topic,
                },
                "reason": (
                    f"{len(dec_findings)} unresolved decision candidate(s) for this topic."
                ),
            }

        # S3 — omit dimension keys that have no score yet (key-absence = unavailable).
        if dimension_scores is not None:
            pulse_ctx = {
                k: v
                for k in (
                    "goal_clarity",
                    "constraint_pressure",
                    "evidence_quality",
                    "execution_momentum",
                )
                if (v := dimension_scores.get(k)) is not None
            }
            if pulse_ctx:
                overlay["pulse_context"] = pulse_ctx
                s3_ok = True

        result.append({**lead, **overlay} if overlay else lead)

    if is_hosted:
        stats: dict[str, Any] = {
            "attempted": 0,
            "succeeded": 0,
            "skipped": 3,
            "mode": "hosted",
        }
    else:
        attempted = 3
        succeeded = sum([s1_ok, s2_ok, s3_ok])
        stats = {
            "attempted": attempted,
            "succeeded": succeeded,
            "skipped": 0,
            "mode": "local",
        }

    return result, stats


def _load_dimension_scores(
    runtime: "DaemonManager | HostedDaemonCoordinator | None",
    namespace: str,
    repo_key: str,
) -> dict[str, Any] | None:
    """Thin wrapper delegating to ``pulse_snapshot.resolve_dimension_scores``.

    Bridges the coordinator-leads caller (which holds the runtime directly) to
    the shared two-path accessor in pulse_snapshot, so both this module and
    pulse_report.py share a single implementation (todo #290).
    """
    from watercooler_mcp.daemons.manager import DaemonManager
    from watercooler_mcp.daemons.pulse_snapshot import resolve_dimension_scores

    mgr = runtime if isinstance(runtime, DaemonManager) else None
    return resolve_dimension_scores(repo_key, namespace, mgr)
