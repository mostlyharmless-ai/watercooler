"""EnrichSupersessionDaemon — report-only one-hop ``superseded_by`` enrichment.

Periodically records (or, once trusted, writes) the one-hop ``superseded_by`` link on
superseded T2 edges via :meth:`GraphitiBackend.enrich_superseded_by` (issue #991, the
earned-edge RFC's P2 supersession-enrichment slice).

Report-only first: ``emit_mode="monitor"`` (default) computes the links that *would* be
written and emits a finding, but makes no graph changes; ``emit_mode="emit"`` writes the
links. The links are *afforded* (probabilistic) earned edges — never authored. Ratifying
them (relevance → authority) is a separate human step, so this daemon **records, never
decides** (authority ladder ``01KS0JTK0RT4EC0M92PMX19XRA``). Opt-in (``enabled=False``).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, List, Optional

from .base import BaseDaemon
from .daemon_write import daemon_write_entry
from .state import Finding

logger = logging.getLogger(__name__)

_ACTOR = "EnrichSupersessionDaemon"


def _format_supersession_candidate_body(
    superseded: str, successor: str, basis: Optional[str]
) -> str:
    """Render the earned_edge supersession candidate body (EXACT promote contract).

    The line prefixes (``Superseded-Entry:`` / ``Superseded-By-Entry:``) are parsed
    verbatim by ``watercooler_promote_candidate(target_type="Supersession")`` — do not
    reorder or reword them. ``basis`` may be ``None`` (older enrichment without a
    recorded ``superseded_basis``); it renders as ``unknown`` so the body stays valid.
    """
    basis_str = basis if basis else "unknown"
    return (
        "Spec: general-purpose\n"
        "Promotion-Source: earned_edge\n"
        f"Superseded-Entry: {superseded}\n"
        f"Superseded-By-Entry: {successor}\n"
        f"Basis: {basis_str}\n"
        "Authority: none\n"
        "Candidate-Status: needs_human_confirmation\n"
        "\n"
        f"Entry {superseded} appears superseded by entry {successor} "
        f"(inferred; basis={basis_str}). Confirm via "
        'watercooler_promote_candidate(target_type="Supersession") to ratify.'
    )


class EnrichSupersessionDaemon(BaseDaemon):
    """Scheduled, report-only ``superseded_by`` enrichment over the project T2 graph."""

    def __init__(
        self,
        *,
        backend: Any,
        interval: float = 900.0,
        emit_mode: str = "monitor",
        emit_bases: Optional[frozenset] = None,
        group_id: Optional[str] = None,
        code_root: Optional[Path] = None,
        enabled: bool = False,
    ) -> None:
        if emit_mode not in ("monitor", "emit"):
            raise ValueError(f"emit_mode must be 'monitor' or 'emit', got {emit_mode!r}")
        super().__init__(
            name="enrich_supersession",
            interval=interval,
            enabled=enabled,
            tick_on_interval=True,
        )
        self._backend = backend
        self._emit_mode = emit_mode
        # Tiered emit (Decision 01KWJK1CS4C5DY8CS735ZBMMQP): bases eligible for
        # writes. None → the backend's ratified strong-tier default.
        self._emit_bases = frozenset(emit_bases) if emit_bases is not None else None
        self._group_id_override = group_id
        self._code_root_override = code_root
        # Per-tick observable metrics.
        self._last_tick_candidates = 0
        self._last_tick_written = 0
        # In-memory dedup of already-emitted (superseded, successor) entry pairs, so
        # the same afforded link is not re-emitted as a candidate every tick.
        self._emitted_pairs: set[tuple[str, str]] = set()

    def _resolve_group_id(self) -> Optional[str]:
        """Resolve the canonical project group_id (== T2 database == graph name)."""
        if self._group_id_override:
            return self._group_id_override
        gid = getattr(getattr(self._backend, "config", None), "database", None)
        if gid:
            return gid
        try:
            from pathlib import Path

            from watercooler.path_resolver import derive_t2_database_name

            return derive_t2_database_name(code_path=Path.cwd())
        except Exception as exc:
            logger.debug(
                "DAEMON[enrich_supersession]: could not resolve group_id: %s", exc
            )
            return None

    def tick(self) -> List[Finding]:
        gid = self._resolve_group_id()
        if not gid:
            return []

        dry_run = self._emit_mode != "emit"
        try:
            pairs = self._backend.enrich_superseded_by(
                gid, dry_run=dry_run, emit_bases=self._emit_bases
            )
        except Exception as exc:
            # Backend/connection hiccup — a transient condition, not a signal to spam.
            logger.debug("DAEMON[enrich_supersession]: enrichment failed: %s", exc)
            return []

        # Tiered emit: the backend marks each pair ``written`` per its basis
        # allowlist (a missing flag means a legacy backend that writes all).
        eligible = sum(1 for p in pairs if p.get("written", True))
        held = len(pairs) - eligible
        self._last_tick_candidates = len(pairs)
        self._last_tick_written = 0 if dry_run else eligible

        # Emit earned_edge supersession candidates (emit mode only; monitor stays
        # findings-only, per the authority ladder — this daemon records, never decides).
        # Runs independently of this tick's edge writes: afforded links written on
        # earlier ticks still need candidates emitted. Never raises.
        if self._emit_mode == "emit":
            self._emit_supersession_candidates(gid)

        if not pairs:
            return []

        from ulid import ULID

        verb = "would link" if dry_run else "linked"
        held_note = f" (held {held} below emit tier)" if held else ""
        return [
            Finding(
                finding_id=str(ULID()),
                daemon_name=self.name,
                severity="info",
                category="supersession_enriched",
                topic="",
                message=f"superseded_by {verb} {eligible} edge(s){held_note} in {gid}",
                details={
                    "group_id": gid,
                    "emit_mode": self._emit_mode,
                    "dry_run": dry_run,
                    "count": len(pairs),
                    "eligible": eligible,
                    "held": held,
                    "pairs": pairs[:50],
                },
                created_at=time.time(),
            )
        ]

    # -- earned_edge candidate emission (emit mode only) ----------------------

    def _resolve_code_root(self) -> Optional[Path]:
        """Resolve the repo root for candidate writes (override wins, else cwd ctx)."""
        if self._code_root_override is not None:
            return self._code_root_override
        try:
            from watercooler_mcp.config import resolve_thread_context

            ctx = resolve_thread_context(Path.cwd())
            return ctx.code_root
        except Exception as exc:  # noqa: BLE001 — best-effort resolution
            logger.debug(
                "DAEMON[enrich_supersession]: could not resolve code_root: %s", exc
            )
            return None

    def _already_ratified(
        self, thread: str, superseded: str, successor: str, code_root: Path
    ) -> bool:
        """True iff an ``xref_supersedes`` annotation already ratifies A → B.

        Reuses the read-only, hosted/local-aware resolver from ``tools.decisions`` so a
        ratified pair is not re-emitted as a candidate. Degrades to ``False`` (not
        ratified) on any error — a duplicate candidate is harmless; a swallowed exception
        that skips a real one is not. Never raises.
        """
        try:
            from watercooler_mcp.tools.decisions import _supersession_is_ratified

            return _supersession_is_ratified(
                thread, superseded, successor, str(code_root)
            )
        except Exception as exc:  # noqa: BLE001 — best-effort, never raise in a tick
            logger.debug(
                "DAEMON[enrich_supersession]: ratified check failed for %s→%s: %s",
                superseded,
                successor,
                exc,
            )
            return False

    def _emit_supersession_candidates(self, gid: str) -> None:
        """Emit one ``needs_human_confirmation`` supersession candidate Note per afforded
        entry pair, deduped and skipping already-ratified pairs. Never raises."""
        try:
            pairs = self._backend.afforded_supersession_entry_pairs(gid)
        except Exception as exc:  # noqa: BLE001 — transient backend hiccup
            logger.debug(
                "DAEMON[enrich_supersession]: afforded-pair read failed: %s", exc
            )
            return

        code_root = self._resolve_code_root()
        if code_root is None:
            logger.debug(
                "DAEMON[enrich_supersession]: no code_root; cannot emit candidates"
            )
            return

        # Hosted-scoped: adopt the tenant's full write identity from the coordinator-
        # installed scope — the worktree clone (write+push target) AND the tenant
        # repo/branch, so the candidate's code_branch tag matches the branch the
        # ratification reads run against (a server-branch tag would be invisible to
        # branch-filtered tenant reads). All None in local mode → code_root is used.
        scope_threads_dir = getattr(self, "_threads_dir_override", None)
        scope_ctx = getattr(self, "_scope_context", None)
        scope_repo = getattr(scope_ctx, "repo", None) if scope_ctx is not None else None
        # Prefer effective_branch (defaults to "main") over the raw nullable branch, so a
        # default-branch scope tags the candidate with the tenant's real read branch.
        scope_branch = None
        if scope_ctx is not None:
            scope_branch = getattr(scope_ctx, "effective_branch", None) or getattr(
                scope_ctx, "branch", None
            )

        for pair in pairs:
            try:
                superseded = pair.get("superseded_entry")
                successor = pair.get("successor_entry")
                thread = pair.get("thread")
                basis = pair.get("basis")
                if not superseded or not successor or not thread:
                    # No thread → no place to emit the on-thread candidate; skip.
                    continue
                key = (superseded, successor)
                if key in self._emitted_pairs:
                    continue
                if self._already_ratified(thread, superseded, successor, code_root):
                    self._emitted_pairs.add(key)
                    continue
                body = _format_supersession_candidate_body(superseded, successor, basis)
                result = daemon_write_entry(
                    thread,
                    code_root=code_root,
                    threads_dir=scope_threads_dir,
                    code_repo=scope_repo,
                    code_branch=scope_branch,
                    title=f"Supersession candidate: {superseded} → {successor}",
                    body=body,
                    agent=_ACTOR,
                    role="scribe",
                    entry_type="Note",
                    agent_spec="general-purpose",
                    user_tag="system",
                )
                if result.written:
                    self._emitted_pairs.add(key)
                else:
                    logger.debug(
                        "DAEMON[enrich_supersession]: candidate write failed for "
                        "%s→%s on %s: %s",
                        superseded,
                        successor,
                        thread,
                        result.error,
                    )
            except Exception as exc:  # noqa: BLE001 — one bad pair must not abort the tick
                logger.debug(
                    "DAEMON[enrich_supersession]: candidate emission error: %s", exc
                )
