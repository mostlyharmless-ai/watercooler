"""Middleware for watercooler MCP server.

Contains:
- Instrumentation: FunctionTool monkey-patching for observability
- Sync wrappers: run_with_sync, run_with_graph_sync
- Per-topic hosted write locks (asyncio-based)
"""

import asyncio
import sys
import json
import time
from typing import Callable, TypeVar

from watercooler.memory_config import is_anthropic_url, AUTH_SKIP_SENTINELS

from .config import (
    ThreadContext,
    get_watercooler_config,
)
# Import from sync package
from .sync import (
    SyncError,
    acquire_topic_lock,
)
from .observability import log_debug, log_action, log_warning
from .helpers import _build_commit_footers

# NOTE: Graph-first mode is now ALWAYS enabled. The WATERCOOLER_GRAPH_FIRST env var
# is deprecated and ignored. All writes go through commands_graph.py which writes
# structural data first, then projects to markdown. Enrichment (summaries/embeddings)
# runs after the structural write if services are available.

# Service availability cache (TTL-based to avoid HTTP checks on every write)
_SERVICE_CACHE_TTL_SECONDS = 60.0  # Cache results for 60 seconds
_service_availability_cache: dict[str, tuple[bool, bool, float]] = {}
# Key format: f"{llm_base}|{embed_base}", Value: (llm_available, embed_available, timestamp)


# =============================================================================
# Per-Topic Hosted Write Locks (asyncio-based)
# =============================================================================
# Serializes concurrent writes to the same (repo, topic) in hosted mode.
# Prevents thundering herd on the GitHub Contents API when multiple agents
# write to the same thread simultaneously.


# Note: per-topic write serialization is handled by the jittered conflict
# retry loop in hosted_ops.py (say_hosted, ack_hosted, etc.) rather than
# an asyncio lock. The retry approach is more robust for the hosted use
# case since multiple Railway instances can't share in-process locks.


def clear_service_availability_cache() -> None:
    """Clear the service availability cache.

    Call this when service configuration changes (e.g., after updating
    credentials.toml or environment variables) to force re-checking
    service availability on the next write operation.
    """
    _service_availability_cache.clear()


def _check_enrichment_services_available(graph_config) -> tuple[bool, bool]:
    """Check which enrichment services are available.

    Returns a tuple of (llm_available, embed_available) indicating which
    services are reachable. This allows the caller to decide whether to
    attempt partial enrichment.

    Args:
        graph_config: GraphConfig with generate_summaries/generate_embeddings flags

    Returns:
        Tuple of (llm_available, embed_available) booleans
    """
    try:
        import httpx
    except ImportError:
        log_debug("[GRAPH] httpx not available, skipping enrichment check")
        return (False, False)

    # If neither is requested, no need to check services
    if not graph_config.generate_summaries and not graph_config.generate_embeddings:
        return (False, False)

    # Build cache key from service URLs
    llm_base_for_cache = getattr(graph_config, 'summarizer_api_base', '') or ''
    embed_base_for_cache = getattr(graph_config, 'embedding_api_base', '') or ''
    cache_key = f"{llm_base_for_cache}|{embed_base_for_cache}"

    # Check cache first (avoid HTTP requests on every write)
    now = time.time()
    if cache_key in _service_availability_cache:
        cached_llm, cached_embed, cached_time = _service_availability_cache[cache_key]
        if now - cached_time < _SERVICE_CACHE_TTL_SECONDS:
            log_debug(f"[GRAPH] Using cached service availability (age: {now - cached_time:.1f}s)")
            return (cached_llm, cached_embed)

    llm_available = False
    embed_available = False

    try:
        # Check LLM service if summaries requested
        if graph_config.generate_summaries:
            from watercooler.baseline_graph.summarizer import SummarizerConfig
            from watercooler.memory_config import _get_provider_api_key
            llm_config = SummarizerConfig.from_env()
            llm_base = getattr(graph_config, 'summarizer_api_base', None) or llm_config.api_base
            # Resolve API key based on actual llm_base URL, not default config
            llm_api_key = _get_provider_api_key(llm_base) if llm_base else llm_config.api_key
            if llm_base:
                try:
                    headers = {}
                    is_anthropic = is_anthropic_url(llm_base)
                    # Add auth header for external APIs (not needed for local llama-server)
                    if llm_api_key and llm_api_key not in AUTH_SKIP_SENTINELS:
                        if is_anthropic:
                            # Anthropic uses x-api-key header
                            headers["x-api-key"] = llm_api_key
                            headers["anthropic-version"] = "2023-06-01"
                        else:
                            headers["Authorization"] = f"Bearer {llm_api_key}"
                    with httpx.Client(timeout=5.0) as client:
                        # Anthropic doesn't have /models endpoint
                        if is_anthropic:
                            # Use GET on /messages which returns 405 Method Not Allowed
                            # This confirms API is reachable without triggering actual
                            # completions (avoids rate limits and potential charges)
                            url = f"{llm_base.rstrip('/')}/messages"
                            response = client.get(url, headers=headers)
                            # 405 = API reachable, method not allowed (expected)
                            # 400 = API reachable, bad request (also acceptable)
                            if response.status_code in (200, 400, 405):
                                llm_available = True
                                log_debug(f"[GRAPH] LLM service available at {llm_base}")
                            else:
                                log_debug(f"[GRAPH] LLM service returned {response.status_code} at {llm_base}")
                        else:
                            url = f"{llm_base.rstrip('/')}/models"
                            response = client.get(url, headers=headers)
                            if 200 <= response.status_code < 300:
                                llm_available = True
                                log_debug(f"[GRAPH] LLM service available at {llm_base}")
                            else:
                                log_debug(f"[GRAPH] LLM service returned {response.status_code} at {llm_base}")
                except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError):
                    log_debug(f"[GRAPH] Cannot connect to LLM at {llm_base}")

        # Check embedding service if embeddings requested
        if graph_config.generate_embeddings:
            from watercooler.baseline_graph.sync import EmbeddingConfig
            from watercooler.memory_config import _get_embedding_provider_api_key
            embed_config = EmbeddingConfig.from_env()
            embed_base = getattr(graph_config, 'embedding_api_base', None) or embed_config.api_base
            # Resolve API key based on actual embed_base URL, not default config
            embed_api_key = _get_embedding_provider_api_key(embed_base) if embed_base else embed_config.api_key
            if embed_base:
                try:
                    headers = {}
                    # Add auth header for external APIs (not needed for local llama-server)
                    if embed_api_key and embed_api_key not in AUTH_SKIP_SENTINELS:
                        headers["Authorization"] = f"Bearer {embed_api_key}"
                    with httpx.Client(timeout=5.0) as client:
                        url = f"{embed_base.rstrip('/')}/models"
                        response = client.get(url, headers=headers)
                        if 200 <= response.status_code < 300:
                            embed_available = True
                            log_debug(f"[GRAPH] Embedding service available at {embed_base}")
                        else:
                            log_debug(f"[GRAPH] Embedding service returned {response.status_code} at {embed_base}")
                except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError):
                    log_debug(f"[GRAPH] Cannot connect to embedding service at {embed_base}")

        # Cache the result
        _service_availability_cache[cache_key] = (llm_available, embed_available, time.time())
        return (llm_available, embed_available)
    except Exception as e:
        # Gracefully handle all errors - service checks should never crash writes
        # Common causes: ImportError (config modules missing), AttributeError (malformed config),
        # ValueError (invalid config values), OSError (file access issues)
        log_debug(f"[GRAPH] Service check failed: {type(e).__name__}: {e}")
        # Cache the failure too (to avoid retrying immediately)
        _service_availability_cache[cache_key] = (False, False, time.time())
        return (False, False)


# Store original FunctionTool.run for instrumentation
_orig_run = None
_instrumentation_installed = False

T = TypeVar("T")

# Tool-level timeouts (seconds). Tools not listed use _DEFAULT_TOOL_TIMEOUT.
# Prevents server process death when tools exceed MCP SDK's 60s hard limit.
# Tools with timeouts >60s still hit the client-side timeout, but the server
# stays alive instead of crashing — the key improvement.
_DEFAULT_TOOL_TIMEOUT: float = 50.0  # Under MCP SDK's 60s hard limit

_TOOL_TIMEOUTS: dict[str, float] = {
    # Baseline graph — the scope="sync" path scans every thread.
    "watercooler_baseline_graph": 180.0,
    "watercooler_graph_enrich": 300.0,
    # Bulk index — the run_pipeline= mode runs LeanRAG clustering/embedding;
    # the preflight and queueing paths return fast but share the ceiling.
    "watercooler_bulk_index": 300.0,
    # Graphiti episode ingestion (fire-and-forget returns fast, but config
    # loading or backend init can stall — 120s safety net)
    "watercooler_graphiti_add_episode": 120.0,
    # Smart query T3 escalation can be slow
    "watercooler_smart_query": 120.0,
}


def get_tool_timeout(tool_name: str) -> float:
    """Return the configured timeout for a tool, or the default.

    Used by the hosted JSON-RPC adapter and local instrumentation. A retired
    tool name forwards to a canonical tool (see aliases.py); the hosted
    adapter resolves the timeout from the *incoming* name before alias
    rewriting, so the alias is canonicalized here — a deprecated name gets the
    timeout of the tool that actually runs (e.g. watercooler_leanrag_run_pipeline
    → watercooler_bulk_index's 300s, not the default).
    """
    from .aliases import resolve_alias

    alias = resolve_alias(tool_name)
    canonical = alias.canonical if alias is not None else tool_name
    return _TOOL_TIMEOUTS.get(canonical, _DEFAULT_TOOL_TIMEOUT)


def setup_instrumentation() -> None:
    """Set up FunctionTool instrumentation for observability.

    Call this once at server startup to monkey-patch FastMCP's FunctionTool.run
    method with timing, logging, and per-tool timeouts.

    Idempotent — repeated calls are safe and do not re-wrap the method.

    FastMCP 3.0 compat: FunctionTool.run still exists at the same import path.
    v3's built-in OpenTelemetry is complementary (tracing, not timeouts).

    Timeouts prevent server process death under CPU pressure. Tools that exceed
    their timeout return a graceful error instead of crashing the server.
    """
    global _orig_run, _instrumentation_installed

    if _instrumentation_installed:
        return

    try:
        from fastmcp.tools.function_tool import FunctionTool  # type: ignore

        _orig_run = FunctionTool.run

        async def _instrumented_run(self, arguments):  # type: ignore
            tool_name = getattr(self, 'name', '<unknown>')
            timeout = _TOOL_TIMEOUTS.get(tool_name, _DEFAULT_TOOL_TIMEOUT)
            input_chars = len(json.dumps(arguments)) if arguments else 0
            start_time = time.perf_counter()
            outcome = "ok"
            try:
                result = await asyncio.wait_for(
                    _orig_run(self, arguments), timeout=timeout
                )
                return result
            except asyncio.TimeoutError:
                # asyncio.wait_for cancels the wrapped coroutine on timeout.
                # This is safe: current tools are stateless HTTP calls with no
                # partial-commit cleanup needed.
                outcome = "timeout"
                # On Python 3.11+ asyncio.TimeoutError IS TimeoutError, but we
                # re-raise as TimeoutError explicitly for 3.10 compat and clarity.
                raise TimeoutError(
                    f"Tool '{tool_name}' exceeded its {timeout:.0f}s timeout. "
                    f"The server is still running. You can retry with a "
                    f"lighter operation or check system load."
                )
            except Exception:
                outcome = "error"
                raise
            finally:
                duration_ms = (time.perf_counter() - start_time) * 1000.0
                try:
                    log_action(
                        "mcp.tool",
                        tool_name=tool_name,
                        input_chars=input_chars,
                        duration_ms=duration_ms,
                        outcome=outcome,
                        timeout_s=timeout,
                    )
                    # Workaround: Force stdout flush on Windows after tool execution
                    if sys.platform == "win32":
                        sys.stdout.flush()
                        sys.stderr.flush()
                except Exception:
                    pass

        FunctionTool.run = _instrumented_run  # type: ignore
        _instrumentation_installed = True
    except Exception:
        pass


def run_with_sync(
    context: ThreadContext,
    commit_title: str,
    operation: Callable[[], T],
    *,
    topic: str | None = None,
    entry_id: str | None = None,
    agent_spec: str | None = None,
    priority_flush: bool = False,
    skip_validation: bool = False,
    sync_status: dict[str, object] | None = None,
) -> T:
    """Execute operation with git sync.

    Flow: acquire lock -> pull -> operation -> commit -> push -> release lock
    No preflight state machine, no branch switching.

    Guards against writing into a non-GitHub-backed target (Bug #3,
    plan v4): before any lock acquisition the shared
    ``assert_github_backed_threads`` helper runs and raises
    ``WatercoolerWriteError`` unless ``WATERCOOLER_ALLOW_LOCAL_ONLY=1``
    is set. Placing the guard here — at the shared wrapper that ALL
    MCP writes route through, including the seven ``graph.py`` tools
    (``annotate``, ``remove_annotation``, ``delete_entry``,
    ``delete_thread``, ``archive_thread``, ``unarchive``) that call
    ``run_with_sync`` directly without going through
    ``_run_with_sync_report_push`` — covers every current and future
    MCP write tool automatically.
    """
    from watercooler.write_guard import (
        WatercoolerWriteError,
        assert_github_backed_threads,
    )

    # Hard-fail when threads_dir is missing. Skipping the guard here
    # would let the write proceed against whatever downstream sync code
    # happened to infer, defeating the "every MCP write is guarded"
    # contract this function's docstring promises. A missing
    # threads_dir is itself a misconfiguration — refuse the write
    # rather than silently bypassing the check.
    if context.threads_dir is None:
        raise WatercoolerWriteError(
            "Cannot write threads — no threads_dir is configured on "
            "this request context. Ensure WATERCOOLER_DIR is set or "
            "the code_path argument resolves to a git repository. "
            "(WATERCOOLER_ALLOW_LOCAL_ONLY does NOT help here — a "
            "missing threads_dir is a configuration gap, not local-"
            "only mode.) "
            "See docs/TROUBLESHOOTING.md#local-only-mode for details."
        )

    # Raises WatercoolerWriteError when threads_dir is not backed by a
    # GitHub remote (and opt-in is absent). MCP tool wrappers convert
    # that to a user-visible error message.
    assert_github_backed_threads(context.threads_dir)

    lock = None
    pre_write_wt_lock = None
    if sync_status is not None:
        sync_status.clear()
        sync_status.update({
            "operation_completed": False,
            "committed": False,
            "pushed": False,
            "error": None,
        })
    try:
        # Per-topic locking to serialize concurrent writes
        if topic and context.threads_dir:
            try:
                lock = acquire_topic_lock(context.threads_dir, topic, timeout=30)
                log_debug(f"[SYNC] Acquired lock for topic '{topic}'")
                log_action("parity.lock.acquire", scope="topic", topic=topic, outcome="ok")
            except TimeoutError as e:
                log_action("parity.lock.acquire", scope="topic", topic=topic, outcome="timeout")
                raise SyncError(f"Failed to acquire lock for topic '{topic}': {e}")

        # Pull latest (if remote exists)
        # Strategy: fetch, then fast-forward. If ff fails (branch diverged),
        # reset to remote HEAD. The orphan branch is append-only thread data;
        # any local-only commits are from previous failed syncs and their
        # entry data is already in the local graph files — it will be
        # re-committed by the current write cycle. This prevents the "99
        # commits behind, rebase fails on manifest.json" failure mode.
        log_debug("[SYNC] Simple sync flow")
        if context.threads_dir and (context.threads_dir / ".git").exists():
            from .sync.primitives import pull_ff_only, pull_rebase, fetch_with_timeout, abort_rebase, is_rebase_in_progress
            from .sync.errors import AuthenticationError, ConflictError, PushError
            from watercooler.sync_common import acquire_worktree_lock
            from git import Repo, GitCommandError
            try:
                threads_repo = Repo(context.threads_dir)

                # Fetch is read-only — no lock needed
                fetched = fetch_with_timeout(threads_repo, timeout=30)

                # Mutating ops (abort, pull, reset) need the worktree lock
                pre_write_wt_lock = None
                try:
                    pre_write_wt_lock = acquire_worktree_lock(context.threads_dir)

                    # Recover from stuck rebase/merge left by a previous failed sync
                    if is_rebase_in_progress(threads_repo):
                        log_warning("[SYNC] Detected stuck rebase/merge state, aborting to recover")
                        abort_rebase(threads_repo)

                    if not fetched:
                        log_warning("[SYNC] Fetch failed (continuing with local state)")
                    elif not pull_ff_only(threads_repo):
                        # FF failed — local-only commits exist. Rebase on
                        # top of remote to preserve their content. Safe now
                        # that global conflict sources (manifest.json) are
                        # removed from git tracking.
                        try:
                            if pull_rebase(threads_repo):
                                log_debug("[SYNC] Rebase succeeded")
                                log_action("sync.pull_rebase", outcome="ok")
                            else:
                                # Rebase failed — continue with local state.
                                # The write will land locally and push_with_retry
                                # will attempt rebase again at push time.
                                log_warning("[SYNC] Rebase failed, continuing with local state")
                                log_action("sync.pull_rebase", outcome="conflict")
                        except Exception as reset_err:
                            log_warning(f"[SYNC] Reset to remote failed: {reset_err} — continuing with local state")
                            log_action("sync.hard_reset", outcome="error", error=str(reset_err)[:200])
                except TimeoutError:
                    if pre_write_wt_lock:
                        pre_write_wt_lock.release()
                        pre_write_wt_lock = None
                    log_warning("[SYNC] Worktree lock timeout during pre-write sync — continuing with local state")
                except Exception:
                    # Release on error; on success, keep held for commit phase
                    if pre_write_wt_lock:
                        pre_write_wt_lock.release()
                        pre_write_wt_lock = None
                    raise
            except GitCommandError as git_err:
                err_msg = str(git_err).lower()
                if "authentication" in err_msg or "permission denied" in err_msg or "could not read from remote" in err_msg:
                    raise AuthenticationError(
                        message=f"Git authentication failed during pull: {git_err}",
                        recovery_hint="Check SSH keys or GitHub token credentials.",
                    )
                if "conflict" in err_msg or "merge" in err_msg:
                    raise ConflictError(
                        message=f"Merge conflict during pull: {git_err}",
                        recovery_hint="Resolve conflicts in the worktree manually.",
                    )
                # Network/transient errors — log and continue
                log_warning(f"[SYNC] Pull failed (transient, continuing): {git_err}")
            except Exception as pull_err:
                log_warning(f"[SYNC] Pull failed (continuing): {pull_err}")

        # Build commit footers
        footers = _build_commit_footers(
            context,
            topic=topic,
            entry_id=entry_id,
            agent_spec=agent_spec,
        )
        commit_message = commit_title if not footers else f"{commit_title}\n\n" + "\n".join(footers)

        # Wrap operation to include graph sync BEFORE commit
        # Graph-first mode: The command (via commands_graph.py) already wrote
        # structural data to the graph, then projected to markdown. Now we run
        # enrichment (summaries/embeddings) if services are available.
        # If services aren't available, we log and continue - the entry is
        # already saved, just without enrichment (can be backfilled later).
        def operation_with_graph_sync():
            result = operation()

            if topic and entry_id and context.threads_dir:
                # Enrichment (summaries/embeddings) - optional, best-effort
                try:
                    # Check if enrichment is configured and services are available
                    wc_config = get_watercooler_config()
                    graph_config = wc_config.mcp.graph

                    wants_enrichment = (
                        graph_config.generate_summaries or graph_config.generate_embeddings
                    )

                    if not wants_enrichment:
                        log_debug(f"[GRAPH] Enrichment not configured, skipping for {topic}/{entry_id}")
                    else:
                        llm_available, embed_available = _check_enrichment_services_available(graph_config)

                        # Only attempt enrichment for services that are actually available
                        do_summaries = graph_config.generate_summaries and llm_available
                        do_embeddings = graph_config.generate_embeddings and embed_available

                        if do_summaries or do_embeddings:
                            # Run enrichment - add summaries/embeddings to existing entry
                            from watercooler.baseline_graph.sync import enrich_graph_entry

                            enrich_result = enrich_graph_entry(
                                threads_dir=context.threads_dir,
                                topic=topic,
                                entry_id=entry_id,
                                generate_summaries=do_summaries,
                                generate_embeddings=do_embeddings,
                            )
                            if enrich_result.success:
                                if enrich_result.is_noop:
                                    log_debug(f"[GRAPH] No enrichment needed for {topic}/{entry_id}")
                                else:
                                    generated = []
                                    if enrich_result.summary_generated:
                                        generated.append("summary")
                                    if enrich_result.embedding_generated:
                                        generated.append("embedding")
                                    log_debug(f"[GRAPH] Enrichment complete for {topic}/{entry_id}: {', '.join(generated)}")
                            else:
                                log_warning(f"[GRAPH] Enrichment failed for {topic}/{entry_id}: {enrich_result.error_message}")

                            # Log partial enrichment if some services were unavailable
                            if graph_config.generate_summaries and not llm_available:
                                log_debug(f"[GRAPH] LLM unavailable, skipping summary for {topic}/{entry_id}")
                            if graph_config.generate_embeddings and not embed_available:
                                log_debug(f"[GRAPH] Embedding service unavailable, skipping embedding for {topic}/{entry_id}")
                        else:
                            # No services available - log and continue without enrichment
                            # Entry is already saved (by graph-first write), just without
                            # summaries/embeddings. Use watercooler_backfill_graph later.
                            log_warning(
                                f"[GRAPH] Enrichment services unavailable for {topic}/{entry_id}. "
                                f"Entry saved without summary/embedding. Run backfill to add later."
                            )
                except Exception as graph_err:
                    # Enrichment failure is logged but doesn't block the write
                    log_warning(f"[GRAPH] Enrichment failed for {topic}/{entry_id}: {graph_err}")

                # Memory backend sync (T2: Graphiti/LeanRAG indexing)
                # Runs independently of enrichment - memory backends need every
                # entry regardless of summary/embedding availability.
                # Note: enrichment runs synchronously above, so if it produced a
                # summary the graph entry already contains it by this point;
                # entry_summary will carry the enriched value to the backend.
                try:
                    from watercooler.baseline_graph.sync import sync_to_memory_backend
                    from watercooler.baseline_graph.writer import get_entry_node_from_graph

                    entry_node = get_entry_node_from_graph(
                        context.threads_dir, entry_id, topic
                    )
                    if entry_node:
                        synced = sync_to_memory_backend(
                            threads_dir=context.threads_dir,
                            topic=topic,
                            entry_id=entry_id,
                            entry_body=entry_node.get("body", ""),
                            entry_title=entry_node.get("title"),
                            entry_summary=entry_node.get("summary", ""),
                            timestamp=entry_node.get("timestamp"),
                            agent=entry_node.get("agent"),
                            role=entry_node.get("role"),
                            entry_type=entry_node.get("entry_type"),
                        )
                        if synced:
                            log_debug(f"[MEMORY] Synced {topic}/{entry_id} to memory backend")
                        else:
                            log_debug(f"[MEMORY] Memory sync skipped for {topic}/{entry_id} (backend not active)")
                    else:
                        log_warning(
                            f"[MEMORY] Entry not found in graph for memory sync: "
                            f"{topic}/{entry_id}"
                        )
                except Exception as mem_err:
                    log_warning(
                        f"[MEMORY] Memory backend sync failed for "
                        f"{topic}/{entry_id}: {mem_err}"
                    )

            return result

        # Run operation, then commit+push directly
        result = operation_with_graph_sync()
        if sync_status is not None:
            sync_status["operation_completed"] = True

        # Commit and push in the worktree
        if context.threads_dir and (context.threads_dir / ".git").exists():
            # Use worktree lock from pre-write phase if held (no gap)
            worktree_lock = pre_write_wt_lock
            pre_write_wt_lock = None  # ownership transferred
            try:
                from git import Repo
                from .sync.primitives import push_with_retry
                from watercooler.sync_common import acquire_worktree_lock, paths_to_stage_for_topic

                if not worktree_lock:
                    worktree_lock = acquire_worktree_lock(context.threads_dir)
                    log_action("sync.lock.acquire", outcome="ok", scope="worktree")

                threads_repo = Repo(context.threads_dir)
                # Topic-scoped staging: only stage files belonging to this topic
                if topic:
                    stage_paths = paths_to_stage_for_topic(
                        context.threads_dir, topic, include_missing=True,
                    )
                    if stage_paths:
                        # --all stages deletions as well as additions
                        threads_repo.git.add("--all", "--", *stage_paths)
                    else:
                        log_debug(f"[SYNC] No paths to stage for topic '{topic}'")
                else:
                    # Fallback for non-topic operations (e.g. graph-only ops)
                    threads_repo.git.add("-A")
                if threads_repo.is_dirty(index=True):
                    threads_repo.index.commit(commit_message)
                    if sync_status is not None:
                        sync_status["committed"] = True
                    log_debug(f"[SYNC] Commit: {commit_title}")
                    # Push with retry — raise PushError on failure so callers
                    # can't silently ignore it
                    push_cause = None
                    try:
                        pushed = push_with_retry(threads_repo, max_retries=5)
                    except Exception as push_err:
                        pushed = False
                        push_cause = push_err
                        log_warning(f"[SYNC] Push failed: {push_err}")

                    if pushed:
                        if sync_status is not None:
                            sync_status["pushed"] = True
                        log_debug("[SYNC] Push succeeded")
                        log_action("sync.push", outcome="ok")
                    else:
                        log_warning(
                            f"[SYNC] Push failed after retries for topic '{topic}' "
                            f"— entry committed locally but not pushed to remote"
                        )
                        log_action("sync.push", outcome="failed", topic=topic or "")
                        raise PushError(
                            message=(
                                f"Entry committed locally but push to remote failed for '{topic}'. "
                                "The entry is safe in the local worktree — fix the push side "
                                "(auth/network); it syncs on the next successful write. "
                                "Inspect with watercooler_sync_repair(diagnose_only=True)."
                            ),
                            context={"topic": topic or ""},
                            recovery_hint=(
                                "Fix push auth/network and verify with `git push --dry-run`. "
                                "The entry is preserved locally — watercooler_sync_repair "
                                "recovers it by pushing/rebasing (it does not discard "
                                "local-only commits unless discard_local_commits=True)."
                            ),
                            cause=push_cause,
                        )
                else:
                    log_debug("[SYNC] No changes to commit")
            except PushError:
                if sync_status is not None and not sync_status.get("error"):
                    sync_status["error"] = "Push failed after retries"
                raise  # Propagate to caller — don't swallow
            except TimeoutError:
                if sync_status is not None:
                    sync_status["error"] = "Worktree lock timeout during commit/push"
                log_action("sync.lock.acquire", outcome="timeout", scope="worktree")
                log_warning("[SYNC] Worktree lock timeout — entry written locally but not committed")
            except Exception as commit_err:
                if sync_status is not None:
                    sync_status["error"] = str(commit_err)
                log_warning(f"[SYNC] Commit/push failed: {commit_err}")
            finally:
                if worktree_lock:
                    worktree_lock.release()
                    log_action("sync.lock.release", scope="worktree")

        return result
    finally:
        # Release pre-write worktree lock if not transferred to commit phase
        if pre_write_wt_lock:
            pre_write_wt_lock.release()
        if lock:
            lock.release()
            log_debug(f"[SYNC] Released lock for topic '{topic}'")
            log_action("parity.lock.release", scope="topic", topic=topic)


def run_with_graph_sync(
    context: ThreadContext,
    operation: Callable[[], T],
    commit_msg: str,
    *,
    topic: str | None = None,
) -> T:
    """Execute graph operation with sync.

    Flow: guard -> operation -> commit graph files -> push

    Guards against writing into a non-GitHub-backed target (Bug #3,
    plan v4): ``assert_github_backed_threads`` runs BEFORE the graph
    mutation, matching ``run_with_sync``'s contract. Without this
    guard the ``graph_project`` and ``graph_enrich`` tools could
    mutate a local-only or non-GitHub target and then silently skip
    the push — leaving the graph divergent from the remote.

    Args:
        topic: If provided, uses topic-scoped staging instead of git add -A.
    """
    from watercooler.write_guard import (
        WatercoolerWriteError,
        assert_github_backed_threads,
    )

    # Same hard-fail as ``run_with_sync``: missing ``threads_dir`` is
    # a misconfiguration, not a reason to silently bypass the guard.
    if context.threads_dir is None:
        raise WatercoolerWriteError(
            "Cannot write threads — no threads_dir is configured on "
            "this request context. Ensure WATERCOOLER_DIR is set or "
            "the code_path argument resolves to a git repository. "
            "(WATERCOOLER_ALLOW_LOCAL_ONLY does NOT help here — a "
            "missing threads_dir is a configuration gap, not local-"
            "only mode.) "
            "See docs/TROUBLESHOOTING.md#local-only-mode for details."
        )

    # Raises WatercoolerWriteError when threads_dir is not GitHub-
    # backed (and the opt-in is absent). MCP tool wrappers convert
    # the exception to a user-visible error.
    assert_github_backed_threads(context.threads_dir)

    result = operation()

    if (context.threads_dir / ".git").exists():
        # Note: acquires worktree lock only (no topic lock). This is safe
        # because run_with_graph_sync never acquires a topic lock, so the
        # "topic → worktree" ordering invariant is not violated. The
        # invariant applies only when BOTH locks are held by one caller.
        worktree_lock = None
        try:
            from git import Repo
            from .sync.primitives import push_with_retry
            from watercooler.sync_common import acquire_worktree_lock, paths_to_stage_for_topic

            worktree_lock = acquire_worktree_lock(context.threads_dir)
            threads_repo = Repo(context.threads_dir)
            if topic:
                stage_paths = paths_to_stage_for_topic(
                    context.threads_dir, topic, include_missing=True,
                )
                if stage_paths:
                    threads_repo.git.add("--all", "--", *stage_paths)
            else:
                # No topic context (e.g. bulk graph ops) — stage all
                threads_repo.git.add("-A")
            if threads_repo.is_dirty(index=True):
                threads_repo.index.commit(commit_msg)
                pushed = False
                try:
                    pushed = push_with_retry(threads_repo, max_retries=5)
                    if pushed:
                        log_debug("[SYNC] Graph push succeeded")
                    else:
                        log_warning(
                            "[SYNC] Graph push failed after retries "
                            "— committed locally but not pushed to remote"
                        )
                except Exception as push_err:
                    log_warning(f"[SYNC] Graph push failed: {push_err}")

                if not pushed and isinstance(result, str):
                    result += (
                        "\n\n⚠️ Entry committed locally but push to remote failed. "
                        "The entry is safe in the local worktree — fix the push side "
                        "(auth/network); it syncs on the next successful write or via "
                        "watercooler_sync_repair."
                    )
        except TimeoutError:
            log_warning("[SYNC] Worktree lock timeout for graph sync")
        except Exception as commit_err:
            log_warning(f"[SYNC] Graph commit/push failed: {commit_err}")
        finally:
            if worktree_lock:
                worktree_lock.release()

    return result
