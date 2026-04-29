"""Shared async utilities for the MCP layer.

Callers from sync callbacks (memory_sync, t1_hybrid) need to invoke
coroutine-returning helpers without assuming anything about whether the
surrounding runtime already owns a loop on the calling thread. Both
previously rolled their own ``_run_coro_in_fresh_loop``; round 17 of the
PR #654 code review caught the duplication and its fix-drift risk.
"""

from __future__ import annotations

import asyncio
from typing import Any


def run_coro_in_fresh_loop(coro: Any) -> Any:
    """Run ``coro`` on a private event loop and tear it down cleanly.

    PR #654 review (CRITICAL): ``asyncio.run()`` cannot be used here —
    sync callbacks run inside a ``ThreadPoolExecutor`` worker and the
    surrounding runtime may already own a loop on that thread. A private
    loop sidesteps both the ``RuntimeError`` and the documented FastMCP
    deadlock path.

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
