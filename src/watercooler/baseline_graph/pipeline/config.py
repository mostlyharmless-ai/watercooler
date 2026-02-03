"""Configuration for baseline graph pipeline."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import os


def _get_default_llm_api_base() -> str:
    """Get default LLM API base from unified config."""
    try:
        from watercooler.memory_config import resolve_baseline_graph_llm_config
        return resolve_baseline_graph_llm_config().api_base
    except ImportError:
        return os.environ.get("LLM_API_BASE", "https://api.openai.com/v1")


def _get_default_llm_model() -> str:
    """Get default LLM model from unified config."""
    try:
        from watercooler.memory_config import resolve_baseline_graph_llm_config
        return resolve_baseline_graph_llm_config().model
    except ImportError:
        return os.environ.get("LLM_MODEL", "gpt-4o-mini")


def _get_default_llm_api_key() -> str:
    """Get default LLM API key from unified config."""
    try:
        from watercooler.memory_config import resolve_baseline_graph_llm_config
        return resolve_baseline_graph_llm_config().api_key
    except ImportError:
        return os.environ.get("LLM_API_KEY", "")


def _get_default_embedding_api_base() -> str:
    """Get default embedding API base from unified config."""
    try:
        from watercooler.memory_config import resolve_baseline_graph_embedding_config
        return resolve_baseline_graph_embedding_config().api_base
    except ImportError:
        return os.environ.get("EMBEDDING_API_BASE", "http://localhost:8080/v1")


def _get_default_embedding_model() -> str:
    """Get default embedding model from unified config."""
    try:
        from watercooler.memory_config import resolve_baseline_graph_embedding_config
        return resolve_baseline_graph_embedding_config().model
    except ImportError:
        return os.environ.get("EMBEDDING_MODEL", "bge-m3")


def _get_default_embedding_dim() -> int:
    """Get default embedding dimension from unified config."""
    try:
        from watercooler.memory_config import resolve_baseline_graph_embedding_config
        return resolve_baseline_graph_embedding_config().dim
    except ImportError:
        dim_str = os.environ.get("EMBEDDING_DIM", "1024")
        try:
            return int(dim_str)
        except ValueError:
            return 1024


def _get_default_embedding_api_key() -> str:
    """Get default embedding API key from unified config."""
    try:
        from watercooler.memory_config import resolve_baseline_graph_embedding_config
        return resolve_baseline_graph_embedding_config().api_key
    except ImportError:
        return os.environ.get("EMBEDDING_API_KEY", "")


@dataclass
class LLMConfig:
    """LLM server configuration.

    Resolved via unified config with priority:
    1. Environment variables (LLM_API_BASE, LLM_MODEL, LLM_API_KEY)
    2. TOML config ([memory.llm])
    3. Built-in defaults from memory_config
    """

    api_base: str = field(default_factory=_get_default_llm_api_base)
    model: str = field(default_factory=_get_default_llm_model)
    api_key: str = field(default_factory=_get_default_llm_api_key)
    timeout: float = 120.0
    max_tokens: int = 256

    @classmethod
    def from_env(cls) -> "LLMConfig":
        """Load from unified config system."""
        return cls(
            api_base=_get_default_llm_api_base(),
            model=_get_default_llm_model(),
            api_key=_get_default_llm_api_key(),
            timeout=float(os.environ.get("LLM_TIMEOUT", 120.0)),
            max_tokens=int(os.environ.get("LLM_MAX_TOKENS", 256)),
        )


@dataclass
class EmbeddingConfig:
    """Embedding server configuration.

    Resolved via unified config with priority:
    1. Environment variables (EMBEDDING_API_BASE, EMBEDDING_MODEL, etc.)
    2. TOML config ([memory.embedding])
    3. Built-in defaults from memory_config
    """

    api_base: str = field(default_factory=_get_default_embedding_api_base)
    model: str = field(default_factory=_get_default_embedding_model)
    api_key: str = field(default_factory=_get_default_embedding_api_key)
    timeout: float = 60.0
    embedding_dim: int = field(default_factory=_get_default_embedding_dim)

    @classmethod
    def from_env(cls) -> "EmbeddingConfig":
        """Load from unified config system."""
        return cls(
            api_base=_get_default_embedding_api_base(),
            model=_get_default_embedding_model(),
            api_key=_get_default_embedding_api_key(),
            timeout=float(os.environ.get("EMBEDDING_TIMEOUT", 60.0)),
            embedding_dim=_get_default_embedding_dim(),
        )


@dataclass
class PipelineConfig:
    """Full pipeline configuration."""

    threads_dir: Path = field(default_factory=lambda: Path("."))
    output_dir: Optional[Path] = None  # Default: {threads_dir}/graph/baseline

    # Processing limits
    test_limit: Optional[int] = None  # Limit threads processed
    skip_closed: bool = False
    fresh: bool = False  # Ignore cached results
    incremental: bool = False  # Only process changed threads

    # Services
    llm: LLMConfig = field(default_factory=LLMConfig.from_env)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig.from_env)

    # Feature flags
    extractive_only: bool = False  # Use extractive summarization (no LLM)
    skip_embeddings: bool = False  # Skip embedding generation

    def __post_init__(self):
        if self.output_dir is None:
            self.output_dir = self.threads_dir / "graph" / "baseline"

    def validate(self) -> list[str]:
        """Validate configuration. Returns list of errors."""
        errors = []
        if not self.threads_dir.exists():
            errors.append(f"Threads directory not found: {self.threads_dir}")
        return errors
