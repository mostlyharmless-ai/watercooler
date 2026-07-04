"""Process-level fleet scheduler for hosted daemons (Design (hosted) v4).

De-fuses tenancy from background-work lifecycle: one :class:`RepoFleet` per
``(org, repo)`` — matching the shared-T2 dedup unit (Plan v20) — driven by a
single scheduler thread and a bounded worker pool, instead of one
thread-per-daemon fleet per ``(user_id, repo)`` scope. Scopes remain
identity/visibility boundaries; they own no threads, so reaping a scope never
stops background work and total thread count is O(pool size) regardless of
tenant count.

Enabled by ``WATERCOOLER_FLEET_SCHEDULER=1`` (Design v4 gate G1; default off —
the legacy per-scope path is untouched when the flag is off). Ratified design:
thread ``daemon-architecture-audit-2026-05``, Plan entry
``01KWFS8X3JHJ1PCMSHQHGG347V``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

from .manager import DaemonManager

if TYPE_CHECKING:
    from .hosted_worktree import HostedWorktree

logger = logging.getLogger(__name__)

_ENV_FLAG = "WATERCOOLER_FLEET_SCHEDULER"

# Idle gates (Design v4 D4): the scheduler skips work for repos with no tenant
# activity instead of killing worker threads — preserving the reaper's
# cost-bounding rationale without breaking the daemon abstraction. Gates are
# deliberately generous: a gated daemon resumes on the next tenant touch
# (next_due is not advanced while gated, so it becomes due immediately).
_HEAVY_IDLE_GATE_S = float(
    os.environ.get("WATERCOOLER_FLEET_HEAVY_IDLE_GATE_S", str(3 * 86400))
)
_LIGHT_IDLE_GATE_S = float(
    os.environ.get("WATERCOOLER_FLEET_LIGHT_IDLE_GATE_S", str(7 * 86400))
)
# Daemons that are cheap per tick (no LLM calls) get the longer gate.
_LIGHT_DAEMONS = frozenset({"t2_indexer"})

_DEFAULT_POLL_INTERVAL_S = 15.0
_DEFAULT_MAX_WORKERS = 8


def fleet_scheduler_enabled() -> bool:
    """True when the Design v4 fleet scheduler is enabled via env flag."""
    return os.environ.get(_ENV_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def fleet_namespace(repo: str) -> str:
    """Findings/checkpoint namespace for a repo fleet.

    ``repo:`` prefix keeps fleet state disjoint from every legacy
    ``{user_id}:{repo}`` scope namespace (user ids never equal ``repo``).
    """
    return f"repo:{repo}"


@dataclass
class RepoFleet:
    """One repo's daemon fleet: passive tick targets owned by the process."""

    repo: str
    manager: DaemonManager
    scope_ctx: Any  # HttpRequestContext of the current token-holder (D3 bridge)
    worktree: Optional["HostedWorktree"] = None
    registration_errors: list[dict[str, str]] = field(default_factory=list)
    last_activity: float = field(default_factory=time.monotonic)
    next_due: dict[str, float] = field(default_factory=dict)
    in_flight: set[str] = field(default_factory=set)


class HostedFleetScheduler:
    """Single scheduler thread + bounded pool driving all repo fleets.

    ``register_fn(fleet, github_token)`` populates the fleet's manager,
    worktree, scope_ctx, and registration_errors — supplied by the hosted
    coordinator so daemon construction/config stays in one place.
    """

    def __init__(
        self,
        register_fn: Callable[["RepoFleet", Optional[str]], None],
        *,
        max_workers: int = _DEFAULT_MAX_WORKERS,
        poll_interval: float = _DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._register_fn = register_fn
        self._poll_interval = poll_interval
        self._fleets: dict[str, RepoFleet] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="fleet-tick"
        )
        self._stop_event = threading.Event()
        self._scheduler_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the scheduler thread (idempotent)."""
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return
        self._stop_event.clear()
        self._scheduler_thread = threading.Thread(
            target=self._loop, daemon=True, name="hosted-fleet-scheduler"
        )
        self._scheduler_thread.start()
        logger.info(
            "FLEET: scheduler started (poll=%.0fs, pool=%d)",
            self._poll_interval,
            self._pool._max_workers,
        )

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the scheduler, the pool, and clean up fleet worktrees."""
        self._stop_event.set()
        if self._scheduler_thread:
            self._scheduler_thread.join(timeout=timeout)
        self._pool.shutdown(wait=False)
        with self._lock:
            fleets = list(self._fleets.values())
            self._fleets.clear()
        for fleet in fleets:
            if fleet.worktree:
                try:
                    fleet.worktree.cleanup()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Fleet management
    # ------------------------------------------------------------------

    def ensure_fleet(
        self,
        *,
        user_id: str,
        repo: str,
        branch: str | None = None,
        github_token: str | None = None,
    ) -> RepoFleet:
        """Return the repo's fleet, creating it on first touch.

        Every call refreshes ``last_activity`` (the idle-gate input) and, when
        a token is supplied, the fleet's write identity (D3 bridge:
        most-recently-validated token wins).
        """
        with self._lock:
            fleet = self._fleets.get(repo)
            if fleet is not None:
                fleet.last_activity = time.monotonic()
                self._refresh_identity_locked(fleet, user_id, branch, github_token)
                return fleet

            from watercooler_mcp.context import HttpRequestContext

            fleet = RepoFleet(
                repo=repo,
                manager=DaemonManager(repo_key=repo),
                scope_ctx=HttpRequestContext(
                    user_id=user_id,
                    repo=repo,
                    branch=branch,
                    github_token=github_token,
                ),
            )
            self._fleets[repo] = fleet

        # Register daemons outside the lock (config loading + worktree clone
        # are slow) — mirrors the coordinator's ensure_scope discipline.
        try:
            self._register_fn(fleet, github_token)
        except Exception as exc:
            fleet.registration_errors.append({"daemon": "_register", "error": str(exc)})
            logger.warning("FLEET: registration failed for %s: %s", repo, exc)

        now = time.monotonic()
        with self._lock:
            # Close the first-touch identity race (D3): a tenant may have
            # refreshed scope_ctx while registration was still cloning —
            # fleet.worktree was None then, so _refresh_identity_locked could
            # not propagate the newer token, and the worktree was constructed
            # with the registration-time token. Sync it to the fleet's
            # current identity before scheduling begins.
            current_token = fleet.scope_ctx.github_token
            if (
                fleet.worktree is not None
                and current_token
                and current_token != github_token
            ):
                fleet.worktree.update_token(current_token)
            for name in fleet.manager.daemon_names:
                d = fleet.manager.get_daemon(name)
                if d is None or not d.enabled:
                    continue
                d.mark_managed()
                # First tick one interval after creation — same timing the
                # owned-thread loop had (wake.wait(timeout=interval) first).
                fleet.next_due[name] = now + d.interval

        logger.info(
            "FLEET: created fleet for %s (%d daemons)",
            repo,
            len(fleet.manager.daemon_names),
        )
        return fleet

    def _refresh_identity_locked(
        self,
        fleet: RepoFleet,
        user_id: str,
        branch: str | None,
        github_token: str | None,
    ) -> None:
        """Adopt a newer tenant token as the fleet's write identity (D3)."""
        if not github_token or github_token == fleet.scope_ctx.github_token:
            return
        from watercooler_mcp.context import HttpRequestContext

        fleet.scope_ctx = HttpRequestContext(
            user_id=user_id,
            repo=fleet.repo,
            branch=branch,
            github_token=github_token,
        )
        if fleet.worktree is not None:
            fleet.worktree.update_token(github_token)
        logger.info("FLEET: %s write identity refreshed (user=%s)", fleet.repo, user_id)

    def get_fleet(self, repo: str) -> RepoFleet | None:
        """Return the live fleet for *repo*, if any."""
        with self._lock:
            return self._fleets.get(repo)

    def fleet_count(self) -> int:
        with self._lock:
            return len(self._fleets)

    def get_all_findings(self, **kwargs: Any) -> list[Any]:
        """Aggregate findings across all fleets (one manager per repo)."""
        with self._lock:
            managers = [f.manager for f in self._fleets.values()]
        findings: list[Any] = []
        for m in managers:
            findings.extend(m.get_all_findings(**kwargs))
        return findings

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def _idle_gate(self, daemon_name: str) -> float:
        return _LIGHT_IDLE_GATE_S if daemon_name in _LIGHT_DAEMONS else _HEAVY_IDLE_GATE_S

    def _collect_due(self, now: float) -> list[tuple[RepoFleet, Any]]:
        """Pick (fleet, daemon) pairs due to tick; marks them in-flight."""
        due: list[tuple[RepoFleet, Any]] = []
        with self._lock:
            for fleet in self._fleets.values():
                idle = now - fleet.last_activity
                for name, due_at in fleet.next_due.items():
                    if now < due_at or name in fleet.in_flight:
                        continue
                    if idle > self._idle_gate(name):
                        # Gated, not rescheduled: the daemon becomes due the
                        # moment tenant activity resumes.
                        continue
                    d = fleet.manager.get_daemon(name)
                    if d is None or not d.enabled:
                        continue
                    fleet.in_flight.add(name)
                    due.append((fleet, d))
        return due

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                for fleet, daemon in self._collect_due(time.monotonic()):
                    self._pool.submit(self._run_tick, fleet, daemon)
            except Exception as exc:
                logger.warning("FLEET: scheduler pass error: %s", exc)
            self._stop_event.wait(timeout=self._poll_interval)

    def _run_tick(self, fleet: RepoFleet, daemon: Any) -> None:
        try:
            # Always tick under the fleet's *current* identity (token may
            # have been refreshed since registration).
            daemon._scope_context = fleet.scope_ctx
            daemon.run_tick_once()
        except Exception as exc:
            logger.exception(
                "FLEET: tick crashed for %s/%s: %s", fleet.repo, daemon.name, exc
            )
        finally:
            with self._lock:
                fleet.in_flight.discard(daemon.name)
                fleet.next_due[daemon.name] = time.monotonic() + daemon.interval
