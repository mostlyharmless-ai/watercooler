"""Startup utilities for watercooler MCP server.

Contains initialization checks and auto-start logic for external services.

Services are started in background threads to avoid blocking MCP initialization.
Use get_service_status() to check current status of all services.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal, Optional

from .helpers import _add_startup_warning
from .observability import log_debug, log_warning

# llama.cpp GitHub releases URL pattern
LLAMA_CPP_RELEASE_URL = "https://github.com/ggml-org/llama.cpp/releases/latest"

# Environment variables for security configuration
ENV_LLAMA_SERVER_VERIFY = "WATERCOOLER_LLAMA_SERVER_VERIFY"  # "strict", "warn", or "skip"
ENV_LLAMA_SERVER_SHA256 = "WATERCOOLER_LLAMA_SERVER_SHA256"  # User-provided SHA256

# Environment variables for auto-provisioning (override config)
ENV_AUTO_PROVISION_MODELS = "WATERCOOLER_AUTO_PROVISION_MODELS"
ENV_AUTO_PROVISION_LLAMA_SERVER = "WATERCOOLER_AUTO_PROVISION_LLAMA_SERVER"

# Service configuration constants
DEFAULT_LLM_PORT = 8000  # Default port for llama-server (LLM completion)
DEFAULT_EMBEDDING_PORT = 8080  # Default port for llama-server (embeddings)
DEFAULT_CONTEXT_SIZE = 8192  # Default context window size (tokens)
DEFAULT_SERVICE_WAIT_TIMEOUT = 60.0  # Seconds to wait for service to become ready
DOWNLOAD_CHUNK_SIZE = 8192  # Bytes per chunk when downloading files

# Known-good SHA256 checksums for verified llama.cpp releases
# Format: {release_tag: {asset_pattern: sha256}}
# These are checksums we've verified - update when testing new releases
#
# To add a new release:
#   1. gh release download <tag> --repo ggml-org/llama.cpp --pattern "llama-*-bin-*.tar.gz"
#   2. sha256sum *.tar.gz
#   3. Add entries below
LLAMA_SERVER_CHECKSUMS: dict[str, dict[str, str]] = {
    # Release b7896 (2026-01-31) - verified checksums
    "b7896": {
        "ubuntu-x64": "329a716c5fb216d49d674d3ac7a9aab90d04942d80b08786aeaaae49a4490b93",
        "ubuntu-vulkan-x64": "85191595f05328f01de8f5852f0679a6dd8cce4271ec52d9d0cf3dca08e1ac74",
        "macos-arm64": "231f8f7ff3763de2ab1cbeb097e728e4bb442b0bc941f6dacc7ef83d01ae47bb",
        "macos-x64": "6de178b3f364734e442b4579554f102a6c36c9343cf31cdb8381c02053b2bf11",
    },
    # Release b7885 (2026-01-30) - verified checksums
    "b7885": {
        "ubuntu-x64": "6e6148e2f8908cbefdf4833e71a8113c71a1a4a14cb155375ad8c1b095d8a5e1",
        "ubuntu-vulkan-x64": "f21649deb021d7b2942227c12a05915dee476835081b65f2698aed4e93459d37",
        "macos-arm64": "608760410b9f65f91a0e9f499dc21f95cea298c59b9df1354bd6a31cad059d35",
        "macos-x64": "4794fd57522f680c17be60dc7c3ef7fb08c89a2524ee2babf3480f9f2c87ffca",
    },
    # Release b7869 (2026-01-28) - verified checksums
    "b7869": {
        "ubuntu-x64": "d35419ff41d6438338fb9942d2250e9c21ea02424e422617650bcab950575d78",
        "ubuntu-vulkan-x64": "45b73da74307eb11463e042253a506f1ccc4a714ad73b1de19630cfba876d2b8",
        "macos-arm64": "45ecd82ead1574c45ae19738e9d890c2c19bd2944b645eaf3619980d87621b51",
        "macos-x64": "d65e43f4ffb1890bc694f417871dba56374a011011da1bc4c4e8e99768d56f20",
    },
}


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
    "llm": ServiceStatus(name="llm"),           # llama-server (completion mode)
    "embedding": ServiceStatus(name="embedding"),  # llama-server (embedding mode)
    "falkordb": ServiceStatus(name="falkordb"),
}
_status_lock = threading.Lock()

# Track spawned process PIDs for cleanup
_spawned_pids: list[int] = []
_pids_lock = threading.Lock()

# Per-port startup locks to prevent race conditions
# Key: port number, Value: Lock for that port
_port_locks: dict[int, threading.Lock] = {}
_port_locks_lock = threading.Lock()  # Protects _port_locks dict itself


def _get_port_lock(port: int) -> threading.Lock:
    """Get or create a lock for a specific port.

    Prevents race condition where multiple threads try to start
    llama-server on the same port simultaneously.
    """
    with _port_locks_lock:
        if port not in _port_locks:
            _port_locks[port] = threading.Lock()
        return _port_locks[port]


def _register_spawned_pid(pid: int) -> None:
    """Register a spawned process PID for cleanup tracking."""
    with _pids_lock:
        _spawned_pids.append(pid)


def _cleanup_spawned_processes() -> None:
    """Terminate all tracked spawned processes.

    Called on module exit via atexit to clean up llama-server processes.
    """
    import signal

    with _pids_lock:
        for pid in _spawned_pids:
            try:
                os.kill(pid, signal.SIGTERM)
                log_debug(f"Sent SIGTERM to spawned process {pid}")
            except ProcessLookupError:
                pass  # Process already exited
            except OSError as e:
                log_debug(f"Failed to terminate process {pid}: {e}")
        _spawned_pids.clear()


# Register cleanup handler
import atexit
atexit.register(_cleanup_spawned_processes)


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
    message: Optional[str] = None,
    endpoint: Optional[str] = None,
    started_at: Optional[float] = None,
    ready_at: Optional[float] = None,
) -> None:
    """Update status for a service.

    Args:
        name: Service name (llm, embedding, falkordb)
        state: New service state
        message: Status message (None = keep existing, "" = clear)
        endpoint: Service endpoint URL (None = keep existing, "" = clear)
        started_at: Timestamp when service started
        ready_at: Timestamp when service became ready
    """
    with _status_lock:
        if name in _service_status:
            status = _service_status[name]
            status.state = state
            if message is not None:
                status.message = message
            if endpoint is not None:
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


def _is_auto_provision_enabled(resource: str) -> bool:
    """Check if auto-provisioning is enabled for a resource type.

    Checks environment variable first (for override), then config file.

    Args:
        resource: "models" or "llama_server"

    Returns:
        True if auto-provisioning is enabled for this resource
    """
    # Environment variable overrides (case-insensitive true/false/1/0)
    env_var = {
        "models": ENV_AUTO_PROVISION_MODELS,
        "llama_server": ENV_AUTO_PROVISION_LLAMA_SERVER,
    }.get(resource)

    if env_var:
        env_value = os.environ.get(env_var, "").lower().strip()
        if env_value in ("true", "1", "yes"):
            return True
        if env_value in ("false", "0", "no"):
            return False
        # Empty or unset - fall through to config

    # Check config file
    try:
        from .config import get_watercooler_config
        config = get_watercooler_config()
        provision_config = config.mcp.service_provision

        if resource == "models":
            return provision_config.models
        elif resource == "llama_server":
            return provision_config.llama_server
    except Exception:
        pass

    # Default to True (current behavior)
    return True


def _is_localhost_url(url: str) -> bool:
    """Check if URL points to localhost."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        host = parsed.netloc.split(":")[0].lower()
        return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")
    except Exception:
        return False


def _extract_port(url: str, default: int = DEFAULT_LLM_PORT) -> int:
    """Extract port from a URL.

    Args:
        url: URL to parse
        default: Default port if not specified

    Returns:
        Port number
    """
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        if parsed.port:
            return parsed.port
        # Default ports based on scheme
        if parsed.scheme == "https":
            return 443
        return default
    except Exception:
        return default


def _check_llm_health(api_base: str, timeout: float = 2.0) -> bool:
    """Check if LLM service (llama-server) is responding.

    Args:
        api_base: API base URL (without /models suffix)
        timeout: Request timeout in seconds

    Returns:
        True if service is responding
    """
    models_url = f"{api_base.rstrip('/')}/models"
    try:
        req = urllib.request.Request(
            models_url,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _wait_for_llm_ready(
    api_base: str,
    max_wait: float = DEFAULT_SERVICE_WAIT_TIMEOUT,
    poll_interval: float = 1.0,
) -> bool:
    """Wait for LLM server to become ready.

    Args:
        api_base: API base URL
        max_wait: Maximum time to wait in seconds
        poll_interval: Time between health checks

    Returns:
        True if server became ready, False if timeout
    """
    elapsed = 0.0
    while elapsed < max_wait:
        if _check_llm_health(api_base):
            return True
        time.sleep(poll_interval)
        elapsed += poll_interval
    return False


def _start_llama_server(
    model_path: Path,
    port: int,
    mode: str,
    context_size: int = DEFAULT_CONTEXT_SIZE,
    host: str = "127.0.0.1",
) -> bool:
    """Start llama-server for either embedding or LLM completion.

    NO FALLBACK - llama-server is required. If not found, we download it.
    If download fails, we raise an error.

    Thread-safe: Uses per-port locking to prevent race conditions when
    multiple threads try to start llama-server on the same port.

    Args:
        model_path: Path to GGUF model file
        port: Port to listen on
        mode: "embedding" or "completion"
        context_size: Context window size
        host: Host to bind to

    Returns:
        True if server started successfully (or already running)

    Raises:
        RuntimeError: If llama-server cannot be found or downloaded
    """
    # Acquire port-specific lock to prevent race conditions
    port_lock = _get_port_lock(port)
    with port_lock:
        # Re-check if service is already running after acquiring lock
        # (another thread may have started it while we were waiting)
        api_base = f"http://{host}:{port}/v1"
        if _check_llm_health(api_base):
            log_debug(f"llama-server already running on port {port} (detected after lock)")
            return True

        return _start_llama_server_unlocked(model_path, port, mode, context_size, host)


def _start_llama_server_unlocked(
    model_path: Path,
    port: int,
    mode: str,
    context_size: int = DEFAULT_CONTEXT_SIZE,
    host: str = "127.0.0.1",
) -> bool:
    """Internal: Start llama-server without locking (caller must hold port lock).

    Args:
        model_path: Path to GGUF model file
        port: Port to listen on
        mode: "embedding" or "completion"
        context_size: Context window size
        host: Host to bind to

    Returns:
        True if server started successfully

    Raises:
        RuntimeError: If llama-server cannot be found or downloaded
    """
    llama_server = _find_llama_server()
    if not llama_server:
        if _is_auto_provision_enabled("llama_server"):
            log_debug("llama-server not found, attempting download from GitHub releases...")
            llama_server = _download_llama_server()
        else:
            log_debug("llama-server not found and auto-provision disabled")

    if not llama_server:
        # Provide clear instructions based on auto-provision setting
        if _is_auto_provision_enabled("llama_server"):
            raise RuntimeError(
                "llama-server binary required but could not be downloaded. "
                "Install manually from: https://github.com/ggml-org/llama.cpp/releases"
            )
        else:
            raise RuntimeError(
                "llama-server binary not found and auto-provisioning is disabled.\n\n"
                "To enable auto-download, set in config.toml:\n"
                "  [mcp.service_provision]\n"
                "  llama_server = true\n\n"
                "Or set environment variable:\n"
                "  WATERCOOLER_AUTO_PROVISION_LLAMA_SERVER=true\n\n"
                "To install manually:\n"
                "  https://github.com/ggml-org/llama.cpp/releases\n"
                "  Extract llama-server to ~/.watercooler/bin/ or add to PATH"
            )

    cmd = [
        str(llama_server),
        "--model", str(model_path),
        "--host", host,
        "--port", str(port),
        "-c", str(context_size),
    ]

    if mode == "embedding":
        # Jay's batch-optimized flags - required for Graphiti's create_batch()
        cmd.extend([
            "--embedding",      # Enable embedding mode
            "--parallel", "8",  # Allow 8 concurrent requests
            "-b", "4096",       # Batch size for prompt processing
            "-ub", "4096",      # Micro-batch size for prompt processing
        ])
        log_debug(f"Starting llama-server in embedding mode: {' '.join(cmd)}")
    else:
        # Completion mode for LLM inference
        cmd.extend([
            "--parallel", "4",  # Allow 4 concurrent requests
        ])
        log_debug(f"Starting llama-server in completion mode: {' '.join(cmd)}")

    try:
        # Set LD_LIBRARY_PATH to include the directory containing llama-server
        # This is needed because llama.cpp shared libraries (.so files) are
        # extracted alongside the binary
        env = os.environ.copy()
        lib_dir = str(llama_server.parent)
        existing_ld_path = env.get("LD_LIBRARY_PATH", "")
        if existing_ld_path:
            env["LD_LIBRARY_PATH"] = f"{lib_dir}:{existing_ld_path}"
        else:
            env["LD_LIBRARY_PATH"] = lib_dir
        log_debug(f"Setting LD_LIBRARY_PATH={env['LD_LIBRARY_PATH']}")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # Detach from parent process
            env=env,
        )
        # Track PID for cleanup on exit
        _register_spawned_pid(proc.pid)
        log_debug(f"Started llama-server with PID {proc.pid}")
        return True
    except Exception as e:
        log_debug(f"Failed to start llama-server: {e}")
        return False


def _llm_startup_worker(model_name: str, api_base: str, context_size: int) -> None:
    """Background worker to start LLM service (llama-server) and wait for it to be ready.

    Args:
        model_name: Model name to load (resolved via GGUF registry)
        api_base: Target API base URL
        context_size: Context window size
    """
    import traceback
    from watercooler.models import (
        ModelDownloadError,
        ModelNotFoundError,
        ensure_llm_model_available,
        is_known_llm_gguf_model,
    )

    start_time = time.time()
    port = _extract_port(api_base, default=DEFAULT_LLM_PORT)
    endpoint = f"http://127.0.0.1:{port}/v1"

    _update_service_status("llm", ServiceState.STARTING, endpoint=endpoint, started_at=start_time)

    try:
        # Check if model is in our GGUF registry
        if not is_known_llm_gguf_model(model_name):
            _update_service_status(
                "llm", ServiceState.FAILED,
                message=f"Model '{model_name}' not in GGUF registry. Add to models.py or use a cloud endpoint."
            )
            log_debug(f"LLM model {model_name} not in GGUF registry")
            return

        # Download model if needed
        _update_service_status("llm", ServiceState.STARTING, message=f"Downloading model: {model_name}")
        try:
            model_path = ensure_llm_model_available(model_name, verbose=False)
        except (ModelNotFoundError, ModelDownloadError) as e:
            _update_service_status("llm", ServiceState.FAILED, message=f"Model download failed: {e}")
            log_debug(f"Failed to download LLM model {model_name}: {e}")
            return

        log_debug(f"LLM model available at: {model_path}")

        # Start llama-server in completion mode
        _update_service_status("llm", ServiceState.STARTING, message="Starting llama-server...")
        try:
            if not _start_llama_server(model_path, port, mode="completion", context_size=context_size):
                _update_service_status(
                    "llm", ServiceState.FAILED,
                    message="Failed to start llama-server process"
                )
                return
        except RuntimeError as e:
            _update_service_status("llm", ServiceState.FAILED, message=str(e))
            log_debug(f"llama-server startup error: {e}")
            return

        # Wait for server to be ready
        if _wait_for_llm_ready(endpoint, max_wait=DEFAULT_SERVICE_WAIT_TIMEOUT):
            _update_service_status(
                "llm", ServiceState.RUNNING,
                message=f"Model: {model_name}",
                ready_at=time.time()
            )
            log_debug(f"LLM service started successfully at {endpoint}")
        else:
            _update_service_status(
                "llm", ServiceState.FAILED,
                message=f"Server started but not responding after {DEFAULT_SERVICE_WAIT_TIMEOUT}s"
            )
            log_debug("LLM server started but health check timed out")

    except Exception as e:
        # Catch-all for any unexpected errors to prevent silent failures
        error_msg = f"Unexpected error in LLM startup: {type(e).__name__}: {e}"
        log_debug(f"{error_msg}\n{traceback.format_exc()}")
        _update_service_status("llm", ServiceState.FAILED, message=error_msg)


def ensure_llm_running() -> None:
    """Start llama-server for LLM if configured for localhost and not running.

    This is non-blocking - spawns a background thread if LLM service needs to start.
    Check get_service_status()["llm"] to see current state.

    Auto-starts only for localhost URLs. Remote endpoints (OpenAI, etc.) are assumed
    to be managed externally.
    """
    try:
        from .config import get_watercooler_config
        from watercooler.memory_config import resolve_baseline_graph_llm_config

        config = get_watercooler_config()
        graph_config = config.mcp.graph

        # Only auto-start if graph features are enabled
        if not (graph_config.generate_summaries or graph_config.generate_embeddings):
            _update_service_status("llm", ServiceState.DISABLED, message="Graph features disabled")
            return

        # Get configured LLM API base and model from unified config
        llm_config = resolve_baseline_graph_llm_config()
        api_base = llm_config.api_base.rstrip("/")
        model_name = llm_config.model

        # Only attempt auto-start for localhost URLs
        if not _is_localhost_url(api_base):
            _update_service_status(
                "llm", ServiceState.NOT_CONFIGURED,
                message=f"Remote endpoint: {api_base}",
                endpoint=api_base
            )
            log_debug(f"LLM API base is not localhost ({api_base}), skipping auto-start")
            return

        # Check if already running
        if _check_llm_health(api_base):
            _update_service_status(
                "llm", ServiceState.RUNNING,
                message=f"Already running, model: {model_name}",
                endpoint=api_base,
                ready_at=time.time()
            )
            log_debug(f"LLM service already running at {api_base}")
            return

        # Start in background thread
        log_debug(f"LLM service not available at {api_base}, starting in background...")

        # Get context size from model spec or use default
        from watercooler.models import get_llm_context_size
        context_size = get_llm_context_size(model_name, default=DEFAULT_CONTEXT_SIZE)

        thread = threading.Thread(
            target=_llm_startup_worker,
            args=(model_name, api_base, context_size),
            daemon=True,
            name="llm-startup"
        )
        thread.start()

    except Exception as e:
        _update_service_status("llm", ServiceState.FAILED, message=str(e))
        log_debug(f"LLM auto-start check failed: {e}")


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


def _is_shared_library(filename: str) -> bool:
    """Check if a filename is a shared library based on extension.

    Handles platform-specific library extensions:
    - Linux: .so (including versioned like .so.0, .so.0.0.123)
    - macOS: .dylib (including versioned like .0.dylib)
    - Windows: .dll

    Args:
        filename: The filename to check

    Returns:
        True if the file appears to be a shared library
    """
    # Linux .so files (libfoo.so, libfoo.so.0, libfoo.so.0.0.123)
    if ".so" in filename:
        return True
    # macOS .dylib files (libfoo.dylib, libfoo.0.dylib)
    if ".dylib" in filename:
        return True
    # Windows .dll files
    if filename.lower().endswith(".dll"):
        return True
    return False


def _is_safe_archive_path(member_name: str, dest_dir: Path) -> bool:
    """Validate that an archive member path doesn't escape the destination directory.

    Prevents path traversal attacks (e.g., ../../../etc/passwd) in archive extraction.

    Args:
        member_name: The path from the archive member
        dest_dir: The destination directory for extraction

    Returns:
        True if the path is safe (resolves within dest_dir), False otherwise
    """
    # Reject absolute paths
    if Path(member_name).is_absolute():
        log_warning(f"Rejecting absolute path in archive: {member_name}")
        return False

    # Resolve the full path and check it's within dest_dir
    try:
        full_path = (dest_dir / member_name).resolve()
        dest_resolved = dest_dir.resolve()

        # Check that the resolved path is within the destination directory
        # Using is_relative_to (Python 3.9+) for clean comparison
        if not full_path.is_relative_to(dest_resolved):
            log_warning(f"Path traversal detected in archive: {member_name}")
            return False

        return True
    except (ValueError, RuntimeError) as e:
        log_warning(f"Invalid path in archive: {member_name} - {e}")
        return False


def _has_nvidia_gpu() -> bool:
    """Check if NVIDIA GPU is available via nvidia-smi.

    Returns:
        True if nvidia-smi succeeds (NVIDIA GPU with drivers installed)
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            gpu_name = result.stdout.decode().strip().split("\n")[0]
            log_debug(f"Detected NVIDIA GPU: {gpu_name}")
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return False


def _compute_sha256(file_path: Path) -> str:
    """Compute SHA256 hash of a file.

    Args:
        file_path: Path to file to hash

    Returns:
        Hex-encoded SHA256 hash
    """
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def _get_expected_checksum(release_tag: str, asset_pattern: str) -> Optional[str]:
    """Get expected SHA256 checksum for a release asset.

    Checks in order:
    1. User-provided checksum via WATERCOOLER_LLAMA_SERVER_SHA256
    2. Known-good checksums from LLAMA_SERVER_CHECKSUMS registry

    Args:
        release_tag: Release version tag (e.g., "b5270")
        asset_pattern: Asset pattern (e.g., "ubuntu-x64")

    Returns:
        Expected SHA256 hex string, or None if unknown
    """
    # Check user-provided checksum first
    user_checksum = os.environ.get(ENV_LLAMA_SERVER_SHA256, "").strip().lower()
    if user_checksum:
        log_debug(f"Using user-provided checksum: {user_checksum[:16]}...")
        return user_checksum

    # Check our known-good checksums registry
    release_checksums = LLAMA_SERVER_CHECKSUMS.get(release_tag, {})
    if asset_pattern in release_checksums:
        return release_checksums[asset_pattern]

    return None


def _verify_checksum(
    file_path: Path,
    expected: Optional[str],
    release_tag: str,
    asset_pattern: str,
) -> bool:
    """Verify file checksum and handle verification policy.

    Verification policy (WATERCOOLER_LLAMA_SERVER_VERIFY):
    - "strict": Fail if checksum unknown or mismatched
    - "warn" (default): Warn if checksum unknown, fail if mismatched
    - "skip": Skip verification entirely

    Args:
        file_path: Path to downloaded file
        expected: Expected SHA256 (None if unknown)
        release_tag: Release tag for error messages
        asset_pattern: Asset pattern for error messages

    Returns:
        True if verification passed (or skipped), False if failed

    Raises:
        RuntimeError: In strict mode when checksum is unknown
    """
    verify_mode = os.environ.get(ENV_LLAMA_SERVER_VERIFY, "warn").lower().strip()

    if verify_mode == "skip":
        log_debug("Checksum verification skipped (WATERCOOLER_LLAMA_SERVER_VERIFY=skip)")
        return True

    actual = _compute_sha256(file_path)
    log_debug(f"Downloaded file SHA256: {actual}")

    if expected is None:
        # Checksum unknown for this release
        if verify_mode == "strict":
            raise RuntimeError(
                f"Checksum verification failed: No known checksum for llama-server "
                f"release {release_tag} ({asset_pattern}).\n"
                f"Actual SHA256: {actual}\n"
                f"To proceed, either:\n"
                f"  1. Set WATERCOOLER_LLAMA_SERVER_SHA256={actual} after manual verification\n"
                f"  2. Set WATERCOOLER_LLAMA_SERVER_VERIFY=warn to allow with warning\n"
                f"  3. Download llama-server manually from https://github.com/ggml-org/llama.cpp/releases"
            )
        else:
            # warn mode
            log_warning(
                f"Downloaded llama-server ({release_tag}) without checksum verification. "
                f"SHA256: {actual}. Set WATERCOOLER_LLAMA_SERVER_VERIFY=strict for mandatory verification."
            )
            return True

    # Have expected checksum - verify it matches
    if actual != expected:
        log_warning(
            f"SECURITY: Checksum mismatch for llama-server download!\n"
            f"Expected: {expected}\n"
            f"Actual:   {actual}\n"
            f"This could indicate tampering or a corrupted download."
        )
        # Delete the suspicious file
        file_path.unlink(missing_ok=True)
        return False

    log_debug(f"Checksum verified: {actual[:16]}...")
    return True


def _download_with_progress(
    url: str,
    dest_path: Path,
    desc: str = "Downloading",
    max_retries: int = 3,
    initial_backoff: float = 1.0,
    max_backoff: float = 30.0,
) -> bool:
    """Download a file with progress indication and retry logic.

    Uses exponential backoff for transient failures (network errors, rate limits).

    Args:
        url: URL to download
        dest_path: Destination file path
        desc: Description for progress display
        max_retries: Maximum number of retry attempts (default: 3)
        initial_backoff: Initial backoff delay in seconds (default: 1.0)
        max_backoff: Maximum backoff delay in seconds (default: 30.0)

    Returns:
        True if download succeeded
    """
    import random

    backoff = initial_backoff
    last_error = None

    for attempt in range(max_retries + 1):
        if attempt > 0:
            # Add jitter to avoid thundering herd
            jitter = random.uniform(0, backoff * 0.1)
            sleep_time = min(backoff + jitter, max_backoff)
            log_debug(f"Retry {attempt}/{max_retries} after {sleep_time:.1f}s backoff...")
            time.sleep(sleep_time)
            backoff = min(backoff * 2, max_backoff)

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "watercooler-cloud"})
            with urllib.request.urlopen(req, timeout=600) as resp:  # 10 min timeout
                # Check for rate limiting
                if resp.status == 429:
                    retry_after = resp.headers.get("Retry-After", "60")
                    try:
                        wait_time = int(retry_after)
                    except ValueError:
                        wait_time = 60
                    log_debug(f"Rate limited, waiting {wait_time}s...")
                    time.sleep(wait_time)
                    continue

                total_size = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                chunk_size = 1024 * 1024  # 1MB chunks

                with open(dest_path, "wb") as f:
                    while True:
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)

                        # Show progress
                        if total_size > 0:
                            pct = (downloaded / total_size) * 100
                            mb_down = downloaded / (1024 * 1024)
                            mb_total = total_size / (1024 * 1024)
                            log_debug(f"{desc}: {mb_down:.1f}/{mb_total:.1f} MB ({pct:.0f}%)")

            return True

        except urllib.error.HTTPError as e:
            last_error = e
            # Don't retry on client errors (4xx) except rate limiting
            if 400 <= e.code < 500 and e.code != 429:
                log_debug(f"Download failed with HTTP {e.code}: {e.reason}")
                break
            log_debug(f"Download attempt {attempt + 1} failed: HTTP {e.code}")

        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_error = e
            log_debug(f"Download attempt {attempt + 1} failed: {e}")

        except Exception as e:
            last_error = e
            log_debug(f"Download attempt {attempt + 1} failed unexpectedly: {e}")
            break  # Don't retry unknown errors

    # All retries exhausted
    log_debug(f"Download failed after {max_retries + 1} attempts: {last_error}")
    if dest_path.exists():
        dest_path.unlink()
    return False


def _download_llama_server() -> Optional[Path]:
    """Download llama-server binary from GitHub releases.

    Downloads the latest release for the current platform and extracts
    llama-server to ~/.watercooler/bin/.

    On Linux with NVIDIA GPU, prefers Vulkan build for GPU acceleration.
    Falls back to CPU build if Vulkan not available.

    Supported platforms:
    - Linux x86_64 (CPU or Vulkan)
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

    # Build list of asset patterns to try (in preference order)
    asset_patterns: list[tuple[str, str]] = []  # (pattern, archive_ext)

    if system == "linux" and machine in ("x86_64", "amd64"):
        # On Linux, prefer Vulkan build if GPU detected (works with NVIDIA via Vulkan)
        if _has_nvidia_gpu():
            log_debug("NVIDIA GPU detected, preferring Vulkan build for GPU acceleration")
            asset_patterns.append(("ubuntu-vulkan-x64", ".tar.gz"))
        # Always have CPU fallback
        asset_patterns.append(("ubuntu-x64", ".tar.gz"))
    elif system == "darwin" and machine == "arm64":
        asset_patterns.append(("macos-arm64", ".tar.gz"))
    elif system == "darwin" and machine in ("x86_64", "amd64"):
        asset_patterns.append(("macos-x64", ".tar.gz"))
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

        # Try each asset pattern in preference order
        download_url = None
        archive_ext = None
        asset_name = None

        for pattern, ext in asset_patterns:
            for asset in release_info.get("assets", []):
                name = asset.get("name", "")
                if pattern in name and name.endswith(ext):
                    download_url = asset.get("browser_download_url")
                    archive_ext = ext
                    asset_name = name
                    log_debug(f"Found matching asset: {name}")
                    break
            if download_url:
                break

        if not download_url:
            patterns_tried = [p[0] for p in asset_patterns]
            log_debug(f"No matching release asset found for patterns: {patterns_tried}")
            return None

        # Extract release tag from asset name or release info
        release_tag = release_info.get("tag_name", "unknown")

        # Download the archive with progress
        archive_path = bin_dir / f"llama-cpp-download{archive_ext}"
        log_debug(f"Downloading llama-server from: {download_url}")

        if not _download_with_progress(download_url, archive_path, "llama-server"):
            return None

        log_debug(f"Downloaded archive to: {archive_path}")

        # Verify checksum before extraction
        # Find which pattern matched for checksum lookup
        matched_pattern = None
        for pattern, _ in asset_patterns:
            if pattern in (asset_name or ""):
                matched_pattern = pattern
                break

        expected_checksum = _get_expected_checksum(release_tag, matched_pattern or "")
        try:
            if not _verify_checksum(archive_path, expected_checksum, release_tag, matched_pattern or "unknown"):
                log_debug("Checksum verification failed, aborting download")
                return None
        except RuntimeError as e:
            # Strict mode failure
            log_debug(f"Checksum verification error: {e}")
            archive_path.unlink(missing_ok=True)
            raise

        # Extract llama-server AND shared libraries from archive
        # The llama.cpp releases include .so files that llama-server depends on:
        # libmtmd.so.0, libllama.so.0, libggml.so.0, libggml-base.so.0, etc.
        extracted_files: list[Path] = []

        if archive_ext == ".tar.gz":
            with tarfile.open(archive_path, "r:gz") as tf:
                found_binary = False
                for member in tf.getmembers():
                    # Security: Validate path doesn't escape destination directory
                    if not _is_safe_archive_path(member.name, bin_dir):
                        continue

                    basename = Path(member.name).name
                    # Extract llama-server binary
                    if basename == "llama-server":
                        log_debug(f"Extracting binary: {member.name}")
                        tf.extract(member, bin_dir)
                        extracted_path = bin_dir / member.name
                        if extracted_path != target_binary:
                            if target_binary.exists():
                                target_binary.unlink()
                            extracted_path.rename(target_binary)
                        found_binary = True
                    # Extract shared libraries - both regular files and symlinks
                    # Linux: .so files (libfoo.so.0.0.123, libfoo.so.0)
                    # macOS: .dylib files (libfoo.dylib, libfoo.0.dylib)
                    # The tarball contains versioned files and symlinks that llama-server needs
                    elif _is_shared_library(basename) and (member.isfile() or member.issym()):
                        log_debug(f"Extracting library: {member.name} (symlink={member.issym()})")
                        tf.extract(member, bin_dir)
                        extracted_path = bin_dir / member.name
                        target_lib = bin_dir / basename
                        if extracted_path != target_lib:
                            if target_lib.exists() or target_lib.is_symlink():
                                target_lib.unlink()
                            extracted_path.rename(target_lib)
                        extracted_files.append(target_lib)

                if not found_binary:
                    log_debug("llama-server not found in tar.gz archive")
                    archive_path.unlink()
                    return None

        elif archive_ext == ".zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                found_binary = False
                for name in zf.namelist():
                    # Security: Validate path doesn't escape destination directory
                    if not _is_safe_archive_path(name, bin_dir):
                        continue

                    basename = Path(name).name
                    # Extract llama-server binary
                    if basename in ("llama-server", "llama-server.exe"):
                        log_debug(f"Extracting binary: {name}")
                        extracted = zf.extract(name, bin_dir)
                        extracted_path = Path(extracted)
                        if extracted_path != target_binary:
                            if target_binary.exists():
                                target_binary.unlink()
                            extracted_path.rename(target_binary)
                        found_binary = True
                    # Extract shared libraries (.dll on Windows, .so on Linux, .dylib on macOS)
                    elif _is_shared_library(basename) and not name.endswith("/"):
                        log_debug(f"Extracting library: {name}")
                        extracted = zf.extract(name, bin_dir)
                        extracted_path = Path(extracted)
                        target_lib = bin_dir / basename
                        if extracted_path != target_lib:
                            if target_lib.exists():
                                target_lib.unlink()
                            extracted_path.rename(target_lib)
                        extracted_files.append(target_lib)

                if not found_binary:
                    log_debug("llama-server not found in zip archive")
                    archive_path.unlink()
                    return None

        # Make binary executable
        target_binary.chmod(0o755)

        # Make libraries readable
        for lib in extracted_files:
            if lib.exists():
                lib.chmod(0o644)

        log_debug(f"Extracted {len(extracted_files)} shared libraries to {bin_dir}")

        # Clean up archive
        archive_path.unlink()

        # Clean up any extracted subdirectories (but keep the .so files we moved)
        for item in bin_dir.iterdir():
            if item.is_dir() and item.name.startswith("llama-"):
                shutil.rmtree(item, ignore_errors=True)

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
    n_ctx: int = DEFAULT_CONTEXT_SIZE,
) -> bool:
    """Start embedding server as a detached background process.

    Uses llama-server with batch-optimized configuration for embeddings:
    - --parallel 8: Allow 8 concurrent requests
    - -c: Context window (default 8192, matches bge-m3)
    - -b 4096: Batch size for prompt processing
    - -ub 4096: Micro-batch size for prompt processing
    - --embedding: Enable embedding mode

    These flags are critical for Graphiti's create_batch() calls which send
    multiple strings in a single API request.

    NO FALLBACK - llama-server is required. If not available, it will be
    downloaded from GitHub releases.

    Args:
        model_path: Path to GGUF model file
        host: Host to bind to
        port: Port to listen on
        n_ctx: Context window size

    Returns:
        True if server started successfully

    Raises:
        RuntimeError: If llama-server cannot be found or downloaded
    """
    api_base = f"http://{host}:{port}/v1"

    try:
        # Use unified _start_llama_server function
        if not _start_llama_server(model_path, port, mode="embedding",
                                   context_size=n_ctx, host=host):
            return False

        if _wait_for_embedding_ready(api_base, max_wait=30.0):
            log_debug("Embedding server started successfully via llama-server (batch mode)")
            return True

        log_debug("Embedding server process started but health check failed")
        return False

    except RuntimeError as e:
        log_debug(f"llama-server startup error: {e}")
        raise


def _start_embedding_windows(
    model_path: Path,
    host: str,
    port: int,
    n_ctx: int = DEFAULT_CONTEXT_SIZE,
) -> bool:
    """Start embedding server on Windows.

    Uses llama-server with DETACHED_PROCESS flag for Windows.

    Args:
        model_path: Path to GGUF model file
        host: Host to bind to
        port: Port to listen on
        n_ctx: Context window size

    Returns:
        True if server started successfully
    """
    api_base = f"http://{host}:{port}/v1"

    llama_server = _find_llama_server()
    if not llama_server:
        log_debug("llama-server not found, attempting download...")
        llama_server = _download_llama_server()

    if not llama_server:
        _add_startup_warning(
            "llama-server not found and could not be downloaded.\n"
            "Download from: https://github.com/ggml-org/llama.cpp/releases\n"
            "Or build from source."
        )
        return False

    try:
        cmd = [
            str(llama_server),
            "--model", str(model_path),
            "--host", host,
            "--port", str(port),
            "--embedding",
            "--parallel", "8",
            "-c", str(n_ctx),
            "-b", "4096",
            "-ub", "4096",
        ]
        log_debug(f"Starting embedding server on Windows: {' '.join(cmd)}")

        # Add llama-server directory to PATH for DLL discovery
        env = os.environ.copy()
        lib_dir = str(llama_server.parent)
        existing_path = env.get("PATH", "")
        env["PATH"] = f"{lib_dir};{existing_path}" if existing_path else lib_dir

        # Windows-specific: DETACHED_PROCESS
        DETACHED_PROCESS = 0x00000008
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS,
            env=env,
            cwd=lib_dir,  # Run from the directory containing DLLs
        )

        if _wait_for_embedding_ready(api_base, max_wait=30.0):
            log_debug("Embedding server started successfully on Windows")
            return True

    except Exception as e:
        log_debug(f"Windows llama-server start failed: {e}")

    _add_startup_warning(
        f"Embedding server failed to start.\n"
        f"Start manually: llama-server --model {model_path} --embedding --port {port}"
    )
    return False


def _ensure_embedding_service_available(
    model_name: str,
    api_base: str,
    context_size: int = DEFAULT_CONTEXT_SIZE,
) -> bool:
    """Ensure embedding service is running, starting it if needed.

    Resolves model name, downloads model if needed, and starts llama-server.
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
        resolve_embedding_model,
    )

    # Resolve model specification
    try:
        model_spec = resolve_embedding_model(model_name)
    except ModelNotFoundError as e:
        _add_startup_warning(f"Unknown embedding model: {model_name}. {e}")
        return False

    # Auto-set EMBEDDING_DIM before any graphiti-core imports
    # This prevents index dimension mismatch errors
    dim = model_spec.get("dim", 1024)
    existing_dim = os.environ.get("EMBEDDING_DIM", "")
    if not existing_dim:
        os.environ["EMBEDDING_DIM"] = str(dim)
        log_debug(f"Auto-set EMBEDDING_DIM={dim} for model {model_name}")
    elif existing_dim != str(dim):
        # Warn about dimension mismatch - could cause FalkorDB index errors
        log_warning(
            f"EMBEDDING_DIM mismatch: env has {existing_dim} but model '{model_name}' "
            f"has dim={dim}. This may cause FalkorDB index dimension errors. "
            f"To fix: unset EMBEDDING_DIM or set it to {dim}, then recreate the index."
        )

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
    port = parsed.port or DEFAULT_EMBEDDING_PORT

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
    """Background worker to start embedding service (llama-server) and wait for it to be ready."""
    import traceback

    start_time = time.time()
    _update_service_status("embedding", ServiceState.STARTING, endpoint=api_base, started_at=start_time)

    try:
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
                message="Could not start llama-server for embeddings"
            )
            log_debug("Embedding auto-start failed")
    except RuntimeError as e:
        _update_service_status(
            "embedding", ServiceState.FAILED,
            message=str(e)
        )
        log_debug(f"Embedding startup error: {e}")
    except Exception as e:
        # Catch-all for any unexpected errors to prevent silent failures
        error_msg = f"Unexpected error in embedding startup: {type(e).__name__}: {e}"
        log_debug(f"{error_msg}\n{traceback.format_exc()}")
        _update_service_status("embedding", ServiceState.FAILED, message=error_msg)


def ensure_embedding_running() -> None:
    """Start embedding service (llama-server) if graph features are enabled and it's not running.

    This is non-blocking - spawns a background thread if embedding service needs to start.
    Check get_service_status()["embedding"] to see current state.

    Features:
    - Auto-downloads model from HuggingFace on first use
    - Auto-starts llama-server with batch embedding support
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


# ============================================================================
# Docker Management for FalkorDB
# ============================================================================


def _get_docker_path() -> Optional[Path]:
    """Get the absolute path to the Docker binary.

    Uses shutil.which to find Docker, then resolves to absolute path.
    This prevents PATH manipulation attacks.

    Can be overridden via WATERCOOLER_DOCKER_PATH environment variable.

    Returns:
        Absolute path to Docker binary, or None if not found.
    """
    # Allow user override
    override = os.environ.get("WATERCOOLER_DOCKER_PATH", "").strip()
    if override:
        path = Path(override)
        if path.exists() and path.is_file():
            return path.resolve()
        log_debug(f"WATERCOOLER_DOCKER_PATH set but invalid: {override}")
        return None

    # Find docker in PATH
    docker = shutil.which("docker")
    if docker:
        return Path(docker).resolve()
    return None


def _is_docker_daemon_running() -> bool:
    """Check if Docker daemon is running (not just if binary exists).

    Returns:
        True if Docker daemon is responsive.
    """
    docker_path = _get_docker_path()
    if not docker_path:
        return False

    try:
        result = subprocess.run(
            [str(docker_path), "info"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _ensure_docker_available() -> tuple[bool, str]:
    """Ensure Docker is available and daemon is running.

    Provides clear instructions if Docker is not available.
    Does NOT attempt auto-install (security: avoids running sudo).

    Returns:
        Tuple of (success, message). If success is False, message contains
        user-friendly error/instructions.
    """
    system = platform.system().lower()
    docker_path = shutil.which("docker")

    # Step 1: Check if Docker binary exists
    if not docker_path:
        if system == "linux":
            return False, (
                "Docker not found. Install options:\n"
                "  • Standard: curl -fsSL https://get.docker.com | sh\n"
                "  • Rootless: curl -fsSL https://get.docker.com/rootless | sh\n"
                "  • Package:  sudo apt install docker.io  (Ubuntu/Debian)\n"
                "After install, add yourself to docker group: sudo usermod -aG docker $USER"
            )
        elif system == "darwin":
            return False, (
                "Docker not found. Install Docker Desktop:\n"
                "  https://docs.docker.com/desktop/install/mac-install/"
            )
        else:
            return False, (
                "Docker not found. Please install Docker for your platform:\n"
                "  https://docs.docker.com/get-docker/"
            )

    # Step 2: Check if Docker daemon is running
    if not _is_docker_daemon_running():
        if system == "darwin":
            return False, (
                "Docker Desktop is installed but not running.\n"
                "Please start Docker Desktop from Applications."
            )
        elif system == "linux":
            return False, (
                "Docker daemon not running. Start it with one of:\n"
                "  • sudo systemctl start docker\n"
                "  • dockerd-rootless-setuptool.sh install  (for rootless)\n"
                "  • Start Docker Desktop if installed"
            )
        else:
            return False, "Docker daemon not running. Please start Docker."

    return True, "Docker ready"


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
    import traceback

    start_time = time.time()
    endpoint = f"{host}:{port}"
    _update_service_status("falkordb", ServiceState.STARTING, endpoint=endpoint, started_at=start_time)

    try:
        # Ensure Docker is available (provides instructions if not)
        docker_available, docker_message = _ensure_docker_available()
        if not docker_available:
            _update_service_status(
                "falkordb", ServiceState.FAILED,
                message=docker_message
            )
            return

        log_debug(f"Docker check: {docker_message}")

        # Get verified Docker path
        docker_path = _get_docker_path()
        if not docker_path:
            _update_service_status(
                "falkordb", ServiceState.FAILED,
                message="Docker binary not found"
            )
            return
        docker_cmd = str(docker_path)

        try:
            result = subprocess.run(
                [docker_cmd, "ps", "-a", "--filter", "name=falkordb", "--format", "{{.Status}}"],
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
                        [docker_cmd, "start", "falkordb"],
                        capture_output=True,
                        timeout=30,
                    )
                    if result.returncode == 0:
                        if _wait_for_falkordb_ready(host, port, max_wait=DEFAULT_SERVICE_WAIT_TIMEOUT):
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
                    if _wait_for_falkordb_ready(host, port, max_wait=DEFAULT_SERVICE_WAIT_TIMEOUT):
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
                        docker_cmd, "run", "-d",
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
                    if _wait_for_falkordb_ready(host, port, max_wait=DEFAULT_SERVICE_WAIT_TIMEOUT):
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

        except subprocess.TimeoutExpired as e:
            log_debug(f"Docker command timed out: {e}")
            _update_service_status(
                "falkordb", ServiceState.FAILED,
                message="Docker command timed out"
            )
            return

        # If we get here, auto-start failed
        _update_service_status(
            "falkordb", ServiceState.FAILED,
            message="Could not start. Run: docker start falkordb"
        )

    except Exception as e:
        # Catch-all for any unexpected errors to prevent silent failures
        error_msg = f"Unexpected error in FalkorDB startup: {type(e).__name__}: {e}"
        log_debug(f"{error_msg}\n{traceback.format_exc()}")
        _update_service_status("falkordb", ServiceState.FAILED, message=error_msg)


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
