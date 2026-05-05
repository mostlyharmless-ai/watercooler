"""DecisionStanceDaemon — open-core role-stance producer.

Emits ``stance_advisory`` findings for ``planner`` / ``critic`` / ``tester`` roles
driven entirely by the open-core decision pipeline (DetectDecisionsDaemon +
ExtractDecisionsDaemon). Mirrors the emission contract of
``ProjectCoordinatorDaemon._emit_stance_advisories`` — same finding ``category``,
``topic`` namespace, ``details["advisory"]`` payload, and replace-on-change
dedup — so agents consume stance identically regardless of producer.

This daemon is registered only when ``project_coordinator`` is not registered
in the same ``init_daemons`` call (premium signal richness wins where both
could run).
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Iterable
from dataclasses import asdict, replace
from pathlib import Path

from watercooler.config_schema import DecisionStanceConfig
from watercooler.pulse_stance_lib import (
    STANCE_ROLES,
    StanceAdvisory,
    extract_decision_stance_signals,
    pulse_to_stance,
    resolve_decision_source_ids,
)

from .base import BaseDaemon
from .state import Finding, build_finding_id, load_findings

logger = logging.getLogger(__name__)

# Re-sync dedup cache from disk every N ticks to evict acknowledged/compacted keys.
_DEDUP_RESYNC_INTERVAL = 10

# Hard cap on findings loaded for dedup — prevents unbounded memory growth.
_DEDUP_LIMIT = 50_000


def _build_emission_signature(
    advisory_signature: str,
    source_lead_ids: Iterable[str],
    *,
    truncated: bool = False,
) -> str:
    """Build the daemon-internal dedup key for a stance advisory.

    The advisory's public ``advisory_signature`` reflects rule-driven
    identity (level + triggered_signals + threshold buckets). It does NOT
    include ``source_lead_ids`` or the truncation bit. For a steady-state
    SOFT/HARD bucket, the signature stays constant while the underlying
    detector/extractor findings rotate out of the rolling window. Re-emission
    must fire when provenance shifts, otherwise the persisted advisory
    keeps citing aged-out finding IDs.

    This helper folds a hash of the (sorted) source ID set plus the
    truncation flag into the dedup key so a provenance change forces a
    fresh ``finding_id`` and thus a new on-disk record. The advisory's
    ``advisory_signature`` field itself stays unchanged.

    The ``truncated`` flag participates because the persisted source ID
    list can stay identical across ticks while the cap status flips — the
    11th matching finding entering the window does not change the first-10
    sorted slice but does change "is this provenance complete?". Without
    folding the flag, a persisted ``source_lead_ids_truncated`` bit could
    drift from reality.
    """
    ids = sorted(source_lead_ids or ())
    if not ids and not truncated:
        return advisory_signature
    payload = "|".join(ids)
    if truncated:
        payload += "|TRUNC"
    src_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
    return f"{advisory_signature}:{src_hash}"


class DecisionStanceDaemon(BaseDaemon):
    """Open-core stance producer driven by decision-pipeline findings.

    Args:
        interval: Seconds between stance evaluations.
        config: ``DecisionStanceConfig`` instance (defaults if None).
        threads_dir: Override threads directory (None = resolve at tick time).
        enabled: Whether this daemon is active.
    """

    def __init__(
        self,
        *,
        interval: float = 600.0,
        config: DecisionStanceConfig | None = None,
        threads_dir: Path | None = None,
        enabled: bool = True,
    ) -> None:
        super().__init__(
            name="decision_stance",
            interval=interval,
            enabled=enabled,
            tick_on_interval=True,
        )
        self._config = config or DecisionStanceConfig()
        self._threads_dir_override = threads_dir
        self._resolved_threads_dir: Path | None = None
        self._scope_id: str = ""

        # Dedup: finding_id -> already on disk
        self._existing_keys: set[str] = set()
        self._ticks_since_resync: int = 0
        # First-tick bootstrap is a one-shot. Tracked explicitly because
        # `not self._existing_keys` would re-fire on every idle tick (no
        # findings ever emitted), causing redundant disk reads.
        self._bootstrapped: bool = False
        # Per-role last advisory signature, used for replace-on-change dedup.
        self._last_stance_signatures: dict[str, str] = {role: "" for role in STANCE_ROLES}

        # Per-tick observability
        self._last_tick_findings: int = 0
        self._last_stance_levels: dict[str, int] = {}
        self._last_source_ids_truncated: dict[str, bool] = {
            role: False for role in STANCE_ROLES
        }

    # ------------------------------------------------------------------ #
    # Resolution helpers
    # ------------------------------------------------------------------ #

    def _resolve_threads_dir(self) -> Path | None:
        """Resolve and cache the threads directory.

        Uses the same resolution path as the coordinator so finding IDs share
        the same scope namespace within a repo.
        """
        if self._threads_dir_override is not None:
            return self._threads_dir_override

        if self._resolved_threads_dir is not None:
            return self._resolved_threads_dir

        try:
            from watercooler_mcp.config import resolve_thread_context

            ctx = resolve_thread_context(Path.cwd())
            self._resolved_threads_dir = ctx.threads_dir
            try:
                from watercooler.pulse_snapshot_lib import derive_repo_key

                self._scope_id = derive_repo_key(ctx.code_root)
            except Exception:
                self._scope_id = ""
            return self._resolved_threads_dir
        except Exception as exc:
            logger.debug(
                "DAEMON[decision_stance]: could not resolve threads_dir: %s", exc
            )
            return None

    def _resync_dedup(self) -> None:
        """Refresh the on-disk dedup set to the *latest* fid per role.

        Restoring every historical ``stance_advisory`` fid would re-suppress
        a legitimate revert (B→A or L0→A): once we move off a signature, its
        fid stops representing current state, so the next time the same
        signature reappears the advisory must re-emit. Only the most recent
        fid per topic ever represents "the current view we already wrote
        out", so that's all the dedup set needs to carry across a resync.
        """
        existing = load_findings(
            self.name,
            limit=_DEDUP_LIMIT,
            category="stance_advisory",
            namespace=self.state_namespace,
        )
        # ``load_findings`` returns newest-first by default — first hit per
        # topic is its current head; everything older is superseded.
        seen_topics: set[str] = set()
        keys: set[str] = set()
        for f in existing:
            if f.topic in seen_topics:
                continue
            seen_topics.add(f.topic)
            keys.add(f.finding_id)
        self._existing_keys = keys
        self._ticks_since_resync = 0

    def _bootstrap_signatures_from_disk(self) -> None:
        """Rebuild per-role last_signature state from the latest on-disk finding.

        Loaded once on first tick so a daemon restart does not re-emit a
        currently-active advisory under a fresh dedup signature.
        """
        recent = load_findings(
            self.name,
            limit=200,
            category="stance_advisory",
            namespace=self.state_namespace,
        )
        seen: set[str] = set()
        # ``load_findings`` returns newest-first, so the first hit per role
        # is the most recent advisory for that role.
        for f in recent:
            if not f.topic.startswith("stance:"):
                continue
            role = f.topic.split(":", 1)[1]
            if role in seen or role not in STANCE_ROLES:
                continue
            seen.add(role)
            details = f.details or {}
            advisory = details.get("advisory") or {}
            sig = advisory.get("advisory_signature", "")
            level = advisory.get("level", 0)
            # If the latest finding for this role is the tombstone (cleared),
            # treat the in-memory signature as empty so we don't suppress the
            # next escalation.
            if level == 0 or not sig:
                self._last_stance_signatures[role] = ""
            else:
                # Reconstruct the emission signature (advisory_signature plus
                # provenance hash and truncation flag) so dedup matches what
                # live ticks compute. ``asdict()`` serializes
                # ``source_lead_ids`` as a list; the truncation bit lives on
                # the Finding wrapper's details (not the advisory dict).
                src_ids = advisory.get("source_lead_ids") or []
                truncated = bool(details.get("source_lead_ids_truncated", False))
                self._last_stance_signatures[role] = _build_emission_signature(
                    sig, src_ids, truncated=truncated
                )

    # ------------------------------------------------------------------ #
    # Signal collection
    # ------------------------------------------------------------------ #

    def _collect_signals(self, now: float) -> tuple[list[dict], list[dict], int]:
        """Load detector + extractor findings inside the rolling window.

        Returns:
            (detector_findings_in_window, extractor_findings_in_window,
             recent_decisions_count). The decision count is currently the
             count of ``extraction_success`` findings — open-core proxy for
             "decisions recorded recently".
        """
        window = float(self._config.window_seconds)
        cutoff = now - window

        det = [
            {
                "finding_id": f.finding_id,
                "topic": f.topic,
                "entry_id": f.entry_id,
                "category": f.category,
                "details": f.details,
                "created_at": f.created_at,
            }
            for f in load_findings(
                "decision_detector",
                limit=_DEDUP_LIMIT,
                category="decision_candidate",
                namespace=self.state_namespace,
            )
            if f.created_at >= cutoff
        ]

        ext = [
            {
                "finding_id": f.finding_id,
                "topic": f.topic,
                "entry_id": f.entry_id,
                "category": f.category,
                "details": f.details,
                "created_at": f.created_at,
            }
            for f in load_findings(
                "decision_extractor",
                limit=_DEDUP_LIMIT,
                namespace=self.state_namespace,
            )
            if f.created_at >= cutoff
        ]

        # Open-core proxy: extraction_success count as recent recorded decisions.
        # Hand-authored Decisions are not counted in Phase 1; this is documented
        # in the proposal thread and the Phase 2 backlog.
        # TODO(phase-2): replace with baseline-graph Decision scan so projects
        # that hand-author Decision entries (without going through the
        # extractor) don't perma-arm the tester drought row.
        recent_decisions = sum(1 for f in ext if f["category"] == "extraction_success")

        return det, ext, recent_decisions

    # ------------------------------------------------------------------ #
    # Tick
    # ------------------------------------------------------------------ #

    def tick(self) -> list[Finding]:
        """Emit one batch of stance advisories per role.

        Idempotent across ticks via per-role advisory-signature dedup, with
        a tombstone emitted on transitions back to L0.
        """
        # Resolve threads_dir for scope_id (best-effort — empty scope is OK
        # for hosted/disk-less environments; finding IDs stay deterministic).
        self._resolve_threads_dir()

        # Re-sync dedup periodically so acknowledged / compacted keys age out.
        self._ticks_since_resync += 1
        if not self._bootstrapped or self._ticks_since_resync >= _DEDUP_RESYNC_INTERVAL:
            self._resync_dedup()
            if not self._bootstrapped:
                self._bootstrap_signatures_from_disk()
                self._bootstrapped = True

        now = time.time()
        detector_findings, extractor_findings, recent_decisions = self._collect_signals(
            now
        )
        signals = extract_decision_stance_signals(
            detector_findings=detector_findings,
            extractor_findings=extractor_findings,
            recent_decisions_count=recent_decisions,
        )

        findings: list[Finding] = []
        levels: dict[str, int] = {}
        truncated_by_role: dict[str, bool] = {}
        for role in STANCE_ROLES:
            advisory = pulse_to_stance(role, signals)
            ids, truncated = resolve_decision_source_ids(
                triggered_signals=advisory.triggered_signals,
                detector_findings=detector_findings,
                extractor_findings=extractor_findings,
            )
            advisory = replace(advisory, source_lead_ids=ids)
            levels[role] = advisory.level
            truncated_by_role[role] = truncated
            self._emit_for_role(advisory, findings, truncated=truncated)

        self._last_stance_levels = levels
        self._last_source_ids_truncated = truncated_by_role
        self._last_tick_findings = len(findings)
        return findings

    # ------------------------------------------------------------------ #
    # Emission helper — replace-on-change dedup
    # ------------------------------------------------------------------ #

    def _emit_for_role(
        self,
        advisory: StanceAdvisory,
        out: list[Finding],
        *,
        truncated: bool = False,
    ) -> None:
        """Append a Finding for this role's advisory iff the signature changed.

        Mirrors ``ProjectCoordinatorDaemon._emit_stance_advisories`` semantics:
        - L1+ with new signature → emit; replace previous.
        - L1+ unchanged signature → no-op (deduped).
        - L0 with prior elevation → emit a "cleared" tombstone exactly once.
        - L0 with no prior elevation → no-op.

        The dedup signature is the *emission signature* — advisory identity
        plus a hash of ``source_lead_ids`` plus the truncation flag. This
        ensures a steady-state SOFT/HARD bucket re-emits when provenance
        rotates (old detector/extractor findings age out of the rolling
        window and are replaced by fresh ones), so persisted advisories
        never cite stale IDs.

        When ``truncated`` is True, ``details["source_lead_ids_truncated"]``
        is set on the emitted Finding so consumers reading the persisted
        record can tell that ``source_lead_ids`` is a partial view (capped
        at ``_SOURCE_LEAD_IDS_CAP``). Mirrors the premium coordinator's
        contract.
        """
        role = advisory.role
        prev_sig = self._last_stance_signatures.get(role, "")
        emission_sig = _build_emission_signature(
            advisory.advisory_signature,
            advisory.source_lead_ids,
            truncated=truncated,
        )

        topic = f"stance:{role}"

        if advisory.level == 0:
            if not prev_sig:
                return  # nothing was elevated — nothing to clear
            # Drop the prior fid so a future re-escalation can re-emit even if
            # it lands on the same signature.
            prev_fid = build_finding_id(
                scope_id=self._scope_id,
                daemon_name=self.name,
                topic=topic,
                category="stance_advisory",
                entry_id="",
                dedup_signature=prev_sig,
            )
            self._existing_keys.discard(prev_fid)

            cleared_fid = build_finding_id(
                scope_id=self._scope_id,
                daemon_name=self.name,
                topic=topic,
                category="stance_advisory",
                entry_id="",
                dedup_signature="cleared",
            )
            if cleared_fid not in self._existing_keys:
                out.append(
                    Finding(
                        finding_id=cleared_fid,
                        daemon_name=self.name,
                        severity="info",
                        category="stance_advisory",
                        topic=topic,
                        entry_id="",
                        message=(
                            f"{role.title()} stance cleared"
                            " — all signals below thresholds"
                        ),
                        details={"advisory": asdict(advisory)},
                    )
                )
                self._existing_keys.add(cleared_fid)
            self._last_stance_signatures[role] = ""
            return

        # L1+ advisory.
        if prev_sig and prev_sig != emission_sig:
            # Emission signature shifted while staying elevated — either the
            # rule-driven advisory_signature changed, or the source_lead_ids
            # rotated. Drop the stale fid so a future cycle back to the
            # previous emission signature can re-emit.
            stale_fid = build_finding_id(
                scope_id=self._scope_id,
                daemon_name=self.name,
                topic=topic,
                category="stance_advisory",
                entry_id="",
                dedup_signature=prev_sig,
            )
            self._existing_keys.discard(stale_fid)
        elif not prev_sig:
            # Escalating from L0 — discard the old tombstone fid so a future
            # L1 → L0 transition emits a fresh "cleared" finding.
            tombstone_fid = build_finding_id(
                scope_id=self._scope_id,
                daemon_name=self.name,
                topic=topic,
                category="stance_advisory",
                entry_id="",
                dedup_signature="cleared",
            )
            self._existing_keys.discard(tombstone_fid)

        fid = build_finding_id(
            scope_id=self._scope_id,
            daemon_name=self.name,
            topic=topic,
            category="stance_advisory",
            entry_id="",
            dedup_signature=emission_sig,
        )
        if fid not in self._existing_keys:
            # ``source_lead_ids_truncated`` lives on the Finding wrapper's
            # details (not StanceAdvisory, which is frozen and has no
            # details field). Mirrors the premium coordinator's contract —
            # only set when the cap was hit, omitted otherwise to keep the
            # payload tight.
            details: dict = {"advisory": asdict(advisory)}
            if truncated:
                details["source_lead_ids_truncated"] = True
            out.append(
                Finding(
                    finding_id=fid,
                    daemon_name=self.name,
                    severity="warning" if advisory.level >= 2 else "info",
                    category="stance_advisory",
                    topic=topic,
                    entry_id="",
                    message=advisory.summary,
                    details=details,
                )
            )
            self._existing_keys.add(fid)
        self._last_stance_signatures[role] = emission_sig

    # ------------------------------------------------------------------ #
    # Observability
    # ------------------------------------------------------------------ #

    def status_summary(self) -> dict:
        """Return a snapshot of last-tick observability for diagnostics.

        Merges with ``BaseDaemon.status_summary`` so MCP consumers
        (``watercooler_daemon_status``) see the same lifecycle fields
        (``status``, ``enabled``, ``total_ticks``, ``last_run``, ``error_count``)
        as every other daemon.
        """
        base = super().status_summary()
        base.update(
            {
                "last_tick_findings": self._last_tick_findings,
                "last_stance_levels": dict(self._last_stance_levels),
                "last_source_ids_truncated": dict(self._last_source_ids_truncated),
                "window_seconds": float(self._config.window_seconds),
            }
        )
        return base
