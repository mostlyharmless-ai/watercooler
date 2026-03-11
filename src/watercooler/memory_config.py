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

import logging
import os
import warnings
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from .config_facade import config

logger = logging.getLogger(__name__)

# Sentinel values that indicate a local/test API key — skip auth headers for these.
# Used across summarizer, sync, and middleware to avoid sending bogus Authorization
# headers to local servers (llama.cpp, Ollama, etc.) that don't expect them.
AUTH_SKIP_SENTINELS = ("", "local", "LOCAL_NO_KEY")


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


def _safe_float(value: Optional[str], default: float, min_val: float, max_val: float) -> float:
    """Parse string to float with bounds validation.

    Args:
        value: String to parse (None or empty returns default)
        default: Default value if parsing fails or value is empty
        min_val: Minimum allowed value
        max_val: Maximum allowed value

    Returns:
        Parsed and bounded float value
    """
    if not value:
        return default
    try:
        parsed = float(value)
        return max(min_val, min(max_val, parsed))
    except (ValueError, TypeError):
        return default


def _safe_int(value: Optional[str], default: int, min_val: int, max_val: int) -> int:
    """Parse string to int with bounds validation.

    Args:
        value: String to parse (None or empty returns default)
        default: Default value if parsing fails or value is empty
        min_val: Minimum allowed value
        max_val: Maximum allowed value

    Returns:
        Parsed and bounded int value
    """
    if not value:
        return default
    try:
        parsed = int(value)
        return max(min_val, min(max_val, parsed))
    except (ValueError, TypeError):
        return default


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
    context_size: int
    # Prompt configuration
    system_prompt: str = ""
    prompt_prefix: str = ""
    summary_prompt: str = "Summarize this thread entry in 1-2 sentences. Be concise and factual."
    thread_summary_prompt: str = "Summarize this development thread in 2-3 sentences. Include the main topic, key decisions, and outcome if any."
    # Few-shot example
    summary_example_input: str = "Implemented OAuth2 authentication with JWT tokens. Added refresh token rotation and secure cookie storage."
    summary_example_output: str = "OAuth2 authentication implemented with JWT tokens, refresh rotation, and secure cookie storage.\ntags: #authentication #OAuth2 #JWT #security"

    def __repr__(self) -> str:
        """Return string representation with redacted API key."""
        return (
            f"ResolvedLLMConfig(api_key='{_redact_key(self.api_key)}', "
            f"api_base='{self.api_base}', model='{self.model}', "
            f"timeout={self.timeout}, max_tokens={self.max_tokens}, "
            f"context_size={self.context_size})"
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
    context_size: int
    timeout: float
    batch_size: int

    def __repr__(self) -> str:
        """Return string representation with redacted API key."""
        return (
            f"ResolvedEmbeddingConfig(api_key='{_redact_key(self.api_key)}', "
            f"api_base='{self.api_base}', model='{self.model}', dim={self.dim}, "
            f"context_size={self.context_size}, timeout={self.timeout}, "
            f"batch_size={self.batch_size})"
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


def is_memory_queue_enabled() -> bool:
    """Check if persistent memory task queue is enabled.

    Checks WATERCOOLER_MEMORY_QUEUE env var first,
    then falls back to config.memory.queue_enabled.

    Returns:
        True if memory queue is enabled
    """
    env_value = os.getenv("WATERCOOLER_MEMORY_QUEUE", "").lower()
    if env_value in ("1", "true", "yes"):
        return True
    if env_value in ("0", "false", "no"):
        return False
    return config.full().memory.queue_enabled


def get_memory_backend() -> str:
    """Get the configured default memory backend.

    Returns:
        Backend name: "graphiti", "leanrag", or "null"
    """
    return os.getenv("WATERCOOLER_MEMORY_BACKEND") or config.full().memory.backend


# =============================================================================
# Provider Detection (shared by LLM and Embedding config resolution)
# =============================================================================

# Provider domain mappings for URL-based detection
# Maps hostname suffixes to (provider_name, env_var_name)
_LLM_PROVIDER_DOMAINS: dict[str, tuple[str, str]] = {
    # OpenAI and Azure OpenAI
    "openai.com": ("openai", "OPENAI_API_KEY"),
    "openai.azure.com": ("openai", "OPENAI_API_KEY"),
    # Major cloud providers
    "anthropic.com": ("anthropic", "ANTHROPIC_API_KEY"),
    "googleapis.com": ("google", "GOOGLE_API_KEY"),
    "generativelanguage.googleapis.com": ("google", "GOOGLE_API_KEY"),
    # Inference providers
    "groq.com": ("groq", "GROQ_API_KEY"),
    "deepseek.com": ("deepseek", "DEEPSEEK_API_KEY"),
    "together.xyz": ("together", "TOGETHER_API_KEY"),
    "api.together.ai": ("together", "TOGETHER_API_KEY"),
    "mistral.ai": ("mistral", "MISTRAL_API_KEY"),
    "cohere.ai": ("cohere", "COHERE_API_KEY"),
    "cohere.com": ("cohere", "COHERE_API_KEY"),
    "perplexity.ai": ("perplexity", "PERPLEXITY_API_KEY"),
    "fireworks.ai": ("fireworks", "FIREWORKS_API_KEY"),
}

_EMBEDDING_PROVIDER_DOMAINS: dict[str, tuple[str, str]] = {
    # OpenAI and Azure OpenAI
    "openai.com": ("openai", "OPENAI_API_KEY"),
    "openai.azure.com": ("openai", "OPENAI_API_KEY"),
    # Embedding specialists
    "voyageai.com": ("voyage", "VOYAGE_API_KEY"),
    "cohere.ai": ("cohere", "COHERE_API_KEY"),
    "cohere.com": ("cohere", "COHERE_API_KEY"),
    # Cloud providers with embedding support
    "googleapis.com": ("google", "GOOGLE_API_KEY"),
    "generativelanguage.googleapis.com": ("google", "GOOGLE_API_KEY"),
    # Inference providers with embedding support
    "together.xyz": ("together", "TOGETHER_API_KEY"),
    "api.together.ai": ("together", "TOGETHER_API_KEY"),
    "mistral.ai": ("mistral", "MISTRAL_API_KEY"),
    "fireworks.ai": ("fireworks", "FIREWORKS_API_KEY"),
}


def is_anthropic_url(url: str | None) -> bool:
    """Check if URL is an Anthropic API endpoint using proper URL parsing.

    This is a shared utility for detecting Anthropic URLs across the codebase.
    Uses proper hostname parsing to avoid false positives from substring matching
    (e.g., 'not-anthropic.com.evil.net' would incorrectly match).

    Args:
        url: API base URL to check

    Returns:
        True if the URL hostname ends with 'anthropic.com'

    Example:
        >>> is_anthropic_url("https://api.anthropic.com/v1")
        True
        >>> is_anthropic_url("https://api.openai.com/v1")
        False
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return False
        hostname_lower = hostname.lower()
        return hostname_lower == "anthropic.com" or hostname_lower.endswith(".anthropic.com")
    except Exception:
        return False


def _validate_api_url(url: str | None) -> str | None:
    """Validate that a string is a valid HTTP(S) URL.

    Args:
        url: URL string to validate

    Returns:
        The URL if valid, None if invalid or empty

    Logs a warning for invalid URLs (helps users catch typos).
    """
    if not url:
        return None

    try:
        parsed = urlparse(url)
        # Must have scheme and netloc (hostname)
        if parsed.scheme not in ("http", "https"):
            logger.warning(f"Invalid URL scheme '{parsed.scheme}' in: {url}")
            return None
        if not parsed.netloc:
            logger.warning(f"Missing hostname in URL: {url}")
            return None
        return url
    except Exception as e:
        logger.warning(f"Failed to parse URL '{url}': {e}")
        return None


def _is_localhost_url(url: str) -> bool:
    """Check if URL points to a localhost endpoint.

    Localhost endpoints typically don't require API keys (e.g., llama-server,
    local embedding servers). This helper is used to skip API key validation
    for local services.

    Note: A similar function exists in watercooler_mcp.startup but lives here
    in the core lib so watercooler_mcp.memory can import it without pulling in
    the full startup module (which has heavy MCP dependencies). Docker-internal
    hostnames (e.g., host.docker.internal) are intentionally not treated as
    localhost — users in those environments should set an explicit API key or
    use env vars.

    Args:
        url: URL string to check

    Returns:
        True if the URL points to localhost, 127.0.0.1, ::1, or 0.0.0.0
    """
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")
    except Exception:
        return False


def _detect_provider_from_url(
    api_base: str | None,
    provider_domains: dict[str, tuple[str, str]],
) -> tuple[str | None, str | None]:
    """Detect provider and env var name from API base URL.

    Uses proper URL parsing to extract hostname and match against known
    provider domains. This is safer than substring matching which could
    have false positives (e.g., 'not-openai.com.evil.net').

    Args:
        api_base: The API base URL to analyze
        provider_domains: Mapping of domain suffixes to (provider, env_var)

    Returns:
        Tuple of (provider_name, env_var_name), or (None, None) if not detected
    """
    if not api_base:
        return (None, None)

    try:
        parsed = urlparse(api_base)
        hostname = parsed.hostname
        if not hostname:
            return (None, None)

        hostname_lower = hostname.lower()

        # Check each known provider domain
        for domain_suffix, (provider, env_key) in provider_domains.items():
            # Match exact domain or subdomain (e.g., 'api.openai.com' ends with 'openai.com')
            if hostname_lower == domain_suffix or hostname_lower.endswith(f".{domain_suffix}"):
                return (provider, env_key)

        # No match - log debug for non-localhost URLs (helps catch typos)
        if not hostname_lower.startswith("localhost") and not hostname_lower.startswith("127."):
            logger.debug(f"Unknown provider for URL: {api_base}")

        return (None, None)
    except Exception:
        return (None, None)


def _get_provider_api_key_impl(
    api_base: str | None,
    provider_domains: dict[str, tuple[str, str]],
) -> str | None:
    """Shared implementation for getting provider API key.

    Args:
        api_base: The resolved API base URL
        provider_domains: Mapping of domain suffixes to (provider, env_var)

    Returns:
        API key from env var or credentials.toml, or None if not found
    """
    from .credentials import get_provider_api_key as get_creds_api_key

    provider, env_key = _detect_provider_from_url(api_base, provider_domains)

    if not provider or not env_key:
        return None

    # 1. Check provider-specific env var first
    key = os.getenv(env_key)
    if key:
        return key

    # Also check GEMINI_API_KEY as fallback for Google
    if provider == "google":
        key = os.getenv("GEMINI_API_KEY")
        if key:
            return key

    # 2. Check credentials.toml
    key = get_creds_api_key(provider)
    if key:
        return key

    return None


def _get_provider_api_key(api_base: str | None) -> str | None:
    """Get provider-specific API key for LLM based on api_base URL.

    Uses proper URL parsing for secure provider detection.
    See _LLM_PROVIDER_DOMAINS for supported providers.

    Args:
        api_base: The resolved API base URL

    Returns:
        API key from env var or credentials.toml, or None if not found
    """
    return _get_provider_api_key_impl(api_base, _LLM_PROVIDER_DOMAINS)


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

    # Resolve api_key: env > provider-specific (env + credentials.toml)
    # Note: API keys belong in credentials.toml, not config.toml
    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        # Check provider-specific env vars + credentials.toml based on resolved api_base
        api_key = _get_provider_api_key(api_base)
    if not api_key:
        api_key = ""  # Empty string if not found (local servers often don't need keys)

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

    # Resolve context_size: env > shared (only used for local llama-server auto-start)
    context_size_str = os.getenv("LLM_CONTEXT_SIZE")
    if context_size_str:
        try:
            context_size = int(context_size_str)
        except ValueError:
            context_size = mem.llm.context_size
    else:
        context_size = mem.llm.context_size

    return ResolvedLLMConfig(
        api_key=api_key,
        api_base=api_base,
        model=model,
        timeout=timeout,
        max_tokens=max_tokens,
        context_size=context_size,
    )


def _get_embedding_provider_api_key(api_base: str | None) -> str | None:
    """Get provider-specific API key for embeddings based on api_base URL.

    Uses proper URL parsing for secure provider detection.
    See _EMBEDDING_PROVIDER_DOMAINS for supported providers.

    Args:
        api_base: The resolved API base URL

    Returns:
        API key from env var or credentials.toml, or None if not found
    """
    return _get_provider_api_key_impl(api_base, _EMBEDDING_PROVIDER_DOMAINS)


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

    # Resolve api_key: env > provider-specific (env + credentials.toml)
    # Note: API keys belong in credentials.toml, not config.toml
    api_key = os.getenv("EMBEDDING_API_KEY")
    if not api_key:
        # Check provider-specific env vars + credentials.toml based on resolved api_base
        api_key = _get_embedding_provider_api_key(api_base)
    if not api_key:
        api_key = ""  # Empty string if not found (local servers often don't need keys)

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

    # Resolve context_size: env > shared
    context_size_str = os.getenv("EMBEDDING_CONTEXT_SIZE")
    if context_size_str:
        try:
            context_size = int(context_size_str)
        except ValueError:
            context_size = mem.embedding.context_size
    else:
        context_size = mem.embedding.context_size

    return ResolvedEmbeddingConfig(
        api_key=api_key,
        api_base=api_base,
        model=model,
        dim=dim,
        context_size=context_size,
        timeout=timeout,
        batch_size=batch_size,
    )


def resolve_database_config() -> ResolvedDatabaseConfig:
    """Resolve database config with proper priority chain.

    Priority (highest first):
    1. Environment variables (FALKORDB_HOST, FALKORDB_PORT, FALKORDB_USERNAME, FALKORDB_PASSWORD)
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

    # Resolve username (env var takes priority)
    _env_username = os.getenv("FALKORDB_USERNAME")
    username = _env_username if _env_username is not None else mem.database.username

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


def get_graphiti_use_summary() -> bool:
    """Whether to send enriched summary to Graphiti instead of raw body.

    When enabled and a non-empty summary is available, the summary is sent
    as episode content instead of the raw entry body. Falls back to raw body
    when summary is empty (e.g., LLM unavailable during enrichment).

    Priority (highest first):
    1. WATERCOOLER_GRAPHITI_USE_SUMMARY env var
    2. TOML config: memory.graphiti.use_summary
    3. Default: False

    Returns:
        True if summary should be preferred over raw body
    """
    env = os.getenv("WATERCOOLER_GRAPHITI_USE_SUMMARY", "").lower()
    if env in ("1", "true", "yes"):
        return True
    if env in ("0", "false", "no"):
        return False
    return config.full().memory.graphiti.use_summary


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


def get_leanrag_path() -> str | None:
    """Get the path to LeanRAG installation.

    Priority (highest first):
    1. LEANRAG_PATH environment variable
    2. TOML config: memory.leanrag.path

    Returns:
        Path to LeanRAG installation directory, or None if not configured
    """
    path = os.getenv("LEANRAG_PATH") or config.full().memory.leanrag.path
    return path if path else None


@dataclass(frozen=True)
class ResolvedTierConfig:
    """Resolved tier orchestration configuration.

    All settings have env vars that override TOML values.
    """

    t1_enabled: bool
    t2_enabled: bool
    t3_enabled: bool
    max_tiers: int
    min_results: int
    min_confidence: float
    t1_limit: int
    t2_limit: int
    t3_limit: int

    def __repr__(self) -> str:
        enabled = []
        if self.t1_enabled:
            enabled.append("T1")
        if self.t2_enabled:
            enabled.append("T2")
        if self.t3_enabled:
            enabled.append("T3")
        return (
            f"ResolvedTierConfig(enabled=[{', '.join(enabled)}], "
            f"max_tiers={self.max_tiers}, min_results={self.min_results})"
        )


def _parse_bool_env(env_var: str, default: bool) -> bool:
    """Parse boolean from environment variable.

    Args:
        env_var: Environment variable name
        default: Default value if env var not set

    Returns:
        Parsed boolean value
    """
    value = os.getenv(env_var, "").lower()
    if value in ("1", "true", "yes"):
        return True
    if value in ("0", "false", "no"):
        return False
    return default


def resolve_tier_config() -> ResolvedTierConfig:
    """Resolve tier orchestration config with proper priority chain.

    Priority (highest first):
    1. Environment variables (WATERCOOLER_TIER_T*_ENABLED, etc.)
    2. TOML config (memory.tiers.*)
    3. Built-in defaults

    Special cases:
    - T2 auto-enables if memory.backend = "graphiti" and env var not explicitly set
    - T3 is opt-in by default (requires explicit enable)

    Returns:
        ResolvedTierConfig with all settings resolved
    """
    cfg = config.full()
    tiers = cfg.memory.tiers

    # Check if Graphiti is enabled (for T2 auto-enable)
    graphiti_enabled = get_memory_backend() == "graphiti"

    # T1: env > TOML > default (True)
    t1_enabled = _parse_bool_env("WATERCOOLER_TIER_T1_ENABLED", tiers.t1_enabled)

    # T2: env > TOML > auto-enable if graphiti backend
    # If env var is set, use it. Otherwise use TOML value, but also enable if graphiti is backend
    t2_env = os.getenv("WATERCOOLER_TIER_T2_ENABLED", "").lower()
    if t2_env in ("1", "true", "yes"):
        t2_enabled = True
    elif t2_env in ("0", "false", "no"):
        t2_enabled = False
    else:
        t2_enabled = tiers.t2_enabled or graphiti_enabled

    # T3: env > TOML > default (False - opt-in)
    t3_enabled = _parse_bool_env("WATERCOOLER_TIER_T3_ENABLED", tiers.t3_enabled)

    # max_tiers: env > TOML > default (2)
    max_tiers = _safe_int(
        os.getenv("WATERCOOLER_TIER_MAX_TIERS"),
        tiers.max_tiers,
        1,
        3,
    )

    # min_results: env > TOML > default (3)
    min_results = _safe_int(
        os.getenv("WATERCOOLER_TIER_MIN_RESULTS"),
        tiers.min_results,
        1,
        100,
    )

    # min_confidence: env > TOML > default (0.5)
    min_confidence = _safe_float(
        os.getenv("WATERCOOLER_TIER_MIN_CONFIDENCE"),
        tiers.min_confidence,
        0.0,
        1.0,
    )

    # Limits: TOML only (env vars would be too verbose)
    t1_limit = tiers.t1_limit
    t2_limit = tiers.t2_limit
    t3_limit = tiers.t3_limit

    return ResolvedTierConfig(
        t1_enabled=t1_enabled,
        t2_enabled=t2_enabled,
        t3_enabled=t3_enabled,
        max_tiers=max_tiers,
        min_results=min_results,
        min_confidence=min_confidence,
        t1_limit=t1_limit,
        t2_limit=t2_limit,
        t3_limit=t3_limit,
    )


# =============================================================================
# Baseline Graph Configuration
# These functions support both unified LLM_API_* vars and legacy BASELINE_GRAPH_* vars
# =============================================================================

# Default values for baseline graph (only used when no env vars or config set)
# llama-server for LLM (completion mode) on port 8000
_BASELINE_GRAPH_DEFAULT_LLM_API_BASE = "http://localhost:8000/v1"
_BASELINE_GRAPH_DEFAULT_LLM_MODEL = "qwen3:1.7b"
_BASELINE_GRAPH_DEFAULT_LLM_API_KEY = ""  # Local llama-server doesn't need a key
# llama-server for embeddings (embedding mode) on port 8080
_BASELINE_GRAPH_DEFAULT_EMBEDDING_API_BASE = "http://localhost:8080/v1"
_BASELINE_GRAPH_DEFAULT_EMBEDDING_MODEL = "bge-m3"


def resolve_baseline_graph_llm_config() -> ResolvedLLMConfig:
    """Resolve LLM config for baseline graph with proper priority chain.

    Priority (highest first):
    1. Environment variables: LLM_API_* (preferred)
    2. Environment variables: BASELINE_GRAPH_* (legacy, for backward compatibility)
    3. TOML settings: [memory.llm]
    4. Built-in defaults (localhost:8000 for llama-server)

    Returns:
        ResolvedLLMConfig with all settings resolved
    """
    cfg = config.full()
    mem = cfg.memory

    # Resolve api_key: LLM_API_KEY > BASELINE_GRAPH_API_KEY > provider credentials > default
    # Note: We no longer fall back to mem.llm.api_key - secrets belong in credentials.toml
    api_key = os.getenv("LLM_API_KEY") or os.getenv("BASELINE_GRAPH_API_KEY")
    if not api_key:
        # Check provider-specific credentials based on api_base (resolved below)
        # We need to resolve api_base first to know which provider to check
        resolved_api_base = (
            os.getenv("LLM_API_BASE")
            or os.getenv("BASELINE_GRAPH_API_BASE")
            or mem.llm.api_base
            or _BASELINE_GRAPH_DEFAULT_LLM_API_BASE
        )
        api_key = _get_provider_api_key(resolved_api_base) or _BASELINE_GRAPH_DEFAULT_LLM_API_KEY

    # Resolve api_base: LLM_API_BASE > BASELINE_GRAPH_API_BASE > TOML > default
    # Note: TOML values are respected as-is, including external APIs like OpenAI.
    # The previous check that rejected OpenAI values was incorrect - it conflated
    # "pydantic schema default" with "user didn't set anything".
    # See thread: debug-model-service-options for analysis.
    api_base = (
        os.getenv("LLM_API_BASE")
        or os.getenv("BASELINE_GRAPH_API_BASE")
        or mem.llm.api_base
        or _BASELINE_GRAPH_DEFAULT_LLM_API_BASE
    )

    # Resolve model: LLM_MODEL > BASELINE_GRAPH_MODEL > TOML > default
    model = (
        os.getenv("LLM_MODEL")
        or os.getenv("BASELINE_GRAPH_MODEL")
        or mem.llm.model
        or _BASELINE_GRAPH_DEFAULT_LLM_MODEL
    )

    # Resolve timeout and max_tokens from env/TOML (use shared defaults)
    # Bounds: timeout 1-600s, max_tokens 1-32768
    timeout = _safe_float(os.getenv("LLM_TIMEOUT"), mem.llm.timeout, 1.0, 600.0)
    max_tokens = _safe_int(os.getenv("LLM_MAX_TOKENS"), mem.llm.max_tokens, 1, 32768)

    # Resolve context_size: env > TOML (only used for local llama-server auto-start)
    # Bounds: 512-131072 tokens
    context_size = _safe_int(os.getenv("LLM_CONTEXT_SIZE"), mem.llm.context_size, 512, 131072)

    # Resolve prompt configuration from env/TOML
    system_prompt = os.getenv("LLM_SYSTEM_PROMPT") or mem.llm.system_prompt
    prompt_prefix = os.getenv("LLM_PROMPT_PREFIX") or mem.llm.prompt_prefix

    # Resolve summary prompts from env/TOML
    summary_prompt = os.getenv("LLM_SUMMARY_PROMPT") or mem.llm.summary_prompt
    thread_summary_prompt = os.getenv("LLM_THREAD_SUMMARY_PROMPT") or mem.llm.thread_summary_prompt

    # Resolve few-shot example from TOML (no env override for these)
    summary_example_input = mem.llm.summary_example_input
    summary_example_output = mem.llm.summary_example_output

    return ResolvedLLMConfig(
        api_key=api_key,
        api_base=api_base,
        model=model,
        timeout=timeout,
        max_tokens=max_tokens,
        context_size=context_size,
        system_prompt=system_prompt,
        prompt_prefix=prompt_prefix,
        summary_prompt=summary_prompt,
        thread_summary_prompt=thread_summary_prompt,
        summary_example_input=summary_example_input,
        summary_example_output=summary_example_output,
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

    # Resolve api_key: EMBEDDING_API_KEY > BASELINE_GRAPH_EMBEDDING_API_KEY > provider credentials > empty
    # Note: We no longer fall back to mem.embedding.api_key - secrets belong in credentials.toml
    api_key = os.getenv("EMBEDDING_API_KEY") or os.getenv("BASELINE_GRAPH_EMBEDDING_API_KEY")
    if not api_key:
        # Check provider-specific credentials based on api_base (resolved below)
        resolved_api_base = (
            os.getenv("EMBEDDING_API_BASE")
            or os.getenv("BASELINE_GRAPH_EMBEDDING_API_BASE")
            or mem.embedding.api_base
            or _BASELINE_GRAPH_DEFAULT_EMBEDDING_API_BASE
        )
        api_key = _get_embedding_provider_api_key(resolved_api_base) or ""  # Embedding servers often don't need keys

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

    # Resolve dim, timeout, batch_size, context_size with bounds validation
    # Bounds: dim 64-8192, timeout 1-300s, batch_size 1-256, context_size 128-32768
    dim = _safe_int(os.getenv("EMBEDDING_DIM"), mem.embedding.dim, 64, 8192)
    timeout = _safe_float(os.getenv("EMBEDDING_TIMEOUT"), mem.embedding.timeout, 1.0, 300.0)
    batch_size = _safe_int(os.getenv("EMBEDDING_BATCH_SIZE"), mem.embedding.batch_size, 1, 256)
    context_size = _safe_int(os.getenv("EMBEDDING_CONTEXT_SIZE"), mem.embedding.context_size, 128, 32768)

    return ResolvedEmbeddingConfig(
        api_key=api_key,
        api_base=api_base,
        model=model,
        dim=dim,
        context_size=context_size,
        timeout=timeout,
        batch_size=batch_size,
    )
