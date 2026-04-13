from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock

import pytest

# Re-export testing utilities from watercooler.testing
# These are now available to all tests via conftest.py
from watercooler.testing import (
    clean_config,
    isolated_config,
    mock_env_vars,
    mock_watercooler_env,
    temp_config,
    temp_threads_dir,
)

# Make pytest aware of these fixtures
__all__ = [
    "clean_config",
    "isolated_config",
    "mock_env_vars",
    "mock_watercooler_env",
    "temp_config",
    "temp_threads_dir",
]


import importlib.util as _importlib_util


def pytest_ignore_collect(collection_path, config):  # type: ignore[override]
    """Skip test files that import unavailable optional modules at collection time.

    Prevents ModuleNotFoundError collection failures on installations without
    the optional private watercooler_memory package (e.g. the public release).
    The check is content-based so new memory test files are handled automatically.
    """
    if collection_path.suffix != ".py":
        return None
    if _importlib_util.find_spec("watercooler_memory") is not None:
        return None  # package is available — collect everything normally
    try:
        content = collection_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if "watercooler_memory" in content:
        return True  # skip: would fail with ModuleNotFoundError at import time
    return None


def pytest_sessionstart(session):  # type: ignore[override]
    root = Path(__file__).resolve().parents[1]
    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    # Ensure console scripts load in editable style as well
    os.environ.setdefault("PYTHONPATH", str(src))


@pytest.fixture(scope="session")
def anyio_backend():
    """Configure anyio to use asyncio backend only.

    This is required because query_memory() uses asyncio.to_thread
    which is incompatible with trio.
    """
    return "asyncio"


# ============================================================================
# Memory Backend Test Fixtures
# ============================================================================


@pytest.fixture
def mock_context() -> MagicMock:
    """Create mock MCP context."""
    return MagicMock()


@pytest.fixture
def disable_memory_backends(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Disable all memory backends for tests that don't need them.

    Usage:
        def test_something(disable_memory_backends):
            # Memory backends are disabled, no server connections attempted
            ...
    """
    monkeypatch.setenv("WATERCOOLER_MEMORY_DISABLED", "1")
    yield


@pytest.fixture
def stub_memory_api_keys(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Set stub API keys for memory backends.

    These stubs allow tests to pass validation without making real API calls.
    Use with mocked backends to prevent outbound connections.

    Usage:
        def test_memory_config(stub_memory_api_keys):
            # API key validation passes with stub values
            ...
    """
    # Graphiti backend keys
    monkeypatch.setenv("LLM_API_KEY", "stub-llm-key-for-testing")
    monkeypatch.setenv("EMBEDDING_API_KEY", "stub-embedding-key-for-testing")
    monkeypatch.setenv("OPENAI_API_KEY", "stub-openai-key-for-testing")

    # LeanRAG backend keys
    monkeypatch.setenv("DEEPSEEK_API_KEY", "stub-deepseek-key-for-testing")

    yield


@pytest.fixture
def clean_api_keys(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Clear all API keys for complete test isolation."""
    api_keys = [
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
        "GEMINI_API_KEY", "GROQ_API_KEY", "VOYAGE_API_KEY",
        "LLM_API_KEY", "EMBEDDING_API_KEY", "DEEPSEEK_API_KEY",
    ]
    for key in api_keys:
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def stub_local_memory_servers(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Configure memory backends to use local server endpoints with stub keys.

    Sets up the environment as if local LLM and embedding servers are running.
    Does NOT actually start servers - use with mocked HTTP calls.

    Usage:
        def test_with_local_servers(stub_local_memory_servers):
            # Environment configured for local servers
            ...
    """
    # Local embedding server (port 8080)
    monkeypatch.setenv("EMBEDDING_API_BASE", "http://localhost:8080/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "not-needed-for-local")
    monkeypatch.setenv("EMBEDDING_MODEL", "bge-m3")

    # Local LLM server (port 8000)
    monkeypatch.setenv("LLM_API_BASE", "http://localhost:8000/v1")
    monkeypatch.setenv("LLM_API_KEY", "not-needed-for-local")
    monkeypatch.setenv("LLM_MODEL", "local")

    # LeanRAG aliases (same servers)
    monkeypatch.setenv("GLM_BASE_URL", "http://localhost:8080/v1")
    monkeypatch.setenv("GLM_MODEL", "bge-m3")
    monkeypatch.setenv("GLM_EMBEDDING_MODEL", "bge-m3")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "not-needed-for-local")
    monkeypatch.setenv("DEEPSEEK_MODEL", "local")

    # FalkorDB config (local instance, no auth for local dev)
    monkeypatch.setenv("FALKORDB_HOST", "localhost")
    monkeypatch.setenv("FALKORDB_PORT", "6379")
    monkeypatch.setenv("FALKORDB_PASSWORD", "")

    yield


@pytest.fixture
def memory_test_env(
    monkeypatch: pytest.MonkeyPatch,
    stub_memory_api_keys: None,
) -> Generator[None, None, None]:
    """Graphiti test environment with stub API keys.

    Sets WATERCOOLER_GRAPHITI_ENABLED=1 with stub API keys.
    Does NOT include local server endpoints - use with stub_local_memory_servers
    if you need server URL configuration.

    Use with mocked backends to prevent outbound connections.

    Usage:
        def test_graphiti_feature(memory_test_env):
            # Graphiti enabled with stub keys (no server URLs)
            ...

        def test_with_servers(memory_test_env, stub_local_memory_servers):
            # Graphiti enabled with stub keys AND local server URLs
            ...
    """
    monkeypatch.setenv("WATERCOOLER_GRAPHITI_ENABLED", "1")
    yield
