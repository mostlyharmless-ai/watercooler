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
    """Load Graphiti configuration from environment variables.

    Returns None if Graphiti is disabled or configuration is invalid.
    Logs warnings for configuration issues.

    Environment Variables:
        WATERCOOLER_MEMORY_DISABLED: "1" to disable all memory backends
        WATERCOOLER_GRAPHITI_ENABLED: "1" to enable (default: "0")
        OPENAI_API_KEY: OpenAI API key (required if enabled)
        WATERCOOLER_GRAPHITI_RERANKER: Reranker algorithm (default: "rrf")
            Options: rrf, mmr, cross_encoder, node_distance, episode_mentions

    Returns:
        GraphitiConfig instance or None if disabled/invalid

    Example:
        >>> config = load_graphiti_config()
        >>> if config:
        ...     backend = get_graphiti_backend(config)
    """
    # Check global memory disable switch first
    if os.getenv("WATERCOOLER_MEMORY_DISABLED", "").lower() in ("1", "true", "yes"):
        log_debug("MEMORY: All memory backends disabled (WATERCOOLER_MEMORY_DISABLED=1)")
        return None

    # Check Graphiti-specific switch (default: disabled)
    enabled = os.getenv("WATERCOOLER_GRAPHITI_ENABLED", "0") == "1"
    if not enabled:
        log_debug("MEMORY: Graphiti disabled (WATERCOOLER_GRAPHITI_ENABLED != '1')")
        return None

    # OpenAI API key is required
    openai_api_key = os.getenv("OPENAI_API_KEY", "")
    if not openai_api_key:
        log_warning(
            "MEMORY: Graphiti enabled but OPENAI_API_KEY not set. "
            "Memory queries will fail."
        )
        return None

    # Get reranker algorithm (default: rrf for speed)
    reranker = os.getenv("WATERCOOLER_GRAPHITI_RERANKER", "rrf").lower()

    # Return backend's GraphitiConfig with defaults
    # Only specify required/overridden fields - backend provides the rest
    return GraphitiConfig(
        openai_api_key=openai_api_key,
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

    # Config is already the backend's GraphitiConfig, just use it directly
    # (with optional FalkorDB environment overrides)
    backend_config = config
    if os.getenv("FALKORDB_HOST") or os.getenv("FALKORDB_PORT") or os.getenv("FALKORDB_PASSWORD"):
        # Override FalkorDB settings from environment if specified
        from dataclasses import replace
        backend_config = replace(
            config,
            falkordb_host=os.getenv("FALKORDB_HOST", config.falkordb_host),
            falkordb_port=int(os.getenv("FALKORDB_PORT", str(config.falkordb_port))),
            falkordb_password=os.getenv("FALKORDB_PASSWORD") or config.falkordb_password,
        )

    try:
        backend = GraphitiBackend(backend_config)
        log_debug(
            f"MEMORY: Initialized Graphiti backend "
            f"(work_dir={backend_config.work_dir})"
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
            "backend_config": str(backend_config),
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
