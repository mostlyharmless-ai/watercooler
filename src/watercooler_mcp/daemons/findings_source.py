"""Resolve which daemon findings logs are the active source of truth.

Single source of truth for the stance-producer conflict-resolution gate:
``resolve_local_stance_producer`` encodes the *config-derived* registration
decision once, and both ``daemons/__init__.py::init_daemons`` (which
registers the daemons) and this module's Stop-hook resolvers call it — so
the two can never drift at the composition level. ``init_daemons`` also
serializes the result to a sidecar file (see
``write_active_stance_producer_sidecar``) that the Stop hook reads directly,
avoiding a full config build + daemons-package import on every turn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import daemon_execution_policy, is_coordinator_active
from .state import _daemon_dir

logger = logging.getLogger(__name__)

# Sidecar written by the daemon-owning process naming the active local stance
# producer (empty file = none). The Stop hook reads this to skip importing the
# daemons package and building the full pydantic config on every turn. Kept as
# a bare path (not via ``_daemon_dir``) so a strict-namespace deployment never
# raises on the write, and so the Stop hook can read it with a plain file open.
ACTIVE_STANCE_PRODUCER_SIDECAR = (
    Path.home() / ".watercooler" / "daemons" / "active_stance_producer"
)


@dataclass(frozen=True)
class FindingsSource:
    """A daemon findings log a reader (e.g. the Stop hook) should poll."""

    daemon_name: str
    findings_path: Path


def resolve_local_stance_producer(daemons_config: Any, transport: str) -> Optional[str]:
    """Return the daemon name that registers as the LOCAL ``stance_advisory``
    producer for this config, or ``None`` if none does.

    This is the single encoding of the config-derived registration decision,
    called by both ``init_daemons`` (to decide what to register) and
    ``resolve_active_stance_producer`` (to decide what to read):

    - The global ``daemons.enabled`` gate: if false, ``init_daemons``
      registers nothing, so there is no local producer.
    - ``project_coordinator`` is the producer iff enabled and
      ``daemon_execution_policy(...)`` resolves to ``"local"`` (it can also
      resolve to ``"hosted"`` when routed to the premium hosted coordinator,
      or ``"skip"`` when disabled/route="disabled").
    - Otherwise ``decision_stance`` is the producer iff enabled AND the
      coordinator is not *active in any form* (local or hosted) — matching
      the ``coordinator_active`` suppression in ``init_daemons`` that avoids
      double emission under the same ``stance:{role}`` topic.

    Does NOT account for hosted-mode or the per-repo daemon lock — those are
    process-runtime state, not config. Callers that may run outside the
    daemon-owning process (e.g. the Stop hook via
    ``resolve_active_stance_producer``) apply those separately.
    """
    if not getattr(daemons_config, "enabled", False):
        return None
    pc_cfg = daemons_config.project_coordinator
    if pc_cfg.enabled and (
        daemon_execution_policy(
            "project_coordinator", pc_cfg, transport, in_hosted_coordinator=False
        )
        == "local"
    ):
        return "project_coordinator"
    if is_coordinator_active(pc_cfg):
        # Active but hosted-routed: decision_stance is correctly suppressed,
        # and no LOCAL daemon produces stance_advisory findings either.
        return None
    if daemons_config.decision_stance.enabled:
        return "decision_stance"
    return None


def write_active_stance_producer_sidecar(producer: Optional[str]) -> None:
    """Persist the resolved active local stance producer for fast Stop-hook reads.

    Called by the daemon-owning process at registration time. Best-effort:
    write failures are swallowed (the Stop hook falls back to full resolution
    when the sidecar is absent or unreadable). An empty file encodes "no local
    stance producer".
    """
    try:
        ACTIVE_STANCE_PRODUCER_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
        ACTIVE_STANCE_PRODUCER_SIDECAR.write_text(producer or "", encoding="utf-8")
    except OSError as exc:
        logger.debug("could not write active_stance_producer sidecar: %s", exc)


def resolve_active_stance_producer() -> Optional[str]:
    """Return the daemon name whose LOCAL findings log actually receives
    ``stance_advisory`` writes, or ``None`` if no local daemon is registered.

    Wraps ``resolve_local_stance_producer`` with the runtime gates that are
    not config-derived: in hosted mode no local findings log is written, so
    this returns ``None`` regardless of config. Falls back to
    ``"decision_stance"`` — the safe open-core default — only if config
    cannot be loaded at all.
    """
    try:
        from watercooler.config_facade import config

        wc_config = config.full()
        daemons_config = wc_config.mcp.daemons
        transport = getattr(wc_config.mcp, "transport", "stdio")
    except Exception:
        return "decision_stance"

    try:
        from ..auth import is_hosted_mode

        if is_hosted_mode():
            # Hosted process: the local manager stays empty and no local
            # findings log is written (matches the ``_is_hosted`` early
            # return in init_daemons).
            return None
    except Exception:
        # If hosted-mode can't be determined, fall through to the
        # config-derived decision — the common local case.
        pass

    return resolve_local_stance_producer(daemons_config, transport)


def resolve_active_findings_sources(scope: str = "") -> list[FindingsSource]:
    """Return the findings sources a reader should poll.

    Always includes ``decision_extractor`` (candidate-Note surfacing) plus
    whichever daemon is the active ``stance_advisory`` producer per
    ``resolve_active_stance_producer()`` — omitted when that resolves to
    ``None`` (no local stance producer registered).

    ``scope`` is accepted for forward compatibility with hosted/multi-tenant
    findings routing; today the only caller (the Stop hook) passes none, so
    every local daemon writes to one shared per-daemon findings log under
    ``~/.watercooler/daemons/`` and callers filter by the ``repo`` field on
    each record.

    ``_allow_unscoped`` is gated on ``not scope`` so the audited
    empty-namespace exemption applies *only* to the local, single-checkout
    Stop-hook read (its real justification): without it a strict-namespace
    deployment would raise ``ValueError`` on every Stop-hook invocation,
    which the hook's broad ``except Exception`` would swallow silently —
    permanently disabling stance-advisory delivery. A future caller that
    passes a real ``scope`` correctly gets the strict-namespace guard
    enforced rather than carrying the exemption ambiently.
    """
    stance_daemon = resolve_active_stance_producer()
    names = ["decision_extractor"]
    if stance_daemon is not None:
        names.append(stance_daemon)
    return [
        FindingsSource(
            daemon_name=name,
            findings_path=_daemon_dir(name, namespace=scope, _allow_unscoped=not scope)
            / "findings.jsonl",
        )
        for name in names
    ]
