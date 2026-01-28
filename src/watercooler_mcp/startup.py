"""Startup utilities for watercooler MCP server.

Contains initialization checks and auto-start logic for external services.

Services are started in background threads to avoid blocking MCP initialization.
Use get_service_status() to check current status of all services.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from .helpers import _add_startup_warning
from .observability import log_debug

# llama.cpp GitHub releases URL pattern
LLAMA_CPP_RELEASE_URL = "https://github.com/ggml-org/llama.cpp/releases/latest"


class ServiceState(Enum):
    """Service lifecycle states."""
    UNKNOWN = "unknown"
    DISABLED = "disabled"
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"
    NOT_CONFIGURED = "not_configured"


@dataclass
class ServiceStatus:
    """Status of a single service."""
    name: str
    state: ServiceState = ServiceState.UNKNOWN
    message: str = ""
    endpoint: str = ""
    started_at: Optional[float] = None
    ready_at: Optional[float] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "state": self.state.value,
            "message": self.message,
            "endpoint": self.endpoint,
            "started_at": self.started_at,
            "ready_at": self.ready_at,
            "startup_time_ms": int((self.ready_at - self.started_at) * 1000)
                if self.started_at and self.ready_at else None,
        }


# Module-level service status tracking
_service_status: dict[str, ServiceStatus] = {
    "ollama": ServiceStatus(name="ollama"),
    "embedding": ServiceStatus(name="embedding"),
    "falkordb": ServiceStatus(name="falkordb"),
}
_status_lock = threading.Lock()


def get_service_status() -> dict[str, dict]:
    """Get current status of all services.

    Returns:
        Dictionary mapping service name to status dict.
    """
    with _status_lock:
        return {name: status.to_dict() for name, status in _service_status.items()}


def _update_service_status(
    name: str,
    state: ServiceState,
    message: str = "",
    endpoint: str = "",
    started_at: Optional[float] = None,
    ready_at: Optional[float] = None,
) -> None:
    """Update status for a service."""
    with _status_lock:
        if name in _service_status:
            status = _service_status[name]
            status.state = state
            if message:
                status.message = message
            if endpoint:
                status.endpoint = endpoint
            if started_at is not None:
                status.started_at = started_at
            if ready_at is not None:
                status.ready_at = ready_at


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


def _check_ollama_health(api_base: str, timeout: float = 2.0) -> bool:
    """Check if Ollama is responding."""
    models_url = f"{api_base}/models"
    try:
        req = urllib.request.Request(
            models_url,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _check_ollama_model_available(model_name: str) -> bool:
    """Check if a model is already pulled in Ollama.

    Args:
        model_name: Model name (e.g., "qwen3:30b")

    Returns:
        True if model is available locally
    """
    ollama_path = shutil.which("ollama")
    if not ollama_path:
        return False

    try:
        result = subprocess.run(
            [ollama_path, "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            # Parse output - model names are in first column
            for line in result.stdout.strip().split("\n")[1:]:  # Skip header
                if line.strip():
                    available_model = line.split()[0]
                    # Check exact match or base name match
                    if available_model == model_name:
                        return True
                    # Handle tag matching (qwen3:30b matches qwen3:30b-q4_K_M)
                    if ":" in model_name:
                        base = model_name.split(":")[0]
                        if available_model.startswith(base + ":"):
                            return True
        return False
    except (subprocess.TimeoutExpired, Exception):
        return False


def _pull_ollama_model(model_name: str) -> bool:
    """Pull an Ollama model if not already available.

    Args:
        model_name: Model name to pull (e.g., "qwen3:30b")

    Returns:
        True if model is now available
    """
    ollama_path = shutil.which("ollama")
    if not ollama_path:
        log_debug("Ollama binary not found, cannot pull model")
        return False

    # Check if already available
    if _check_ollama_model_available(model_name):
        log_debug(f"Ollama model {model_name} already available")
        return True

    log_debug(f"Pulling Ollama model: {model_name}")
    try:
        # Pull can take a long time for large models
        result = subprocess.run(
            [ollama_path, "pull", model_name],
            capture_output=True,
            text=True,
            timeout=1800,  # 30 minute timeout for large models
        )
        if result.returncode == 0:
            log_debug(f"Successfully pulled Ollama model: {model_name}")
            return True
        else:
            log_debug(f"Failed to pull Ollama model {model_name}: {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        log_debug(f"Timeout pulling Ollama model: {model_name}")
        return False
    except Exception as e:
        log_debug(f"Error pulling Ollama model {model_name}: {e}")
        return False


def _ollama_startup_worker(api_base: str, model_name: Optional[str] = None) -> None:
    """Background worker to start Ollama and ensure model is available.

    Prefers 'ollama serve' over systemctl because:
    - No root/sudo permissions required
    - Works consistently across Linux, macOS
    - start_new_session=True ensures process survives parent exit

    After starting Ollama, pulls the configured model if not already available.

    Args:
        api_base: Ollama API base URL
        model_name: Model to ensure is available (e.g., "qwen3:30b")
    """
    start_time = time.time()
    _update_service_status("ollama", ServiceState.STARTING, endpoint=api_base, started_at=start_time)

    ollama_started = False

    # Method 1 (preferred): Try ollama serve directly - no root permissions needed
    ollama_path = shutil.which("ollama")
    if ollama_path:
        try:
            subprocess.Popen(
                [ollama_path, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True  # Detach from parent process
            )
            # Wait for it to be ready (up to 30s)
            for _ in range(60):
                time.sleep(0.5)
                if _check_ollama_health(api_base):
                    ollama_started = True
                    log_debug("Ollama started successfully via ollama serve.")
                    break
        except Exception as e:
            log_debug(f"ollama serve failed: {e}")

    # Method 2 (fallback): Try systemctl (Linux with systemd, if ollama serve fails)
    if not ollama_started:
        try:
            result = subprocess.run(
                ["systemctl", "start", "ollama"],
                capture_output=True,
                timeout=5
            )
            if result.returncode == 0:
                # Wait for it to be ready (up to 30s)
                for _ in range(60):
                    time.sleep(0.5)
                    if _check_ollama_health(api_base):
                        ollama_started = True
                        log_debug("Ollama started successfully via systemctl.")
                        break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    if not ollama_started:
        # Failed to start
        system = platform.system().lower()
        if system == "windows":
            install_cmd = "winget install Ollama.Ollama"
        elif system == "darwin":
            install_cmd = "brew install ollama"
        else:
            install_cmd = "curl -fsSL https://ollama.com/install.sh | sh"

        _update_service_status(
            "ollama", ServiceState.FAILED,
            message=f"Could not start. Install with: {install_cmd}"
        )
        log_debug("Ollama auto-start failed")
        return

    # Ollama is running - now ensure the model is available
    if model_name:
        _update_service_status(
            "ollama", ServiceState.STARTING,
            message=f"Pulling model: {model_name}"
        )
        if _pull_ollama_model(model_name):
            _update_service_status(
                "ollama", ServiceState.RUNNING,
                message=f"Model: {model_name}",
                ready_at=time.time()
            )
        else:
            _update_service_status(
                "ollama", ServiceState.RUNNING,
                message=f"Running (model pull failed: {model_name})",
                ready_at=time.time()
            )
    else:
        _update_service_status(
            "ollama", ServiceState.RUNNING,
            message="Started (no model configured)",
            ready_at=time.time()
        )


def ensure_ollama_running() -> None:
    """Start Ollama if graph features are enabled and it's not running.

    This is non-blocking - spawns a background thread if Ollama needs to start.
    Also ensures the configured model is available (pulls if needed).
    Check get_service_status()["ollama"] to see current state.
    """
    try:
        from .config import get_watercooler_config
        from watercooler.memory_config import resolve_baseline_graph_llm_config

        config = get_watercooler_config()
        graph_config = config.mcp.graph

        # Only auto-start if graph features are enabled
        if not (graph_config.generate_summaries or graph_config.generate_embeddings):
            _update_service_status("ollama", ServiceState.DISABLED, message="Graph features disabled")
            return

        # Get configured LLM API base and model from unified config
        llm_config = resolve_baseline_graph_llm_config()
        api_base = llm_config.api_base.rstrip("/")
        model_name = llm_config.model  # e.g., "qwen3:30b"

        # Only attempt auto-start for localhost URLs
        if not _is_localhost_url(api_base):
            _update_service_status(
                "ollama", ServiceState.NOT_CONFIGURED,
                message=f"Remote endpoint: {api_base}",
                endpoint=api_base
            )
            log_debug(f"LLM API base is not localhost ({api_base}), skipping Ollama auto-start")
            return

        # Check if already running
        if _check_ollama_health(api_base):
            log_debug(f"Ollama already running at {api_base}")
            # Even if running, ensure model is available in background
            if model_name:
                log_debug(f"Checking if model {model_name} needs to be pulled...")
                thread = threading.Thread(
                    target=_ensure_model_available_worker,
                    args=(model_name, api_base),
                    daemon=True,
                    name="ollama-model-check"
                )
                thread.start()
            else:
                _update_service_status(
                    "ollama", ServiceState.RUNNING,
                    message="Already running",
                    endpoint=api_base,
                    ready_at=time.time()
                )
            return

        # Start in background thread (will also pull model)
        log_debug(f"Starting Ollama in background (model: {model_name})...")
        thread = threading.Thread(
            target=_ollama_startup_worker,
            args=(api_base, model_name),
            daemon=True,
            name="ollama-startup"
        )
        thread.start()

    except Exception as e:
        _update_service_status("ollama", ServiceState.FAILED, message=str(e))
        log_debug(f"Ollama auto-start check failed: {e}")


def _ensure_model_available_worker(model_name: str, api_base: str) -> None:
    """Background worker to ensure model is available when Ollama is already running."""
    start_time = time.time()
    _update_service_status(
        "ollama", ServiceState.STARTING,
        endpoint=api_base,
        started_at=start_time,
        message=f"Checking model: {model_name}"
    )

    if _pull_ollama_model(model_name):
        _update_service_status(
            "ollama", ServiceState.RUNNING,
            message=f"Model: {model_name}",
            ready_at=time.time()
        )
    else:
        _update_service_status(
            "ollama", ServiceState.RUNNING,
            message=f"Running (model pull failed: {model_name})",
            ready_at=time.time()
        )


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


def _find_llama_server() -> Optional[Path]:
    """Find llama-server binary in PATH or common locations.

    Checks:
    1. System PATH (via shutil.which)
    2. ~/.local/bin/llama-server (user-local install)
    3. /usr/local/bin/llama-server (system install)
    4. ~/.watercooler/bin/llama-server (watercooler-managed)

    Returns:
        Path to llama-server binary if found, None otherwise
    """
    # Check PATH first
    binary = shutil.which("llama-server")
    if binary:
        log_debug(f"Found llama-server in PATH: {binary}")
        return Path(binary)

    # Check common install locations
    common_locations = [
        Path.home() / ".local" / "bin" / "llama-server",
        Path("/usr/local/bin/llama-server"),
        Path.home() / ".watercooler" / "bin" / "llama-server",
    ]

    for location in common_locations:
        if location.exists() and location.is_file():
            log_debug(f"Found llama-server at: {location}")
            return location

    log_debug("llama-server binary not found")
    return None


def _download_llama_server() -> Optional[Path]:
    """Download llama-server binary from GitHub releases.

    Downloads the latest release for the current platform and extracts
    llama-server to ~/.watercooler/bin/.

    Supported platforms:
    - Linux x86_64
    - macOS arm64 (Apple Silicon)
    - macOS x86_64 (Intel)

    Returns:
        Path to downloaded llama-server binary, or None if download failed
    """
    import json
    import tarfile
    import zipfile
    from urllib.error import HTTPError

    # Determine platform
    system = platform.system().lower()
    machine = platform.machine().lower()

    # Map platform to release asset pattern
    # Release assets follow pattern: llama-<version>-bin-<platform>.zip
    if system == "linux" and machine in ("x86_64", "amd64"):
        asset_pattern = "ubuntu-x64"
        archive_type = "zip"
    elif system == "darwin" and machine == "arm64":
        asset_pattern = "macos-arm64"
        archive_type = "zip"
    elif system == "darwin" and machine in ("x86_64", "amd64"):
        asset_pattern = "macos-x64"
        archive_type = "zip"
    else:
        log_debug(f"Unsupported platform for llama-server download: {system}/{machine}")
        return None

    # Create target directory
    bin_dir = Path.home() / ".watercooler" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    target_binary = bin_dir / "llama-server"

    try:
        # Get latest release info from GitHub API
        api_url = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
        log_debug(f"Fetching latest llama.cpp release info from: {api_url}")

        req = urllib.request.Request(
            api_url,
            headers={
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "watercooler-cloud",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            release_info = json.loads(resp.read().decode())

        # Find the matching asset
        download_url = None
        for asset in release_info.get("assets", []):
            name = asset.get("name", "")
            if asset_pattern in name and name.endswith(".zip"):
                download_url = asset.get("browser_download_url")
                log_debug(f"Found matching asset: {name}")
                break

        if not download_url:
            log_debug(f"No matching release asset found for pattern: {asset_pattern}")
            return None

        # Download the archive
        log_debug(f"Downloading llama-server from: {download_url}")
        archive_path = bin_dir / f"llama-cpp-{asset_pattern}.zip"

        req = urllib.request.Request(
            download_url,
            headers={"User-Agent": "watercooler-cloud"},
        )
        with urllib.request.urlopen(req, timeout=300) as resp:  # 5 min timeout for large download
            with open(archive_path, "wb") as f:
                f.write(resp.read())

        log_debug(f"Downloaded archive to: {archive_path}")

        # Extract llama-server from archive
        if archive_type == "zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                # Find llama-server in the archive
                for name in zf.namelist():
                    if name.endswith("llama-server") or name.endswith("llama-server.exe"):
                        log_debug(f"Extracting {name} from archive")
                        # Extract to temp location first
                        extracted = zf.extract(name, bin_dir)
                        extracted_path = Path(extracted)
                        # Move to target location
                        if extracted_path != target_binary:
                            extracted_path.rename(target_binary)
                        break
                else:
                    log_debug("llama-server not found in archive")
                    archive_path.unlink()
                    return None

        # Make executable
        target_binary.chmod(0o755)

        # Clean up archive
        archive_path.unlink()

        log_debug(f"llama-server installed to: {target_binary}")
        return target_binary

    except HTTPError as e:
        log_debug(f"HTTP error downloading llama-server: {e}")
        return None
    except Exception as e:
        log_debug(f"Error downloading llama-server: {e}")
        return None


def _start_embedding_direct(
    model_path: Path,
    host: str,
    port: int,
    n_ctx: int = 8192,
) -> bool:
    """Start embedding server as a detached background process.

    Uses llama-server with Jay's proven configuration for batch embeddings:
    - --parallel 8: Allow 8 concurrent requests
    - -c 8192: Context window (matches bge-m3)
    - -b 4096: Batch size for prompt processing
    - -ub 4096: Micro-batch size for prompt processing
    - --embedding: Enable embedding mode

    These flags are critical for Graphiti's create_batch() calls which send
    multiple strings in a single API request.

    Falls back to Python module if llama-server is not available.

    Args:
        model_path: Path to GGUF model file
        host: Host to bind to
        port: Port to listen on
        n_ctx: Context window size

    Returns:
        True if server started successfully
    """
    api_base = f"http://{host}:{port}/v1"

    # Method 1: Try llama-server binary (faster startup, better batch support)
    llama_server = _find_llama_server()
    if not llama_server:
        log_debug("llama-server not found, attempting download...")
        llama_server = _download_llama_server()

    if llama_server:
        try:
            # Jay's proven configuration for batch embeddings
            cmd = [
                str(llama_server),
                "--model", str(model_path),
                "--host", host,
                "--port", str(port),
                "--embedding",         # Enable embedding mode
                "--parallel", "8",     # Allow 8 concurrent requests
                "-c", str(n_ctx),      # Context window (8192 for bge-m3)
                "-b", "4096",          # Batch size for prompt processing
                "-ub", "4096",         # Micro-batch size for prompt processing
            ]
            log_debug(f"Starting embedding server with batch support: {' '.join(cmd)}")

            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # Detach from parent
            )

            if _wait_for_embedding_ready(api_base, max_wait=30.0):
                log_debug("Embedding server started successfully via llama-server (batch mode)")
                return True

        except Exception as e:
            log_debug(f"llama-server start failed: {e}")

    # Method 2: Fall back to Python module (no batch support)
    try:
        cmd = [
            sys.executable,
            "-m", "watercooler_memory.embedding_server",
            "--model", str(model_path),
            "--host", host,
            "--port", str(port),
            "--n-ctx", str(n_ctx),
        ]
        log_debug(f"Starting embedding server via Python (fallback, no batch): {' '.join(cmd)}")

        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        if _wait_for_embedding_ready(api_base, max_wait=30.0):
            log_debug("Embedding server started successfully via Python module")
            _add_startup_warning(
                "Embedding server running in fallback mode (Python).\n"
                "Batch embeddings may fail. Install llama-server for full support:\n"
                "  # Build from source or download from:\n"
                "  # https://github.com/ggml-org/llama.cpp/releases"
            )
            return True

    except Exception as e:
        log_debug(f"Python embedding server start failed: {e}")

    return False


def _start_embedding_windows(
    model_path: Path,
    host: str,
    port: int,
    n_ctx: int = 8192,
) -> bool:
    """Start embedding server on Windows.

    Windows doesn't have start_new_session equivalent, so we try pythonw.exe
    with DETACHED_PROCESS flag. Falls back to guidance if that fails.

    Args:
        model_path: Path to GGUF model file
        host: Host to bind to
        port: Port to listen on
        n_ctx: Context window size

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
                "--n-ctx", str(n_ctx),
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
    context_size: int = 8192,
) -> bool:
    """Ensure embedding service is running, starting it if needed.

    Resolves model name, downloads model if needed, and starts the server.
    Also auto-sets EMBEDDING_DIM to prevent graphiti-core index mismatch.

    Args:
        model_name: Friendly model name (e.g., "bge-m3")
        api_base: Target API base URL
        context_size: Context window size for embedding server (tokens)

    Returns:
        True if service is available
    """
    from urllib.parse import urlparse

    from watercooler.models import (
        ModelDownloadError,
        ModelNotFoundError,
        ensure_model_available,
        get_model_dimension,
        is_ollama_embedding_model as is_ollama_model,
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
        return _start_embedding_direct(model_path, host, port, context_size)

    elif system == "darwin":
        # macOS: direct process only
        return _start_embedding_direct(model_path, host, port, context_size)

    elif system == "windows":
        # Windows: try pythonw, provide guidance if fails
        return _start_embedding_windows(model_path, host, port, context_size)

    else:
        log_debug(f"Unknown platform {system}, trying direct start")
        return _start_embedding_direct(model_path, host, port, context_size)


def _embedding_startup_worker(model_name: str, api_base: str, context_size: int) -> None:
    """Background worker to start embedding service and wait for it to be ready."""
    start_time = time.time()
    _update_service_status("embedding", ServiceState.STARTING, endpoint=api_base, started_at=start_time)

    if _ensure_embedding_service_available(model_name, api_base, context_size):
        _update_service_status(
            "embedding", ServiceState.RUNNING,
            message=f"Model: {model_name}",
            ready_at=time.time()
        )
        log_debug("Embedding service started successfully")
    else:
        _update_service_status(
            "embedding", ServiceState.FAILED,
            message="Could not start. Run: python -m watercooler_memory.embedding_server"
        )
        log_debug("Embedding auto-start failed")


def ensure_embedding_running() -> None:
    """Start embedding service if graph features are enabled and it's not running.

    This is non-blocking - spawns a background thread if embedding service needs to start.
    Check get_service_status()["embedding"] to see current state.

    Features:
    - Auto-downloads model from HuggingFace on first use
    - Auto-starts llama.cpp or Python embedding server
    - Auto-sets EMBEDDING_DIM to match model
    - Works on Linux, macOS, Windows (with platform-specific handling)
    """
    try:
        from .config import get_watercooler_config
        from watercooler.memory_config import resolve_baseline_graph_embedding_config

        config = get_watercooler_config()
        graph_config = config.mcp.graph

        # Only auto-start if graph features are enabled and embedding generation is on
        if not graph_config.generate_embeddings:
            _update_service_status("embedding", ServiceState.DISABLED, message="Embedding generation disabled")
            log_debug("Embedding generation disabled, skipping auto-start")
            return

        # Get configured embedding API base from unified config
        embed_config = resolve_baseline_graph_embedding_config()
        api_base = embed_config.api_base.rstrip("/")
        model_name = embed_config.model

        log_debug(f"Embedding config: api_base={api_base}, model={model_name}")

        # Only attempt auto-start for localhost URLs
        if not _is_localhost_url(api_base):
            _update_service_status(
                "embedding", ServiceState.NOT_CONFIGURED,
                message=f"Remote endpoint: {api_base}",
                endpoint=api_base
            )
            log_debug(f"Embedding API base is not localhost ({api_base}), skipping auto-start")
            return

        # Check if embedding service is already responding
        if _check_embedding_health(api_base):
            _update_service_status(
                "embedding", ServiceState.RUNNING,
                message=f"Already running, model: {model_name}",
                endpoint=api_base,
                ready_at=time.time()
            )
            log_debug(f"Embedding service already running at {api_base}")
            return

        # Check if this is an Ollama endpoint (same port as LLM)
        from watercooler.memory_config import resolve_baseline_graph_llm_config

        llm_config = resolve_baseline_graph_llm_config()
        llm_api_base = llm_config.api_base.rstrip("/")

        # If embedding uses same endpoint as LLM, Ollama should handle both
        if api_base == llm_api_base:
            _update_service_status(
                "embedding", ServiceState.NOT_CONFIGURED,
                message="Using Ollama endpoint",
                endpoint=api_base
            )
            log_debug("Embedding uses same endpoint as LLM, Ollama should serve both")
            # Still set EMBEDDING_DIM for Ollama models
            from watercooler.models import is_ollama_embedding_model as is_ollama_model

            if is_ollama_model(model_name):
                if "nomic" in model_name.lower():
                    os.environ.setdefault("EMBEDDING_DIM", "768")
            return

        # Start in background thread
        log_debug(f"Embedding service not available at {api_base}, starting in background...")
        context_size = embed_config.context_size

        thread = threading.Thread(
            target=_embedding_startup_worker,
            args=(model_name, api_base, context_size),
            daemon=True,
            name="embedding-startup"
        )
        thread.start()

    except Exception as e:
        _update_service_status("embedding", ServiceState.FAILED, message=str(e))
        log_debug(f"Embedding auto-start check failed: {e}")


def _check_falkordb_health(host: str, port: int, timeout: float = 2.0) -> bool:
    """Check if FalkorDB is responding.

    Args:
        host: FalkorDB host
        port: FalkorDB port
        timeout: Connection timeout in seconds

    Returns:
        True if FalkorDB is responding to PING
    """
    import socket

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        # Send Redis PING command
        sock.send(b"*1\r\n$4\r\nPING\r\n")
        response = sock.recv(32)
        sock.close()
        return b"+PONG" in response
    except (socket.error, socket.timeout, OSError):
        return False


def _wait_for_falkordb_ready(
    host: str,
    port: int,
    max_wait: float = 30.0,
    poll_interval: float = 1.0,
) -> bool:
    """Wait for FalkorDB to become ready.

    Args:
        host: FalkorDB host
        port: FalkorDB port
        max_wait: Maximum time to wait in seconds
        poll_interval: Time between health checks

    Returns:
        True if FalkorDB became ready, False if timeout
    """
    elapsed = 0.0
    while elapsed < max_wait:
        if _check_falkordb_health(host, port):
            return True
        time.sleep(poll_interval)
        elapsed += poll_interval
    return False


def _falkordb_startup_worker(host: str, port: int) -> None:
    """Background worker to start FalkorDB and wait for it to be ready."""
    start_time = time.time()
    endpoint = f"{host}:{port}"
    _update_service_status("falkordb", ServiceState.STARTING, endpoint=endpoint, started_at=start_time)

    # Check if Docker is available
    docker_path = shutil.which("docker")
    if not docker_path:
        _update_service_status(
            "falkordb", ServiceState.FAILED,
            message="Docker not found. Install Docker to use FalkorDB."
        )
        return

    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=falkordb", "--format", "{{.Status}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        container_status = result.stdout.strip()

        if container_status:
            # Container exists - try to start it
            if "Exited" in container_status or "Created" in container_status:
                log_debug("Starting existing FalkorDB container...")
                result = subprocess.run(
                    ["docker", "start", "falkordb"],
                    capture_output=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    if _wait_for_falkordb_ready(host, port, max_wait=60.0):
                        _update_service_status(
                            "falkordb", ServiceState.RUNNING,
                            message="Container started",
                            ready_at=time.time()
                        )
                        log_debug("FalkorDB container started successfully")
                        return
            elif "Up" in container_status:
                # Container is running but not responding - might be loading
                log_debug("FalkorDB container is up, waiting for it to be ready...")
                if _wait_for_falkordb_ready(host, port, max_wait=60.0):
                    _update_service_status(
                        "falkordb", ServiceState.RUNNING,
                        message="Container ready",
                        ready_at=time.time()
                    )
                    log_debug("FalkorDB is now ready")
                    return
        else:
            # Container doesn't exist - create and start it
            log_debug("Creating new FalkorDB container...")
            result = subprocess.run(
                [
                    "docker", "run", "-d",
                    "-p", f"{port}:6379",
                    "-p", "3000:3000",
                    "--name", "falkordb",
                    "-v", "falkordb_data:/var/lib/falkordb/data",
                    "-e", "FALKORDB_ARGS=TIMEOUT 120000",
                    "falkordb/falkordb:latest",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                if _wait_for_falkordb_ready(host, port, max_wait=60.0):
                    _update_service_status(
                        "falkordb", ServiceState.RUNNING,
                        message="Container created",
                        ready_at=time.time()
                    )
                    log_debug("FalkorDB container created and started successfully")
                    return
                else:
                    log_debug("FalkorDB container created but not responding")
            else:
                log_debug(f"Failed to create FalkorDB container: {result.stderr}")

    except subprocess.TimeoutExpired:
        log_debug("Docker command timed out")
    except Exception as e:
        log_debug(f"Docker command failed: {e}")

    # If we get here, auto-start failed
    _update_service_status(
        "falkordb", ServiceState.FAILED,
        message="Could not start. Run: docker start falkordb"
    )


def ensure_falkordb_running() -> None:
    """Start FalkorDB if Graphiti backend is enabled and it's not running.

    This is non-blocking - spawns a background thread if FalkorDB needs to start.
    Check get_service_status()["falkordb"] to see current state.

    Requires Docker to be installed and accessible.
    """
    try:
        from watercooler.memory_config import get_memory_backend, resolve_database_config

        # Only auto-start if Graphiti backend is enabled
        backend = get_memory_backend()
        if backend != "graphiti":
            _update_service_status("falkordb", ServiceState.DISABLED, message=f"Backend is '{backend}'")
            log_debug(f"Memory backend is '{backend}', skipping FalkorDB auto-start")
            return

        # Get database config
        db_config = resolve_database_config()
        host = db_config.host
        port = db_config.port
        endpoint = f"{host}:{port}"

        # Only auto-start for localhost
        if host not in ("localhost", "127.0.0.1", "::1"):
            _update_service_status(
                "falkordb", ServiceState.NOT_CONFIGURED,
                message=f"Remote host: {host}",
                endpoint=endpoint
            )
            log_debug(f"FalkorDB host is not localhost ({host}), skipping auto-start")
            return

        # Check if FalkorDB is already running
        if _check_falkordb_health(host, port):
            _update_service_status(
                "falkordb", ServiceState.RUNNING,
                message="Already running",
                endpoint=endpoint,
                ready_at=time.time()
            )
            log_debug(f"FalkorDB already running at {host}:{port}")
            return

        # Start in background thread
        log_debug(f"FalkorDB not responding at {host}:{port}, starting in background...")
        thread = threading.Thread(
            target=_falkordb_startup_worker,
            args=(host, port),
            daemon=True,
            name="falkordb-startup"
        )
        thread.start()

    except Exception as e:
        _update_service_status("falkordb", ServiceState.FAILED, message=str(e))
        log_debug(f"FalkorDB auto-start check failed: {e}")
