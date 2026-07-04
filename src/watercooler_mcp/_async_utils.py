"""Shared async utilities for the MCP layer.

Callers from sync callbacks (memory_sync, t1_hybrid) need to invoke
coroutine-returning helpers without assuming anything about whether the
surrounding runtime already owns a loop on the calling thread. Both
previously rolled their own ``_run_coro_in_fresh_loop``; round 17 of the
PR #654 code review caught the duplication and its fix-drift risk.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any


def run_coro_in_fresh_loop(coro: Any) -> Any:
    """Run ``coro`` to completion on a private event loop and return its result.

    PR #654 review (CRITICAL): ``asyncio.run()`` cannot be used here —
    sync callbacks run inside a ``ThreadPoolExecutor`` worker and the
    surrounding runtime may already own a loop on that thread. A private
    loop sidesteps the documented FastMCP deadlock path.

    #937 (CRITICAL): a fresh loop *object* does NOT bypass asyncio's
    per-thread running-loop guard. ``run_until_complete`` raises
    ``RuntimeError: Cannot run the event loop while another loop is
    running`` whenever the *calling thread* already owns a running loop,
    regardless of loop identity — which is exactly what happens when these
    hybrid handoff callbacks (``t1_hybrid`` → ``watercooler_semantic``;
    ``memory_sync`` → ``watercooler_graphiti_add_episode``) are reached
    from the async MCP handler / memory-queue worker thread. That made
    hybrid T2/T1 handoffs mass-fail and dead-letter. So: only run inline
    when this thread has no running loop; otherwise offload to a dedicated
    worker thread (which owns no running loop) and run the private loop
    there.

    Note: when offloaded, this still *blocks* the caller until the
    coroutine completes (the callback contract is synchronous). It trades
    a brief block for correctness — the prior behaviour failed outright.

    Constraint on ``coro``: it is created on the caller thread but awaited
    on the worker's private loop, so it must be self-contained — it must
    not capture the caller's loop or loop-bound resources. The handoff
    coroutines (``watercooler_semantic`` / ``watercooler_graphiti_add_episode``)
    satisfy this; it is also the same assumption the inline private-loop
    path already relied on.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No loop running on this thread — safe to run a private loop inline.
        return _run_on_private_loop(coro)

    # A loop is already running on this thread; running another loop here is
    # illegal. Offload to a worker thread that owns no running loop.
    #
    # Deliberately a per-call executor, not a shared singleton: a shared
    # ``max_workers=1`` worker would deadlock if a handoff coroutine ever
    # re-entered this helper (the lone worker would block waiting on itself).
    # This offload path is cold (per-entry background handoff, not a
    # per-request hot loop) and each call already blocks on ``.result()``,
    # so the one-shot worker spawn is acceptable and re-entrancy-safe.
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="wc-handoff-loop"
    ) as executor:
        return executor.submit(_run_on_private_loop, coro).result()


def _run_on_private_loop(coro: Any) -> Any:
    """Run ``coro`` on a brand-new event loop owned by the current thread,
    draining still-pending tasks before teardown.

    PR #654 review round 11 (MEDIUM): cancel and await any tasks still
    pending when ``coro`` returns before closing the loop. Async HTTP
    clients used by ``premium_client`` spawn background tasks; closing
    without draining leaks file descriptors and prints
    "Task was destroyed but it is pending!" on every call.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            if pending:
                for task in pending:
                    task.cancel()
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
        except Exception:
            # Draining must never mask the original coro's result or error.
            pass
        finally:
            loop.close()
