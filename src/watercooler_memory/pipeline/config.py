"""Pipeline configuration."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import os

# Load .env.pipeline from repo root if it exists
def _load_env_pipeline() -> None:
    """Load .env.pipeline file if present."""
    # Find repo root by looking for .env.pipeline or pyproject.toml
    current = Path(__file__).resolve()
    for parent in [current] + list(current.parents):
        env_file = parent / ".env.pipeline"
        if env_file.exists():
            # Simple .env parser (no external deps)
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        key = key.strip()
                        value = value.strip()
                        # Only set if not already in environment
                        if key not in os.environ:
                            os.environ[key] = value
            break

_load_env_pipeline()

# Import credentials module for unified credential access
from watercooler.credentials import (
    get_deepseek_api_key,
    get_deepseek_api_base,
    get_deepseek_model,
    get_embedding_api_base,
)


def _get_api_key() -> str:
    """Get DeepSeek API key from credentials or environment."""
    key = get_deepseek_api_key()
    return key or ""


@dataclass
class LLMConfig:
    """LLM service configuration.

    Model priority: DEEPSEEK_MODEL env > config.toml [memory_graph.llm].model > default
    """

    api_key: str = field(default_factory=_get_api_key)
    model: str = field(default_factory=get_deepseek_model)
    base_url: str = field(default_factory=get_deepseek_api_base)

    def validate(self) -> list[str]:
        """Validate configuration. Returns list of errors."""
        errors = []
        if not self.api_key:
            errors.append("DEEPSEEK_API_KEY not set (env or ~/.watercooler/credentials.toml)")
        if self.base_url and not self.base_url.startswith(("http://", "https://")):
            errors.append(f"Invalid LLM API base URL (must start with http:// or https://): {self.base_url}")
        return errors


@dataclass
class EmbeddingConfig:
    """Embedding service configuration."""

    model: str = field(default_factory=lambda: os.environ.get("GLM_MODEL", "bge_m3"))
    base_url: str = field(default_factory=get_embedding_api_base)
    embedding_dim: int = 1024  # BGE-M3 dimension
    batch_size: int = field(default_factory=lambda: max(1, int(os.environ.get("EMBEDDING_BATCH_SIZE", "8"))))

    def validate(self) -> list[str]:
        """Validate configuration. Returns list of errors."""
        errors = []
        if not self.base_url:
            errors.append("Embedding API base URL not set (env EMBEDDING_API_BASE or ~/.watercooler/config.toml)")
        elif not self.base_url.startswith(("http://", "https://")):
            errors.append(f"Invalid Embedding API base URL (must start with http:// or https://): {self.base_url}")
        return errors


@dataclass
class PipelineConfig:
    """Full pipeline configuration."""

    # Paths
    threads_dir: Path = field(default_factory=lambda: Path("."))
    work_dir: Path = field(default_factory=lambda: Path("./pipeline_work"))
    leanrag_dir: Optional[Path] = None  # Path to LeanRAG repo (for stage runners)

    # Processing
    batch_size: int = 10  # Documents per batch for LLM stages
    max_concurrent: int = 4  # Concurrent LLM calls

    # Chunking (768/64 balances comprehensiveness vs "lost in the middle")
    max_tokens: int = 768
    overlap_tokens: int = 64

    # Services
    llm: LLMConfig = field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)

    # Test mode
    test_mode: bool = False
    test_limit: int = 5  # Max documents in test mode

    # Thread filtering
    thread_filter: Optional[list[str]] = None  # List of thread .md filenames to process (None = all)

    def validate(self) -> list[str]:
        """Validate full configuration. Returns list of errors."""
        errors = []

        if not self.threads_dir.exists():
            errors.append(f"Threads directory not found: {self.threads_dir}")

        if self.leanrag_dir and not self.leanrag_dir.exists():
            errors.append(f"LeanRAG directory not found: {self.leanrag_dir}")

        # Validate processing parameters
        if self.batch_size < 1:
            errors.append(f"batch_size must be >= 1, got {self.batch_size}")
        if self.max_concurrent < 1:
            errors.append(f"max_concurrent must be >= 1, got {self.max_concurrent}")

        errors.extend(self.llm.validate())
        errors.extend(self.embedding.validate())

        return errors

    def ensure_work_dir(self) -> None:
        """Create work directory structure."""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        (self.work_dir / "logs").mkdir(exist_ok=True)
        (self.work_dir / "state").mkdir(exist_ok=True)
        (self.work_dir / "export").mkdir(exist_ok=True)
        (self.work_dir / "extract").mkdir(exist_ok=True)
        (self.work_dir / "graph").mkdir(exist_ok=True)


def _default_cache_dir() -> Path:
    """Get default cache directory (~/.watercooler/cache/)."""
    return Path.home() / ".watercooler" / "cache"


def load_config_from_env(
    threads_dir: Optional[Path] = None,
    work_dir: Optional[Path] = None,
    leanrag_dir: Optional[Path] = None,
    test_mode: bool = False,
    thread_filter: Optional[list[str]] = None,
) -> PipelineConfig:
    """Load pipeline configuration from environment variables.

    Args:
        threads_dir: Override threads directory
        work_dir: Override work directory (defaults to ~/.watercooler/cache/)
        leanrag_dir: Override LeanRAG directory
        test_mode: Enable test mode with limited data
        thread_filter: List of thread .md filenames to process (None = all)

    Returns:
        Configured PipelineConfig
    """
    # Default cache dir is ~/.watercooler/cache/
    default_cache = str(_default_cache_dir())

    config = PipelineConfig(
        threads_dir=threads_dir or Path(os.environ.get("WC_THREADS_DIR", ".")),
        work_dir=work_dir or Path(os.environ.get("WC_PIPELINE_WORK_DIR", default_cache)),
        leanrag_dir=leanrag_dir or (Path(os.environ["LEANRAG_DIR"]) if "LEANRAG_DIR" in os.environ else None),
        batch_size=max(1, int(os.environ.get("WC_BATCH_SIZE", "10"))),
        max_concurrent=max(1, int(os.environ.get("WC_MAX_CONCURRENT", "4"))),
        test_mode=test_mode,
        thread_filter=thread_filter,
    )

    return config
