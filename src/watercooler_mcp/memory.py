"""Memory backend integration for MCP server.

Provides lazy-loading of Graphiti memory backend with graceful degradation.
Follows MCP server patterns for configuration, observability, and error handling.
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from .observability import log_debug, log_error, log_warning, log_warning_once

# Import unified config helpers
from watercooler.memory_config import (
    is_memory_enabled,
    get_memory_backend,
    resolve_llm_config,
    resolve_embedding_config,
    resolve_database_config,
    get_graphiti_reranker,
    get_leanrag_path,
    is_localhost_url,
)

# Import unified derive_group_id from path_resolver
from watercooler.path_resolver import derive_group_id

# Import backend's GraphitiConfig directly (consolidates duplicate configs)
try:
    from watercooler_memory.backends.graphiti import GraphitiConfig, _derive_database_name
except ImportError:
    # If backend not installed, define a stub that matches all fields used by
    # load_graphiti_config() so the function can construct and return a config
    # object on public installations (backend calls will not succeed without
    # the private watercooler_memory package).
    from dataclasses import dataclass
    from typing import Optional as _Opt
    @dataclass
    class GraphitiConfig:  # type: ignore
        """Config stub when watercooler_memory backend is not installed."""
        llm_api_key: str = ""
        llm_api_base: _Opt[str] = None
        llm_model: str = ""
        embedding_api_key: str = ""
        embedding_api_base: _Opt[str] = None
        embedding_model: str = ""
        falkordb_host: str = "localhost"
        falkordb_port: int = 6379
        falkordb_password: _Opt[str] = None
        falkordb_socket_timeout: int = 600
        reranker: str = "rrf"
        database: str = ""

    def _derive_database_name(code_path: Path | str | None) -> str:
        """Fallback database name derivation using unified function."""
        return derive_group_id(code_path=Path(code_path) if code_path else None)


@functools.lru_cache(maxsize=1)
def _graphiti_importable() -> bool:
    """Return True if the GraphitiBackend class can be imported.

    Result is cached for the lifetime of the process.
    """
    try:
        from watercooler_memory.backends import GraphitiBackend  # noqa: F401
        return True
    except ImportError:
        return False


def load_graphiti_config(
    code_path: str | Path | None = None,
    *,
    database: str | None = None,
) -> Optional[GraphitiConfig]:
    """Load Graphiti configuration from unified config system.

    Uses the new unified configuration with priority chain:
    1. Environment variables (highest)
    2. Backend-specific TOML overrides (memory.graphiti.*)
    3. Shared TOML settings (memory.llm.*, memory.embedding.*)
    4. Built-in defaults (lowest)

    Returns None if Graphiti is disabled or configuration is invalid.
    Logs warnings for configuration issues.

    Args:
        code_path: Path to the project directory. Used to derive the database name
            for the unified project graph (e.g., 'watercooler-cloud' -> 'watercooler_cloud').
            If not provided, defaults to 'watercooler'.
        database: Optional explicit T2 database name override. The trusted
            "no-request-context" escape hatch for queue workers, the startup
            warmup probe, and other code paths that run outside an HTTP
            request. Hosted-request scope (``http_ctx.repo``) ALWAYS dominates
            this override; a hosted request whose derived database disagrees
            with this override raises ``RuntimeError`` (fail closed) rather
            than risk routing one tenant's traffic into another tenant's
            graph. Empty strings are treated as ``None``.

    Configuration Sources:
        TOML (config.toml):
            [memory]
            enabled = true
            backend = "graphiti"

            [memory.llm]
            api_key = ""
            api_base = "https://api.openai.com/v1"
            model = "gpt-4o-mini"

            [memory.embedding]
            api_key = ""
            api_base = "http://localhost:8080/v1"
            model = "bge-m3"

            [memory.graphiti]
            reranker = "rrf"

        Environment Variables (override TOML):
            WATERCOOLER_MEMORY_DISABLED: "1" to disable all memory backends
            WATERCOOLER_GRAPHITI_ENABLED: "1" to enable, "0" to disable
                (if not set, uses [memory].backend from TOML config)
            WATERCOOLER_GRAPHITI_DATABASE: Override derived database name
            LLM_API_KEY, LLM_API_BASE, LLM_MODEL
            EMBEDDING_API_KEY, EMBEDDING_API_BASE, EMBEDDING_MODEL
            WATERCOOLER_GRAPHITI_RERANKER

        Deprecated Fallback:
            OPENAI_API_KEY: Falls back to this if LLM_API_KEY or EMBEDDING_API_KEY
                is not set. A warning is logged when the fallback is used.

    Returns:
        GraphitiConfig instance or None if disabled/invalid

    Example:
        >>> config = load_graphiti_config(code_path="/home/user/my-project")
        >>> if config:
        ...     backend = get_graphiti_backend(config)
    """
    # Check global memory disable switch first
    if not is_memory_enabled():
        log_debug("MEMORY: All memory backends disabled (WATERCOOLER_MEMORY_DISABLED=1)")
        return None

    # Check Graphiti-specific switch
    # Priority: env var > TOML config
    # - WATERCOOLER_GRAPHITI_ENABLED=1 explicitly enables
    # - WATERCOOLER_GRAPHITI_ENABLED=0 explicitly disables
    # - If env var not set, check [memory] backend in TOML
    env_enabled = os.getenv("WATERCOOLER_GRAPHITI_ENABLED", "").lower()
    if env_enabled in ("1", "true", "yes"):
        enabled = True
    elif env_enabled in ("0", "false", "no"):
        enabled = False
    else:
        # Fall back to TOML config: memory.backend == "graphiti"
        try:
            enabled = get_memory_backend() == "graphiti"
        except ValueError:
            enabled = False

    if not enabled:
        log_debug("MEMORY: Graphiti disabled (WATERCOOLER_GRAPHITI_ENABLED != '1' and memory.backend != 'graphiti')")
        return None

    # Resolve LLM configuration using unified config
    llm = resolve_llm_config("graphiti")
    llm_is_local = is_localhost_url(llm.api_base)
    if not llm.api_key and not llm_is_local:
        log_warning(
            "MEMORY: Graphiti enabled but LLM API key not set. "
            "Memory queries will fail. Set LLM_API_KEY env var, add key to "
            "~/.watercooler/credentials.toml, or use a localhost endpoint."
        )
        return None

    # Resolve embedding configuration using unified config
    embedding = resolve_embedding_config("graphiti")
    embedding_is_local = is_localhost_url(embedding.api_base)
    if not embedding.api_key and not embedding_is_local:
        log_warning(
            "MEMORY: Graphiti enabled but embedding API key not set. "
            "Memory queries will fail. Set EMBEDDING_API_KEY env var, add key to "
            "~/.watercooler/credentials.toml, or use a localhost endpoint."
        )
        return None

    # Resolve database configuration
    db = resolve_database_config()

    # Get reranker algorithm
    reranker = get_graphiti_reranker()

    # Derive the canonical T2 database name. Resolution precedence:
    #
    # 1. Active hosted ``http_ctx.repo`` — per-request multi-tenant routing.
    #    On a hosted Railway deployment a single MCP process serves
    #    multiple ``<org>/<repo>`` tenants; the database name MUST come
    #    from the request's ``X-Repo`` header so reads target the same
    #    tenant graph that the write-side ``_canonicalize_t2_group_id``
    #    routes to. This is the multi-tenant correctness invariant from
    #    Plan v20 ("Multi-org hosted users already exist (Caleb + Jay)").
    # 2. ``database=`` keyword override — trusted no-request-context escape
    #    hatch for queue workers, the startup warmup probe, and daemon
    #    threads. Always dominated by (1); a mismatch raises so a buggy
    #    caller cannot silently route a hosted request into the wrong
    #    tenant's graph.
    # 3. ``WATERCOOLER_GRAPHITI_DATABASE`` env var — single-tenant escape
    #    hatch for stdio / dev / single-org self-hosted deployments.
    # 4. ``derive_t2_database_name(code_path=...)`` — final fallback that
    #    resolves the canonical ``<org>_<repo>_t2`` from a real local
    #    code repository when one is available.
    from watercooler.path_resolver import derive_t2_database_name

    # Treat empty-string override as absent (defensive — prevents an
    # accidental empty value from silently bypassing scope under hosted mode).
    database_override: str | None = database if database else None

    resolved_database: str | None = None
    http_ctx = None
    try:
        from .context import get_effective_context
        http_ctx = get_effective_context()
    except ImportError:
        http_ctx = None
    except Exception as _ctx_err:
        # Runtime error from get_effective_context() (rare — contextvar
        # access in unusual threading configs). Log at ERROR before deciding
        # whether hosted mode must fail closed or local mode may fall back.
        log_error(
            f"MEMORY: get_effective_context() raised "
            f"{_ctx_err.__class__.__name__}: {_ctx_err}; hosted mode will "
            f"fail closed before env/code_path fallback."
        )
        http_ctx = None
    if http_ctx is not None and getattr(http_ctx, "repo", None):
        # Hosted multi-tenant invariant: when an HTTP request context is
        # active, the database MUST come from ``X-Repo`` per request. We
        # refuse to fall back to a process-wide env var / code_path here
        # — a silent fallback under hosted multi-tenancy would let one
        # tenant's reads target another tenant's graph (or a shared default).
        # Malformed headers (no ``/``, empty owner, or empty repo after
        # the threads-suffix strip) are rejected so callers see a
        # structured "graphiti unavailable" rather than silently
        # writing/reading from the wrong place.
        raw = http_ctx.repo
        if "/" not in raw:
            log_error(
                f"MEMORY: hosted http_ctx.repo={raw!r} is malformed (no '/'); "
                f"refusing to silently fall back to a single-tenant database. "
                f"Graphiti config unavailable for this request."
            )
            return None
        owner, repo_part = raw.split("/", 1)
        # Use ``get_threads_suffix()`` rather than a hardcoded ``"-threads"``
        # so deployments that override ``WATERCOOLER_THREADS_SUFFIX`` derive
        # the same canonical name as the writer-side helper.
        from watercooler.path_resolver import get_threads_suffix
        threads_suffix = get_threads_suffix()
        if threads_suffix and repo_part.endswith(threads_suffix):
            repo_part = repo_part[: -len(threads_suffix)]
        if not owner or not repo_part:
            log_error(
                f"MEMORY: hosted http_ctx.repo={raw!r} has empty owner or "
                f"repo after threads-suffix strip (owner={owner!r}, "
                f"repo={repo_part!r}); refusing to fall back to a "
                f"non-canonical database name. Graphiti config unavailable."
            )
            return None
        try:
            resolved_database = derive_t2_database_name(repo_slug=f"{owner}/{repo_part}")
        except Exception as e:
            log_error(
                f"MEMORY: derive_t2_database_name failed for http_ctx.repo={raw!r}: {e}; "
                f"refusing to fall back to env/code_path under hosted ctx. "
                f"Graphiti config unavailable for this request."
            )
            return None

    # Hosted-request scope DOMINATES the explicit ``database=`` override.
    # If both are set and they disagree, fail closed — never silently route
    # one tenant's traffic into another tenant's graph. The override is a
    # trusted escape hatch ONLY for code paths that have no request scope.
    if resolved_database is not None and database_override is not None and resolved_database != database_override:
        raise RuntimeError(
            f"database= override conflicts with hosted request scope: "
            f"override={database_override!r} but request scope derived "
            f"{resolved_database!r}; refusing to silently route across tenants"
        )

    try:
        from .auth import is_hosted_mode
    except ImportError as _hosted_err:
        log_error(
            f"MEMORY: failed to import is_hosted_mode: "
            f"{_hosted_err.__class__.__name__}: {_hosted_err}; allowing "
            f"env/code_path fallback because hosted mode cannot be active "
            f"without hosted auth helpers."
        )
        hosted_mode = False
    except Exception as _hosted_err:
        log_error(
            f"MEMORY: failed to import is_hosted_mode: "
            f"{_hosted_err.__class__.__name__}: {_hosted_err}; refusing "
            f"env/code_path fallback because hosted-mode detection could "
            f"not be loaded safely."
        )
        hosted_mode = True
    else:
        try:
            hosted_mode = is_hosted_mode()
        except Exception as _hosted_err:
            log_error(
                f"MEMORY: is_hosted_mode() raised "
                f"{_hosted_err.__class__.__name__}: {_hosted_err}; refusing "
                f"env/code_path fallback because hosted scope safety could not "
                f"be determined."
            )
            hosted_mode = True

    # Apply the trusted ``database=`` override BEFORE the hosted-mode
    # fail-closed guard. Without an active ``http_ctx`` the worker thread
    # / warmup probe / daemon path has no other source of canonical scope;
    # the override carries that scope (e.g. ``MemoryTask.group_id``).
    if not resolved_database and database_override is not None:
        resolved_database = database_override

    if hosted_mode and not resolved_database:
        log_error(
            "MEMORY: hosted mode has no effective X-Repo scope; refusing to "
            "fall back to WATERCOOLER_GRAPHITI_DATABASE or code_path because "
            "that would route hosted T2 traffic to a shared/non-canonical graph."
        )
        return None

    if not resolved_database:
        resolved_database = os.getenv("WATERCOOLER_GRAPHITI_DATABASE")
    if not resolved_database:
        resolved_database = derive_t2_database_name(code_path=code_path)

    # Return backend's GraphitiConfig with all fields
    # For localhost endpoints without keys, pass a sentinel placeholder
    # (local servers like llama-server don't need real API keys)
    llm_api_key = llm.api_key or ("LOCAL_NO_KEY" if llm_is_local else "")
    embedding_api_key = embedding.api_key or ("LOCAL_NO_KEY" if embedding_is_local else "")

    return GraphitiConfig(
        llm_api_key=llm_api_key,
        llm_api_base=llm.api_base or None,
        llm_model=llm.model,
        embedding_api_key=embedding_api_key,
        embedding_api_base=embedding.api_base or None,
        embedding_model=embedding.model,
        falkordb_host=db.host,
        falkordb_port=db.port,
        falkordb_password=db.password if db.password else None,
        falkordb_socket_timeout=db.socket_timeout,
        reranker=reranker,
        database=resolved_database,
    )


def load_leanrag_config(code_path: str | Path | None = None) -> Optional["LeanRAGConfig"]:
    """Load LeanRAG configuration from unified config system.

    Returns None if LeanRAG is disabled or configuration is invalid.

    Args:
        code_path: Path to the project directory. Used to set the work_dir
            for LeanRAG exports.

    Configuration Sources:
        Environment Variables:
            WATERCOOLER_MEMORY_DISABLED: "1" to disable all memory backends
            WATERCOOLER_LEANRAG_ENABLED: "1" to enable, "0" to disable
                (if not set, uses [memory].backend from TOML config)
            LEANRAG_PATH: Path to the LeanRAG submodule (required)
            WATERCOOLER_LEANRAG_DATABASE: Override derived database name

        TOML (config.toml):
            [memory]
            enabled = true
            backend = "leanrag"

            [memory.leanrag]
            max_workers = 8

    Returns:
        LeanRAGConfig instance or None if disabled/invalid
    """
    # 1. Global disable check
    if not is_memory_enabled():
        log_debug("MEMORY: All memory backends disabled")
        return None

    # 2. LeanRAG-specific enable check
    # Priority: env var > TOML tier config > TOML backend setting
    env_enabled = os.getenv("WATERCOOLER_LEANRAG_ENABLED", "").lower()
    if env_enabled in ("1", "true", "yes"):
        enabled = True
    elif env_enabled in ("0", "false", "no"):
        enabled = False
    else:
        # Check tier config first (t3_enabled implies LeanRAG should be available)
        try:
            from watercooler.memory_config import resolve_tier_config
            tier_cfg = resolve_tier_config()
            if tier_cfg.t3_enabled:
                enabled = True
            else:
                try:
                    enabled = get_memory_backend() == "leanrag"
                except ValueError:
                    enabled = False
        except ImportError:
            try:
                enabled = get_memory_backend() == "leanrag"
            except ValueError:
                enabled = False

    if not enabled:
        log_debug("MEMORY: LeanRAG disabled (set memory.tiers.t3_enabled=true or WATERCOOLER_LEANRAG_ENABLED=1)")
        return None

    # 3. Check LEANRAG_PATH exists (env var > TOML config)
    leanrag_path = get_leanrag_path()
    if not leanrag_path:
        log_warning(
            "MEMORY: LeanRAG enabled but path not configured. "
            "Set LEANRAG_PATH env var or memory.leanrag.path in config.toml"
        )
        return None

    leanrag_path_obj = Path(leanrag_path).expanduser()
    if not leanrag_path_obj.exists():
        log_warning(f"MEMORY: LeanRAG path does not exist: {leanrag_path}")
        return None

    # 4. Import and create config using unified system
    try:
        from watercooler_memory.backends.leanrag import LeanRAGConfig
    except ImportError:
        log_warning("MEMORY: LeanRAG backend not available")
        return None

    # 5. Use from_unified() which handles LLM/embedding/database config
    try:
        config = LeanRAGConfig.from_unified()
        config.leanrag_path = leanrag_path_obj

        # Derive database name with leanrag_ prefix to avoid collision
        # with Graphiti backend in the same FalkorDB instance.
        # Graphiti uses "watercooler_cloud", LeanRAG uses "leanrag_watercooler_cloud".
        # Explicit WATERCOOLER_LEANRAG_DATABASE overrides are respected as-is.
        database = os.getenv("WATERCOOLER_LEANRAG_DATABASE")
        if not database:
            database = f"leanrag_{_derive_database_name(code_path)}"

        # Set work_dir to ~/.watercooler/{database_name}
        # LeanRAG uses work_dir.name as FalkorDB graph name
        watercooler_home = Path.home() / ".watercooler"
        config.work_dir = watercooler_home / database

        return config
    except Exception as e:
        log_warning(f"MEMORY: Failed to create LeanRAG config: {e}")
        return None


# Import LeanRAGConfig for type hints
try:
    from watercooler_memory.backends.leanrag import LeanRAGConfig
except ImportError:
    # If backend not installed, define minimal config for type hints
    from dataclasses import dataclass as _dataclass
    @_dataclass
    class LeanRAGConfig:  # type: ignore
        """Minimal config stub when backend unavailable."""
        leanrag_path: Path | None = None
        work_dir: Path | None = None


def get_graphiti_backend(config: GraphitiConfig) -> Any:
    """Lazy-load and initialize Graphiti backend.

    Args:
        config: GraphitiConfig instance from load_graphiti_config()

    Returns:
        GraphitiBackend instance or None if dependencies unavailable

    Raises:
        ImportError: If watercooler_memory.backends not installed

    Example:
        >>> config = load_graphiti_config()
        >>> if config:
        ...     backend = get_graphiti_backend(config)
        ...     if backend:
        ...         results = query_memory(backend, "test query", limit=10)
    """
    if not _graphiti_importable():
        log_warning_once(
            "graphiti_import",
            "MEMORY: watercooler_memory not installed — T2 features disabled",
        )
        return {"error": "import_failed", "details": "watercooler_memory not installed"}

    # Plan v20 Phase 5: hybrid must never live-write T2 locally. If the
    # module-level runtime reports ``local_hybrid``, refuse to construct
    # a local Graphiti backend so the submission path has no silent
    # local fallback.
    try:
        from .memory_sync import get_runtime as _get_sync_runtime
        runtime = _get_sync_runtime()
    except Exception:
        runtime = None
    if runtime is not None and getattr(runtime, "surface", None) == "local_hybrid":
        log_warning_once(
            "graphiti_hybrid_refused",
            "MEMORY: refusing to construct local Graphiti backend in local_hybrid — "
            "T2 submission must route via premium_client.",
        )
        return {
            "error": "hybrid_refused",
            "details": (
                "Local Graphiti backend construction is blocked in local_hybrid "
                "(Plan v20 Phase 5). Route via premium_client."
            ),
        }

    try:
        from watercooler_memory.backends import GraphitiBackend  # type: ignore[attr-defined]
    except ImportError as e:
        # Rare edge case: probe succeeded but a sub-dependency broke
        log_warning(f"MEMORY: Graphiti sub-dependency import failed: {e}")
        return {"error": "import_failed", "details": str(e)}

    try:
        backend = GraphitiBackend(config)
        log_debug(
            f"MEMORY: Initialized Graphiti backend "
            f"(work_dir={config.work_dir})"
        )
        return backend
    except Exception as e:
        error_msg = f"MEMORY: Failed to initialize Graphiti backend: {e}"
        log_warning(error_msg)
        return {"error": "init_failed", "details": str(e)}


async def query_memory(
    backend: Any,
    query_text: str,
    limit: int = 10,
    topic: Optional[str] = None,
) -> tuple[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]]:
    """Execute memory query against Graphiti backend.

    Note: In the unified group_id model, all threads in a project share a single
    group_id (e.g., "watercooler_cloud"). Thread-level filtering is typically not
    needed since entities are shared across threads.

    Args:
        backend: GraphitiBackend instance
        query_text: Search query string
        limit: Maximum results to return (1-50)
        topic: Optional group_id filter. In the unified model, this would be the
              project database name (e.g., "watercooler_cloud"), not a thread topic.
              If None, searches across ALL accessible groups.

    Returns:
        Tuple of (results, communities):
        - results: List of result dictionaries with keys: query, content, score, metadata
        - communities: List of community dictionaries with top-level domain clusters

    Raises:
        Exception: For query execution failures

    Example:
        >>> backend = get_graphiti_backend(config)
        >>> results, communities = await query_memory(backend, "What auth was implemented?", limit=5)
        >>> for result in results:
        ...     print(f"{result['content']} (score: {result['score']})")
        >>> print(f"Found {len(communities)} communities")
    """
    from watercooler_memory.backends import QueryPayload

    # Build query dict
    query_dict: dict[str, Any] = {
        "query": query_text,
        "limit": limit,
    }

    # Add topic for group_id filtering
    # Note: If topic is None, backend will search across all available graphs
    if topic:
        query_dict["topic"] = topic

    payload = QueryPayload(
        manifest_version="1.0",
        queries=[query_dict],
    )

    # Backend query() is synchronous (uses asyncio.run internally)
    # Use to_thread to avoid "cannot call asyncio.run from running loop" error
    import asyncio
    result = await asyncio.to_thread(backend.query, payload)
    return result.results, result.communities


def create_error_response(
    error: str,
    message: str,
    operation: str,
    **kwargs: Any,
) -> ToolResult:
    """Create standardized error response for memory tools.
    
    Args:
        error: Error type (e.g., "Invalid UUID", "Graphiti not enabled")
        message: Human-readable error message
        operation: Tool name (e.g., "search_nodes", "get_entity_edge")
        **kwargs: Additional fields to include in error response
    
    Returns:
        ToolResult with JSON error response
    
    Example:
        >>> return create_error_response(
        ...     "Invalid UUID",
        ...     "UUID parameter is required",
        ...     "get_entity_edge"
        ... )
    """
    error_dict = {
        **kwargs,
        "error": error,
        "message": message,
        "operation": operation,
    }
    return ToolResult(content=[TextContent(
        type="text",
        text=json.dumps(error_dict, indent=2)
    )])


def validate_memory_prerequisites(
    operation: str,
    code_path: str | Path | None = None,
) -> tuple[Any, Optional[ToolResult]]:
    """Validate memory module, config, and backend prerequisites.

    Centralizes common validation logic for all memory tools:
    1. Load Graphiti configuration
    2. Initialize backend

    Args:
        operation: Tool name for error messages (e.g., "search_nodes")
        code_path: Path to the project directory (used for database name derivation)

    Returns:
        Tuple of (backend, error_response):
        - (backend, None) if successful
        - (None, error_response) if validation fails

    Example:
        >>> backend, error = validate_memory_prerequisites("search_nodes", "/path/to/project")
        >>> if error:
        ...     return error
        >>> # Use backend...
    """
    # Step 1: Load configuration
    config = load_graphiti_config(code_path=code_path)
    if config is None:
        return None, create_error_response(
            "Graphiti not enabled",
            (
                "Set WATERCOOLER_GRAPHITI_ENABLED=1 and configure "
                "OPENAI_API_KEY to enable memory queries."
            ),
            operation
        )

    # Step 2: Get backend instance
    backend = get_graphiti_backend(config)
    if backend is None or isinstance(backend, dict):
        if isinstance(backend, dict):
            error_type = backend.get("error", "unknown")
            details = backend.get("details", "No details available")
            return None, create_error_response(
                f"Backend {error_type}",
                details,
                operation
            )
        else:
            return None, create_error_response(
                "Backend initialization failed",
                "Check logs for Graphiti backend errors",
                operation
            )
    
    return backend, None
