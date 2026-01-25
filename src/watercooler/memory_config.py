"""Memory backend configuration resolution.

Resolves configuration for memory backends with proper priority:
1. Environment variables (highest)
2. Backend-specific TOML overrides
3. Shared TOML settings
4. Built-in defaults (lowest)

This module provides a unified way to access memory configuration
regardless of whether it's set via environment variables or TOML files.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from typing import Optional

from .config_facade import config


def _redact_key(key: str) -> str:
    """Redact API key for safe logging/repr.

    Shows first 8 chars followed by '...' for keys longer than 8 chars,
    otherwise shows '***' for short keys.
    """
    if not key:
        return "<not set>"
    if len(key) > 8:
        return key[:8] + "..."
    return "***"


@dataclass(frozen=True)
class ResolvedLLMConfig:
    """Resolved LLM configuration after applying all overrides.

    Note: __repr__ redacts api_key for safe logging.
    """

    api_key: str
    api_base: str
    model: str
    timeout: float
    max_tokens: int

    def __repr__(self) -> str:
        """Return string representation with redacted API key."""
        return (
            f"ResolvedLLMConfig(api_key='{_redact_key(self.api_key)}', "
            f"api_base='{self.api_base}', model='{self.model}', "
            f"timeout={self.timeout}, max_tokens={self.max_tokens})"
        )


@dataclass(frozen=True)
class ResolvedEmbeddingConfig:
    """Resolved embedding configuration after applying all overrides.

    Note: __repr__ redacts api_key for safe logging.
    """

    api_key: str
    api_base: str
    model: str
    dim: int
    timeout: float
    batch_size: int

    def __repr__(self) -> str:
        """Return string representation with redacted API key."""
        return (
            f"ResolvedEmbeddingConfig(api_key='{_redact_key(self.api_key)}', "
            f"api_base='{self.api_base}', model='{self.model}', dim={self.dim}, "
            f"timeout={self.timeout}, batch_size={self.batch_size})"
        )


@dataclass(frozen=True)
class ResolvedDatabaseConfig:
    """Resolved database configuration after applying all overrides.

    Note: __repr__ redacts password for safe logging.
    """

    host: str
    port: int
    username: str
    password: str

    def __repr__(self) -> str:
        """Return string representation with redacted password."""
        return (
            f"ResolvedDatabaseConfig(host='{self.host}', port={self.port}, "
            f"username='{self.username}', password='{_redact_key(self.password)}')"
        )


def is_memory_enabled() -> bool:
    """Check if memory backends are enabled globally.

    Checks WATERCOOLER_MEMORY_DISABLED env var first (inverted logic),
    then falls back to config.memory.enabled.

    Returns:
        True if memory backends are enabled
    """
    disabled_value = os.getenv("WATERCOOLER_MEMORY_DISABLED", "").lower()
    if disabled_value in ("1", "true", "yes"):
        return False
    return config.full().memory.enabled


def get_memory_backend() -> str:
    """Get the configured default memory backend.

    Returns:
        Backend name: "graphiti", "leanrag", or "null"
    """
    return os.getenv("WATERCOOLER_MEMORY_BACKEND") or config.full().memory.backend


def _get_provider_api_key(api_base: str) -> str | None:
    """Get provider-specific API key based on api_base URL.

    Checks standard provider environment variables based on the API endpoint.
    This allows users to use their existing OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.

    Args:
        api_base: The resolved API base URL

    Returns:
        API key from provider-specific env var, or None if not found
    """
    api_base_lower = api_base.lower() if api_base else ""

    # Map provider domains to their standard env vars
    if "openai.com" in api_base_lower or "openai.azure.com" in api_base_lower:
        return os.getenv("OPENAI_API_KEY")
    if "anthropic.com" in api_base_lower:
        return os.getenv("ANTHROPIC_API_KEY")
    if "googleapis.com" in api_base_lower or "generativelanguage" in api_base_lower:
        return os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if "groq.com" in api_base_lower:
        return os.getenv("GROQ_API_KEY")

    return None


def resolve_llm_config(backend: str = "graphiti") -> ResolvedLLMConfig:
    """Resolve LLM config with proper priority chain.

    Priority (highest first):
    1. Environment variables (LLM_API_KEY, LLM_API_BASE, LLM_MODEL)
    2. Backend-specific TOML overrides (memory.{backend}.llm_*)
    3. Provider-specific env vars (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.)
    4. Shared TOML settings (memory.llm.*)
    5. Built-in defaults

    Args:
        backend: Backend name for backend-specific overrides ("graphiti" or "leanrag")

    Returns:
        ResolvedLLMConfig with all settings resolved
    """
    cfg = config.full()
    mem = cfg.memory

    # Get backend-specific config if available
    backend_cfg = getattr(mem, backend, None)

    # Resolve api_base FIRST (needed for provider-specific API key lookup)
    api_base = os.getenv("LLM_API_BASE")
    if not api_base and backend_cfg:
        api_base = backend_cfg.llm_api_base or None
    if not api_base:
        api_base = mem.llm.api_base

    # Resolve api_key: env > backend-specific > provider-specific > shared
    api_key = os.getenv("LLM_API_KEY")
    if not api_key and backend_cfg:
        api_key = backend_cfg.llm_api_key or None
    if not api_key:
        # Check provider-specific env vars based on resolved api_base
        api_key = _get_provider_api_key(api_base)
    if not api_key:
        api_key = mem.llm.api_key

    # Resolve model: env > backend-specific > shared
    model = os.getenv("LLM_MODEL")
    if not model and backend_cfg:
        model = backend_cfg.llm_model or None
    if not model:
        model = mem.llm.model

    # Resolve timeout: env > shared
    timeout_str = os.getenv("LLM_TIMEOUT")
    if timeout_str:
        try:
            timeout = float(timeout_str)
        except ValueError:
            timeout = mem.llm.timeout
    else:
        timeout = mem.llm.timeout

    # Resolve max_tokens: env > shared
    max_tokens_str = os.getenv("LLM_MAX_TOKENS")
    if max_tokens_str:
        try:
            max_tokens = int(max_tokens_str)
        except ValueError:
            max_tokens = mem.llm.max_tokens
    else:
        max_tokens = mem.llm.max_tokens

    return ResolvedLLMConfig(
        api_key=api_key,
        api_base=api_base,
        model=model,
        timeout=timeout,
        max_tokens=max_tokens,
    )


def _get_embedding_provider_api_key(api_base: str) -> str | None:
    """Get provider-specific API key for embeddings based on api_base URL.

    Checks standard provider environment variables based on the API endpoint.
    This allows users to use their existing OPENAI_API_KEY, VOYAGE_API_KEY, etc.

    Args:
        api_base: The resolved API base URL

    Returns:
        API key from provider-specific env var, or None if not found
    """
    api_base_lower = api_base.lower() if api_base else ""

    # Map provider domains to their standard env vars
    if "openai.com" in api_base_lower or "openai.azure.com" in api_base_lower:
        return os.getenv("OPENAI_API_KEY")
    if "voyageai.com" in api_base_lower:
        return os.getenv("VOYAGE_API_KEY")
    if "googleapis.com" in api_base_lower or "generativelanguage" in api_base_lower:
        return os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")

    return None


def resolve_embedding_config(backend: str = "graphiti") -> ResolvedEmbeddingConfig:
    """Resolve embedding config with proper priority chain.

    Priority (highest first):
    1. Environment variables (EMBEDDING_API_KEY, EMBEDDING_API_BASE, EMBEDDING_MODEL, EMBEDDING_DIM)
    2. Backend-specific TOML overrides (memory.{backend}.embedding_*)
    3. Provider-specific env vars (OPENAI_API_KEY, VOYAGE_API_KEY, etc.)
    4. Shared TOML settings (memory.embedding.*)
    5. Built-in defaults

    Args:
        backend: Backend name for backend-specific overrides ("graphiti" or "leanrag")

    Returns:
        ResolvedEmbeddingConfig with all settings resolved
    """
    cfg = config.full()
    mem = cfg.memory

    # Get backend-specific config if available
    backend_cfg = getattr(mem, backend, None)

    # Resolve api_base FIRST (needed for provider-specific API key lookup)
    api_base = os.getenv("EMBEDDING_API_BASE")
    if not api_base and backend_cfg:
        api_base = backend_cfg.embedding_api_base or None
    if not api_base:
        api_base = mem.embedding.api_base

    # Resolve api_key: env > backend-specific > provider-specific > shared
    api_key = os.getenv("EMBEDDING_API_KEY")
    if not api_key and backend_cfg:
        api_key = backend_cfg.embedding_api_key or None
    if not api_key:
        # Check provider-specific env vars based on resolved api_base
        api_key = _get_embedding_provider_api_key(api_base)
    if not api_key:
        api_key = mem.embedding.api_key

    # Resolve model: env > backend-specific > shared
    model = os.getenv("EMBEDDING_MODEL")
    if not model and backend_cfg:
        model = backend_cfg.embedding_model or None
    if not model:
        model = mem.embedding.model

    # Resolve dim: env > shared (no backend-specific override)
    dim_str = os.getenv("EMBEDDING_DIM")
    if dim_str:
        try:
            dim = int(dim_str)
        except ValueError:
            dim = mem.embedding.dim
    else:
        dim = mem.embedding.dim

    # Resolve timeout: env > shared
    timeout_str = os.getenv("EMBEDDING_TIMEOUT")
    if timeout_str:
        try:
            timeout = float(timeout_str)
        except ValueError:
            timeout = mem.embedding.timeout
    else:
        timeout = mem.embedding.timeout

    # Resolve batch_size: env > shared
    batch_size_str = os.getenv("EMBEDDING_BATCH_SIZE")
    if batch_size_str:
        try:
            batch_size = int(batch_size_str)
        except ValueError:
            batch_size = mem.embedding.batch_size
    else:
        batch_size = mem.embedding.batch_size

    return ResolvedEmbeddingConfig(
        api_key=api_key,
        api_base=api_base,
        model=model,
        dim=dim,
        timeout=timeout,
        batch_size=batch_size,
    )


def resolve_database_config() -> ResolvedDatabaseConfig:
    """Resolve database config with proper priority chain.

    Priority (highest first):
    1. Environment variables (FALKORDB_HOST, FALKORDB_PORT, FALKORDB_PASSWORD)
    2. Shared TOML settings (memory.database.*)
    3. Built-in defaults

    Returns:
        ResolvedDatabaseConfig with all settings resolved
    """
    cfg = config.full()
    mem = cfg.memory

    # Resolve host
    host = os.getenv("FALKORDB_HOST") or mem.database.host

    # Resolve port
    port_str = os.getenv("FALKORDB_PORT")
    if port_str:
        try:
            port = int(port_str)
        except ValueError:
            port = mem.database.port
    else:
        port = mem.database.port

    # Resolve username (no env var, TOML only)
    username = mem.database.username

    # Resolve password
    password = os.getenv("FALKORDB_PASSWORD") or mem.database.password

    return ResolvedDatabaseConfig(host=host, port=port, username=username, password=password)


def get_graphiti_reranker() -> str:
    """Get the configured Graphiti reranker algorithm.

    Returns:
        Reranker name: "rrf", "mmr", "cross_encoder", "node_distance", or "episode_mentions"
    """
    return os.getenv("WATERCOOLER_GRAPHITI_RERANKER") or config.full().memory.graphiti.reranker


def get_graphiti_track_entry_episodes() -> bool:
    """Get whether to track entry-episode mappings for Graphiti.

    Returns:
        True if entry-episode tracking is enabled
    """
    return config.full().memory.graphiti.track_entry_episodes


def get_graphiti_chunk_on_sync() -> bool:
    """Get whether to chunk entries when syncing to Graphiti.

    Checks WATERCOOLER_GRAPHITI_CHUNK_ON_SYNC env var first,
    then falls back to config.memory.graphiti.chunk_on_sync.

    Returns:
        True if chunking is enabled during sync
    """
    env_value = os.getenv("WATERCOOLER_GRAPHITI_CHUNK_ON_SYNC", "").lower()
    if env_value in ("1", "true", "yes"):
        return True
    if env_value in ("0", "false", "no"):
        return False
    return config.full().memory.graphiti.chunk_on_sync


def get_graphiti_chunk_config() -> tuple[int, int]:
    """Get chunking configuration for Graphiti sync.

    Checks environment variables first:
    - WATERCOOLER_GRAPHITI_CHUNK_MAX_TOKENS
    - WATERCOOLER_GRAPHITI_CHUNK_OVERLAP

    Then falls back to TOML config values.

    Returns:
        Tuple of (max_tokens, overlap)
    """
    cfg = config.full().memory.graphiti

    # Resolve max_tokens
    max_tokens_str = os.getenv("WATERCOOLER_GRAPHITI_CHUNK_MAX_TOKENS")
    if max_tokens_str:
        try:
            max_tokens = int(max_tokens_str)
            # Apply validation bounds
            max_tokens = max(100, min(4096, max_tokens))
        except ValueError:
            max_tokens = cfg.chunk_max_tokens
    else:
        max_tokens = cfg.chunk_max_tokens

    # Resolve overlap
    overlap_str = os.getenv("WATERCOOLER_GRAPHITI_CHUNK_OVERLAP")
    if overlap_str:
        try:
            overlap = int(overlap_str)
            # Apply validation bounds
            overlap = max(0, min(256, overlap))
        except ValueError:
            overlap = cfg.chunk_overlap
    else:
        overlap = cfg.chunk_overlap

    return (max_tokens, overlap)


def get_leanrag_max_workers() -> int:
    """Get the max workers setting for LeanRAG graph building.

    Returns:
        Max workers count
    """
    return config.full().memory.leanrag.max_workers


# =============================================================================
# Baseline Graph Configuration
# These functions support both unified LLM_API_* vars and legacy BASELINE_GRAPH_* vars
# =============================================================================

# Default values for baseline graph (only used when no env vars or config set)
_BASELINE_GRAPH_DEFAULT_LLM_API_BASE = "http://localhost:11434/v1"
_BASELINE_GRAPH_DEFAULT_LLM_MODEL = "llama3.2:3b"
_BASELINE_GRAPH_DEFAULT_LLM_API_KEY = "ollama"
_BASELINE_GRAPH_DEFAULT_EMBEDDING_API_BASE = "http://localhost:8080/v1"
_BASELINE_GRAPH_DEFAULT_EMBEDDING_MODEL = "bge-m3"


def resolve_baseline_graph_llm_config() -> ResolvedLLMConfig:
    """Resolve LLM config for baseline graph with proper priority chain.

    Priority (highest first):
    1. Environment variables: LLM_API_* (preferred)
    2. Environment variables: BASELINE_GRAPH_* (legacy, for backward compatibility)
    3. TOML settings: [memory.llm]
    4. Built-in defaults (localhost:11434 for Ollama)

    Returns:
        ResolvedLLMConfig with all settings resolved
    """
    cfg = config.full()
    mem = cfg.memory

    # Resolve api_key: LLM_API_KEY > BASELINE_GRAPH_API_KEY > TOML > default
    api_key = (
        os.getenv("LLM_API_KEY")
        or os.getenv("BASELINE_GRAPH_API_KEY")
        or mem.llm.api_key
        or _BASELINE_GRAPH_DEFAULT_LLM_API_KEY
    )

    # Resolve api_base: LLM_API_BASE > BASELINE_GRAPH_API_BASE > TOML > default
    api_base = (
        os.getenv("LLM_API_BASE")
        or os.getenv("BASELINE_GRAPH_API_BASE")
        or (mem.llm.api_base if mem.llm.api_base != "https://api.openai.com/v1" else None)
        or _BASELINE_GRAPH_DEFAULT_LLM_API_BASE
    )

    # Resolve model: LLM_MODEL > BASELINE_GRAPH_MODEL > TOML > default
    model = (
        os.getenv("LLM_MODEL")
        or os.getenv("BASELINE_GRAPH_MODEL")
        or (mem.llm.model if mem.llm.model != "gpt-4o-mini" else None)
        or _BASELINE_GRAPH_DEFAULT_LLM_MODEL
    )

    # Resolve timeout and max_tokens from env/TOML (use shared defaults)
    timeout_str = os.getenv("LLM_TIMEOUT")
    timeout = float(timeout_str) if timeout_str else mem.llm.timeout

    max_tokens_str = os.getenv("LLM_MAX_TOKENS")
    max_tokens = int(max_tokens_str) if max_tokens_str else mem.llm.max_tokens

    return ResolvedLLMConfig(
        api_key=api_key,
        api_base=api_base,
        model=model,
        timeout=timeout,
        max_tokens=max_tokens,
    )


def resolve_baseline_graph_embedding_config() -> ResolvedEmbeddingConfig:
    """Resolve embedding config for baseline graph with proper priority chain.

    Priority (highest first):
    1. Environment variables: EMBEDDING_API_* (preferred)
    2. Environment variables: BASELINE_GRAPH_EMBEDDING_* (legacy)
    3. TOML settings: [memory.embedding]
    4. Built-in defaults (localhost:8080 for llama.cpp)

    Returns:
        ResolvedEmbeddingConfig with all settings resolved
    """
    cfg = config.full()
    mem = cfg.memory

    # Resolve api_key: EMBEDDING_API_KEY > BASELINE_GRAPH_EMBEDDING_API_KEY > TOML > empty
    api_key = (
        os.getenv("EMBEDDING_API_KEY")
        or os.getenv("BASELINE_GRAPH_EMBEDDING_API_KEY")
        or mem.embedding.api_key
        or ""  # Embedding servers often don't need keys
    )

    # Resolve api_base: EMBEDDING_API_BASE > BASELINE_GRAPH_EMBEDDING_API_BASE > TOML > default
    api_base = (
        os.getenv("EMBEDDING_API_BASE")
        or os.getenv("BASELINE_GRAPH_EMBEDDING_API_BASE")
        or mem.embedding.api_base
        or _BASELINE_GRAPH_DEFAULT_EMBEDDING_API_BASE
    )

    # Resolve model: EMBEDDING_MODEL > BASELINE_GRAPH_EMBEDDING_MODEL > TOML > default
    model = (
        os.getenv("EMBEDDING_MODEL")
        or os.getenv("BASELINE_GRAPH_EMBEDDING_MODEL")
        or mem.embedding.model
        or _BASELINE_GRAPH_DEFAULT_EMBEDDING_MODEL
    )

    # Resolve dim
    dim_str = os.getenv("EMBEDDING_DIM")
    if dim_str:
        try:
            dim = int(dim_str)
        except ValueError:
            dim = mem.embedding.dim
    else:
        dim = mem.embedding.dim

    # Resolve timeout and batch_size from env/TOML
    timeout_str = os.getenv("EMBEDDING_TIMEOUT")
    timeout = float(timeout_str) if timeout_str else mem.embedding.timeout

    batch_size_str = os.getenv("EMBEDDING_BATCH_SIZE")
    batch_size = int(batch_size_str) if batch_size_str else mem.embedding.batch_size

    return ResolvedEmbeddingConfig(
        api_key=api_key,
        api_base=api_base,
        model=model,
        dim=dim,
        timeout=timeout,
        batch_size=batch_size,
    )
