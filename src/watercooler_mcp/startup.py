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
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal, Optional

from .helpers import _add_startup_warning
from .observability import log_debug, log_error, log_warning

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
#   Linux/macOS:
#     1. gh release download <tag> --repo ggml-org/llama.cpp --pattern "llama-*-bin-*.tar.gz"
#     2. sha256sum *.tar.gz
#   Windows:
#     1. gh release download <tag> --repo ggml-org/llama.cpp --pattern "llama-*-bin-win-cpu-x64.zip"
#     2. certutil -hashfile <file>.zip SHA256   (or sha256sum on Git Bash)
#   3. Add entries below
LLAMA_SERVER_CHECKSUMS: dict[str, dict[str, str]] = {
    # Release b7896 (2026-01-31) - verified checksums
    "b7896": {
        "ubuntu-x64": "329a716c5fb216d49d674d3ac7a9aab90d04942d80b08786aeaaae49a4490b93",
        "ubuntu-vulkan-x64": "85191595f05328f01de8f5852f0679a6dd8cce4271ec52d9d0cf3dca08e1ac74",
        "macos-arm64": "231f8f7ff3763de2ab1cbeb097e728e4bb442b0bc941f6dacc7ef83d01ae47bb",
        "macos-x64": "6de178b3f364734e442b4579554f102a6c36c9343cf31cdb8381c02053b2bf11",
        "win-cpu-x64": "6a1dcc9a3d5344c3afe461c7a9247a69bb4099e15ef1da3a115fea94584b09eb",
    },
    # Release b7885 (2026-01-30) - verified checksums
    "b7885": {
        "ubuntu-x64": "6e6148e2f8908cbefdf4833e71a8113c71a1a4a14cb155375ad8c1b095d8a5e1",
        "ubuntu-vulkan-x64": "f21649deb021d7b2942227c12a05915dee476835081b65f2698aed4e93459d37",
        "macos-arm64": "608760410b9f65f91a0e9f499dc21f95cea298c59b9df1354bd6a31cad059d35",
        "macos-x64": "4794fd57522f680c17be60dc7c3ef7fb08c89a2524ee2babf3480f9f2c87ffca",
        "win-cpu-x64": "992bd27f00ec1f5e7979e46e453fd34906034787d786282b0914c0627d829c9c",
    },
    # Release b7869 (2026-01-28) - verified checksums
    "b7869": {
        "ubuntu-x64": "d35419ff41d6438338fb9942d2250e9c21ea02424e422617650bcab950575d78",
        "ubuntu-vulkan-x64": "45b73da74307eb11463e042253a506f1ccc4a714ad73b1de19630cfba876d2b8",
        "macos-arm64": "45ecd82ead1574c45ae19738e9d890c2c19bd2944b645eaf3619980d87621b51",
        "macos-x64": "d65e43f4ffb1890bc694f417871dba56374a011011da1bc4c4e8e99768d56f20",
        "win-cpu-x64": "bf29a6fcd0dc59435e528d00175c68a7a45981317eb55e3b69e7a83b4034f8b5",
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
# Track spawned Popen handles by port so wait loops can detect the child
# exiting before it becomes ready (surfaces bind failures that the probe-
# then-spawn TOCTOU window allows). Keyed by port → Popen; overwritten
# on respawn.
_spawned_procs: "dict[int, subprocess.Popen]" = {}
_pids_lock = threading.Lock()

# Cross-port binary-download lock. Both the LLM and embedding workers call
# ``_find_llama_server()`` → ``_download_llama_server()`` on a clean install.
# Without serialization the two workers race to write the same archive at
# ``~/.watercooler/bin/llama-cpp-download.{zip,tar.gz}`` and one returns None,
# surfacing as "llama-server binary required but could not be downloaded" even
# though the peer worker just finished downloading successfully. Serialize so
# the first arrival downloads once and the second re-uses that binary.
_binary_download_lock = threading.Lock()

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


def _normalize_loopback(host: str) -> str:
    """Coerce a loopback hostname to IPv4 127.0.0.1.

    On Windows, ``localhost`` resolves to ``::1`` (IPv6) first. When
    llama-server binds without an explicit ``--host``, or when health
    probes target ``localhost``, this resolution order causes IPv4
    listeners and IPv6 probes (or vice versa) to miss each other — the
    F15 failure mode documented in windows-release-hardening. Coercing
    the loopback name to ``127.0.0.1`` makes both sides deterministic.

    The comparison is case-insensitive so ``LLAMA_SERVER_HOST=LOCALHOST``
    and similar mixed-case overrides are also normalized — ``urlparse``
    already lowercases hostnames for the probe side, so matching that
    behavior here keeps bind and probe aligned on the same address.

    Any non-loopback host (``0.0.0.0``, a LAN IP, a container hostname)
    is returned unchanged so Docker / remote-binding setups keep working.
    """
    if host.lower() in ("", "localhost"):
        return "127.0.0.1"
    return host


def _normalize_probe_url(url: str) -> str:
    """Return ``url`` with a ``localhost`` hostname rewritten to 127.0.0.1.

    Used to make health-check probes deterministic on Windows where
    ``localhost`` prefers IPv6 loopback.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.hostname != "localhost":
            return url
        port_part = f":{parsed.port}" if parsed.port else ""
        # Preserve userinfo if present (uncommon for loopback, but be safe)
        userinfo = ""
        if parsed.username:
            userinfo = parsed.username
            if parsed.password:
                userinfo += f":{parsed.password}"
            userinfo += "@"
        new_netloc = f"{userinfo}127.0.0.1{port_part}"
        return urllib.parse.urlunparse(parsed._replace(netloc=new_netloc))
    except ValueError:
        return url


def _find_pid_on_port(port: int) -> Optional[int]:
    """Best-effort lookup of the PID listening on ``port``.

    Uses ``netstat -ano`` on Windows and ``lsof`` on Unix. Returns
    ``None`` if the port is free, the lookup command is unavailable,
    or output cannot be parsed. Callers must tolerate ``None`` —
    PID identification is a UX convenience, not a correctness requirement.
    """
    system = platform.system().lower()
    try:
        if system == "windows":
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                return None
            for line in result.stdout.splitlines():
                parts = line.split()
                # netstat -ano columns: Proto, LocalAddress, ForeignAddress, State, PID
                if len(parts) < 5 or parts[0] != "TCP" or parts[3] != "LISTENING":
                    continue
                local = parts[1]
                # LocalAddress formats: 0.0.0.0:8080, 127.0.0.1:8080, [::]:8080, [::1]:8080
                if local.endswith(f":{port}") or local.endswith(f"]:{port}"):
                    try:
                        return int(parts[4])
                    except ValueError:
                        continue
            return None

        # Unix: try lsof first (widely available, clean output)
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            # lsof columns: COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME.
            # A single listener can appear on multiple rows (one per IPv4
            # and IPv6 binding). Scan all data rows and return the first
            # valid PID rather than trusting ``lines[1]`` — otherwise the
            # remediation message can name the wrong PID when dual-stack
            # listeners are present.
            lines = result.stdout.strip().splitlines()
            for row in lines[1:]:
                parts = row.split()
                if len(parts) >= 2:
                    try:
                        return int(parts[1])
                    except ValueError:
                        continue
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _check_port_available(
    port: int,
    host: str = "127.0.0.1",
) -> tuple[bool, Optional[int]]:
    """Check whether ``port`` is free to bind on the addresses that
    ``llama-server`` would actually bind when configured for ``host``.

    Returns ``(True, None)`` if the port is free.

    Returns ``(False, pid)`` if something is actually listening, where
    ``pid`` is the best-effort PID or ``None`` when it could not be
    identified.

    Address selection:

    - Loopback bind (``127.0.0.1`` / ``::1``): probe both IPv4 and IPv6
      loopback so an IPv6-only orphan (F15 in windows-release-hardening)
      is detected even when the caller plans to bind IPv4.
    - Wildcard bind (``0.0.0.0`` / ``::``): probe the wildcard family
      so any listener on the port conflicts.
    - Specific non-loopback bind (a LAN IP, container bridge, etc.):
      probe only that address — a loopback listener does not conflict
      with a bind to a LAN IP, so probing loopback would false-positive.

    TIME_WAIT handling: on POSIX the probe uses ``SO_REUSEADDR`` so a
    recently-closed socket does not masquerade as an occupied port. On
    Windows ``SO_REUSEADDR`` has permissive semantics (a second bind
    can succeed while another process still holds the port), so it is
    deliberately *not* set there — a normal bind failure is the source
    of truth. As a safety net, if the probe fails but no listener can
    be identified by ``_find_pid_on_port``, the port is reported as
    free; llama-server's own bind error will then surface via the
    wait-loop ``proc.poll()`` path rather than a misleading preflight.
    """
    is_windows = platform.system().lower() == "windows"

    def _can_bind(family: int, bind_host: str) -> bool:
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            if not is_windows:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                except OSError:
                    pass
            sock.bind((bind_host, port))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    h = host.lower()
    # Determine which addresses to probe based on the configured bind host.
    if h in ("127.0.0.1", "::1", "localhost", ""):
        probes: list[tuple[int, str]] = [
            (socket.AF_INET, "127.0.0.1"),
            (socket.AF_INET6, "::1"),
        ]
    elif h == "0.0.0.0":
        # IPv4 wildcard — llama-server binds every IPv4 interface. Any
        # IPv4 listener on the port conflicts. IPv6 listeners don't
        # (separate namespace unless IPV6_V6ONLY is off, which is
        # platform-default anyway).
        probes = [(socket.AF_INET, "0.0.0.0")]
    elif h == "::":
        # IPv6 wildcard — bind every IPv6 interface. Must probe IPv6
        # specifically; an IPv4 probe would be invisible to an IPv6-only
        # orphan on ``::``, which is exactly the masking this preflight
        # exists to prevent.
        probes = [(socket.AF_INET6, "::")]
    else:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        probes = [(family, host)]

    all_free = True
    for family, bind_host in probes:
        if family == socket.AF_INET6 and not socket.has_ipv6:
            continue
        try:
            free = _can_bind(family, bind_host)
        except OSError as exc:
            # socket.socket(AF_INET6) can raise with EAFNOSUPPORT or
            # EPROTONOSUPPORT when IPv6 is compiled in but runtime-disabled.
            # Treat those as "IPv6 unavailable, skip" — any other OSError
            # must surface so the caller sees a real permissions/kernel
            # problem rather than a silent "available".
            import errno as _errno
            if exc.errno in (_errno.EAFNOSUPPORT, _errno.EPROTONOSUPPORT):
                continue
            raise
        if not free:
            all_free = False
            break

    if all_free:
        return True, None

    pid = _find_pid_on_port(port)
    if pid is None:
        # Bind failed but nothing is actually listening — typically a
        # TIME_WAIT state on Windows (where SO_REUSEADDR is unsafe to
        # use as a probe) or a transient kernel hold. Report free; the
        # real-spawn bind error, if any, gets caught by proc.poll() in
        # the readiness wait.
        return True, None
    return False, pid


def _format_port_in_use_error(service: str, port: int, pid: Optional[int]) -> str:
    """Format an actionable error message when a service port is occupied.

    ``service`` is a short tag like ``"LLM"`` or ``"embedding"``. The
    message is deliberately explicit — we refuse to start rather than
    kill a foreign process, so the user needs clear remediation steps
    in the failure path itself.
    """
    who = f" by PID {pid}" if pid is not None else ""
    win_cmd = (
        f"Stop-Process -Id {pid} -Force"
        if pid is not None
        else f"Get-NetTCPConnection -LocalPort {port} -State Listen"
    )
    unix_cmd = (
        f"kill {pid}"
        if pid is not None
        else f"lsof -nP -iTCP:{port} -sTCP:LISTEN"
    )
    return (
        f"{service} port {port} is already in use{who}.\n\n"
        f"Watercooler refuses to start a new llama-server while this port is\n"
        f"occupied, because an existing listener (likely an orphan llama-server\n"
        f"from a prior session that did not shut down cleanly) would mask genuine\n"
        f"startup failures — a class of bug that previously made 'clean install'\n"
        f"tests look green when they were not.\n\n"
        f"To resolve:\n"
        f"  Windows:  {win_cmd}\n"
        f"  Unix:     {unix_cmd}\n\n"
        f"Then restart your MCP client. "
        f"See docs/TROUBLESHOOTING.md#port-in-use-by-orphan-llama-server "
        f"for more detail."
    )


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
        _spawned_procs.clear()


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


def get_live_service_status() -> dict[str, dict]:
    """Get service status with live health probes.

    Unlike get_service_status() which returns cached state,
    this function actively pings each service that was previously
    reported as running. If a service is down, updates the cached
    state to reflect reality.

    Returns:
        Dictionary mapping service name to status dict with live state.
    """
    cached = get_service_status()

    for name, status in cached.items():
        if status["state"] not in ("running", "starting"):
            continue

        endpoint = status.get("endpoint", "")
        if not endpoint:
            continue

        alive = False
        if name == "falkordb":
            # Parse host:port from endpoint
            parts = endpoint.split(":")
            if len(parts) == 2:
                try:
                    alive = _check_falkordb_health(parts[0], int(parts[1]))
                except (ValueError, TypeError):
                    pass
        elif name in ("llm", "embedding"):
            # HTTP health check against the API endpoint
            check_fn = _check_llm_health if name == "llm" else _check_embedding_health
            alive = check_fn(endpoint)

        if not alive:
            _update_service_status(
                name, ServiceState.FAILED,
                message="Not responding (was running)"
            )
            cached[name] = {**status, "state": "failed", "message": "Not responding (was running)"}

    return cached


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
                "  uvx watercooler config init --user\n"
                "Using built-in defaults for now."
            )
    except Exception:
        # Don't let config check errors break server startup
        pass

    # Warn if Graphiti/FalkorDB env vars are set but backend resolves to "null".
    # This catches misconfigured deployments that set FALKORDB_* or
    # WATERCOOLER_GRAPHITI_ENABLED without explicitly setting memory.backend.
    try:
        import os as _os
        from watercooler.memory_config import get_memory_backend as _get_backend

        if _get_backend() == "null":
            graphiti_signals = [
                _os.getenv("FALKORDB_HOST"),
                _os.getenv("FALKORDB_PASSWORD"),
                _os.getenv("FALKORDB_USERNAME"),
                _os.getenv("WATERCOOLER_GRAPHITI_ENABLED"),
            ]
            if any(graphiti_signals):
                log_error(
                    "MEMORY: backend resolved to 'null' but Graphiti/FalkorDB environment "
                    "variables are set. T2 indexing is disabled. "
                    "Set memory.backend = 'graphiti' in your config to enable it."
                )
    except ValueError as exc:
        # Invalid backend name configured — surface this clearly at startup.
        log_error("MEMORY: %s", exc)
    except Exception:
        pass


def _ensure_llama_server_binary() -> Optional[Path]:
    """Return the llama-server binary path, downloading if necessary.

    Deduplicates concurrent callers: both the LLM and embedding startup
    workers race here on first-run clean installs. The lock serializes
    the download and the loser re-checks ``_find_llama_server()`` so it
    reuses the winner's binary instead of re-downloading (which would
    fail on Windows because two processes can't write the same archive
    file, producing a misleading "could not be downloaded" error).

    Returns:
        Path to the binary on success, or None if not found and
        auto-provision is disabled or the download itself failed.
    """
    # Fast path: binary already present (either from a previous run or
    # because the peer worker finished downloading ahead of us).
    found = _find_llama_server()
    if found:
        return found

    if not _is_auto_provision_enabled("llama_server"):
        return None

    with _binary_download_lock:
        # Re-check under the lock — the peer worker may have completed
        # the download while we were waiting. This is the dedup path
        # that prevents double-downloads on clean installs.
        found = _find_llama_server()
        if found:
            log_debug(
                "llama-server appeared while waiting for download lock "
                "(peer worker finished download)."
            )
            return found
        log_debug("llama-server not found, attempting download from GitHub releases...")
        return _download_llama_server()


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
    models_url = f"{_normalize_probe_url(api_base).rstrip('/')}/models"
    try:
        req = urllib.request.Request(
            models_url,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _get_spawned_proc(port: int) -> "Optional[subprocess.Popen]":
    """Return the Popen handle registered for ``port``, or None.

    Wait loops use this to check whether a child we spawned has exited
    early (silent bind failure, missing DLL, invalid args) so they can
    fail fast with an explicit exit code instead of blocking until the
    health-probe deadline.
    """
    with _pids_lock:
        return _spawned_procs.get(port)


def _proc_exited_early(service_tag: str, api_base: str, proc: "subprocess.Popen") -> bool:
    """Return True and log the exit if ``proc`` has terminated.

    Centralizes the "did our child die before it could serve?" check so
    both wait loops log a consistent diagnostic.
    """
    rc = proc.poll()
    if rc is None:
        return False
    # Be honest: stderr is DEVNULL on every spawn path in this module,
    # so any claim like "re-run with stderr capture" would be
    # unfollowable. Point the user at the actually-achievable
    # diagnostic path — rerunning the spawn command manually, which is
    # logged at DEBUG at spawn time ("Starting llama-server in <mode>
    # mode: ...") and therefore available whenever the MCP client
    # surfaces DEBUG-level logs.
    log_warning(
        f"[{service_tag.upper()}] llama-server (PID {proc.pid}) exited with "
        f"code {rc} before becoming ready on {api_base}. This typically "
        f"means the port was grabbed by another process in the probe/"
        f"spawn window, the model file is corrupt, or required arguments "
        f"are missing. llama-server's stderr was discarded (spawn uses "
        f"stderr=subprocess.DEVNULL). To see the underlying error, copy "
        f"the 'Starting llama-server in <mode> mode: ...' debug line from "
        f"your MCP log and re-run that command manually in a terminal."
    )
    return True


def _wait_for_llm_ready(
    api_base: str,
    max_wait: float = DEFAULT_SERVICE_WAIT_TIMEOUT,
    poll_interval: float = 1.0,
) -> bool:
    """Wait for LLM server to become ready.

    Polls both the HTTP health endpoint and (if available) the Popen
    handle registered by ``_start_llama_server_unlocked``. If the
    child exits before responding — typically a silent bind failure
    from a TOCTOU race in the preflight check — this returns False
    immediately with a specific log line instead of blocking until
    ``max_wait`` expires.

    Args:
        api_base: API base URL
        max_wait: Maximum time to wait in seconds
        poll_interval: Time between health checks

    Returns:
        True if server became ready, False if timeout or early exit
    """
    port = _extract_port(api_base, default=DEFAULT_LLM_PORT)
    proc = _get_spawned_proc(port)
    elapsed = 0.0
    while elapsed < max_wait:
        if proc is not None and _proc_exited_early("LLM", api_base, proc):
            return False
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
    host: str = "",
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
    if not host:
        host = os.environ.get("LLAMA_SERVER_HOST", "127.0.0.1")
    # Mirror the normalization that _start_llama_server_unlocked does.
    # Without this, LLAMA_SERVER_HOST=localhost would leave the outer
    # wrapper's api_base pointing at http://localhost:{port}/v1 while
    # the inner function operates on 127.0.0.1 — bind and probe would
    # diverge for the same env-var value even though _check_llm_health
    # applies _normalize_probe_url internally. Normalize early and once.
    host = _normalize_loopback(host)

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
    host: str = "",
) -> bool:
    """Internal: Start llama-server without locking (caller must hold port lock).

    Args:
        model_path: Path to GGUF model file
        port: Port to listen on
        mode: "embedding" or "completion"
        context_size: Context window size
        host: Host to bind to (defaults to LLAMA_SERVER_HOST env var or 127.0.0.1)

    Returns:
        True if server started successfully

    Raises:
        RuntimeError: If llama-server cannot be found or downloaded
    """
    if not host:
        host = os.environ.get("LLAMA_SERVER_HOST", "127.0.0.1")
    host = _normalize_loopback(host)

    # Preflight: refuse to start if the target port is already held by
    # something we did not spawn. An orphan llama-server on the port would
    # otherwise make health checks succeed and mask genuine failures (F15
    # in windows-release-hardening). Explicit refusal > silent masking.
    #
    # Note: this is a fast-fail only. A TOCTOU race exists between the
    # probe and the Popen below; a colocated process could grab the port
    # in that window. The real-spawn failure then surfaces via
    # proc.poll() in the readiness wait, which logs the exit code
    # rather than hanging until the health-probe timeout.
    #
    # Pass ``host`` so the probe matches llama-server's actual bind
    # target — for a non-loopback host (LAN IP, container bridge), a
    # loopback-only probe would false-positive on unrelated local
    # services.
    available, conflict_pid = _check_port_available(port, host)
    service_tag = "Embedding" if mode == "embedding" else "LLM"
    if not available:
        with _pids_lock:
            is_ours = conflict_pid is not None and conflict_pid in _spawned_pids
        if not is_ours:
            message = _format_port_in_use_error(service_tag, port, conflict_pid)
            log_warning(f"[{service_tag.upper()}] {message}")
            raise RuntimeError(message)

    # Find or download the binary. _ensure_llama_server_binary serializes
    # concurrent callers so the LLM and embedding workers don't race on
    # the download archive file (which previously produced a misleading
    # "could not be downloaded" error for whichever worker lost the
    # race on a clean install).
    llama_server = _ensure_llama_server_binary()

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
        # Track PID for atexit cleanup AND Popen handle by port so
        # ``_wait_for_llm_ready`` / ``_wait_for_embedding_ready`` can
        # detect an immediate spawn-side exit (bind failure, invalid
        # args, missing DLL) instead of hanging until the health-probe
        # timeout. Locked together to preserve the pid/proc invariant.
        with _pids_lock:
            _spawned_pids.append(proc.pid)
            _spawned_procs[port] = proc
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

        # Get context size: config.toml > model registry > default
        # Config takes priority (user explicitly set it), then model spec, then default
        from watercooler.models import get_llm_context_size
        config_context_size = llm_config.context_size
        model_context_size = get_llm_context_size(model_name, default=DEFAULT_CONTEXT_SIZE)

        # Use config if explicitly set (not default), otherwise use model spec
        if config_context_size != DEFAULT_CONTEXT_SIZE:
            context_size = config_context_size
            log_debug(f"Using context_size={context_size} from config.toml (overrides default {DEFAULT_CONTEXT_SIZE})")
        else:
            context_size = model_context_size
            log_debug(f"Using context_size={context_size} from model registry (config used default {DEFAULT_CONTEXT_SIZE})")

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
    models_url = f"{_normalize_probe_url(api_base)}/models"
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

    Mirror of ``_wait_for_llm_ready``: polls both the HTTP health
    endpoint and the Popen handle so silent bind failures during the
    TOCTOU window are reported as an exit code rather than swallowed
    as a timeout.

    Args:
        api_base: API base URL
        max_wait: Maximum time to wait in seconds
        poll_interval: Time between health checks

    Returns:
        True if server became ready, False if timeout or early exit
    """
    port = _extract_port(api_base, default=DEFAULT_EMBEDDING_PORT)
    proc = _get_spawned_proc(port)
    elapsed = 0.0
    while elapsed < max_wait:
        if proc is not None and _proc_exited_early("EMBEDDING", api_base, proc):
            return False
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
    exe = ".exe" if sys.platform == "win32" else ""
    common_locations = [
        Path.home() / ".local" / "bin" / f"llama-server{exe}",
        Path.home() / ".watercooler" / "bin" / f"llama-server{exe}",
    ]
    if sys.platform != "win32":
        common_locations.insert(1, Path("/usr/local/bin/llama-server"))

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
    elif system == "windows" and machine in ("x86_64", "amd64"):
        asset_patterns.append(("win-cpu-x64", ".zip"))
    else:
        log_debug(f"Unsupported platform for llama-server download: {system}/{machine}")
        return None

    # Create target directory
    bin_dir = Path.home() / ".watercooler" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    binary_name = "llama-server.exe" if system == "windows" else "llama-server"
    target_binary = bin_dir / binary_name

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
    log_debug(f"[EMBEDDING] Resolving model spec for: {model_name}")
    try:
        model_spec = resolve_embedding_model(model_name)
    except ModelNotFoundError as e:
        log_warning(f"[EMBEDDING] Unknown model: {model_name}. {e}")
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
    log_debug(f"[EMBEDDING] Ensuring model available: {model_name}")
    try:
        model_path = ensure_model_available(model_name, verbose=False)
    except (ModelNotFoundError, ModelDownloadError) as e:
        log_warning(f"[EMBEDDING] Model download failed: {type(e).__name__}: {e}")
        _add_startup_warning(f"Could not prepare embedding model: {e}")
        return False

    log_debug(f"Model available at: {model_path}")

    # Parse API base to get host/port.
    # LLAMA_SERVER_HOST overrides the parsed hostname so operators can bind
    # to 0.0.0.0 for Docker container access without changing the config URL.
    parsed = urlparse(api_base)
    host = os.environ.get("LLAMA_SERVER_HOST") or parsed.hostname or "127.0.0.1"
    host = _normalize_loopback(host)
    port = parsed.port or DEFAULT_EMBEDDING_PORT

    # Start server. All platforms route through _start_embedding_direct,
    # which delegates to the generic _start_llama_server used by the LLM
    # path. Windows previously had its own _start_embedding_windows using
    # DETACHED_PROCESS without a DEVNULL stdin — that combination gave the
    # child an invalid stdin handle, which llama-server's Windows console
    # handler reads as a close event, so the server died immediately after
    # `main: starting the main loop...`. The unified path uses the same
    # spawn code that already works for the LLM on Windows.
    system = platform.system().lower()
    if system == "linux" and _try_systemctl_embedding():
        if _wait_for_embedding_ready(api_base, max_wait=10.0):
            return True
        # systemctl started watercooler-embedding but it did not become
        # ready within the wait window. The port is now held by the
        # systemd unit, so falling through to direct spawn would trip
        # the preflight's port-in-use check and misleadingly instruct
        # the user to kill "their orphan" — but that process is a
        # service they deliberately enabled. Surface the real
        # diagnostic instead and stop here.
        message = (
            f"systemctl --user started watercooler-embedding but the "
            f"service did not respond at {api_base} within 10 seconds.\n\n"
            f"Inspect the unit to find the underlying cause:\n"
            f"  systemctl --user status watercooler-embedding\n"
            f"  journalctl --user -u watercooler-embedding -n 200\n\n"
            f"If you want watercooler to manage llama-server directly "
            f"instead of via systemd, disable the unit:\n"
            f"  systemctl --user stop watercooler-embedding\n"
            f"  systemctl --user disable watercooler-embedding\n\n"
            f"Then restart your MCP client."
        )
        log_warning(f"[EMBEDDING] {message}")
        _add_startup_warning(message)
        return False
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


def _ensure_falkordb_restart_policy(docker_cmd: str) -> None:
    """Ensure the FalkorDB container has restart=unless-stopped policy.

    Existing containers created before the restart policy was added will
    have restart=no. This updates them so Docker auto-restarts FalkorDB
    after daemon restarts or SIGTERM events.
    """
    try:
        result = subprocess.run(
            [docker_cmd, "inspect", "falkordb", "--format", "{{.HostConfig.RestartPolicy.Name}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        current_policy = result.stdout.strip()
        if current_policy and current_policy not in ("unless-stopped", "always"):
            log_debug(f"Updating FalkorDB restart policy from '{current_policy}' to 'unless-stopped'")
            subprocess.run(
                [docker_cmd, "update", "--restart", "unless-stopped", "falkordb"],
                capture_output=True,
                timeout=10,
            )
    except subprocess.TimeoutExpired:
        log_debug("Docker command timed out checking FalkorDB restart policy")
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        log_debug(f"Could not check/update FalkorDB restart policy: {e}")


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
                # Container exists - ensure restart policy is set
                _ensure_falkordb_restart_policy(docker_cmd)

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
                        "--restart", "unless-stopped",
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

        # Plan v20 follow-on: in ``hybrid`` and ``proxy`` modes, T1/T2 graph
        # operations are routed to the hosted FalkorDB on Railway. The local
        # FalkorDB on 127.0.0.1:6379 isn't on any code path — auto-starting
        # it produces the "Local FalkorDB reachable but memory_ingest=remote"
        # mismatch warning every health-check, and risks shadowing the
        # hosted path if a regression accidentally re-enables an
        # in-process GraphitiBackend (design principle #9).
        try:
            from .config import get_watercooler_config
            transport = get_watercooler_config().mcp.transport
        except Exception as cfg_exc:
            # PR #656 review (LOW): a malformed config or unexpected
            # runtime error here would silently fall through to stdio
            # behavior — auto-starting a local FalkorDB even when the
            # operator's intent was hybrid. Log so the failure is
            # visible in operator-facing logs; behavior still falls
            # through to the conservative path so a broken config
            # doesn't lock the operator out of stdio mode entirely.
            log_error(
                "STARTUP: failed to resolve transport config; "
                "falling back to stdio (local FalkorDB auto-start "
                "may run unexpectedly): %s", cfg_exc,
            )
            transport = "stdio"
        if transport in ("hybrid", "proxy"):
            _update_service_status(
                "falkordb", ServiceState.DISABLED,
                message=f"Transport is '{transport}' — using hosted FalkorDB",
            )
            log_debug(
                f"Transport is '{transport}', skipping local FalkorDB "
                f"auto-start (hosted Railway FalkorDB owns T1/T2 in this mode)"
            )
            return

        # Only auto-start if Graphiti backend is enabled
        try:
            backend = get_memory_backend()
        except ValueError as exc:
            log_error("MEMORY config error: %s", exc)
            return
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
