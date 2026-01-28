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

from pathlib import Path
from typing import Optional, TypedDict


# =============================================================================
# Common
# =============================================================================

# Default models directory
DEFAULT_MODELS_DIR = Path.home() / ".watercooler" / "models"


class ModelNotFoundError(Exception):
    """Raised when a model name cannot be resolved."""

    pass


class ModelDownloadError(Exception):
    """Raised when a model cannot be downloaded."""

    pass


# =============================================================================
# Embedding Models
# =============================================================================


class EmbeddingModelSpec(TypedDict, total=False):
    """Specification for an embedding model."""

    hf_repo: str  # HuggingFace repository ID
    hf_file: str  # Filename within the repo
    dim: int  # Embedding dimension
    context: int  # Context window size


# Registry of known embedding models
# Maps friendly names to their specifications
EMBEDDING_MODELS: dict[str, EmbeddingModelSpec | str] = {
    # Primary models
    "bge-m3": {
        "hf_repo": "KimChen/bge-m3-GGUF",
        "hf_file": "bge-m3-q8_0.gguf",
        "dim": 1024,
        "context": 8192,
    },
    "nomic-embed-text": {
        "hf_repo": "nomic-ai/nomic-embed-text-v1.5-GGUF",
        "hf_file": "nomic-embed-text-v1.5.Q8_0.gguf",
        "dim": 768,
        "context": 8192,
    },
    "e5-mistral-7b": {
        "hf_repo": "lm-kit/e5-mistral-7b-instruct-GGUF",
        "hf_file": "e5-mistral-7b-instruct-Q4_K_M.gguf",
        "dim": 4096,
        "context": 4096,
    },
    # Aliases (resolve to primary names)
    "bge-m3:latest": "bge-m3",
    "nomic-embed-text:latest": "nomic-embed-text",
    "e5-mistral-7b:latest": "e5-mistral-7b",
    # Ollama-compatible names
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


def is_ollama_embedding_model(name: str) -> bool:
    """Check if a model name refers to an Ollama embedding model.

    Ollama models are identified by:
    - Not being in our registry (custom Ollama model)
    - Having certain naming patterns (e.g., contains "/" without "GGUF")

    Args:
        name: Model name to check

    Returns:
        True if this appears to be an Ollama model name
    """
    normalized = name.strip().lower()

    # If it's in our registry, it's a llama.cpp model
    try:
        resolve_embedding_model(normalized)
        return False
    except ModelNotFoundError:
        pass

    # Common Ollama model patterns
    ollama_patterns = [
        "nomic-embed-text",  # Ollama's native embedding model
        "all-minilm",
        "mxbai-embed",
    ]

    for pattern in ollama_patterns:
        if pattern in normalized:
            return True

    return False


# Backwards compatibility alias
is_ollama_model = is_ollama_embedding_model


# =============================================================================
# LLM GGUF Models (for llama-server)
# =============================================================================


class LLMGGUFModelSpec(TypedDict, total=False):
    """Specification for an LLM GGUF model for llama-server."""

    hf_repo: str  # HuggingFace repository ID
    hf_file: str  # Filename within the repo
    context: int  # Context window size


# Registry of known LLM GGUF models for llama-server
# Maps friendly names (Ollama-style) to HuggingFace GGUF specs
LLM_GGUF_MODELS: dict[str, LLMGGUFModelSpec | str] = {
    # Qwen models (instruct versions for chat/summarization)
    "qwen3:30b": {
        "hf_repo": "Qwen/Qwen2.5-32B-Instruct-GGUF",
        "hf_file": "qwen2.5-32b-instruct-q4_k_m.gguf",
        "context": 32768,
    },
    "qwen3:14b": {
        "hf_repo": "Qwen/Qwen2.5-14B-Instruct-GGUF",
        "hf_file": "qwen2.5-14b-instruct-q4_k_m.gguf",
        "context": 32768,
    },
    "qwen3:8b": {
        "hf_repo": "Qwen/Qwen2.5-7B-Instruct-GGUF",
        "hf_file": "qwen2.5-7b-instruct-q4_k_m.gguf",
        "context": 32768,
    },
    "qwen3:4b": {
        "hf_repo": "Qwen/Qwen2.5-3B-Instruct-GGUF",
        "hf_file": "qwen2.5-3b-instruct-q4_k_m.gguf",
        "context": 32768,
    },
    "qwen3:1.7b": {
        "hf_repo": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        "hf_file": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "context": 32768,
    },
    "qwen3:0.6b": {
        "hf_repo": "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
        "hf_file": "qwen2.5-0.5b-instruct-q4_k_m.gguf",
        "context": 32768,
    },
    # Llama 3.2 models
    "llama3.2:3b": {
        "hf_repo": "hugging-quants/Llama-3.2-3B-Instruct-Q8_0-GGUF",
        "hf_file": "llama-3.2-3b-instruct-q8_0.gguf",
        "context": 8192,
    },
    "llama3.2:1b": {
        "hf_repo": "hugging-quants/Llama-3.2-1B-Instruct-Q8_0-GGUF",
        "hf_file": "llama-3.2-1b-instruct-q8_0.gguf",
        "context": 8192,
    },
    # Aliases
    "qwen3:latest": "qwen3:30b",
    "qwen3:30b-q4_K_M": "qwen3:30b",
    "qwen3:30b-q8_0": "qwen3:30b",
    "llama3.2": "llama3.2:3b",
    "llama3.2:latest": "llama3.2:3b",
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
