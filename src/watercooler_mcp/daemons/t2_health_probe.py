"""T2 Health Probe Daemon — scheduled synthetic T2 liveness + alerting.

Periodically probes the remote T2 (Graphiti) channel with a bounded, read-only
``facts`` query and alerts (Slack) on state transitions. This is the autonomous
half of the observability work: it turns the on-demand ``watercooler_health_probe``
tool into a standing watch so a dead T2 channel can't go unnoticed for days
(thread ``list-decisions-supersession-hosted-premium-unmounted`` — the channel
was dead for 11 days while ``/health`` stayed green).

Scope / loop-safety: the daemon runs in its own thread and probes with a FRESH
client per tick (``probe_t2_backend``) — reusing the server's long-lived
``premium_client`` across event loops is unsafe. It therefore detects backend /
auth / total-channel failures. The *specific* long-lived-client wedge is
diagnosed by the ``watercooler_health_probe`` tool (which runs on the server's
own loop) and is eliminated by the premium_client evict-on-timeout fix.

Alerts fire only on state transitions (green↔amber↔red), not every tick, so a
sustained outage produces one alert plus a recovery alert — not a flood.
Only meaningful under ``hybrid``/``proxy`` transport (a remote channel must
exist); a no-op otherwise. Opt-in (``enabled=False`` by default).
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import List, Optional

from ..health_probe import AMBER, GREEN, RED, maybe_alert, probe_t2_backend
from .base import BaseDaemon
from .state import Finding

logger = logging.getLogger(__name__)


def _make_finding(result: dict) -> Finding:
    from ulid import ULID

    verdict = result.get("verdict", "unknown")
    channel = result.get("channel_diagnosis", "unknown")
    ll = result.get("checks", {}).get("long_lived", {})
    return Finding(
        finding_id=str(ULID()),
        daemon_name="t2_health_probe",
        category="t2_health",
        topic="",
        message=(
            f"T2 channel {verdict.upper()} ({channel}); "
            f"latency={ll.get('latency_ms')}ms reason={ll.get('reason')}"
        ),
        severity="error" if verdict == RED else "warning",
        details={
            "verdict": verdict,
            "channel_diagnosis": channel,
            "checks": result.get("checks", {}),
        },
        created_at=time.time(),
    )


class T2HealthProbeDaemon(BaseDaemon):
    """Scheduled synthetic T2 liveness probe with transition-gated alerting."""

    def __init__(
        self,
        *,
        interval: float = 300.0,
        timeout_s: float = 8.0,
        amber_ms: float = 3000.0,
        enabled: bool = True,
    ) -> None:
        super().__init__(
            name="t2_health_probe",
            interval=interval,
            enabled=enabled,
            tick_on_interval=True,
        )
        self._timeout_s = timeout_s
        self._amber_ms = amber_ms
        # Capture cwd once at construction (server boot) for stable premium
        # header (X-Repo/X-Branch) resolution; the daemon thread's cwd is the
        # same process cwd but we avoid re-reading it every tick.
        self._boot_cwd: Optional[Path] = Path.cwd()
        self._last_verdict: Optional[str] = None

    def _remote_channel_configured(self) -> bool:
        """True only when a remote T2 channel exists (hybrid/proxy transport)."""
        try:
            from watercooler.config_facade import config as wc_config

            return getattr(wc_config.full().mcp, "transport", "stdio") in (
                "hybrid",
                "proxy",
            )
        except Exception:
            return False

    def tick(self) -> List[Finding]:
        if not self._remote_channel_configured():
            return []

        try:
            result = asyncio.run(
                probe_t2_backend(
                    timeout_s=self._timeout_s,
                    amber_ms=self._amber_ms,
                    boot_cwd=self._boot_cwd,
                )
            )
        except Exception as exc:
            # e.g. no url configured / fresh-client build failure. Don't spam;
            # this is a configuration condition, not a channel outage signal.
            logger.debug("DAEMON[t2_health_probe]: probe could not run: %s", exc)
            return []

        verdict = result.get("verdict", GREEN)
        prev = self._last_verdict
        self._last_verdict = verdict

        degraded = verdict in (AMBER, RED)
        was_degraded = prev in (AMBER, RED)

        # Alert only on state transitions (backoff against per-tick spam).
        if verdict != prev:
            if degraded:
                maybe_alert(result, min_verdict=AMBER)
            elif was_degraded and verdict == GREEN:
                # Recovery — announce once.
                maybe_alert(result, force=True)

        return [_make_finding(result)] if degraded else []
