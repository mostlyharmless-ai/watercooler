"""Middleware for watercooler MCP server.

Contains:
- Instrumentation: FunctionTool monkey-patching for observability
- Sync wrappers: run_with_sync, run_with_graph_sync
"""

import asyncio
import sys
import json
import time
from typing import Callable, TypeVar

from watercooler.memory_config import is_anthropic_url, AUTH_SKIP_SENTINELS

from .config import (
    ThreadContext,
    get_git_sync_manager_from_context,
    get_watercooler_config,
)
# Import from sync package
from .sync import (
    BranchPairingError,
    ParityStatus,
    read_parity_state,
    write_parity_state,
    run_preflight,
    acquire_topic_lock,
    acquire_parity_lock,
    _now_iso,
)
from .observability import log_debug, log_action, log_warning
from .helpers import _should_auto_branch, _build_commit_footers

# NOTE: Graph-first mode is now ALWAYS enabled. The WATERCOOLER_GRAPH_FIRST env var
# is deprecated and ignored. All writes go through commands_graph.py which writes
# structural data first, then projects to markdown. Enrichment (summaries/embeddings)
# runs after the structural write if services are available.

# Service availability cache (TTL-based to avoid HTTP checks on every write)
_SERVICE_CACHE_TTL_SECONDS = 60.0  # Cache results for 60 seconds
_service_availability_cache: dict[str, tuple[bool, bool, float]] = {}
# Key format: f"{llm_base}|{embed_base}", Value: (llm_available, embed_available, timestamp)


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

T = TypeVar("T")

# Tool-level timeouts (seconds). Tools not listed use _DEFAULT_TOOL_TIMEOUT.
# Prevents server process death when tools exceed MCP SDK's 60s hard limit.
# Tools with timeouts >60s still hit the client-side timeout, but the server
# stays alive instead of crashing — the key improvement.
_DEFAULT_TOOL_TIMEOUT: float = 50.0  # Under MCP SDK's 60s hard limit

_TOOL_TIMEOUTS: dict[str, float] = {
    # Heavy graph operations (file parsing, verify_parity)
    "watercooler_graph_health": 180.0,
    "watercooler_graph_enrich": 300.0,
    "watercooler_graph_recover": 300.0,
    # Memory pipeline operations (clustering, embedding generation)
    "watercooler_leanrag_run_pipeline": 300.0,
    # Smart query T3 escalation can be slow
    "watercooler_smart_query": 120.0,
    # Branch operations with git fetch + checkout loop
    "watercooler_audit_branch_pairing": 120.0,
}


def setup_instrumentation() -> None:
    """Set up FunctionTool instrumentation for observability.

    Call this once at server startup to monkey-patch FastMCP's FunctionTool.run
    method with timing, logging, and per-tool timeouts.

    Timeouts prevent server process death under CPU pressure. Tools that exceed
    their timeout return a graceful error instead of crashing the server.
    """
    global _orig_run

    try:
        from fastmcp.tools.tool import FunctionTool  # type: ignore

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
) -> T:
    """Execute operation with git sync and branch parity enforcement.

    Flow: acquire lock → run preflight → operation → commit → push → release lock

    The new preflight state machine replaces the old _validate_and_sync_branches()
    with comprehensive auto-remediation:
    - Branch mismatch: auto-checkout threads to match code
    - Missing remote branch: auto-push threads branch
    - Threads behind origin: auto-pull with ff-only or rebase
    - Main protection: block writes when threads=main but code=feature
    """
    sync = get_git_sync_manager_from_context(context)
    if not sync:
        return operation()

    # Per-topic locking to serialize concurrent writes
    parity_lock = None
    lock = None
    try:
        # Parity/lifecycle lock serializes branch topology-sensitive actions
        if context.threads_dir:
            try:
                parity_lock = acquire_parity_lock(context.threads_dir, timeout=30)
                log_debug("[PARITY] Acquired parity lock")
                log_action("parity.lock.acquire", scope="repo-pair", outcome="ok")
            except TimeoutError as e:
                log_action("parity.lock.acquire", scope="repo-pair", outcome="timeout")
                raise BranchPairingError(f"Failed to acquire parity lock: {e}")

        if topic and context.threads_dir:
            try:
                lock = acquire_topic_lock(context.threads_dir, topic, timeout=30)
                log_debug(f"[PARITY] Acquired lock for topic '{topic}'")
                log_action("parity.lock.acquire", scope="topic", topic=topic, outcome="ok")
            except TimeoutError as e:
                log_action("parity.lock.acquire", scope="topic", topic=topic, outcome="timeout")
                raise BranchPairingError(f"Failed to acquire lock for topic '{topic}': {e}")

        # Run preflight with auto-remediation instead of old validation
        log_debug(f"[PARITY] Preflight check: skip_validation={skip_validation}, code_root={context.code_root}, threads_dir={context.threads_dir}")
        if not skip_validation and context.code_root and context.threads_dir:
            log_debug("[PARITY] Running run_preflight...")
            preflight_result = run_preflight(
                code_repo_path=context.code_root,
                threads_repo_path=context.threads_dir,
                auto_fix=_should_auto_branch(),
                fetch_first=True,
            )
            if not preflight_result.can_proceed:
                raise BranchPairingError(
                    preflight_result.blocking_reason or "Branch parity preflight failed"
                )
            if preflight_result.auto_fixed:
                log_debug(f"[PARITY] Auto-fixed: {preflight_result.state.actions_taken}")
            log_debug(f"[PARITY] Preflight result: can_proceed={preflight_result.can_proceed}")
        else:
            log_debug(f"[PARITY] Skipping preflight (condition not met)")

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

                # Always sync to memory backend, independent of enrichment.
                # This ensures Graphiti/LeanRAG indexing runs on every write,
                # even when enrichment is skipped, disabled, or fails.
                #
                # Safety: enrichment (above) is synchronous and completes before
                # this point, so the graph entry is fully written when we read it.
                try:
                    from watercooler.baseline_graph.sync import sync_entry_to_memory_backend
                    if sync_entry_to_memory_backend(context.threads_dir, topic, entry_id):
                        log_debug(f"[GRAPH] Memory sync submitted for {topic}/{entry_id}")
                except Exception as mem_err:
                    log_warning(f"[GRAPH] Memory sync failed for {topic}/{entry_id}: {mem_err}")

            return result

        # Execute operation with git sync (pull → operation+graph → commit → push)
        result = sync.with_sync(
            operation_with_graph_sync,
            commit_message,
            topic=topic,
            entry_id=entry_id,
            priority_flush=priority_flush,
        )

        # NOTE: Enrichment (summaries/embeddings) is now handled in operation_with_graph_sync
        # above, within the same atomic commit. If services are unavailable, structural-only
        # sync is performed. Use watercooler_backfill_graph to add enrichment later.
        # This eliminates the race condition from the previous two-phase commit approach.

        # Update parity state file after successful write
        if context.code_root and context.threads_dir:
            try:
                state = read_parity_state(context.threads_dir)
                # Mark as clean after successful sync
                state.status = ParityStatus.CLEAN.value
                state.pending_push = False
                state.last_check_at = _now_iso()
                state.last_error = None
                write_parity_state(context.threads_dir, state)
            except Exception as state_err:
                log_debug(f"[PARITY] Failed to update state after write: {state_err}")

        return result
    finally:
        if lock:
            lock.release()
            log_debug(f"[PARITY] Released lock for topic '{topic}'")
            log_action("parity.lock.release", scope="topic", topic=topic)
        if parity_lock:
            parity_lock.release()
            log_debug("[PARITY] Released parity lock")
            log_action("parity.lock.release", scope="repo-pair")


def run_with_graph_sync(
    context: ThreadContext,
    operation: Callable[[], T],
    commit_msg: str,
) -> T:
    """Execute graph operation with full parity protocol.

    Flow: preflight → operation → commit graph files → push with retry

    Unlike run_with_sync, this is simpler:
    - No topic lock (graph operations are idempotent)
    - No entry footers (graph files, not thread entries)
    - Commits only graph/baseline/* files
    """
    sync = get_git_sync_manager_from_context(context)

    parity_lock = None
    try:
        # 1. Preflight (Factor 1 + Factor 2 pre-check) with parity lock
        if context.threads_dir:
            try:
                parity_lock = acquire_parity_lock(context.threads_dir, timeout=30)
                log_debug("[PARITY] Acquired parity lock")
                log_action("parity.lock.acquire", scope="repo-pair", outcome="ok")
            except TimeoutError as e:
                log_action("parity.lock.acquire", scope="repo-pair", outcome="timeout")
                raise BranchPairingError(f"Failed to acquire parity lock: {e}")

        if context.code_root and context.threads_dir:
            preflight_result = run_preflight(
                code_repo_path=context.code_root,
                threads_repo_path=context.threads_dir,
                auto_fix=True,
                fetch_first=True,
            )
            if not preflight_result.can_proceed:
                raise BranchPairingError(
                    preflight_result.blocking_reason or "Branch parity preflight failed"
                )
            if preflight_result.auto_fixed:
                log_debug(f"[GRAPH-SYNC] Preflight auto-fixed: {preflight_result.state.actions_taken}")

        # 2. Execute operation
        result = operation()

        # 3. Commit and push graph files (blocking)
        if sync and context.threads_dir:
            sync.commit_graph_changes_sync(commit_msg)

        return result
    finally:
        if parity_lock:
            parity_lock.release()
            log_debug("[PARITY] Released parity lock")
            log_action("parity.lock.release", scope="repo-pair")
