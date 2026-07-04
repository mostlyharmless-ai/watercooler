"""Regression: load_all_entries_hosted must propagate the request/worker
context into its ThreadPoolExecutor workers.

The worker (load_entries_hosted) resolves its own GitHub client via
_get_github_client() -> get_effective_context(). ContextVars set in the calling
thread are NOT visible to ThreadPoolExecutor worker threads, so without explicit
propagation every worker sees no context and fails with
"No HTTP context available for hosted mode" (the real cause behind the masked
hosted list_decisions total:0). This test fails on the unfixed code and passes
once the context is propagated.
"""

from __future__ import annotations

from unittest.mock import patch

from watercooler_mcp import context as ctx_mod
from watercooler_mcp import hosted_ops


def test_load_all_entries_hosted_propagates_context_to_workers():
    sentinel = object()  # stand-in for the request's HttpRequestContext

    seen: dict[str, object] = {}

    def _fake_load_entries(topic):
        # Runs inside a ThreadPoolExecutor worker thread.
        seen[topic] = ctx_mod.get_effective_context()
        return (None, [])

    token = ctx_mod.set_worker_context(sentinel)  # type: ignore[arg-type]
    try:
        with patch.object(
            hosted_ops, "load_entries_hosted", side_effect=_fake_load_entries
        ):
            err, result = hosted_ops.load_all_entries_hosted(
                topics=["alpha", "beta", "gamma"]
            )
    finally:
        ctx_mod._worker_context.reset(token)

    assert err is None
    # Every worker must have seen the caller's context — not None.
    assert seen == {"alpha": sentinel, "beta": sentinel, "gamma": sentinel}
