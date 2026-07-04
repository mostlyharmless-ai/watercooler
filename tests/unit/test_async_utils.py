"""Tests for watercooler_mcp._async_utils.run_coro_in_fresh_loop.

Regression coverage for #937: the hybrid memory handoff invoked this helper
from a thread that already owned a running event loop, and the prior
implementation (``asyncio.new_event_loop().run_until_complete(...)``) raised
``RuntimeError: Cannot run the event loop while another loop is running`` —
mass-dead-lettering T1/T2 handoffs in hybrid mode.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from watercooler_mcp._async_utils import run_coro_in_fresh_loop


async def _echo(value):
    await asyncio.sleep(0)
    return value


async def _boom():
    await asyncio.sleep(0)
    raise ValueError("kaboom")


def test_runs_with_no_running_loop():
    """Baseline: no loop on the calling thread → runs inline, returns result."""
    assert run_coro_in_fresh_loop(_echo(42)) == 42


def test_runs_from_within_running_loop():
    """#937 regression: called from inside a running loop, the helper must
    NOT raise 'Cannot run the event loop while another loop is running' and
    must return the coroutine's result."""

    async def _driver():
        # asyncio.run() means a loop is running on this thread; the sync
        # callback contract calls run_coro_in_fresh_loop from right here.
        return run_coro_in_fresh_loop(_echo("ok"))

    assert asyncio.run(_driver()) == "ok"


def test_offloads_to_a_different_thread_when_loop_is_running():
    """When a loop is already running, the coroutine executes on a separate
    worker thread (not the caller's loop thread)."""
    caller_thread = threading.get_ident()
    seen = {}

    async def _record():
        await asyncio.sleep(0)
        seen["thread"] = threading.get_ident()
        return True

    async def _driver():
        return run_coro_in_fresh_loop(_record())

    assert asyncio.run(_driver()) is True
    assert seen["thread"] != caller_thread


def test_exception_propagates_with_no_running_loop():
    with pytest.raises(ValueError, match="kaboom"):
        run_coro_in_fresh_loop(_boom())


def test_exception_propagates_from_within_running_loop():
    """Errors raised inside the offloaded coroutine surface to the caller."""

    async def _driver():
        return run_coro_in_fresh_loop(_boom())

    with pytest.raises(ValueError, match="kaboom"):
        asyncio.run(_driver())
