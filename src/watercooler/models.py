"""Model registry and resolution for embeddings and LLMs.

Maps friendly model names to their specifications. Supports:
- Embedding models (GGUF files for llama.cpp)
- LLM models (GGUF files for llama-server, with response field configuration)

Usage:
    from watercooler.models import (
        # Embedding models
        resolve_embedding_model,
        get_model_path,
        ensure_model_available,
        get_model_dimension,
        # LLM models (GGUF)
        resolve_llm_gguf_model,
        ensure_llm_model_available,
        # LLM response fields
        resolve_llm_model,
        get_response_field,
    )

    # Get embedding model spec
    spec = resolve_embedding_model("bge-m3")
    # Returns: {"hf_repo": "...", "dim": 1024, ...}

    # Get LLM GGUF model spec for llama-server
    spec = resolve_llm_gguf_model("qwen3:30b")
    # Returns: {"hf_repo": "...", "hf_file": "...q4_k_m.gguf", ...}

    # Get LLM response field
    field = get_response_field("qwen3:30b")
    # Returns: "content" (for thinking models)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, TypedDict


# =============================================================================
# Common
# =============================================================================

# Default models directory
DEFAULT_MODELS_DIR = Path.home() / ".watercooler" / "models"

# Environment variable for auto-provisioning models
ENV_AUTO_PROVISION_MODELS = "WATERCOOLER_AUTO_PROVISION_MODELS"


class ModelNotFoundError(Exception):
    """Raised when a model name cannot be resolved."""

    pass


class ModelDownloadError(Exception):
    """Raised when a model cannot be downloaded."""

    pass


class InsufficientDiskSpaceError(ModelDownloadError):
    """Raised when there isn't enough disk space for a model download."""

    pass


def check_disk_space(target_dir: Path, required_mb: int, buffer_mb: int = 500) -> None:
    """Check if there's enough disk space for a download.

    Args:
        target_dir: Directory where download will be saved
        required_mb: Required space in megabytes
        buffer_mb: Additional buffer space in MB (default: 500 MB)

    Raises:
        InsufficientDiskSpaceError: If not enough disk space is available
    """
    import shutil

    # Ensure directory exists for statvfs
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Get free space in bytes
        free_bytes = shutil.disk_usage(target_dir).free
        free_mb = free_bytes // (1024 * 1024)

        total_required = required_mb + buffer_mb

        if free_mb < total_required:
            raise InsufficientDiskSpaceError(
                f"Insufficient disk space for model download.\n"
                f"  Required: {required_mb:,} MB (+ {buffer_mb} MB buffer)\n"
                f"  Available: {free_mb:,} MB\n"
                f"  Location: {target_dir}\n\n"
                f"Free up at least {total_required - free_mb:,} MB of disk space and retry."
            )
    except OSError as e:
        # If we can't check disk space, log a warning but don't fail
        # (e.g., on some network filesystems)
        import logging
        logging.getLogger(__name__).warning(
            f"Could not check disk space at {target_dir}: {e}. Proceeding with download."
        )


def is_model_auto_provision_enabled() -> bool:
    """Check if model auto-provisioning is enabled.

    Checks environment variable first, then config file if available.
    Defaults to True (auto-download models when needed).

    Returns:
        True if model downloads are allowed
    """
    # Check environment variable override
    env_value = os.environ.get(ENV_AUTO_PROVISION_MODELS, "").lower().strip()
    if env_value in ("true", "1", "yes"):
        return True
    if env_value in ("false", "0", "no"):
        return False

    # Try to check config file (optional - may not have MCP dependencies)
    try:
        from watercooler_mcp.config import get_watercooler_config
        config = get_watercooler_config()
        return config.mcp.service_provision.models
    except Exception:
        pass

    # Default: allow model downloads
    return True


# =============================================================================
# Embedding Models
# =============================================================================


class EmbeddingModelSpec(TypedDict, total=False):
    """Specification for an embedding model."""

    hf_repo: str  # HuggingFace repository ID
    hf_file: str  # Filename within the repo
    dim: int  # Embedding dimension
    context: int  # Context window size
    size_mb: int  # Approximate file size in MB (for disk space checks)


# Registry of known embedding models
# Maps friendly names to their specifications
EMBEDDING_MODELS: dict[str, EmbeddingModelSpec | str] = {
    # Primary models
    "bge-m3": {
        "hf_repo": "KimChen/bge-m3-GGUF",
        "hf_file": "bge-m3-q8_0.gguf",
        "dim": 1024,
        "context": 8192,
        "size_mb": 1200,  # ~1.2 GB
    },
    "nomic-embed-text": {
        "hf_repo": "nomic-ai/nomic-embed-text-v1.5-GGUF",
        "hf_file": "nomic-embed-text-v1.5.Q8_0.gguf",
        "dim": 768,
        "context": 8192,
        "size_mb": 150,  # ~150 MB
    },
    "e5-mistral-7b": {
        "hf_repo": "lm-kit/e5-mistral-7b-instruct-GGUF",
        "hf_file": "e5-mistral-7b-instruct-Q4_K_M.gguf",
        "dim": 4096,
        "context": 4096,
        "size_mb": 4400,  # ~4.4 GB
    },
    # Aliases (resolve to primary names)
    "bge-m3:latest": "bge-m3",
    "nomic-embed-text:latest": "nomic-embed-text",
    "e5-mistral-7b:latest": "e5-mistral-7b",
    # Version tag aliases
    "nomic-embed-text:v1.5": "nomic-embed-text",
}

# Default embedding model when none specified
DEFAULT_EMBEDDING_MODEL = "bge-m3"


def resolve_embedding_model(name: str) -> EmbeddingModelSpec:
    """Resolve a friendly model name to its full specification.

    Follows aliases to get the actual model spec.

    Args:
        name: Model name (e.g., "bge-m3", "nomic-embed-text:latest")

    Returns:
        EmbeddingModelSpec with hf_repo, hf_file, dim, context

    Raises:
        ModelNotFoundError: If the model name is not in the registry
    """
    # Normalize name (lowercase, strip whitespace)
    normalized = name.strip().lower()

    # Follow aliases
    visited: set[str] = set()
    current = normalized

    while True:
        if current in visited:
            raise ModelNotFoundError(f"Circular alias detected for model: {name}")
        visited.add(current)

        entry = EMBEDDING_MODELS.get(current)
        if entry is None:
            # Try without version suffix
            if ":" in current:
                base_name = current.split(":")[0]
                entry = EMBEDDING_MODELS.get(base_name)
                if entry is not None:
                    current = base_name
                    continue

            known_models = [k for k, v in EMBEDDING_MODELS.items() if isinstance(v, dict)]
            raise ModelNotFoundError(
                f"Unknown embedding model: {name}\n"
                f"Known models: {', '.join(sorted(known_models))}"
            )

        if isinstance(entry, str):
            # It's an alias, follow it
            current = entry
        else:
            # It's a spec, return it
            return entry


def get_model_dimension(name: str) -> int:
    """Get the embedding dimension for a model.

    Args:
        name: Model name

    Returns:
        Embedding dimension (e.g., 1024 for bge-m3)

    Raises:
        ModelNotFoundError: If model is not known
    """
    spec = resolve_embedding_model(name)
    return spec.get("dim", 1024)


def get_models_dir() -> Path:
    """Get the directory where models are stored.

    Creates the directory if it doesn't exist.

    Returns:
        Path to models directory
    """
    models_dir = DEFAULT_MODELS_DIR
    models_dir.mkdir(parents=True, exist_ok=True)
    return models_dir


def get_model_path(name: str) -> Optional[Path]:
    """Get the cached path for a model, if it exists.

    Args:
        name: Model name

    Returns:
        Path to the cached model file, or None if not downloaded
    """
    try:
        spec = resolve_embedding_model(name)
    except ModelNotFoundError:
        return None

    model_file = spec.get("hf_file", "")
    if not model_file:
        return None

    model_path = get_models_dir() / model_file
    if model_path.exists():
        return model_path

    return None


def ensure_model_available(
    name: str,
    verbose: bool = True,
) -> Path:
    """Ensure a model is downloaded and return its path.

    Downloads the model from HuggingFace if not already cached.

    Args:
        name: Model name
        verbose: Print progress messages

    Returns:
        Path to the model file

    Raises:
        ModelNotFoundError: If model name is unknown
        ModelDownloadError: If download fails
    """
    spec = resolve_embedding_model(name)
    hf_repo = spec.get("hf_repo", "")
    hf_file = spec.get("hf_file", "")

    if not hf_repo or not hf_file:
        raise ModelNotFoundError(f"Model {name} has incomplete specification")

    models_dir = get_models_dir()
    model_path = models_dir / hf_file

    # Return cached path if exists
    if model_path.exists():
        if verbose:
            print(f"Using cached model: {model_path}")
        return model_path

    # Check if auto-provisioning is enabled
    if not is_model_auto_provision_enabled():
        raise ModelDownloadError(
            f"Model '{name}' not found and auto-provisioning is disabled.\n\n"
            f"To enable auto-download, set in config.toml:\n"
            f"  [mcp.service_provision]\n"
            f"  models = true\n\n"
            f"Or set environment variable:\n"
            f"  WATERCOOLER_AUTO_PROVISION_MODELS=true\n\n"
            f"To download manually:\n"
            f"  pip install huggingface_hub\n"
            f"  huggingface-cli download {hf_repo} {hf_file} --local-dir {models_dir}"
        )

    # Check disk space before downloading
    size_mb = spec.get("size_mb", 0)
    if size_mb > 0:
        check_disk_space(models_dir, size_mb)

    # Download from HuggingFace
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as e:
        raise ModelDownloadError(
            "huggingface_hub is required for model download.\n"
            "Install with: pip install huggingface_hub"
        ) from e

    if verbose:
        print(f"Downloading embedding model: {hf_repo}/{hf_file}")
        dim = spec.get("dim", "unknown")
        print(f"  Dimension: {dim}")
        if size_mb > 0:
            print(f"  Size: ~{size_mb:,} MB")
        print("  This may take a few minutes...")

    try:
        downloaded_path = hf_hub_download(
            repo_id=hf_repo,
            filename=hf_file,
            local_dir=models_dir,
        )
        result_path = Path(downloaded_path)

        if verbose:
            print(f"Model downloaded to: {result_path}")

        return result_path

    except Exception as e:
        raise ModelDownloadError(f"Failed to download model {name}: {e}") from e


# =============================================================================
# LLM GGUF Models (for llama-server)
# =============================================================================


class LLMGGUFModelSpec(TypedDict, total=False):
    """Specification for an LLM GGUF model for llama-server."""

    hf_repo: str  # HuggingFace repository ID
    hf_file: str  # Filename within the repo
    context: int  # Context window size
    size_mb: int  # Approximate file size in MB (for disk space checks)


# Registry of known LLM GGUF models for llama-server
# Maps friendly names to HuggingFace GGUF specs
LLM_GGUF_MODELS: dict[str, LLMGGUFModelSpec | str] = {
    # Qwen3 models (MoE architecture with thinking mode support)
    "qwen3:30b": {
        "hf_repo": "Qwen/Qwen3-30B-A3B-GGUF",
        "hf_file": "Qwen3-30B-A3B-Q4_K_M.gguf",
        "context": 40960,
        "size_mb": 18000,  # ~18 GB
    },
    "qwen3:8b": {
        "hf_repo": "Qwen/Qwen3-8B-GGUF",
        "hf_file": "Qwen3-8B-Q4_K_M.gguf",
        "context": 40960,
        "size_mb": 5000,  # ~5 GB
    },
    "qwen3:4b": {
        "hf_repo": "Qwen/Qwen3-4B-GGUF",
        "hf_file": "Qwen3-4B-Q4_K_M.gguf",
        "context": 40960,
        "size_mb": 2700,  # ~2.7 GB
    },
    # Qwen3 small models - require /no_think prefix to disable reasoning mode
    "qwen3:1.7b": {
        "hf_repo": "unsloth/Qwen3-1.7B-GGUF",  # Community GGUF (official not available)
        "hf_file": "Qwen3-1.7B-Q4_K_M.gguf",
        "context": 40960,
        "size_mb": 1100,  # ~1.1 GB, good quality with /no_think prefix
    },
    "qwen3:0.6b": {
        "hf_repo": "unsloth/Qwen3-0.6B-GGUF",  # Community GGUF (official not available)
        "hf_file": "Qwen3-0.6B-Q4_K_M.gguf",
        "context": 40960,
        "size_mb": 400,  # ~400 MB, smallest viable summarizer with /no_think
    },
    # Llama 3.2 models
    "llama3.2:3b": {
        "hf_repo": "hugging-quants/Llama-3.2-3B-Instruct-Q8_0-GGUF",
        "hf_file": "llama-3.2-3b-instruct-q8_0.gguf",
        "context": 8192,
        "size_mb": 3400,  # ~3.4 GB
    },
    "llama3.2:1b": {
        "hf_repo": "hugging-quants/Llama-3.2-1B-Instruct-Q8_0-GGUF",
        "hf_file": "llama-3.2-1b-instruct-q8_0.gguf",
        "context": 8192,
        "size_mb": 1300,  # ~1.3 GB
    },
    # SmolLM2 models - requires two-phase prompting for summarization (extract→synthesize)
    "smollm2:1.7b": {
        "hf_repo": "bartowski/SmolLM2-1.7B-Instruct-GGUF",
        "hf_file": "SmolLM2-1.7B-Instruct-Q4_K_M.gguf",
        "context": 8192,
        "size_mb": 1000,  # ~1 GB, needs two-phase prompting for summarization
    },
    # Qwen2.5 models - excellent for summarization with few-shot prompting, no special prefix needed
    "qwen2.5:3b": {
        "hf_repo": "Qwen/Qwen2.5-3B-Instruct-GGUF",
        "hf_file": "qwen2.5-3b-instruct-q4_k_m.gguf",
        "context": 32768,
        "size_mb": 2000,  # ~2 GB, best quality for summarization
    },
    "qwen2.5:1.5b": {
        "hf_repo": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        "hf_file": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "context": 32768,
        "size_mb": 1100,  # ~1.1 GB, fastest and recommended for summarization
    },
    # Phi-3 models (Microsoft, high accuracy but verbose)
    "phi3:3.8b": {
        "hf_repo": "microsoft/Phi-3-mini-4k-instruct-gguf",
        "hf_file": "Phi-3-mini-4k-instruct-q4.gguf",
        "context": 4096,
        "size_mb": 2300,  # ~2.3 GB
    },
    # Aliases
    "qwen3:latest": "qwen3:30b",
    "qwen3": "qwen3:30b",
    "llama3.2": "llama3.2:3b",
    "llama3.2:latest": "llama3.2:3b",
    "smollm2": "smollm2:1.7b",
    "smollm2:latest": "smollm2:1.7b",
    "qwen2.5": "qwen2.5:3b",
    "qwen2.5:latest": "qwen2.5:3b",
    "phi3": "phi3:3.8b",
    "phi3:latest": "phi3:3.8b",
    "phi3-mini": "phi3:3.8b",
}

# Default LLM model when none specified
DEFAULT_LLM_GGUF_MODEL = "llama3.2:3b"


def resolve_llm_gguf_model(name: str) -> LLMGGUFModelSpec:
    """Resolve an LLM model name to its GGUF specification.

    Follows aliases to get the actual model spec.

    Args:
        name: Model name (e.g., "qwen3:30b", "llama3.2:3b")

    Returns:
        LLMGGUFModelSpec with hf_repo, hf_file, context

    Raises:
        ModelNotFoundError: If the model name is not in the registry
    """
    # Normalize name (lowercase, strip whitespace)
    normalized = name.strip().lower()

    # Follow aliases
    visited: set[str] = set()
    current = normalized

    while True:
        if current in visited:
            raise ModelNotFoundError(f"Circular alias detected for model: {name}")
        visited.add(current)

        entry = LLM_GGUF_MODELS.get(current)
        if entry is None:
            # Try without version suffix
            if ":" in current:
                base_name = current.split(":")[0]
                entry = LLM_GGUF_MODELS.get(base_name)
                if entry is not None:
                    current = base_name
                    continue

            known_models = [k for k, v in LLM_GGUF_MODELS.items() if isinstance(v, dict)]
            raise ModelNotFoundError(
                f"Unknown LLM GGUF model: {name}\n"
                f"Known models: {', '.join(sorted(known_models))}"
            )

        if isinstance(entry, str):
            # It's an alias, follow it
            current = entry
        else:
            # It's a spec, return it
            return entry


def get_llm_model_path(name: str) -> Optional[Path]:
    """Get the cached path for an LLM GGUF model, if it exists.

    Args:
        name: Model name

    Returns:
        Path to the cached model file, or None if not downloaded
    """
    try:
        spec = resolve_llm_gguf_model(name)
    except ModelNotFoundError:
        return None

    model_file = spec.get("hf_file", "")
    if not model_file:
        return None

    model_path = get_models_dir() / model_file
    if model_path.exists():
        return model_path

    return None


def ensure_llm_model_available(
    name: str,
    verbose: bool = True,
) -> Path:
    """Ensure an LLM GGUF model is downloaded and return its path.

    Downloads the model from HuggingFace if not already cached.

    Args:
        name: Model name (e.g., "qwen3:30b", "llama3.2:3b")
        verbose: Print progress messages

    Returns:
        Path to the model file

    Raises:
        ModelNotFoundError: If model name is unknown
        ModelDownloadError: If download fails
    """
    spec = resolve_llm_gguf_model(name)
    hf_repo = spec.get("hf_repo", "")
    hf_file = spec.get("hf_file", "")

    if not hf_repo or not hf_file:
        raise ModelNotFoundError(f"LLM model {name} has incomplete specification")

    models_dir = get_models_dir()
    model_path = models_dir / hf_file

    # Return cached path if exists
    if model_path.exists():
        if verbose:
            print(f"Using cached LLM model: {model_path}")
        return model_path

    # Check if auto-provisioning is enabled
    if not is_model_auto_provision_enabled():
        raise ModelDownloadError(
            f"LLM model '{name}' not found and auto-provisioning is disabled.\n\n"
            f"To enable auto-download, set in config.toml:\n"
            f"  [mcp.service_provision]\n"
            f"  models = true\n\n"
            f"Or set environment variable:\n"
            f"  WATERCOOLER_AUTO_PROVISION_MODELS=true\n\n"
            f"To download manually:\n"
            f"  pip install huggingface_hub\n"
            f"  huggingface-cli download {hf_repo} {hf_file} --local-dir {models_dir}"
        )

    # Check disk space before downloading
    size_mb = spec.get("size_mb", 0)
    if size_mb > 0:
        check_disk_space(models_dir, size_mb)

    # Download from HuggingFace
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as e:
        raise ModelDownloadError(
            "huggingface_hub is required for model download.\n"
            "Install with: pip install huggingface_hub"
        ) from e

    if verbose:
        print(f"Downloading LLM model: {hf_repo}/{hf_file}")
        context = spec.get("context", "unknown")
        print(f"  Context window: {context}")
        if size_mb > 0:
            print(f"  Size: ~{size_mb:,} MB")
        print("  This may take a while for large models...")

    try:
        downloaded_path = hf_hub_download(
            repo_id=hf_repo,
            filename=hf_file,
            local_dir=models_dir,
        )
        result_path = Path(downloaded_path)

        if verbose:
            print(f"LLM model downloaded to: {result_path}")

        return result_path

    except Exception as e:
        raise ModelDownloadError(f"Failed to download LLM model {name}: {e}") from e


def is_known_llm_gguf_model(name: str) -> bool:
    """Check if a model name is in the LLM GGUF registry.

    Args:
        name: Model name to check

    Returns:
        True if this model is in our GGUF registry
    """
    try:
        resolve_llm_gguf_model(name)
        return True
    except ModelNotFoundError:
        return False


def get_llm_context_size(name: str, default: int = 8192) -> int:
    """Get the context window size for an LLM GGUF model.

    Args:
        name: Model name
        default: Default context size if model not found

    Returns:
        Context window size in tokens
    """
    try:
        spec = resolve_llm_gguf_model(name)
        return spec.get("context", default)
    except ModelNotFoundError:
        return default


# =============================================================================
# LLM Models (for summarization - response field configuration)
# =============================================================================


class LLMModelSpec(TypedDict, total=False):
    """Specification for an LLM model."""

    response_field: str  # Field containing response: "content" or "reasoning"
    supports_thinking: bool  # Whether model uses thinking/reasoning mode
    min_max_tokens: int  # Minimum max_tokens needed (thinking models need more)
    default_temperature: float  # Suggested temperature for this model


# Registry of known LLM models with special configurations
# Models not in this registry default to response_field="content"
LLM_MODELS: dict[str, LLMModelSpec | str] = {
    # Qwen3 models output to "content" but need extra tokens for thinking
    # The thinking happens internally, final answer goes to content
    "qwen3:30b": {
        "response_field": "content",
        "supports_thinking": True,
        "min_max_tokens": 512,  # Thinking needs extra tokens
    },
    "qwen3:14b": {
        "response_field": "content",
        "supports_thinking": True,
        "min_max_tokens": 512,
    },
    "qwen3:8b": {
        "response_field": "content",
        "supports_thinking": True,
        "min_max_tokens": 512,
    },
    "qwen3:4b": {
        "response_field": "content",
        "supports_thinking": True,
        "min_max_tokens": 512,
    },
    "qwen3:1.7b": {
        "response_field": "content",
        "supports_thinking": True,
        "min_max_tokens": 512,
    },
    "qwen3:0.6b": {
        "response_field": "content",
        "supports_thinking": True,
        "min_max_tokens": 512,
    },
    # Aliases for version tags
    "qwen3:30b-q4_K_M": "qwen3:30b",
    "qwen3:30b-q8_0": "qwen3:30b",
    "qwen3:latest": "qwen3:30b",
    # Standard models use "content" (explicit for documentation)
    "llama3.2": {
        "response_field": "content",
        "supports_thinking": False,
    },
    "llama3.2:latest": "llama3.2",
    "llama3.1": {
        "response_field": "content",
        "supports_thinking": False,
    },
    # SmolLM2 models - requires two-phase prompting for summarization
    "smollm2:1.7b": {
        "response_field": "content",
        "supports_thinking": False,
    },
    "smollm2": "smollm2:1.7b",
    # Qwen2.5 models - excellent for summarization with few-shot prompting
    "qwen2.5:3b": {
        "response_field": "content",
        "supports_thinking": False,
    },
    "qwen2.5:1.5b": {
        "response_field": "content",
        "supports_thinking": False,
    },
    "qwen2.5": "qwen2.5:3b",
    # Phi-3 models (clean output)
    "phi3:3.8b": {
        "response_field": "content",
        "supports_thinking": False,
    },
    "phi3": "phi3:3.8b",
    "mistral": {
        "response_field": "content",
        "supports_thinking": False,
    },
    "mixtral": {
        "response_field": "content",
        "supports_thinking": False,
    },
}

# Default LLM configuration for unknown models
DEFAULT_LLM_SPEC: LLMModelSpec = {
    "response_field": "content",
    "supports_thinking": False,
}


def resolve_llm_model(name: str) -> LLMModelSpec:
    """Resolve an LLM model name to its specification.

    For unknown models, returns default spec (response_field="content").

    Args:
        name: Model name (e.g., "qwen3:30b", "llama3.2")

    Returns:
        LLMModelSpec with response_field and other config
    """
    # Normalize name (lowercase, strip whitespace)
    normalized = name.strip().lower()

    # Follow aliases
    visited: set[str] = set()
    current = normalized

    while True:
        if current in visited:
            # Circular alias, return default
            return DEFAULT_LLM_SPEC
        visited.add(current)

        entry = LLM_MODELS.get(current)
        if entry is None:
            # Try without version suffix
            if ":" in current:
                base_name = current.split(":")[0]
                entry = LLM_MODELS.get(base_name)
                if entry is not None:
                    current = base_name
                    continue

            # Unknown model, return default
            return DEFAULT_LLM_SPEC

        if isinstance(entry, str):
            # It's an alias, follow it
            current = entry
        else:
            # It's a spec, return it
            return entry


def get_response_field(model_name: str) -> str:
    """Get the response field for an LLM model.

    Args:
        model_name: Model name (e.g., "qwen3:30b")

    Returns:
        Field name to extract response from: "content" or "reasoning"
    """
    spec = resolve_llm_model(model_name)
    return spec.get("response_field", "content")


def supports_thinking(model_name: str) -> bool:
    """Check if a model supports thinking/reasoning mode.

    Args:
        model_name: Model name

    Returns:
        True if model uses thinking mode with reasoning field
    """
    spec = resolve_llm_model(model_name)
    return spec.get("supports_thinking", False)


def get_min_max_tokens(model_name: str, default: int = 256) -> int:
    """Get the minimum max_tokens needed for a model.

    Thinking models need more tokens to complete their reasoning
    before producing the final answer.

    Args:
        model_name: Model name
        default: Default value for models not in registry

    Returns:
        Minimum max_tokens value
    """
    spec = resolve_llm_model(model_name)
    return spec.get("min_max_tokens", default)


# Model family detection for prompt configuration
# These defaults are applied when user doesn't specify system_prompt or prompt_prefix

_MODEL_FAMILY_DEFAULTS: dict[str, dict[str, str]] = {
    # Qwen3 family needs /no_think to disable reasoning mode
    "qwen3": {
        "prompt_prefix": "/no_think ",
        "system_prompt": "",  # No system prompt needed with /no_think
    },
    # Qwen2.5 family works well with system prompt, no prefix needed
    "qwen2.5": {
        "prompt_prefix": "",
        "system_prompt": "You summarize technical entries concisely with relevant tags.",
    },
    # Default for unknown models
    "default": {
        "prompt_prefix": "",
        "system_prompt": "You summarize technical entries concisely with relevant tags.",
    },
}


def get_model_family(model_name: str) -> str:
    """Detect model family from model name.

    Args:
        model_name: Model name (e.g., "qwen3:1.7b", "qwen2.5:3b")

    Returns:
        Model family identifier (e.g., "qwen3", "qwen2.5", "default")
    """
    name_lower = model_name.lower()

    # Check known families (order matters - check more specific first)
    if "qwen3" in name_lower:
        return "qwen3"
    if "qwen2.5" in name_lower or "qwen2-5" in name_lower:
        return "qwen2.5"
    if "smollm" in name_lower:
        return "smollm2"
    if "phi3" in name_lower or "phi-3" in name_lower:
        return "phi3"
    if "llama" in name_lower:
        return "llama"

    return "default"


def get_model_prompt_defaults(model_name: str) -> dict[str, str]:
    """Get prompt configuration defaults for a model.

    Returns appropriate system_prompt and prompt_prefix based on model family.
    These are used when user doesn't configure explicit values.

    Args:
        model_name: Model name (e.g., "qwen3:1.7b")

    Returns:
        Dict with "system_prompt" and "prompt_prefix" keys
    """
    family = get_model_family(model_name)
    return _MODEL_FAMILY_DEFAULTS.get(family, _MODEL_FAMILY_DEFAULTS["default"])
