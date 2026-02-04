"""Memory backend integration for MCP server.

Provides lazy-loading of Graphiti memory backend with graceful degradation.
Follows MCP server patterns for configuration, observability, and error handling.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from .observability import log_debug, log_warning

# Import unified config helpers
from watercooler.memory_config import (
    is_memory_enabled,
    get_memory_backend,
    resolve_llm_config,
    resolve_embedding_config,
    resolve_database_config,
    get_graphiti_reranker,
    get_leanrag_path,
    _is_localhost_url,
)

# Import unified derive_group_id from path_resolver
from watercooler.path_resolver import derive_group_id

# Import backend's GraphitiConfig directly (consolidates duplicate configs)
try:
    from watercooler_memory.backends.graphiti import GraphitiConfig, _derive_database_name
except ImportError:
    # If backend not installed, define minimal config for type hints
    from dataclasses import dataclass
    @dataclass
    class GraphitiConfig:  # type: ignore
        """Minimal config stub when backend unavailable."""
        openai_api_key: str
        reranker: str = "rrf"

    def _derive_database_name(code_path: Path | str | None) -> str:
        """Fallback database name derivation using unified function."""
        return derive_group_id(code_path=Path(code_path) if code_path else None)


def load_graphiti_config(code_path: str | Path | None = None) -> Optional[GraphitiConfig]:
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
        enabled = get_memory_backend() == "graphiti"

    if not enabled:
        log_debug("MEMORY: Graphiti disabled (WATERCOOLER_GRAPHITI_ENABLED != '1' and memory.backend != 'graphiti')")
        return None

    # Resolve LLM configuration using unified config
    llm = resolve_llm_config("graphiti")
    llm_is_local = _is_localhost_url(llm.api_base)
    if not llm.api_key and not llm_is_local:
        log_warning(
            "MEMORY: Graphiti enabled but LLM API key not set. "
            "Memory queries will fail. Set LLM_API_KEY env var, add key to "
            "~/.watercooler/credentials.toml, or use a localhost endpoint."
        )
        return None

    # Resolve embedding configuration using unified config
    embedding = resolve_embedding_config("graphiti")
    embedding_is_local = _is_localhost_url(embedding.api_base)
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

    # Derive database name from code_path (or use env override)
    database = os.getenv("WATERCOOLER_GRAPHITI_DATABASE")
    if not database:
        database = _derive_database_name(code_path)

    # Return backend's GraphitiConfig with all fields
    # For localhost endpoints without keys, pass a sentinel placeholder
    # (local servers like llama-server don't need real API keys)
    llm_api_key = llm.api_key or ("LOCAL_NO_KEY" if llm_is_local else "")
    embedding_api_key = embedding.api_key or ("LOCAL_NO_KEY" if embedding_is_local else "")

    return GraphitiConfig(
        llm_api_key=llm_api_key,
        llm_api_base=llm.api_base if llm.api_base != "https://api.openai.com/v1" else None,
        llm_model=llm.model,
        embedding_api_key=embedding_api_key,
        embedding_api_base=embedding.api_base if embedding.api_base != "https://api.openai.com/v1" else None,
        embedding_model=embedding.model,
        falkordb_host=db.host,
        falkordb_port=db.port,
        falkordb_password=db.password if db.password else None,
        reranker=reranker,
        database=database,
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
                enabled = get_memory_backend() == "leanrag"
        except ImportError:
            enabled = get_memory_backend() == "leanrag"

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

        # Derive database name consistently with Graphiti and index_leanrag.py
        # Uses the same pattern as load_graphiti_config:
        # 1. Check backend-specific env var override
        # 2. Fall back to _derive_database_name(code_path)
        database = os.getenv("WATERCOOLER_LEANRAG_DATABASE")
        if not database:
            database = _derive_database_name(code_path)

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
    try:
        from watercooler_memory.backends import GraphitiBackend  # type: ignore[attr-defined]
    except ImportError as e:
        # Add path and Python version diagnostics
        import sys
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

        # Try to get package path, but guard against watercooler_memory itself being missing
        package_path = "unknown"
        try:
            import watercooler_memory
            package_path = watercooler_memory.__file__
        except ImportError:
            # watercooler_memory itself is missing - keep original error
            pass

        error_msg = (
            f"MEMORY: Graphiti backend unavailable: {e}\n"
            f"Python version: {python_version}\n"
            f"Package loaded from: {package_path}\n"
            f"Expected source path: {Path(__file__).parent.parent.parent}/src\n"
            f"Fix: Ensure MCP server uses correct Python environment"
        )
        log_warning(error_msg)
        return {
            "error": "import_failed",
            "details": str(e),
            "package_path": package_path,
            "python_version": python_version,
        }

    # Config already has all settings resolved via unified config system
    # (FalkorDB settings are included from load_graphiti_config)
    try:
        backend = GraphitiBackend(config)
        log_debug(
            f"MEMORY: Initialized Graphiti backend "
            f"(work_dir={config.work_dir})"
        )
        return backend
    except Exception as e:
        import sys
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        error_msg = f"MEMORY: Failed to initialize Graphiti backend: {e}"
        log_warning(error_msg)
        return {
            "error": "init_failed",
            "details": str(e),
            "python_version": python_version,
            "backend_config": str(config),
        }


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
