"""Startup utilities for watercooler MCP server.

Contains initialization checks and auto-start logic for external services.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from .helpers import _add_startup_warning
from .observability import log_debug


def check_first_run() -> None:
    """Check if this is first run and suggest config initialization."""
    try:
        from watercooler.config_loader import get_config_paths

        paths = get_config_paths()
        user_config = paths.get("user_config")
        project_config = paths.get("project_config")

        # Check if any config file exists
        has_config = (
            (user_config and user_config.exists()) or
            (project_config and project_config.exists())
        )

        if not has_config:
            _add_startup_warning(
                "No config file found. Create one to customize settings:\n"
                "  uvx watercooler-cloud config init --user\n"
                "Using built-in defaults for now."
            )
    except Exception:
        # Don't let config check errors break server startup
        pass


def _is_localhost_url(url: str) -> bool:
    """Check if URL points to localhost."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        host = parsed.netloc.split(":")[0].lower()
        return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")
    except Exception:
        return False


def ensure_ollama_running() -> None:
    """Start Ollama if graph features are enabled and it's not running.

    This reduces friction for new users - if they have Ollama installed
    and graph features enabled, we'll start it automatically.

    Only attempts auto-start for localhost URLs (won't try to start remote services).
    """
    try:
        from .config import get_watercooler_config
        from watercooler.memory_config import resolve_baseline_graph_llm_config

        config = get_watercooler_config()
        graph_config = config.mcp.graph

        # Only auto-start if graph features are enabled
        if not (graph_config.generate_summaries or graph_config.generate_embeddings):
            return

        # Get configured LLM API base from unified config
        llm_config = resolve_baseline_graph_llm_config()
        api_base = llm_config.api_base.rstrip("/")

        # Only attempt auto-start for localhost URLs
        if not _is_localhost_url(api_base):
            log_debug(f"LLM API base is not localhost ({api_base}), skipping Ollama auto-start")
            return

        models_url = f"{api_base}/models"

        # Check if LLM service is already responding
        try:
            req = urllib.request.Request(
                models_url,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return  # Already running
        except (urllib.error.URLError, TimeoutError, OSError):
            pass  # Not running, try to start

        # Try to start Ollama
        log_debug("Starting Ollama for graph features...")

        # Method 1: Try systemctl (Linux with systemd)
        try:
            result = subprocess.run(
                ["systemctl", "start", "ollama"],
                capture_output=True,
                timeout=5
            )
            if result.returncode == 0:
                # Wait for it to be ready
                for _ in range(10):
                    time.sleep(0.5)
                    try:
                        req = urllib.request.Request(models_url)
                        with urllib.request.urlopen(req, timeout=2):
                            log_debug("Ollama started successfully via systemctl.")
                            return
                    except (urllib.error.URLError, TimeoutError, OSError):
                        continue
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Method 2: Try ollama serve directly (macOS, or Linux without systemd)
        try:
            # Check if ollama command exists
            result = subprocess.run(
                ["which", "ollama"],
                capture_output=True,
                timeout=2
            )
            if result.returncode == 0:
                # Start ollama serve in background
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
                # Wait for it to be ready
                for _ in range(10):
                    time.sleep(0.5)
                    try:
                        req = urllib.request.Request(models_url)
                        with urllib.request.urlopen(req, timeout=2):
                            log_debug("Ollama started successfully via ollama serve.")
                            return
                    except (urllib.error.URLError, TimeoutError, OSError):
                        continue
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # If we get here, couldn't start Ollama - give platform-aware guidance
        import platform
        system = platform.system().lower()

        if system == "windows":
            install_cmd = "winget install Ollama.Ollama"
            alt_msg = "Or download from: https://ollama.com/download/windows\n"
        elif system == "darwin":
            install_cmd = "brew install ollama"
            alt_msg = "Or: curl -fsSL https://ollama.com/install.sh | sh\n"
        else:  # Linux
            install_cmd = "curl -fsSL https://ollama.com/install.sh | sh"
            alt_msg = ""

        msg = (
            "Ollama not available - graph features (summaries/embeddings) disabled.\n"
            "To enable AI-powered summaries and semantic search:\n"
            f"  {install_cmd}\n"
        )
        if alt_msg:
            msg += f"  {alt_msg}"
        msg += (
            "Then pull models:\n"
            "  ollama pull llama3.2:3b\n"
            "  ollama pull nomic-embed-text\n"
            "Restart your IDE to reload the MCP server."
        )
        _add_startup_warning(msg)
    except Exception as e:
        # Don't let auto-start errors break server startup
        log_debug(f"Ollama auto-start check failed: {e}")


def _check_embedding_health(api_base: str, timeout: float = 2.0) -> bool:
    """Check if embedding service is responding.

    Args:
        api_base: API base URL (without /models suffix)
        timeout: Request timeout in seconds

    Returns:
        True if service is responding
    """
    models_url = f"{api_base}/models"
    try:
        req = urllib.request.Request(
            models_url,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _wait_for_embedding_ready(
    api_base: str,
    max_wait: float = 30.0,
    poll_interval: float = 0.5,
) -> bool:
    """Wait for embedding server to become ready.

    Args:
        api_base: API base URL
        max_wait: Maximum time to wait in seconds
        poll_interval: Time between health checks

    Returns:
        True if server became ready, False if timeout
    """
    elapsed = 0.0
    while elapsed < max_wait:
        if _check_embedding_health(api_base):
            return True
        time.sleep(poll_interval)
        elapsed += poll_interval
    return False


def _try_systemctl_embedding() -> bool:
    """Try to start embedding server via systemctl (Linux with systemd).

    Looks for a user service named 'watercooler-embedding'.

    Returns:
        True if successfully started via systemctl
    """
    try:
        # Check if service exists first
        result = subprocess.run(
            ["systemctl", "--user", "is-enabled", "watercooler-embedding"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode != 0:
            return False

        # Try to start it
        result = subprocess.run(
            ["systemctl", "--user", "start", "watercooler-embedding"],
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            log_debug("Started embedding server via systemctl --user")
            return True

    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return False


def _start_embedding_direct(
    model_path: Path,
    host: str,
    port: int,
    n_ctx: int = 512,
) -> bool:
    """Start embedding server as a detached background process.

    Tries llama-server binary first (faster startup), falls back to Python module.

    Args:
        model_path: Path to GGUF model file
        host: Host to bind to
        port: Port to listen on
        n_ctx: Context window size

    Returns:
        True if server started successfully
    """
    api_base = f"http://{host}:{port}/v1"

    # Method 1: Try llama-server binary (faster startup)
    llama_server = shutil.which("llama-server")
    if llama_server:
        try:
            cmd = [
                llama_server,
                "--model", str(model_path),
                "--host", host,
                "--port", str(port),
                "--embedding",
                "--ctx-size", str(n_ctx),
            ]
            log_debug(f"Starting embedding server: {' '.join(cmd)}")

            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # Detach from parent
            )

            if _wait_for_embedding_ready(api_base, max_wait=30.0):
                log_debug("Embedding server started successfully via llama-server")
                return True

        except Exception as e:
            log_debug(f"llama-server start failed: {e}")

    # Method 2: Fall back to Python module
    try:
        cmd = [
            sys.executable,
            "-m", "watercooler_memory.embedding_server",
            "--model", str(model_path),
            "--host", host,
            "--port", str(port),
        ]
        log_debug(f"Starting embedding server via Python: {' '.join(cmd)}")

        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        if _wait_for_embedding_ready(api_base, max_wait=30.0):
            log_debug("Embedding server started successfully via Python module")
            return True

    except Exception as e:
        log_debug(f"Python embedding server start failed: {e}")

    return False


def _start_embedding_windows(model_path: Path, host: str, port: int) -> bool:
    """Start embedding server on Windows.

    Windows doesn't have start_new_session equivalent, so we try pythonw.exe
    with DETACHED_PROCESS flag. Falls back to guidance if that fails.

    Args:
        model_path: Path to GGUF model file
        host: Host to bind to
        port: Port to listen on

    Returns:
        True if server started successfully
    """
    api_base = f"http://{host}:{port}/v1"

    # Try pythonw.exe with DETACHED_PROCESS
    try:
        pythonw = shutil.which("pythonw")
        if pythonw:
            cmd = [
                pythonw,
                "-m", "watercooler_memory.embedding_server",
                "--model", str(model_path),
                "--host", host,
                "--port", str(port),
            ]
            log_debug(f"Starting embedding server via pythonw: {' '.join(cmd)}")

            # Windows-specific: DETACHED_PROCESS
            DETACHED_PROCESS = 0x00000008
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=DETACHED_PROCESS,
            )

            if _wait_for_embedding_ready(api_base, max_wait=30.0):
                log_debug("Embedding server started successfully via pythonw")
                return True

    except Exception as e:
        log_debug(f"pythonw start failed: {e}")

    # Provide guidance if auto-start failed
    _add_startup_warning(
        f"Embedding server not running.\n"
        f"On Windows, start it manually in a separate terminal:\n"
        f"  python -m watercooler_memory.embedding_server --model {model_path}\n"
        f"Or configure Ollama for embeddings (simpler on Windows)."
    )
    return False


def _ensure_embedding_service_available(
    model_name: str,
    api_base: str,
) -> bool:
    """Ensure embedding service is running, starting it if needed.

    Resolves model name, downloads model if needed, and starts the server.
    Also auto-sets EMBEDDING_DIM to prevent graphiti-core index mismatch.

    Args:
        model_name: Friendly model name (e.g., "bge-m3")
        api_base: Target API base URL

    Returns:
        True if service is available
    """
    from urllib.parse import urlparse

    from watercooler.embedding_models import (
        ModelDownloadError,
        ModelNotFoundError,
        ensure_model_available,
        get_model_dimension,
        is_ollama_model,
        resolve_embedding_model,
    )

    # Check if this is an Ollama model - those are handled by ensure_ollama_running()
    if is_ollama_model(model_name):
        log_debug(f"Model {model_name} appears to be Ollama model, skipping llama.cpp start")
        return False

    # Resolve model specification
    try:
        model_spec = resolve_embedding_model(model_name)
    except ModelNotFoundError as e:
        _add_startup_warning(f"Unknown embedding model: {model_name}. {e}")
        return False

    # Auto-set EMBEDDING_DIM before any graphiti-core imports
    # This prevents index dimension mismatch errors
    dim = model_spec.get("dim", 1024)
    if "EMBEDDING_DIM" not in os.environ:
        os.environ["EMBEDDING_DIM"] = str(dim)
        log_debug(f"Auto-set EMBEDDING_DIM={dim} for model {model_name}")

    # Ensure model is downloaded
    try:
        model_path = ensure_model_available(model_name, verbose=False)
    except (ModelNotFoundError, ModelDownloadError) as e:
        _add_startup_warning(f"Could not prepare embedding model: {e}")
        return False

    log_debug(f"Model available at: {model_path}")

    # Parse API base to get host/port
    parsed = urlparse(api_base)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8080

    # Start server (platform-aware)
    system = platform.system().lower()

    if system == "linux":
        # Try systemctl first for user service
        if _try_systemctl_embedding():
            # Wait for it to be ready
            if _wait_for_embedding_ready(api_base, max_wait=10.0):
                return True
        # Fall back to direct process
        return _start_embedding_direct(model_path, host, port)

    elif system == "darwin":
        # macOS: direct process only
        return _start_embedding_direct(model_path, host, port)

    elif system == "windows":
        # Windows: try pythonw, provide guidance if fails
        return _start_embedding_windows(model_path, host, port)

    else:
        log_debug(f"Unknown platform {system}, trying direct start")
        return _start_embedding_direct(model_path, host, port)


def ensure_embedding_running() -> None:
    """Start embedding service if graph features are enabled and it's not running.

    This provides seamless embedding server management - users just specify
    `model = "bge-m3"` in config and the service starts automatically.

    Features:
    - Auto-downloads model from HuggingFace on first use
    - Auto-starts llama.cpp or Python embedding server
    - Auto-sets EMBEDDING_DIM to match model
    - Works on Linux, macOS, Windows (with platform-specific handling)

    For Ollama-based embeddings (:11434), this is a no-op if ensure_ollama_running()
    already ran. Only attempts auto-start for localhost URLs.
    """
    try:
        from .config import get_watercooler_config
        from watercooler.memory_config import resolve_baseline_graph_embedding_config

        config = get_watercooler_config()
        graph_config = config.mcp.graph

        # Only auto-start if graph features are enabled and embedding generation is on
        if not graph_config.generate_embeddings:
            log_debug("Embedding generation disabled, skipping auto-start")
            return

        # Get configured embedding API base from unified config
        embed_config = resolve_baseline_graph_embedding_config()
        api_base = embed_config.api_base.rstrip("/")
        model_name = embed_config.model

        log_debug(f"Embedding config: api_base={api_base}, model={model_name}")

        # Only attempt auto-start for localhost URLs
        if not _is_localhost_url(api_base):
            log_debug(f"Embedding API base is not localhost ({api_base}), skipping auto-start")
            return

        # Check if embedding service is already responding
        if _check_embedding_health(api_base):
            log_debug(f"Embedding service already running at {api_base}")
            return

        # Check if this is an Ollama endpoint (same port as LLM)
        from watercooler.memory_config import resolve_baseline_graph_llm_config

        llm_config = resolve_baseline_graph_llm_config()
        llm_api_base = llm_config.api_base.rstrip("/")

        # If embedding uses same endpoint as LLM, Ollama should handle both
        if api_base == llm_api_base:
            log_debug("Embedding uses same endpoint as LLM, Ollama should serve both")
            # Still set EMBEDDING_DIM for Ollama models
            from watercooler.embedding_models import is_ollama_model

            if is_ollama_model(model_name):
                # nomic-embed-text is 768 dim
                if "nomic" in model_name.lower():
                    os.environ.setdefault("EMBEDDING_DIM", "768")
            return

        # For llama.cpp embedding server, try auto-start
        log_debug(f"Embedding service not available at {api_base}, attempting auto-start")

        if _ensure_embedding_service_available(model_name, api_base):
            log_debug("Embedding service started successfully")
        else:
            # Auto-start failed, provide guidance
            from urllib.parse import urlparse

            parsed = urlparse(api_base)
            port = parsed.port or 8080

            if port == 8080:
                _add_startup_warning(
                    f"Could not auto-start embedding service at {api_base}.\n"
                    "To start manually:\n"
                    "  python -m watercooler_memory.embedding_server\n"
                    "Or use Ollama for embeddings:\n"
                    "  [memory.embedding]\n"
                    "  api_base = \"http://localhost:11434/v1\"\n"
                    "  model = \"nomic-embed-text:latest\"\n"
                    "  dim = 768"
                )
            else:
                _add_startup_warning(
                    f"Embedding service not available at {api_base}.\n"
                    "Graph embedding features will be disabled until the service is started."
                )

    except Exception as e:
        # Don't let auto-start errors break server startup
        log_debug(f"Embedding auto-start check failed: {e}")
