"""Embedding model registry and resolution.

Maps friendly model names (like "bge-m3") to their HuggingFace repo paths,
file names, and configurations. This enables zero-config embedding setup
where users just specify `model = "bge-m3"` and everything works.

Usage:
    from watercooler.embedding_models import (
        resolve_embedding_model,
        get_model_path,
        ensure_model_available,
    )

    # Get model spec from friendly name
    spec = resolve_embedding_model("bge-m3")
    # Returns: {"hf_repo": "KimChen/bge-m3-GGUF", "hf_file": "bge-m3-q8_0.gguf", ...}

    # Get cached model path (None if not downloaded)
    path = get_model_path("bge-m3")

    # Ensure model is downloaded and return path
    path = ensure_model_available("bge-m3")
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional, TypedDict

# Default models directory
DEFAULT_MODELS_DIR = Path.home() / ".watercooler" / "models"


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

# Default model when none specified
DEFAULT_MODEL = "bge-m3"


class ModelNotFoundError(Exception):
    """Raised when a model name cannot be resolved."""

    pass


class ModelDownloadError(Exception):
    """Raised when a model cannot be downloaded."""

    pass


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


def is_ollama_model(name: str) -> bool:
    """Check if a model name refers to an Ollama model.

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
