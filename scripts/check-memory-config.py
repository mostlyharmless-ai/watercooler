#!/usr/bin/env python3
"""Memory backend configuration checker.

Validates that all memory backends (Graphiti, LeanRAG, MemoryGraph) are
configured consistently and their servers are healthy.

Usage:
    python scripts/check-memory-config.py           # Warn-only (default)
    python scripts/check-memory-config.py --strict  # Exit non-zero on issues
    python scripts/check-memory-config.py --skip-health-check  # Skip server pings
    python scripts/check-memory-config.py --generate-env       # Output .env template
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# ANSI colors for terminal output
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"
BOLD = "\033[1m"


@dataclass
class ServerConfig:
    """Configuration for a single server (embedding or LLM)."""

    name: str
    url: Optional[str]
    key: Optional[str]
    model: Optional[str]


@dataclass
class BackendConfig:
    """Configuration for a memory backend."""

    name: str
    embedding: ServerConfig
    llm: ServerConfig


def get_env(var: str, default: Optional[str] = None) -> Optional[str]:
    """Get environment variable, returning None for empty strings."""
    value = os.environ.get(var, default)
    if value == "":
        return None
    return value


def check_server_health(url: str, timeout: float = 5.0) -> Tuple[bool, str]:
    """Check if a server is healthy by pinging its models endpoint.

    Args:
        url: Base URL of the OpenAI-compatible server
        timeout: Request timeout in seconds

    Returns:
        Tuple of (is_healthy, message)
    """
    try:
        import urllib.request
        import urllib.error
        import json

        # Try /v1/models endpoint (OpenAI-compatible)
        models_url = url.rstrip("/")
        if not models_url.endswith("/v1"):
            models_url = models_url + "/v1"
        models_url = models_url + "/models"

        req = urllib.request.Request(models_url, method="GET")
        req.add_header("Accept", "application/json")

        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode())
            models = data.get("data", [])
            if models:
                model_ids = [m.get("id", "unknown") for m in models[:3]]
                return True, f"healthy ({', '.join(model_ids)})"
            return True, "healthy (no models listed)"

    except urllib.error.URLError as e:
        return False, f"unreachable: {e.reason}"
    except json.JSONDecodeError:
        return False, "error: invalid JSON response"
    except Exception as e:
        # Avoid exposing sensitive info in error messages
        return False, f"error: {type(e).__name__}"


def check_falkordb_health(host: str, port: int, timeout: float = 5.0) -> Tuple[bool, str]:
    """Check if FalkorDB is healthy.

    Args:
        host: FalkorDB host
        port: FalkorDB port
        timeout: Connection timeout in seconds

    Returns:
        Tuple of (is_healthy, message)
    """
    try:
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()

        if result == 0:
            return True, "connected"
        return False, f"connection refused (port {port})"

    except socket.timeout:
        return False, "connection timeout"
    except socket.gaierror as e:
        return False, f"DNS resolution failed: {e.strerror}"
    except Exception as e:
        # Avoid exposing sensitive info in error messages
        return False, f"error: {type(e).__name__}"


def load_backend_configs() -> Dict[str, BackendConfig]:
    """Load configuration for all memory backends from environment."""

    backends = {}

    # Graphiti config
    backends["graphiti"] = BackendConfig(
        name="Graphiti",
        embedding=ServerConfig(
            name="EMBEDDING_API_BASE",
            url=get_env("EMBEDDING_API_BASE"),
            key=get_env("EMBEDDING_API_KEY"),
            model=get_env("EMBEDDING_MODEL"),
        ),
        llm=ServerConfig(
            name="LLM_API_BASE",
            url=get_env("LLM_API_BASE"),
            key=get_env("LLM_API_KEY"),
            model=get_env("LLM_MODEL"),
        ),
    )

    # LeanRAG config
    backends["leanrag"] = BackendConfig(
        name="LeanRAG",
        embedding=ServerConfig(
            name="GLM_BASE_URL",
            url=get_env("GLM_BASE_URL"),
            key=None,  # LeanRAG doesn't use embedding key
            model=get_env("GLM_EMBEDDING_MODEL") or get_env("GLM_MODEL"),
        ),
        llm=ServerConfig(
            name="DEEPSEEK_BASE_URL",
            url=get_env("DEEPSEEK_BASE_URL"),
            key=get_env("DEEPSEEK_API_KEY"),
            model=get_env("DEEPSEEK_MODEL"),
        ),
    )

    # MemoryGraph uses the same vars as Graphiti
    backends["memorygraph"] = BackendConfig(
        name="MemoryGraph",
        embedding=ServerConfig(
            name="EMBEDDING_API_BASE",
            url=get_env("EMBEDDING_API_BASE", "http://localhost:8080/v1"),
            key=get_env("EMBEDDING_API_KEY"),
            model=get_env("EMBEDDING_MODEL", "bge-m3"),
        ),
        llm=ServerConfig(
            name="LLM_API_BASE",
            url=get_env("LLM_API_BASE"),
            key=get_env("LLM_API_KEY"),
            model=get_env("LLM_MODEL"),
        ),
    )

    return backends


def normalize_url(url: Optional[str]) -> Optional[str]:
    """Normalize URL for comparison (remove trailing slashes, /v1 suffix)."""
    if url is None:
        return None
    url = url.rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url


def check_consistency(backends: Dict[str, BackendConfig]) -> Tuple[List[str], List[str]]:
    """Check if all backends point to the same servers.

    Returns:
        Tuple of (warnings, errors)
    """
    warnings = []
    errors = []

    # Group embedding URLs
    embedding_urls: Dict[str, List[str]] = {}
    for name, config in backends.items():
        url = normalize_url(config.embedding.url)
        if url:
            embedding_urls.setdefault(url, []).append(f"{config.name} ({config.embedding.name})")

    if len(embedding_urls) > 1:
        warnings.append(
            f"Embedding servers differ: {list(embedding_urls.items())}"
        )

    # Group LLM URLs
    llm_urls: Dict[str, List[str]] = {}
    for name, config in backends.items():
        url = normalize_url(config.llm.url)
        if url:
            llm_urls.setdefault(url, []).append(f"{config.name} ({config.llm.name})")

    if len(llm_urls) > 1:
        warnings.append(
            f"LLM servers differ: {list(llm_urls.items())}"
        )

    return warnings, errors


def print_header(text: str) -> None:
    """Print a section header."""
    print(f"\n{BOLD}{text}{RESET}")
    print("=" * len(text))


def print_status(label: str, ok: bool, message: str) -> None:
    """Print a status line."""
    icon = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
    print(f"  {icon} {label}: {message}")


def print_warning(message: str) -> None:
    """Print a warning."""
    print(f"  {YELLOW}⚠{RESET} {message}")


def generate_env_template() -> str:
    """Generate .env template with all memory config vars."""
    return """# Memory Backend Configuration
# ============================
# This file configures LLM and embedding servers for all memory backends.

# === Disable Switch ===
# Set to 1 to bypass all memory backend functionality
# WATERCOOLER_MEMORY_DISABLED=1

# === Graphiti Backend ===
WATERCOOLER_GRAPHITI_ENABLED=0

# === Embedding Server ===
# Uses llama-cpp-python with OpenAI-compatible API
# Example: llama-cpp-python[server] or any OpenAI-compatible endpoint
# Default: http://localhost:8080/v1
EMBEDDING_API_BASE=http://localhost:8080/v1
EMBEDDING_API_KEY=not-needed-for-local
EMBEDDING_MODEL=bge-m3

# LeanRAG uses different env var names (must match above)
GLM_BASE_URL=http://localhost:8080/v1
GLM_MODEL=bge-m3
GLM_EMBEDDING_MODEL=bge-m3

# === LLM Server ===
# Uses llama-cpp-python with OpenAI-compatible API, Ollama, or cloud provider
# Example: http://localhost:11434/v1 (Ollama) or cloud endpoint
# Default: http://localhost:8000/v1
LLM_API_BASE=http://localhost:8000/v1
LLM_API_KEY=not-needed-for-local
LLM_MODEL=local

# LeanRAG uses different env var names (must match above)
DEEPSEEK_BASE_URL=http://localhost:8000/v1
DEEPSEEK_API_KEY=not-needed-for-local
DEEPSEEK_MODEL=local

# === FalkorDB (Graph Database) ===
FALKORDB_HOST=localhost
FALKORDB_PORT=6379
# FALKORDB_PASSWORD=
"""


def main() -> int:
    """Run the configuration checker."""
    parser = argparse.ArgumentParser(
        description="Check memory backend configuration consistency and health."
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on any warnings or errors",
    )
    parser.add_argument(
        "--skip-health-check",
        action="store_true",
        help="Skip server health checks (for offline/CI use)",
    )
    parser.add_argument(
        "--generate-env",
        action="store_true",
        help="Output .env template and exit",
    )
    args = parser.parse_args()

    # Generate .env template if requested
    if args.generate_env:
        print(generate_env_template())
        return 0

    print(f"{BOLD}Memory Backend Configuration Check{RESET}")
    print("=" * 35)

    # Check disable switch (consistent with is_memory_disabled() in baseline_graph/sync.py)
    disable_val = get_env("WATERCOOLER_MEMORY_DISABLED") or ""
    if disable_val.lower() in ("1", "true", "yes"):
        print(f"\n{YELLOW}Memory backends disabled (WATERCOOLER_MEMORY_DISABLED=1){RESET}")
        print("  No configuration checks performed.")
        return 0

    # Load configurations
    backends = load_backend_configs()

    warnings: List[str] = []
    errors: List[str] = []

    # === Embedding Server ===
    print_header("Embedding Server")

    # Determine the embedding URL to check (prefer Graphiti, fallback to LeanRAG)
    embedding_url = backends["graphiti"].embedding.url or backends["leanrag"].embedding.url

    if embedding_url and not args.skip_health_check:
        healthy, message = check_server_health(embedding_url)
        print_status(embedding_url, healthy, message)
        if not healthy:
            errors.append(f"Embedding server unhealthy: {embedding_url}")
    elif embedding_url:
        print(f"  {embedding_url} (health check skipped)")
    else:
        warnings.append("No embedding server URL configured")
        print_warning("No embedding server URL configured")

    print("\n  Backend Configuration:")
    for name, config in backends.items():
        var_name = config.embedding.name
        url = config.embedding.url or "(not set)"
        print(f"    {config.name} ({var_name}): {url}")

    # === LLM Server ===
    print_header("LLM Server")

    # Determine the LLM URL to check
    llm_url = backends["graphiti"].llm.url or backends["leanrag"].llm.url

    if llm_url and not args.skip_health_check:
        healthy, message = check_server_health(llm_url)
        print_status(llm_url, healthy, message)
        if not healthy:
            errors.append(f"LLM server unhealthy: {llm_url}")
    elif llm_url:
        print(f"  {llm_url} (health check skipped)")
    else:
        warnings.append("No LLM server URL configured")
        print_warning("No LLM server URL configured")

    print("\n  Backend Configuration:")
    for name, config in backends.items():
        var_name = config.llm.name
        url = config.llm.url or "(not set)"
        print(f"    {config.name} ({var_name}): {url}")

    # === FalkorDB ===
    print_header("FalkorDB")

    falkordb_host = get_env("FALKORDB_HOST") or "localhost"
    port_str = get_env("FALKORDB_PORT") or "6379"
    try:
        falkordb_port = int(port_str)
    except ValueError:
        errors.append(f"FALKORDB_PORT must be a number, got: '{port_str}'")
        print_status("FALKORDB_PORT", False, f"invalid port: '{port_str}'")
        falkordb_port = None

    if falkordb_port is not None:
        if not args.skip_health_check:
            healthy, message = check_falkordb_health(falkordb_host, falkordb_port)
            print_status(f"{falkordb_host}:{falkordb_port}", healthy, message)
            if not healthy:
                warnings.append(f"FalkorDB not reachable at {falkordb_host}:{falkordb_port}")
        else:
            print(f"  {falkordb_host}:{falkordb_port} (health check skipped)")

    # === Consistency Check ===
    consistency_warnings, consistency_errors = check_consistency(backends)
    warnings.extend(consistency_warnings)
    errors.extend(consistency_errors)

    # === API Key Warnings ===
    print_header("API Keys")

    # Check for missing keys
    if not get_env("EMBEDDING_API_KEY"):
        print_warning("EMBEDDING_API_KEY not set (OK for local server)")
    else:
        print(f"  {GREEN}✓{RESET} EMBEDDING_API_KEY set")

    if not get_env("LLM_API_KEY"):
        print_warning("LLM_API_KEY not set (OK for local server)")
    else:
        print(f"  {GREEN}✓{RESET} LLM_API_KEY set")

    if not get_env("DEEPSEEK_API_KEY"):
        print_warning("DEEPSEEK_API_KEY not set (OK for local server)")
    else:
        print(f"  {GREEN}✓{RESET} DEEPSEEK_API_KEY set")

    # === Summary ===
    print_header("Summary")

    if warnings:
        print(f"\n{YELLOW}Warnings:{RESET}")
        for w in warnings:
            print(f"  {YELLOW}⚠{RESET} {w}")

    if errors:
        print(f"\n{RED}Errors:{RESET}")
        for e in errors:
            print(f"  {RED}✗{RESET} {e}")

    if not warnings and not errors:
        print(f"\n{GREEN}All backends configured consistently ✓{RESET}")
        return 0
    elif errors:
        print(f"\n{RED}Configuration has errors{RESET}")
        return 1 if args.strict else 0
    else:
        print(f"\n{YELLOW}Configuration has warnings{RESET}")
        if args.strict:
            print(f"\n{YELLOW}Tip: For local servers without API keys, set:{RESET}")
            print("  LLM_API_KEY=stub EMBEDDING_API_KEY=stub")
        return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
