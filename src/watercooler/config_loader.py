"""Configuration loading and merging for Watercooler.

Handles TOML loading, config discovery, deep merging, and environment overlay.

Note on dependencies:
    This module uses external dependencies (tomllib/tomli, pydantic) which differs
    from the stdlib-only policy for the core watercooler library. This is intentional:

    1. tomllib is stdlib in Python 3.11+, tomli is the backport for 3.10
    2. The config system is an optional enhancement - core thread operations
       (init, say, ack, etc.) work without loading config files
    3. Pydantic provides schema validation with clear error messages, essential
       for complex nested configuration
    4. These dependencies are already required by the MCP server component,
       so they add no additional burden for MCP users

    For pure stdlib usage, skip config files and use environment variables directly.
"""

from __future__ import annotations

import os
import sys
import threading
import warnings
from pathlib import Path
from typing import Any, Dict, Optional

# TOML loading: tomllib (3.11+) with tomli fallback
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore

from pydantic import ValidationError

from .config_schema import WatercoolerConfig


# Config file names
CONFIG_FILENAME = "config.toml"
CREDENTIALS_FILENAME = "credentials.toml"
CREDENTIALS_LEGACY_FILENAME = "credentials.json"

# Directory names
USER_CONFIG_DIR = ".watercooler"
PROJECT_CONFIG_DIR = ".watercooler"


class ConfigError(Exception):
    """Configuration loading or validation error."""

    pass


def _get_user_config_dir() -> Path:
    """Get user-level config directory (~/.watercooler/)."""
    return Path.home() / USER_CONFIG_DIR


def _get_project_config_dir(project_path: Optional[Path] = None) -> Optional[Path]:
    """Get project-level config directory (.watercooler/).

    Searches upward from project_path to find .watercooler/ directory.
    """
    if project_path is None:
        project_path = Path.cwd()

    if not project_path.is_absolute():
        project_path = project_path.resolve()

    # Search upward for .watercooler/ directory
    current = project_path
    while current != current.parent:
        config_dir = current / PROJECT_CONFIG_DIR
        if config_dir.is_dir():
            return config_dir
        current = current.parent

    return None


def _load_toml(path: Path) -> Dict[str, Any]:
    """Load a TOML file.

    Args:
        path: Path to TOML file

    Returns:
        Parsed TOML as dict

    Raises:
        ConfigError: If file cannot be loaded or parsed
    """
    if tomllib is None:
        raise ConfigError(
            "TOML support requires Python 3.11+ or 'tomli' package. "
            "Install with: pip install tomli"
        )

    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}")
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Invalid TOML in {path}: {e}")


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Deep merge two dictionaries.

    Override values take precedence. Nested dicts are merged recursively.
    Lists are replaced, not merged.

    Args:
        base: Base dictionary
        override: Override dictionary (takes precedence)

    Returns:
        Merged dictionary
    """
    result = base.copy()

    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value

    return result


def _env_to_config_key(env_var: str) -> tuple[list[str], str]:
    """Map environment variable to config path.

    Returns tuple of (section_path, key_name).

    Examples:
        WATERCOOLER_THREADS_PATTERN -> (["common"], "threads_pattern")
        WATERCOOLER_GIT_SSH_KEY -> (["mcp", "git"], "ssh_key")
        WATERCOOLER_ASYNC_SYNC -> (["mcp", "sync"], "async")
    """
    # Environment variable mapping
    ENV_MAPPING: Dict[str, tuple[list[str], str]] = {
        # Common
        "WATERCOOLER_THREADS_PATTERN": (["common"], "threads_pattern"),
        "WATERCOOLER_TEMPLATES": (["common"], "templates_dir"),
        "WATERCOOLER_THREADS_SUFFIX": (["common"], "threads_suffix"),
        # MCP core
        "WATERCOOLER_DIR": (["mcp"], "threads_dir"),
        "WATERCOOLER_THREADS_BASE": (["mcp"], "threads_base"),
        "WATERCOOLER_AGENT": (["mcp"], "default_agent"),
        "WATERCOOLER_AGENT_TAG": (["mcp"], "agent_tag"),
        "WATERCOOLER_AUTO_BRANCH": (["mcp"], "auto_branch"),
        "WATERCOOLER_AUTO_PROVISION": (["mcp"], "auto_provision"),
        "WATERCOOLER_MCP_TRANSPORT": (["mcp"], "transport"),
        "WATERCOOLER_MCP_HOST": (["mcp"], "host"),
        "WATERCOOLER_MCP_PORT": (["mcp"], "port"),
        # MCP git
        "WATERCOOLER_GIT_AUTHOR": (["mcp", "git"], "author"),
        "WATERCOOLER_GIT_EMAIL": (["mcp", "git"], "email"),
        "WATERCOOLER_GIT_SSH_KEY": (["mcp", "git"], "ssh_key"),
        # MCP sync
        "WATERCOOLER_ASYNC_SYNC": (["mcp", "sync"], "async_sync"),
        "WATERCOOLER_BATCH_WINDOW": (["mcp", "sync"], "batch_window"),
        "WATERCOOLER_SYNC_INTERVAL": (["mcp", "sync"], "interval"),
        "WATERCOOLER_SYNC_MAX_RETRIES": (["mcp", "sync"], "max_retries"),
        "WATERCOOLER_SYNC_MAX_BACKOFF": (["mcp", "sync"], "max_backoff"),
        # MCP logging
        "WATERCOOLER_LOG_LEVEL": (["mcp", "logging"], "level"),
        "WATERCOOLER_LOG_DIR": (["mcp", "logging"], "dir"),
        "WATERCOOLER_LOG_MAX_BYTES": (["mcp", "logging"], "max_bytes"),
        "WATERCOOLER_LOG_BACKUP_COUNT": (["mcp", "logging"], "backup_count"),
        "WATERCOOLER_LOG_DISABLE_FILE": (["mcp", "logging"], "disable_file"),
        # Validation
        "WATERCOOLER_VALIDATE_ON_WRITE": (["validation"], "on_write"),
        "WATERCOOLER_FAIL_ON_VIOLATION": (["validation"], "fail_on_violation"),
        # Memory global
        "WATERCOOLER_MEMORY_BACKEND": (["memory"], "backend"),
        "WATERCOOLER_MEMORY_QUEUE": (["memory"], "queue_enabled"),
        # Memory LLM
        "LLM_API_KEY": (["memory", "llm"], "api_key"),
        "LLM_API_BASE": (["memory", "llm"], "api_base"),
        "LLM_MODEL": (["memory", "llm"], "model"),
        # Memory embedding
        "EMBEDDING_API_KEY": (["memory", "embedding"], "api_key"),
        "EMBEDDING_API_BASE": (["memory", "embedding"], "api_base"),
        "EMBEDDING_MODEL": (["memory", "embedding"], "model"),
        "EMBEDDING_DIM": (["memory", "embedding"], "dim"),
        # Memory database
        "FALKORDB_HOST": (["memory", "database"], "host"),
        "FALKORDB_PORT": (["memory", "database"], "port"),
        "FALKORDB_PASSWORD": (["memory", "database"], "password"),
        # Graphiti-specific
        "WATERCOOLER_GRAPHITI_RERANKER": (["memory", "graphiti"], "reranker"),
        "WATERCOOLER_GRAPHITI_PATH": (["memory", "graphiti"], "path"),
    }

    return ENV_MAPPING.get(env_var, ([], env_var))


def _apply_env_overlay(config_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Apply environment variable overrides to config dict.

    Environment variables have highest priority after CLI args.
    """
    result = config_dict.copy()

    # Known env vars to check
    env_vars = [
        "WATERCOOLER_THREADS_PATTERN",
        "WATERCOOLER_TEMPLATES",
        "WATERCOOLER_THREADS_SUFFIX",
        "WATERCOOLER_DIR",
        "WATERCOOLER_THREADS_BASE",
        "WATERCOOLER_AGENT",
        "WATERCOOLER_AGENT_TAG",
        "WATERCOOLER_AUTO_BRANCH",
        "WATERCOOLER_AUTO_PROVISION",
        "WATERCOOLER_MCP_TRANSPORT",
        "WATERCOOLER_MCP_HOST",
        "WATERCOOLER_MCP_PORT",
        "WATERCOOLER_GIT_AUTHOR",
        "WATERCOOLER_GIT_EMAIL",
        "WATERCOOLER_GIT_SSH_KEY",
        "WATERCOOLER_ASYNC_SYNC",
        "WATERCOOLER_BATCH_WINDOW",
        "WATERCOOLER_SYNC_INTERVAL",
        "WATERCOOLER_SYNC_MAX_RETRIES",
        "WATERCOOLER_SYNC_MAX_BACKOFF",
        "WATERCOOLER_LOG_LEVEL",
        "WATERCOOLER_LOG_DIR",
        "WATERCOOLER_LOG_MAX_BYTES",
        "WATERCOOLER_LOG_BACKUP_COUNT",
        "WATERCOOLER_LOG_DISABLE_FILE",
        "WATERCOOLER_VALIDATE_ON_WRITE",
        "WATERCOOLER_FAIL_ON_VIOLATION",
        # Memory settings
        "WATERCOOLER_MEMORY_BACKEND",
        "WATERCOOLER_MEMORY_QUEUE",
        "LLM_API_KEY",
        "LLM_API_BASE",
        "LLM_MODEL",
        "EMBEDDING_API_KEY",
        "EMBEDDING_API_BASE",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIM",
        "FALKORDB_HOST",
        "FALKORDB_PORT",
        "FALKORDB_PASSWORD",
        "WATERCOOLER_GRAPHITI_RERANKER",
        "WATERCOOLER_GRAPHITI_PATH",
    ]

    for env_var in env_vars:
        value = os.getenv(env_var)
        if value is None:
            continue

        section_path, key_name = _env_to_config_key(env_var)
        if not section_path:
            continue

        # Navigate to correct section
        current = result
        for section in section_path:
            if section not in current:
                current[section] = {}
            current = current[section]

        # Set value (type conversion happens during Pydantic validation)
        current[key_name] = value

    # Special case: WATERCOOLER_MEMORY_DISABLED has inverted logic
    # disabled=1/true/yes → enabled=False
    disabled_value = os.getenv("WATERCOOLER_MEMORY_DISABLED", "").lower()
    if disabled_value in ("1", "true", "yes"):
        if "memory" not in result:
            result["memory"] = {}
        result["memory"]["enabled"] = False

    return result


def load_config(
    project_path: Optional[Path] = None,
    skip_env: bool = False,
) -> WatercoolerConfig:
    """Load and merge Watercooler configuration.

    Discovery order (later sources override earlier):
    1. Built-in defaults
    2. User config (~/.watercooler/config.toml)
    3. Project config (.watercooler/config.toml)
    4. Environment variables (unless skip_env=True)

    Args:
        project_path: Project directory for config discovery
        skip_env: Skip environment variable overlay

    Returns:
        Merged WatercoolerConfig

    Raises:
        ConfigError: If config files are invalid
    """
    config_dict: Dict[str, Any] = {}

    # 1. User config
    user_config_path = _get_user_config_dir() / CONFIG_FILENAME
    if user_config_path.exists():
        try:
            user_config = _load_toml(user_config_path)
            config_dict = _deep_merge(config_dict, user_config)
        except ConfigError as e:
            # User config is optional, warn but continue
            warnings.warn(
                f"Skipping invalid user config at {user_config_path}: {e}",
                UserWarning,
            )

    # 2. Project config
    project_config_dir = _get_project_config_dir(project_path)
    if project_config_dir:
        project_config_path = project_config_dir / CONFIG_FILENAME
        if project_config_path.exists():
            try:
                project_config = _load_toml(project_config_path)
                config_dict = _deep_merge(config_dict, project_config)
            except ConfigError as e:
                # Project config errors should be reported
                raise ConfigError(f"Invalid project config: {e}")

    # 3. Environment overlay
    if not skip_env:
        config_dict = _apply_env_overlay(config_dict)

    # 4. Validate and create config object
    try:
        return WatercoolerConfig.model_validate(config_dict)
    except ValidationError as e:
        raise ConfigError(f"Config validation failed:\n{e}")


def get_config_paths(project_path: Optional[Path] = None) -> Dict[str, Optional[Path]]:
    """Get paths to all config files.

    Returns dict with keys: user_config, project_config, user_credentials, project_credentials
    """
    user_dir = _get_user_config_dir()
    project_dir = _get_project_config_dir(project_path)

    return {
        "user_config": user_dir / CONFIG_FILENAME if user_dir else None,
        "project_config": project_dir / CONFIG_FILENAME if project_dir else None,
        "user_credentials": user_dir / CREDENTIALS_FILENAME if user_dir else None,
        "project_credentials": project_dir / CREDENTIALS_FILENAME if project_dir else None,
    }


def ensure_config_dir(user: bool = True, project_path: Optional[Path] = None) -> Path:
    """Ensure config directory exists.

    Args:
        user: Create user config dir (~/.watercooler/)
        project_path: Create project config dir (.watercooler/)

    Returns:
        Path to created/existing config directory
    """
    if user:
        config_dir = _get_user_config_dir()
    else:
        if project_path is None:
            project_path = Path.cwd()
        config_dir = project_path / PROJECT_CONFIG_DIR

    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


# Global cached config (thread-safe)
_cached_config: Optional[WatercoolerConfig] = None
_cached_project_path: Optional[Path] = None
_config_lock = threading.Lock()


def get_config(project_path: Optional[Path] = None, force_reload: bool = False) -> WatercoolerConfig:
    """Get cached config, loading if necessary.

    Thread-safe: Uses a lock to prevent race conditions when multiple
    threads access the config cache concurrently.

    Args:
        project_path: Project directory for config discovery
        force_reload: Force reload from disk

    Returns:
        Cached or newly loaded WatercoolerConfig
    """
    global _cached_config, _cached_project_path

    # Normalize path for comparison (treat empty Path as None)
    if project_path and str(project_path):
        normalized_path = project_path.resolve()
    else:
        normalized_path = None

    with _config_lock:
        if (
            force_reload
            or _cached_config is None
            or _cached_project_path != normalized_path
        ):
            _cached_config = load_config(project_path)
            _cached_project_path = normalized_path

        return _cached_config


def clear_config_cache() -> None:
    """Clear cached config (thread-safe)."""
    global _cached_config, _cached_project_path
    with _config_lock:
        _cached_config = None
        _cached_project_path = None
