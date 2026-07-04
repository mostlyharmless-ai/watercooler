"""Committer Daemon — single-writer group-commit for the write path (Phase B, #904).

ONE daemon owns the orphan worktree on the normal path. Writers append to the
append-only graph (fast, under a short topic lock) and enqueue a ``commit`` task;
this daemon drains the commit queue, BATCHES pending entries per worktree, and
performs **one commit + one push per batch** under the worktree lock. On the normal
path there is a single writer on the worktree, so the concurrent-commit race that
clobbered threads (#904) does not arise, and commit cadence is decoupled from write
rate (#903 spam).

The strict invariant is **"all worktree mutation is serialized by the worktree
lock,"** not "literally one writer": under red-tier backpressure the request thread
falls back to an inline commit while this daemon may also be draining. Both paths
take ``acquire_worktree_lock`` (an ``O_CREAT|O_EXCL`` advisory lock that mutually
excludes threads in-process), so they never mutate the worktree concurrently — that
lock is the load-bearing guarantee, here and in the middleware inline fallback.

Isolated-first: this daemon is self-contained and unit-tested here. Wiring the write
path to enqueue ``commit`` tasks instead of committing inline is a separate step
(Phase B middleware flip) — until then nothing in the live path changes.

A ``commit`` ``MemoryTask`` carries: ``threads_dir`` (worktree), ``topic``,
``entry_id`` (dedup/receipt), ``content`` (the commit message the writer would have
used). The entry is already durable in the graph before the task is enqueued, so the
daemon only stages + commits + pushes.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

from ulid import ULID

from .base import BaseDaemon
from .state import Finding

if TYPE_CHECKING:
    from watercooler_mcp.memory_queue.queue import MemoryTaskQueue
    from watercooler_mcp.memory_queue.task import MemoryTask

logger = logging.getLogger(__name__)

COMMIT_BACKEND = "commit"

_SEED_UNSET = object()  # sentinel: seed worktree not yet resolved

_commit_queue: "MemoryTaskQueue | None" = None


def get_commit_queue() -> "MemoryTaskQueue":
    """Process-global commit queue, shared by the ``CommitterDaemon`` (consumer)
    and the write path (producer, once wired in the Phase B middleware flip).

    A single in-process instance is required so the producer's enqueues are
    visible to the consumer's dequeues — two instances over one ``queue_dir``
    would each keep their own in-memory task map. Storage is separate from the
    memory/enrichment queue (``~/.watercooler/commit_queue``).
    """
    global _commit_queue
    if _commit_queue is None:
        from pathlib import Path as _P

        from watercooler_mcp.memory_queue.queue import MemoryTaskQueue

        max_depth = 2000
        try:
            from watercooler_mcp.config import get_watercooler_config

            max_depth = int(
                getattr(
                    get_watercooler_config().mcp.sync,
                    "commit_queue_max_depth",
                    2000,
                )
            )
        except Exception:  # pragma: no cover - config unavailable -> safe default
            pass

        _commit_queue = MemoryTaskQueue(
            queue_dir=_P.home() / ".watercooler" / "commit_queue",
            max_depth=max_depth,
        )
    return _commit_queue


class CommitterDaemon(BaseDaemon):
    """Single-writer group-commit daemon draining a dedicated commit queue.

    Args:
        commit_queue: A ``MemoryTaskQueue`` holding ``backend="commit"`` tasks
            (a separate instance from the memory/enrichment queue).
        interval: Flush cadence in seconds (the ``batch_window``). Default 5.0.
        max_batch_size: Max tasks drained per flush. Default 50.
        max_retries: Push attempts **within one batch** (passed to
            ``push_with_retry``). Default 5. Distinct from the task-level retry
            budget: how many times a *task* is re-queued across batches before it
            dead-letters is governed by ``MemoryTask.max_attempts`` (default 3) on
            the queue, not by this value.
        stale_seconds: Reset a RUNNING task to PENDING after this age (crash
            recovery). Default 120.0.
        reconcile_interval: Seconds between reconciliation sweeps that self-heal
            worktree state with no pending task — the pre-enqueue crash window
            (#907) and any dropped task. Default 60.0; <=0 disables the sweep.
        threads_dir_override: Explicit worktree to reconcile even with an empty
            queue (tests / known local repo). When unset, the seed worktree is
            resolved once from the current thread context.
        enabled: Whether the daemon is active.
    """

    def __init__(
        self,
        *,
        commit_queue: "MemoryTaskQueue",
        interval: float = 5.0,
        max_batch_size: int = 50,
        max_retries: int = 5,
        stale_seconds: float = 120.0,
        reconcile_interval: float = 60.0,
        threads_dir_override: "str | Path | None" = None,
        enabled: bool = True,
    ) -> None:
        if max_batch_size < 1:
            raise ValueError(f"max_batch_size must be >= 1, got {max_batch_size}")
        super().__init__(
            name="committer",
            interval=interval,
            enabled=enabled,
            tick_on_interval=True,
        )
        self._queue = commit_queue
        self._max_batch = max_batch_size
        self._max_retries = max_retries
        self._stale_seconds = stale_seconds
        # Reconciliation sweep (#907): worktrees seen via tasks, plus a seed
        # resolved from context, are periodically checked for uncommitted/unpushed
        # state and flushed even when no task references them.
        self._reconcile_interval = reconcile_interval
        self._threads_dir_override = (
            str(threads_dir_override) if threads_dir_override is not None else None
        )
        self._known_worktrees: Set[str] = set()
        self._seed_resolved: Any = _SEED_UNSET
        # Start the reconcile clock "now" so the first sweep is delayed by one
        # full interval — a stranded entry is durable on disk, so a one-interval
        # wait to flush it is fine, and it keeps a single stray tick() (e.g. in
        # tests) from reconciling an unrelated seed worktree on construction.
        self._last_reconcile = time.monotonic()
        # Per-tick observable metrics
        self._last_batches = 0
        self._last_committed = 0
        self._last_push_failures = 0
        self._last_reconciled = 0

    # ------------------------------------------------------------------ #
    # Tick
    # ------------------------------------------------------------------ #

    def tick(self) -> List[Finding]:
        """Drain pending commit tasks, flush them as batched commits, then run a
        reconciliation sweep to self-heal any worktree state with no task."""
        findings: List[Finding] = []
        try:
            self._queue.recover_stale(self._stale_seconds)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("DAEMON[committer]: recover_stale failed: %s", exc)

        # Drain up to max_batch tasks this tick.
        tasks: List["MemoryTask"] = []
        while len(tasks) < self._max_batch:
            t = self._queue.dequeue()
            if t is None:
                break
            tasks.append(t)

        self._last_batches = 0
        self._last_committed = 0
        self._last_push_failures = 0
        self._last_reconciled = 0

        # Group by worktree (one writer per orphan worktree); within a worktree a
        # single transaction stages every pending topic and commits once.
        handled: Set[str] = set()
        if tasks:
            by_worktree: Dict[str, List["MemoryTask"]] = {}
            for t in tasks:
                by_worktree.setdefault(t.threads_dir, []).append(t)
            for threads_dir_str, group in by_worktree.items():
                handled.add(threads_dir_str)
                self._known_worktrees.add(threads_dir_str)
                self._last_batches += 1
                finding = self._commit_batch(threads_dir_str, group)
                if finding is not None:
                    findings.append(finding)

        # Reconciliation sweep — flush worktrees with no pending task (the
        # pre-enqueue crash window and any dropped task), skipping those a batch
        # just handled this tick.
        findings.extend(self._maybe_reconcile(skip=handled))
        return findings

    # ------------------------------------------------------------------ #
    # Reconciliation sweep (#907) — self-heal task-less worktree state
    # ------------------------------------------------------------------ #

    def _maybe_reconcile(self, *, skip: Set[str]) -> List[Finding]:
        # Coverage = worktrees seen via tasks (``_known_worktrees``) ∪ one seed
        # resolved from context. This heals the common single-primary-worktree
        # server and any worktree that has had at least one task. The residual
        # edge: a brand-new, non-seed worktree whose *very first* write crashes
        # before its task is enqueued is not healed until it has had one
        # successful task (joining ``_known_worktrees``) — not "every worktree,
        # always."
        if self._reconcile_interval <= 0:
            return []
        now = time.monotonic()
        if now - self._last_reconcile < self._reconcile_interval:
            return []
        self._last_reconcile = now

        targets = set(self._known_worktrees)
        seed = self._seed_worktree()
        if seed:
            targets.add(seed)

        findings: List[Finding] = []
        for threads_dir_str in sorted(targets):
            if threads_dir_str in skip:
                continue
            finding = self._reconcile(threads_dir_str)
            if finding is not None:
                findings.append(finding)
        return findings

    def _seed_worktree(self) -> Optional[str]:
        """The worktree to reconcile even with an empty queue. Override wins;
        otherwise resolved once from the current thread context (best-effort)."""
        if self._threads_dir_override is not None:
            return self._threads_dir_override
        if self._seed_resolved is not _SEED_UNSET:
            return self._seed_resolved
        try:
            from watercooler_mcp.config import resolve_thread_context

            ctx = resolve_thread_context(Path.cwd())
            self._seed_resolved = str(ctx.threads_dir) if ctx.threads_dir else None
        except Exception as exc:  # pragma: no cover - resolution is best-effort
            logger.debug("DAEMON[committer]: seed worktree resolve failed: %s", exc)
            self._seed_resolved = None
        return self._seed_resolved

    def _reconcile(self, threads_dir_str: str) -> Finding | None:
        """Commit any uncommitted graph state and push any unpushed commits for a
        worktree that has no pending task. Idempotent + lock-serialized with the
        committer's own batches, so it never races a normal write."""
        if not threads_dir_str:
            return None
        threads_dir = Path(threads_dir_str)
        if not (threads_dir / ".git").exists():
            return None

        from git import Repo

        from watercooler.sync_common import acquire_worktree_lock
        from watercooler_mcp.sync.primitives import push_with_retry

        lock = None
        try:
            lock = acquire_worktree_lock(threads_dir)
            repo = Repo(threads_dir)
            # Intentional catch-all: stage and commit ALL uncommitted state in the
            # worktree, not just one topic — the sweep's job is to flush whatever
            # was stranded. Safe because (a) it holds the worktree lock (no
            # concurrent writer) and (b) the orphan worktree gitignores
            # ``.watercooler/`` (locks etc.) via _ensure_watercooler_gitignored, so
            # ``-A`` never captures transient files. The reconcile commit carries
            # no Watercooler-Entry-ID footers (cosmetic — the entries are already
            # in the graph; this only lands them on the branch).
            repo.git.add("-A")

            did: List[str] = []
            if repo.is_dirty(index=True):
                repo.index.commit(
                    "chore(threads): reconcile uncommitted worktree state (#907)"
                )
                did.append("committed")
            if repo.remotes and self._has_unpushed_commits(repo):
                try:
                    pushed = push_with_retry(repo, max_retries=self._max_retries)
                except Exception as push_err:
                    pushed = False
                    logger.warning(
                        "DAEMON[committer]: reconcile push raised: %s", push_err
                    )
                did.append("pushed" if pushed else "push_failed")

            if not did:
                # Clean + up-to-date — nothing was out of sync, stay quiet.
                return None
            self._last_reconciled += 1
            severity = "warning" if "push_failed" in did else "info"
            return self._finding(
                severity, "reconciled",
                f"reconciled task-less worktree {threads_dir.name}: "
                f"{', '.join(did)}",
                details={"threads_dir": threads_dir_str, "actions": did},
            )
        except Exception as exc:
            logger.warning(
                "DAEMON[committer]: reconcile failed for %s: %s",
                threads_dir_str, exc,
            )
            return self._finding(
                "warning", "reconcile_error",
                f"reconcile failed for {threads_dir_str}: {exc}",
                details={"threads_dir": threads_dir_str},
            )
        finally:
            if lock is not None:
                try:
                    lock.release()
                except Exception:  # pragma: no cover - defensive
                    pass

    # ------------------------------------------------------------------ #
    # One batched commit transaction (single-writer on the worktree)
    # ------------------------------------------------------------------ #

    def _commit_batch(
        self, threads_dir_str: str, group: List["MemoryTask"]
    ) -> Finding | None:
        from git import Repo

        from watercooler.sync_common import (
            acquire_worktree_lock,
            paths_to_stage_for_topic,
        )
        from watercooler_mcp.sync.primitives import push_with_retry

        if not threads_dir_str:
            for t in group:
                self._fail(t, "commit task missing threads_dir")
            return self._finding("warning", "commit_missing_threads_dir",
                                  "commit task(s) missing threads_dir")
        threads_dir = Path(threads_dir_str)
        topics = sorted({t.topic for t in group if t.topic})
        topicless = [t for t in group if not t.topic]

        lock = None
        try:
            lock = acquire_worktree_lock(threads_dir)
            repo = Repo(threads_dir)

            for topic in topics:
                paths = paths_to_stage_for_topic(
                    threads_dir, topic, include_missing=True,
                    include_decision_index=True,
                )
                if paths:
                    repo.git.add("--all", "--", *paths)
            # A topic-less task can't be topic-staged, so its content would be
            # silently dropped in a mixed batch (the write path always sets a
            # topic, so this is defensive, #907). Fall back to staging everything
            # whenever any task lacks a topic, and warn if the batch was mixed.
            # As with the reconcile sweep, this ``-A`` relies on the orphan
            # worktree gitignoring ``.watercooler/`` so it can't stage transients.
            if topicless or not topics:
                if topicless and topics:
                    logger.warning(
                        "DAEMON[committer]: %d topic-less task(s) in a mixed batch "
                        "for %s; staging -A so no content is lost",
                        len(topicless), threads_dir.name,
                    )
                repo.git.add("-A")

            # Branch on PUSH state, not index state. A retry after a failed push
            # arrives with the content already committed (index clean) but the
            # commit unpushed — completing here without re-pushing would strand it
            # off origin AND write a "completed" receipt that falsely confirms a
            # push (the exact invisible-on-origin failure class this daemon
            # exists to kill). So: commit iff dirty; then push iff there are any
            # unpushed commits; only treat a clean+fully-pushed tree as a no-op.
            if repo.is_dirty(index=True):
                repo.index.commit(self._batch_message(group, topics))
                self._last_committed += len(group)
            elif not self._has_unpushed_commits(repo):
                # Already committed AND pushed by an earlier batch (idempotent).
                for t in group:
                    self._complete(t)
                return None
            # else: clean index but local commits are unpushed (prior push failed)
            # — fall through and (re)push them.

            try:
                pushed = push_with_retry(repo, max_retries=self._max_retries)
            except Exception as push_err:
                pushed = False
                logger.warning("DAEMON[committer]: push raised: %s", push_err)

            if pushed:
                for t in group:
                    self._complete(t)
                return None

            # Committed locally but push failed: the data is safe in the worktree;
            # re-queue the tasks. On the next tick the index is clean but
            # _has_unpushed_commits() is True, so the retry re-attempts the push
            # (instead of short-circuiting to complete) until it lands on origin
            # or the tasks dead-letter.
            self._last_push_failures += 1
            for t in group:
                self._fail(t, "push failed after retries (committed locally)")
            return self._finding(
                "warning", "push_failed",
                f"batch committed locally but push failed for "
                f"{threads_dir.name} ({len(group)} entries, topics={topics})",
                details={"threads_dir": threads_dir_str, "topics": topics,
                         "entries": [t.entry_id for t in group]},
            )
        except Exception as exc:
            logger.warning("DAEMON[committer]: batch failed for %s: %s",
                           threads_dir_str, exc)
            for t in group:
                self._fail(t, f"commit batch error: {exc}")
            return self._finding(
                "warning", "commit_batch_error",
                f"commit batch failed for {threads_dir_str}: {exc}",
                details={"threads_dir": threads_dir_str},
            )
        finally:
            if lock is not None:
                try:
                    lock.release()
                except Exception:  # pragma: no cover - defensive
                    pass

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _has_unpushed_commits(self, repo) -> bool:
        """True if the worktree branch has local commits not on its upstream.

        Distinguishes 'already committed AND pushed' (idempotent ack) from
        'committed but the push failed' (must retry the push). On ANY uncertainty
        we return True: ``push_with_retry`` is idempotent, so re-pushing
        already-pushed commits is a harmless "everything up-to-date" no-op,
        whereas a false 'already pushed' would silently strand a local commit off
        origin (the #904 failure class).
        """
        try:
            branch = repo.active_branch
            tracking = branch.tracking_branch()
            if tracking is None:
                return True
            return any(
                True for _ in repo.iter_commits(f"{tracking.name}..{branch.name}")
            )
        except Exception:
            return True

    def _batch_message(self, group: List["MemoryTask"], topics: List[str]) -> str:
        if len(group) == 1 and group[0].content:
            return group[0].content
        lines = [
            f"chore(threads): commit {len(group)} entr"
            f"{'y' if len(group) == 1 else 'ies'} across "
            f"{len(topics)} topic{'' if len(topics) == 1 else 's'}",
            "",
        ]
        for t in group:
            if t.entry_id:
                lines.append(f"Watercooler-Entry-ID: {t.entry_id}")
        return "\n".join(lines)

    def _complete(self, task: "MemoryTask") -> None:
        try:
            self._queue.complete(task.task_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("DAEMON[committer]: complete(%s) failed: %s",
                         task.task_id, exc)

    def _fail(self, task: "MemoryTask", error: str) -> None:
        try:
            self._queue.fail(task.task_id, error)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("DAEMON[committer]: fail(%s) failed: %s",
                         task.task_id, exc)

    def _finding(
        self, severity: str, category: str, message: str,
        *, details: Dict[str, Any] | None = None,
    ) -> Finding:
        return Finding(
            finding_id=str(ULID()),
            daemon_name=self.name,
            severity=severity,
            category=category,
            topic="",
            message=message,
            details=details or {},
        )

    def status_summary(self) -> Dict[str, Any]:
        base = super().status_summary()
        base["last_batches"] = self._last_batches
        base["last_committed"] = self._last_committed
        base["last_push_failures"] = self._last_push_failures
        base["last_reconciled"] = self._last_reconciled
        try:
            base["commit_queue_depth"] = self._queue.depth()
        except Exception:  # pragma: no cover - defensive
            pass
        return base
