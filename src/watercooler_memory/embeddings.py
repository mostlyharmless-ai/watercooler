"""Embedding generation for memory graph nodes.

Generates vector embeddings using bge-m3 via OpenAI-compatible API.
Runs on port 8080 by default (separate from summarization on port 8000).
Supports batch processing and retry logic for reliability.

Embeddings are cached to disk to survive pipeline failures and avoid
re-generating expensive API calls.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from .cache import EmbeddingCache
from ._utils import (
    _ensure_httpx,
    _http_post_with_retry,
    _HTTPX_AVAILABLE,
    _resolve_embedding_field,
)


# Default configuration resolved via unified config
# Standard env vars (highest priority):
#   EMBEDDING_API_BASE - Embedding service endpoint
#   EMBEDDING_MODEL - Model name
#   EMBEDDING_DIM - Vector dimension
#
# Resolution is done via watercooler.memory_config which checks:
#   1. Environment variables
#   2. TOML config
#   3. Built-in defaults (localhost:8080 for llama.cpp)
DEFAULT_BATCH_SIZE = 32
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 3

# Backward-compatible constants for tests and external consumers
# These resolve to the built-in defaults (not env-aware at import time)
DEFAULT_API_BASE = "http://localhost:8080/v1"
DEFAULT_MODEL = "bge-m3"
DEFAULT_DIM = 1024


def _get_default_api_base() -> str:
    """Get default embedding API base from unified config."""
    return _resolve_embedding_field("api_base", "EMBEDDING_API_BASE", "http://localhost:8080/v1")


def _get_default_model() -> str:
    """Get default embedding model from unified config."""
    return _resolve_embedding_field("model", "EMBEDDING_MODEL", "bge-m3")


def _get_default_dim() -> int:
    """Get default embedding dimension from unified config."""
    return _resolve_embedding_field("dim", "EMBEDDING_DIM", 1024)


def _get_default_timeout() -> float:
    """Get default embedding timeout from unified config."""
    return _resolve_embedding_field("timeout", "EMBEDDING_TIMEOUT", DEFAULT_TIMEOUT)


def _get_default_batch_size() -> int:
    """Get default embedding batch_size from unified config."""
    return _resolve_embedding_field("batch_size", "EMBEDDING_BATCH_SIZE", DEFAULT_BATCH_SIZE)


@dataclass
class EmbeddingConfig:
    """Configuration for embedding generation.

    Settings are resolved via unified config with priority:
    1. Environment variables (EMBEDDING_API_BASE, EMBEDDING_MODEL, etc.)
    2. TOML config
    3. Built-in defaults
    """

    api_base: str = field(default_factory=_get_default_api_base)
    model: str = field(default_factory=_get_default_model)
    batch_size: int = DEFAULT_BATCH_SIZE
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    api_key: Optional[str] = None

    def __post_init__(self) -> None:
        """Validate config values after initialization."""
        if self.batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {self.batch_size}")
        if self.timeout <= 0:
            raise ValueError(f"timeout must be positive, got {self.timeout}")
        if self.max_retries < 1:
            raise ValueError(f"max_retries must be >= 1, got {self.max_retries}")

    @classmethod
    def from_env(cls) -> EmbeddingConfig:
        """Create config from unified config system.

        Priority: Environment variables > TOML config > Built-in defaults

        Uses watercooler.memory_config for resolution when available.
        """
        # Use unified config for API base and model
        api_base = _get_default_api_base()
        model = _get_default_model()

        # Get API key from unified config or env
        api_key = None
        try:
            from watercooler.memory_config import resolve_embedding_config
            api_key = resolve_embedding_config().api_key or None
        except ImportError:
            api_key = os.environ.get("EMBEDDING_API_KEY")

        return cls(
            api_base=api_base,
            model=model,
            batch_size=_get_default_batch_size(),
            timeout=_get_default_timeout(),
            max_retries=int(os.environ.get("EMBEDDING_MAX_RETRIES", DEFAULT_MAX_RETRIES)),
            api_key=api_key,
        )


class EmbeddingError(Exception):
    """Error during embedding generation."""

    pass


def embed_texts(
    texts: list[str],
    config: Optional[EmbeddingConfig] = None,
    use_cache: bool = True,
) -> list[list[float]]:
    """Generate embeddings for a list of texts.

    Embeddings are cached to disk to survive pipeline failures.

    Args:
        texts: List of texts to embed.
        config: Embedding configuration.
        use_cache: Whether to use disk cache.

    Returns:
        List of embedding vectors (same order as input texts).

    Raises:
        EmbeddingError: If embedding generation fails.
        ImportError: If httpx is not available.
    """
    _ensure_httpx()

    if not texts:
        return []

    if config is None:
        config = EmbeddingConfig.from_env()

    cache = EmbeddingCache() if use_cache else None

    # Check cache first for all texts
    if cache:
        cached_results, missing_indices = cache.get_batch(texts)
    else:
        cached_results = [None] * len(texts)
        missing_indices = list(range(len(texts)))

    # If everything is cached, return immediately
    if not missing_indices:
        return [r for r in cached_results if r is not None]

    # Get texts that need embedding
    texts_to_embed = [texts[i] for i in missing_indices]

    # Process uncached texts in batches
    new_embeddings: list[list[float]] = []
    for i in range(0, len(texts_to_embed), config.batch_size):
        batch = texts_to_embed[i : i + config.batch_size]
        batch_embeddings = _embed_batch(batch, config)

        # Save each embedding to cache immediately
        if cache:
            for text, embedding in zip(batch, batch_embeddings):
                cache.set(text, embedding)

        new_embeddings.extend(batch_embeddings)

    # Combine cached and new results in correct order
    final_results: list[list[float]] = []
    new_idx = 0
    for i in range(len(texts)):
        if cached_results[i] is not None:
            final_results.append(cached_results[i])
        else:
            final_results.append(new_embeddings[new_idx])
            new_idx += 1

    return final_results


def _embed_batch(
    texts: list[str],
    config: EmbeddingConfig,
) -> list[list[float]]:
    """Embed a single batch of texts with retry logic."""
    url = f"{config.api_base.rstrip('/')}/embeddings"

    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    payload = {
        "input": texts,
        "model": config.model,
    }

    data = _http_post_with_retry(
        url=url,
        payload=payload,
        headers=headers,
        timeout=config.timeout,
        max_retries=config.max_retries,
        error_cls=EmbeddingError,
    )

    # OpenAI-compatible format: {"data": [{"embedding": [...]}]}
    if "data" not in data:
        raise EmbeddingError(f"Unexpected response format: {data}")

    # Sort by index to ensure correct order
    sorted_data = sorted(data["data"], key=lambda x: x.get("index", 0))
    return [item["embedding"] for item in sorted_data]


def is_httpx_available() -> bool:
    """Check if httpx is available for API calls."""
    return _HTTPX_AVAILABLE
