"""Backend registry helpers."""

from __future__ import annotations

import os
import warnings
from typing import Callable

from . import BackendError, MemoryBackend
from .null import NullBackend

try:
    from .leanrag import LeanRAGBackend

    _LEANRAG_AVAILABLE = True
except ImportError:
    _LEANRAG_AVAILABLE = False

BackendFactory = Callable[[], MemoryBackend]

_REGISTRY: dict[str, BackendFactory] = {
    "null": lambda: NullBackend(),
}

# Register LeanRAG if available
if _LEANRAG_AVAILABLE:
    _REGISTRY["leanrag"] = lambda: LeanRAGBackend()


def register_backend(name: str, factory: BackendFactory) -> None:
    """Register a backend factory."""
    _REGISTRY[name] = factory


def get_backend(name: str) -> MemoryBackend:
    """Instantiate a backend by name."""
    try:
        factory = _REGISTRY[name]
    except KeyError as exc:
        raise BackendError(f"Backend '{name}' is not registered") from exc
    return factory()


def list_backends() -> list[str]:
    """List registered backend names."""
    return sorted(_REGISTRY)


def resolve_backend(name: str | None = None) -> MemoryBackend:
    """Resolve backend by explicit name or unified config (default: null).

    Resolution priority:
    1. Explicit name parameter
    2. WATERCOOLER_MEMORY_BACKEND env var
    3. TOML config: [memory].backend
    4. WC_MEMORY_BACKEND env var (legacy)
    5. Default: "null"
    """
    if name:
        return get_backend(name)

    # Try unified config first
    try:
        from watercooler.memory_config import get_memory_backend
        backend_name = get_memory_backend()
        if backend_name and backend_name != "null":
            return get_backend(backend_name)
    except ImportError:
        pass

    # Legacy fallback
    backend_name = os.environ.get("WC_MEMORY_BACKEND", "null")
    return get_backend(backend_name)


def auto_register_builtin() -> None:
    """Attempt to register built-in adapters if available."""
    # LeanRAG (optional)
    if "leanrag" not in _REGISTRY:
        try:
            from .leanrag import LeanRAGBackend, LeanRAGConfig  # type: ignore

            def _create_leanrag():
                """Create LeanRAG backend with unified config."""
                try:
                    config = LeanRAGConfig.from_unified()
                except Exception:
                    config = None  # Fall back to defaults
                return LeanRAGBackend(config)

            register_backend("leanrag", _create_leanrag)
        except Exception as exc:  # noqa: BLE001
            warnings.warn(f"Skipping LeanRAG backend registration: {exc}")

    # Graphiti (optional)
    if "graphiti" not in _REGISTRY:
        try:
            from .graphiti import GraphitiBackend, GraphitiConfig  # type: ignore

            def _create_graphiti():
                """Create Graphiti backend with unified config."""
                try:
                    config = GraphitiConfig.from_unified()
                except Exception:
                    config = None  # Fall back to defaults
                return GraphitiBackend(config)

            register_backend("graphiti", _create_graphiti)
        except Exception as exc:  # noqa: BLE001
            warnings.warn(f"Skipping Graphiti backend registration: {exc}")

