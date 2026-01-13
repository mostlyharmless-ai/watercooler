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
    resolve_llm_config,
    resolve_embedding_config,
    resolve_database_config,
    get_graphiti_reranker,
)

# Import backend's GraphitiConfig directly (consolidates duplicate configs)
try:
    from watercooler_memory.backends.graphiti import GraphitiConfig
except ImportError:
    # If backend not installed, define minimal config for type hints
    from dataclasses import dataclass
    @dataclass
    class GraphitiConfig:  # type: ignore
        """Minimal config stub when backend unavailable."""
        openai_api_key: str
        reranker: str = "rrf"


def load_graphiti_config() -> Optional[GraphitiConfig]:
    """Load Graphiti configuration from unified config system.

    Uses the new unified configuration with priority chain:
    1. Environment variables (highest)
    2. Backend-specific TOML overrides (memory.graphiti.*)
    3. Shared TOML settings (memory.llm.*, memory.embedding.*)
    4. Built-in defaults (lowest)

    Returns None if Graphiti is disabled or configuration is invalid.
    Logs warnings for configuration issues.

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
            WATERCOOLER_GRAPHITI_ENABLED: "1" to enable (default: "0")
            LLM_API_KEY, LLM_API_BASE, LLM_MODEL
            EMBEDDING_API_KEY, EMBEDDING_API_BASE, EMBEDDING_MODEL
            WATERCOOLER_GRAPHITI_RERANKER

        Deprecated Fallback:
            OPENAI_API_KEY: Falls back to this if LLM_API_KEY or EMBEDDING_API_KEY
                is not set. A warning is logged when the fallback is used.

    Returns:
        GraphitiConfig instance or None if disabled/invalid

    Example:
        >>> config = load_graphiti_config()
        >>> if config:
        ...     backend = get_graphiti_backend(config)
    """
    # Check global memory disable switch first
    if not is_memory_enabled():
        log_debug("MEMORY: All memory backends disabled (WATERCOOLER_MEMORY_DISABLED=1)")
        return None

    # Check Graphiti-specific switch (default: disabled)
    enabled = os.getenv("WATERCOOLER_GRAPHITI_ENABLED", "0") == "1"
    if not enabled:
        log_debug("MEMORY: Graphiti disabled (WATERCOOLER_GRAPHITI_ENABLED != '1')")
        return None

    # Resolve LLM configuration using unified config
    llm = resolve_llm_config("graphiti")
    if not llm.api_key:
        log_warning(
            "MEMORY: Graphiti enabled but LLM_API_KEY not set. "
            "Memory queries will fail. Set LLM_API_KEY or configure [memory.llm].api_key in config.toml."
        )
        return None

    # Resolve embedding configuration using unified config
    embedding = resolve_embedding_config("graphiti")
    if not embedding.api_key:
        log_warning(
            "MEMORY: Graphiti enabled but EMBEDDING_API_KEY not set. "
            "Memory queries will fail. Set EMBEDDING_API_KEY or configure [memory.embedding].api_key in config.toml."
        )
        return None

    # Resolve database configuration
    db = resolve_database_config()

    # Get reranker algorithm
    reranker = get_graphiti_reranker()

    # Return backend's GraphitiConfig with all fields
    return GraphitiConfig(
        llm_api_key=llm.api_key,
        llm_api_base=llm.api_base if llm.api_base != "https://api.openai.com/v1" else None,
        llm_model=llm.model,
        embedding_api_key=embedding.api_key,
        embedding_api_base=embedding.api_base if embedding.api_base != "https://api.openai.com/v1" else None,
        embedding_model=embedding.model,
        falkordb_host=db.host,
        falkordb_port=db.port,
        falkordb_password=db.password if db.password else None,
        reranker=reranker,
    )


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

    Args:
        backend: GraphitiBackend instance
        query_text: Search query string
        limit: Maximum results to return (1-50)
        topic: Optional thread topic to filter by (will be converted to group_id)
              If None, searches across ALL indexed threads.

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


def validate_memory_prerequisites(operation: str) -> tuple[Any, Optional[ToolResult]]:
    """Validate memory module, config, and backend prerequisites.

    Centralizes common validation logic for all memory tools:
    1. Load Graphiti configuration
    2. Initialize backend

    Args:
        operation: Tool name for error messages (e.g., "search_nodes")

    Returns:
        Tuple of (backend, error_response):
        - (backend, None) if successful
        - (None, error_response) if validation fails

    Example:
        >>> backend, error = validate_memory_prerequisites("search_nodes")
        >>> if error:
        ...     return error
        >>> # Use backend...
    """
    # Step 1: Load configuration
    config = load_graphiti_config()
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
