"""CoordinatorRefinerDaemon — L2 LLM synthesis on ``coordinator_lead`` findings.

Reads unacknowledged ``coordinator_lead`` findings produced by
``ProjectCoordinatorDaemon``, sends each lead's normalized payload
(``CoordinatorLead``) to an LLM for narrative synthesis, and emits one refined
finding per eligible raw lead under its own producer identity.

Per-lead 1:1 refinement: no clustering, no multi-dimensional scoring. Output
is narrative only — ``assessment`` (2-4 sentences) + ``recommended_next_step``
(1-2 sentences) — plus verbatim passthrough of ``suggested_action`` and
``t2_context`` from the source lead.

Progressive cursor stored in ``checkpoint.extras["refined_lead_ids"]``. Append
on success; skip-on-failure for retriable cases (LLM unavailable, LLM raise,
parse failure). Cursor advances on malformed payload (permanent skip to avoid
infinite retry).

Fail-open: the daemon never raises to the tick scheduler.

See ``dev_docs/plans/2026-04-21-feat-coordinator-refiner-daemon-design-addendum-plan.md``
for the full contract.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from watercooler.config_schema import CoordinatorRefinerConfig
from watercooler.pulse_stance_lib import CoordinatorLead

from .base import BaseDaemon
from .llm_client import DaemonLLMClient
from .state import Finding, build_finding_id, load_findings

logger = logging.getLogger(__name__)

_MESSAGE_TARGET_CHARS = 160
_SCHEMA_VERSION = 1
_SOURCE_DAEMON = "project_coordinator"
_SOURCE_CATEGORY = "coordinator_lead"

_SYSTEM_PROMPT = """\
You are an engineering project coordinator assistant. You read normalized \
"coordinator leads" — structured hints that flag a specific thread and \
category as worth an agent's attention — and produce short narrative \
synthesis to help an agent investigate.

Your output is strictly two short prose fields:

- `assessment`: 2-4 sentences explaining what is probably going on in this \
thread and why this lead matters. Be concrete. Avoid restating the lead \
verbatim; add synthesis.
- `recommended_next_step`: 1-2 sentences suggesting ONE concrete \
investigation step an agent could take. Do NOT nominate a specific agent \
by name. Do NOT rewrite or replace the `suggested_action` — that is \
read-only by contract. Do NOT produce multi-step plans.

Respond with JSON only, no commentary outside the JSON object:

{
  "assessment": "<2-4 sentences>",
  "recommended_next_step": "<1-2 sentences>"
}
"""


def _parse_llm_response(response: Optional[str]) -> Optional[Dict[str, str]]:
    """Parse LLM JSON response; return None on any failure.

    Strips ```json``` code fences if present. Falls back to extracting the
    first balanced ``{...}`` block. Returns only when both ``assessment`` and
    ``recommended_next_step`` are non-empty strings.
    """
    if response is None:
        return None
    text = response.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start == -1:
            return None
        try:
            data, _ = json.JSONDecoder().raw_decode(text, start)
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    assessment = data.get("assessment")
    next_step = data.get("recommended_next_step")
    if not isinstance(assessment, str) or not isinstance(next_step, str):
        return None
    assessment = assessment.strip()
    next_step = next_step.strip()
    if not assessment or not next_step:
        return None
    return {"assessment": assessment, "recommended_next_step": next_step}


def _derive_message(assessment: str) -> str:
    """Derive a short scannable header from the assessment.

    Contract: non-empty and ≤ ``_MESSAGE_TARGET_CHARS``.
    Takes the first sentence when present, else the whole assessment.
    """
    text = assessment.strip()
    if not text:
        return "coordinator_lead refined"
    match = re.search(r"[.!?]", text)
    header = text[: match.end()].strip() if match else text
    if len(header) > _MESSAGE_TARGET_CHARS:
        header = header[: _MESSAGE_TARGET_CHARS - 1].rstrip() + "…"
    return header


def _build_prompt(lead: CoordinatorLead) -> str:
    """Assemble the per-lead user prompt from the normalized lead payload."""
    summary = str(lead.summary or "(no summary)")[:2000]
    parts = [
        f"Source category: {lead.source_category}",
        f"Source topic: {lead.source_topic}",
        "",
        "## Summary",
        summary,
    ]
    if lead.relevance_tags:
        parts.append("")
        parts.append(f"Relevance tags: {', '.join(lead.relevance_tags)}")
    if lead.suggested_action is not None:
        parts.append("")
        parts.append("## Suggested action (read-only — do NOT rewrite)")
        parts.append(f"- tool: {lead.suggested_action.tool}")
        parts.append(f"- phase: {lead.suggested_action.phase}")
        parts.append(f"- reason: {lead.suggested_action.reason}")
    if lead.t2_context:
        parts.append("")
        parts.append("## Analysis context (T2)")
        for k, v in sorted(lead.t2_context.items()):
            # Strip newlines to prevent injected fake section headers.
            safe_v = str(v).replace("\n", " ").replace("\r", "")
            parts.append(f"- {k}: {safe_v}")
    parts.append("")
    parts.append(
        "Produce JSON with `assessment` (2-4 sentences) and "
        "`recommended_next_step` (1-2 sentences)."
    )
    return "\n".join(parts)


class CoordinatorRefinerDaemon(BaseDaemon):
    """Layer-2 LLM-powered coordinator-lead refinement daemon.

    Reads unacknowledged ``coordinator_lead`` findings from
    ``ProjectCoordinatorDaemon``, synthesizes ``assessment`` +
    ``recommended_next_step`` via an LLM, and emits
    ``refined_coordinator_lead`` findings under its own producer identity.

    Args:
        interval: Seconds between refinement ticks.
        config: ``CoordinatorRefinerConfig`` for tuning (see addendum §Config
            Knobs).
        llm_client: Override LLM client (None → create from config).
        enabled: Whether the daemon is active (BaseDaemon-level flag;
            separate from ``config.enabled`` which is consulted inside
            ``tick()`` as defense-in-depth).
    """

    def __init__(
        self,
        *,
        interval: float = 600.0,
        config: Optional[CoordinatorRefinerConfig] = None,
        llm_client: Optional[DaemonLLMClient] = None,
        enabled: bool = True,
    ) -> None:
        super().__init__(
            name="coordinator_refiner",
            interval=interval,
            enabled=enabled,
            tick_on_interval=True,
        )
        self._config = config or CoordinatorRefinerConfig()
        self._llm_client = llm_client
        self._ticks_since_gc: int = 0

    def _get_llm_client(self) -> DaemonLLMClient:
        if self._llm_client is None:
            self._llm_client = DaemonLLMClient(daemon_name="coordinator_refiner")
        return self._llm_client

    def _get_refined_ids(self) -> List[str]:
        return self._checkpoint.extras.get("refined_lead_ids", [])

    def _set_refined_ids(self, ids: List[str]) -> None:
        self._checkpoint.extras["refined_lead_ids"] = ids

    def _gc_refined_ids(self, live_lead_ids: set[str]) -> None:
        """Prune cursor entries whose source lead id is no longer live."""
        current = self._get_refined_ids()
        pruned = [fid for fid in current if fid in live_lead_ids]
        removed = len(current) - len(pruned)
        if removed > 0:
            logger.debug(
                "DAEMON[coordinator_refiner]: cursor GC pruned %d stale IDs",
                removed,
            )
            self._set_refined_ids(pruned)

    def tick(self) -> List[Finding]:
        """Run one refinement cycle.

        Fail-open: returns ``[]`` on any terminal skip (disabled, LLM
        unavailable, no leads, empty batch). Per-lead failures are absorbed
        and do not abort the tick.
        """
        cfg = self._config

        if not cfg.enabled:
            return []

        llm = self._get_llm_client()
        if not llm.is_available():
            logger.debug("DAEMON[coordinator_refiner]: LLM unavailable, skipping tick")
            return []

        # limit=None: cursor filter runs against the full unacknowledged set.
        # A hard ceiling applied before cursor filtering would leave leads
        # beyond the limit permanently unreachable once the first N are refined.
        # max_leads_per_tick is the only batch cap; JSONL compaction (~5 000
        # lines) bounds memory in practice.
        raw_leads = load_findings(
            _SOURCE_DAEMON,
            limit=None,
            category=_SOURCE_CATEGORY,
            unacknowledged_only=True,
            namespace=self.state_namespace,  # namespace is scope-level; routes to project_coordinator's subdir within same scope
            order="oldest",
        )
        if not raw_leads:
            logger.debug("DAEMON[coordinator_refiner]: no coordinator_lead findings")
            return []

        live_lead_ids = {f.finding_id for f in raw_leads}
        self._ticks_since_gc += 1
        if self._ticks_since_gc >= cfg.cursor_gc_interval:
            self._gc_refined_ids(live_lead_ids)
            self._ticks_since_gc = 0

        # Single read post-GC so both the set-check and the append use the same snapshot.
        refined_ids = self._get_refined_ids()
        refined_set = set(refined_ids)

        # Stable ordering across ticks: created_at ASC, then finding_id ASC.
        raw_leads.sort(key=lambda f: (f.created_at, f.finding_id))

        candidates = [f for f in raw_leads if f.finding_id not in refined_set]
        batch = candidates[: cfg.max_leads_per_tick]

        findings: List[Finding] = []
        refined_this_tick: List[str] = []

        for raw in batch:
            try:
                refined, advance = self._refine_lead(raw, llm, cfg)
            except Exception as exc:
                logger.exception(
                    "DAEMON[coordinator_refiner]: unexpected error refining %s: %s",
                    raw.finding_id,
                    exc,
                )
                refined, advance = None, False
            if refined is not None:
                findings.append(refined)
            if advance:
                refined_this_tick.append(raw.finding_id)

        if refined_this_tick:
            self._set_refined_ids(refined_ids + refined_this_tick)

        self._checkpoint.threads_processed = len(batch)
        logger.debug(
            "DAEMON[coordinator_refiner]: processed %d leads, %d refined",
            len(batch),
            len(findings),
        )
        return findings

    def _refine_lead(
        self,
        raw: Finding,
        llm: DaemonLLMClient,
        cfg: CoordinatorRefinerConfig,
    ) -> Tuple[Optional[Finding], bool]:
        """Refine a single raw ``coordinator_lead``.

        Returns ``(finding_or_none, advance_cursor)``:
        - ``(Finding, True)`` — success; include finding, advance cursor.
        - ``(None, False)`` — retriable skip (LLM unavailable, LLM raise/None,
          parse failure).
        - ``(None, True)`` — permanent skip (malformed payload); cursor
          advances so a bad record doesn't retry forever.
        """
        lead_payload = (
            raw.details.get("lead") if isinstance(raw.details, dict) else None
        )
        if not isinstance(lead_payload, dict):
            logger.warning(
                "DAEMON[coordinator_refiner]: skip malformed lead "
                "(no 'lead' payload) %s",
                raw.finding_id,
            )
            return None, True

        try:
            lead = CoordinatorLead.from_dict(lead_payload)
        except Exception as exc:
            logger.warning(
                "DAEMON[coordinator_refiner]: skip malformed lead %s: %s",
                raw.finding_id,
                exc,
            )
            return None, True

        prompt = _build_prompt(lead)
        try:
            response = llm.complete(
                prompt=prompt,
                system=_SYSTEM_PROMPT,
                max_tokens=cfg.llm_max_tokens,
                temperature=cfg.llm_temperature,
                timeout=cfg.llm_timeout_seconds,
            )
        except Exception as exc:
            logger.debug(
                "DAEMON[coordinator_refiner]: LLM raised for %s: %s",
                raw.finding_id,
                exc,
            )
            return None, False
        if response is None:
            logger.debug(
                "DAEMON[coordinator_refiner]: LLM returned None for %s",
                raw.finding_id,
            )
            return None, False

        parsed = _parse_llm_response(response)
        if parsed is None:
            logger.warning(
                "DAEMON[coordinator_refiner]: parse failure for %s: %s",
                raw.finding_id,
                response[:500],
            )
            return None, False

        # Verbatim passthrough: read directly from the raw lead payload so
        # `from_dict()`'s schema migrations / AdvisoryAction drops do not
        # alter the preserved shape. Tests assert deep equality.
        raw_action = lead_payload.get("suggested_action")
        raw_t2 = lead_payload.get("t2_context")
        details: Dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "source_finding_id": raw.finding_id,
            "source_category": lead.source_category,
            "source_topic": lead.source_topic,
            "source_summary": lead.summary or "",
            "assessment": parsed["assessment"],
            "recommended_next_step": parsed["recommended_next_step"],
            "relevance_tags": list(lead.relevance_tags),
            "suggested_action": (
                copy.deepcopy(raw_action) if isinstance(raw_action, dict) else None
            ),
            # source_t2_context: key always present; None when raw lead has no t2_context.
            "source_t2_context": (copy.deepcopy(raw_t2) if isinstance(raw_t2, dict) else None),
        }

        return (
            Finding(
                finding_id=build_finding_id(
                    self.state_namespace,
                    self.name,
                    lead.source_topic,
                    "refined_coordinator_lead",
                    raw.finding_id,
                ),
                daemon_name=self.name,
                severity="info",
                category="refined_coordinator_lead",
                topic=lead.source_topic,
                message=_derive_message(parsed["assessment"]),
                details=details,
            ),
            True,
        )
