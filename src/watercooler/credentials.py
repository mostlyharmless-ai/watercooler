"""Credentials management for Watercooler.

Handles loading, migration, and secure storage of credentials.

Note on dependencies:
    This module uses external dependencies (pydantic, tomllib/tomli, tomlkit) which
    differs from the stdlib-only policy for the core watercooler library. This is
    intentional - credentials management is an optional enhancement for users who
    prefer file-based credential storage over environment variables.

    For pure stdlib usage, set GITHUB_TOKEN or GH_TOKEN environment variables directly.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
import warnings
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

# TOML writing requires tomlkit (for preserving comments) or tomli_w
try:
    import tomlkit
    HAS_TOMLKIT = True
except ImportError:
    tomlkit = None  # type: ignore
    HAS_TOMLKIT = False

# TOML reading
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore


CREDENTIALS_FILENAME = "credentials.toml"
CREDENTIALS_LEGACY_FILENAME = "credentials.json"
USER_CONFIG_DIR = ".watercooler"


class GitHubCredentials(BaseModel):
    """GitHub authentication credentials."""

    token: str = Field(
        default="",
        description="GitHub personal access token",
    )
    ssh_key: str = Field(
        default="",
        description="Path to SSH private key",
    )


class DashboardCredentials(BaseModel):
    """Dashboard authentication credentials."""

    session_secret: str = Field(
        default="",
        description="Session encryption secret (self-hosted)",
    )


class DeepSeekCredentials(BaseModel):
    """DeepSeek API credentials for LLM summarization.

    .. deprecated::
        Use OpenAICredentials or other provider-specific credentials instead.
        DeepSeek is kept for backward compatibility only.
    """

    api_key: str = Field(
        default="",
        description="DeepSeek API key",
    )


# =============================================================================
# Provider API Credentials
# =============================================================================


class OpenAICredentials(BaseModel):
    """OpenAI API credentials."""

    api_key: str = Field(
        default="",
        description="OpenAI API key (sk-...)",
    )


class AnthropicCredentials(BaseModel):
    """Anthropic API credentials."""

    api_key: str = Field(
        default="",
        description="Anthropic API key (sk-ant-...)",
    )


class GroqCredentials(BaseModel):
    """Groq API credentials."""

    api_key: str = Field(
        default="",
        description="Groq API key (gsk_...)",
    )


class VoyageCredentials(BaseModel):
    """Voyage AI credentials for embeddings."""

    api_key: str = Field(
        default="",
        description="Voyage API key",
    )


class GoogleCredentials(BaseModel):
    """Google/Gemini API credentials."""

    api_key: str = Field(
        default="",
        description="Google API key",
    )


class Credentials(BaseModel):
    """All Watercooler credentials.

    Credentials are stored in ~/.watercooler/credentials.toml separately from
    configuration (config.toml) for security:
    - credentials.toml: secrets (API keys, tokens) - gitignored, 0600 permissions
    - config.toml: settings (api_base, model, etc.) - can be version controlled
    """

    # GitHub authentication
    github: GitHubCredentials = Field(default_factory=GitHubCredentials)
    # Dashboard (self-hosted)
    dashboard: DashboardCredentials = Field(default_factory=DashboardCredentials)

    # Provider API keys (new)
    openai: OpenAICredentials = Field(default_factory=OpenAICredentials)
    anthropic: AnthropicCredentials = Field(default_factory=AnthropicCredentials)
    groq: GroqCredentials = Field(default_factory=GroqCredentials)
    voyage: VoyageCredentials = Field(default_factory=VoyageCredentials)
    google: GoogleCredentials = Field(default_factory=GoogleCredentials)

    # Legacy (deprecated)
    deepseek: DeepSeekCredentials = Field(default_factory=DeepSeekCredentials)


def _get_user_credentials_path() -> Path:
    """Get path to user credentials file."""
    return Path.home() / USER_CONFIG_DIR / CREDENTIALS_FILENAME


def _get_legacy_credentials_path() -> Path:
    """Get path to legacy JSON credentials file."""
    return Path.home() / USER_CONFIG_DIR / CREDENTIALS_LEGACY_FILENAME


def _secure_file_permissions(path: Path) -> None:
    """Set secure file permissions (owner read/write only).

    On Windows, this is a no-op as permissions work differently.
    """
    if os.name == "posix":
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        except OSError as e:
            warnings.warn(
                f"Could not set secure permissions on {path}: {e}. "
                "Credentials file may be readable by other users.",
                UserWarning,
            )


# Maximum file size for JSON migration (1MB - credentials should be small)
_MAX_JSON_SIZE_BYTES = 1 * 1024 * 1024


def _migrate_json_to_toml(json_path: Path, toml_path: Path) -> bool:
    """Migrate legacy JSON credentials to TOML format.

    Args:
        json_path: Path to legacy JSON file
        toml_path: Path to new TOML file

    Returns:
        True if migration successful, False otherwise
    """
    if not HAS_TOMLKIT:
        warnings.warn(
            "Cannot migrate credentials: tomlkit not installed. "
            "Install with: pip install tomlkit",
            UserWarning,
        )
        return False

    try:
        # Check file size to prevent OOM from maliciously large files
        file_size = json_path.stat().st_size
        if file_size > _MAX_JSON_SIZE_BYTES:
            warnings.warn(
                f"Credentials file too large ({file_size} bytes). "
                f"Maximum allowed: {_MAX_JSON_SIZE_BYTES} bytes. Skipping migration.",
                UserWarning,
            )
            return False

        # Read JSON
        with open(json_path, "r") as f:
            data = json.load(f)

        # Convert to TOML structure
        toml_data: Dict[str, Any] = {}

        # Map known JSON fields to TOML structure
        if "github_token" in data:
            if "github" not in toml_data:
                toml_data["github"] = {}
            toml_data["github"]["token"] = data["github_token"]

        if "github_ssh_key" in data or "ssh_key" in data:
            if "github" not in toml_data:
                toml_data["github"] = {}
            toml_data["github"]["ssh_key"] = data.get("github_ssh_key", data.get("ssh_key", ""))

        if "session_secret" in data:
            if "dashboard" not in toml_data:
                toml_data["dashboard"] = {}
            toml_data["dashboard"]["session_secret"] = data["session_secret"]

        # Handle nested structures
        if "github" in data and isinstance(data["github"], dict):
            if "github" not in toml_data:
                toml_data["github"] = {}
            toml_data["github"].update(data["github"])

        if "dashboard" in data and isinstance(data["dashboard"], dict):
            if "dashboard" not in toml_data:
                toml_data["dashboard"] = {}
            toml_data["dashboard"].update(data["dashboard"])

        # Write TOML
        toml_path.parent.mkdir(parents=True, exist_ok=True)

        doc = tomlkit.document()
        doc.add(tomlkit.comment(" Watercooler Credentials"))
        doc.add(tomlkit.comment(" Auto-migrated from credentials.json"))
        doc.add(tomlkit.comment(" Keep this file secure - do not commit to version control"))
        doc.add(tomlkit.nl())

        for section, values in toml_data.items():
            if isinstance(values, dict):
                table = tomlkit.table()
                for key, value in values.items():
                    table.add(key, value)
                doc.add(section, table)
            else:
                doc.add(section, values)

        with open(toml_path, "w") as f:
            f.write(tomlkit.dumps(doc))

        # Secure permissions
        _secure_file_permissions(toml_path)

        # Rename old file to .bak
        backup_path = json_path.with_suffix(".json.bak")
        json_path.rename(backup_path)

        return True

    except (json.JSONDecodeError, OSError, KeyError) as e:
        warnings.warn(f"Failed to migrate credentials: {e}", UserWarning)
        return False


def _load_toml_credentials(path: Path) -> Dict[str, Any]:
    """Load credentials from TOML file."""
    if tomllib is None:
        raise RuntimeError(
            "TOML support requires Python 3.11+ or 'tomli' package. "
            "Install with: pip install tomli"
        )

    with open(path, "rb") as f:
        return tomllib.load(f)


def load_credentials(auto_migrate: bool = True) -> Credentials:
    """Load credentials from TOML file.

    Automatically migrates from legacy JSON format if needed.

    Args:
        auto_migrate: Automatically migrate from JSON if TOML not found

    Returns:
        Loaded Credentials object
    """
    toml_path = _get_user_credentials_path()
    json_path = _get_legacy_credentials_path()

    # Try TOML first
    if toml_path.exists():
        try:
            data = _load_toml_credentials(toml_path)
            return Credentials.model_validate(data)
        except Exception as e:
            warnings.warn(f"Error loading credentials: {e}", UserWarning)
            return Credentials()

    # Try migration from JSON
    if auto_migrate and json_path.exists():
        if _migrate_json_to_toml(json_path, toml_path):
            # Try loading the migrated file
            try:
                data = _load_toml_credentials(toml_path)
                return Credentials.model_validate(data)
            except Exception:
                pass

    # Return empty credentials
    return Credentials()


def save_credentials(creds: Credentials) -> Path:
    """Save credentials to TOML file.

    Args:
        creds: Credentials to save

    Returns:
        Path to saved file

    Raises:
        RuntimeError: If tomlkit not installed
    """
    if not HAS_TOMLKIT:
        raise RuntimeError(
            "Saving credentials requires tomlkit. Install with: pip install tomlkit"
        )

    toml_path = _get_user_credentials_path()
    toml_path.parent.mkdir(parents=True, exist_ok=True)

    doc = tomlkit.document()
    doc.add(tomlkit.comment(" Watercooler Credentials"))
    doc.add(tomlkit.comment(" Keep this file secure - do not commit to version control"))
    doc.add(tomlkit.nl())

    # GitHub section
    if creds.github.token or creds.github.ssh_key:
        github = tomlkit.table()
        if creds.github.token:
            github.add("token", creds.github.token)
        if creds.github.ssh_key:
            github.add("ssh_key", creds.github.ssh_key)
        doc.add("github", github)

    # Dashboard section
    if creds.dashboard.session_secret:
        dashboard = tomlkit.table()
        dashboard.add("session_secret", creds.dashboard.session_secret)
        doc.add("dashboard", dashboard)

    # Provider API keys
    if creds.openai.api_key:
        openai = tomlkit.table()
        openai.add("api_key", creds.openai.api_key)
        doc.add("openai", openai)

    if creds.anthropic.api_key:
        anthropic = tomlkit.table()
        anthropic.add("api_key", creds.anthropic.api_key)
        doc.add("anthropic", anthropic)

    if creds.groq.api_key:
        groq = tomlkit.table()
        groq.add("api_key", creds.groq.api_key)
        doc.add("groq", groq)

    if creds.voyage.api_key:
        voyage = tomlkit.table()
        voyage.add("api_key", creds.voyage.api_key)
        doc.add("voyage", voyage)

    if creds.google.api_key:
        google = tomlkit.table()
        google.add("api_key", creds.google.api_key)
        doc.add("google", google)

    # Legacy deepseek (deprecated)
    if creds.deepseek.api_key:
        deepseek = tomlkit.table()
        deepseek.add("api_key", creds.deepseek.api_key)
        doc.add("deepseek", deepseek)

    with open(toml_path, "w") as f:
        f.write(tomlkit.dumps(doc))

    _secure_file_permissions(toml_path)
    return toml_path


def get_github_token() -> Optional[str]:
    """Get GitHub token from credentials or environment.

    Priority: Environment > Credentials file
    """
    # Check environment first
    env_token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if env_token:
        return env_token

    # Load from credentials
    creds = load_credentials()
    return creds.github.token or None


def get_provider_api_key(provider: str) -> Optional[str]:
    """Get API key for a provider from credentials.toml.

    This is the canonical way to retrieve provider-specific API keys from
    the credentials file. Provider names are case-insensitive.

    Supported providers:
    - openai: OpenAI API key
    - anthropic: Anthropic API key
    - groq: Groq API key
    - voyage: Voyage AI API key
    - google: Google/Gemini API key
    - deepseek: DeepSeek API key (legacy)

    Args:
        provider: Provider name (e.g., "openai", "anthropic", "groq")

    Returns:
        API key string if found and non-empty, None otherwise

    Example:
        >>> key = get_provider_api_key("openai")
        >>> if key:
        ...     print("OpenAI key found in credentials.toml")
    """
    creds = load_credentials()
    provider_lower = provider.lower()

    # Map provider name to credential attribute
    provider_creds = getattr(creds, provider_lower, None)
    if provider_creds and hasattr(provider_creds, "api_key"):
        key = provider_creds.api_key
        return key if key else None

    return None


def get_ssh_key_path() -> Optional[Path]:
    """Get SSH key path from credentials or environment.

    Priority: Environment > Credentials file
    """
    # Check environment first
    env_key = os.getenv("WATERCOOLER_GIT_SSH_KEY")
    if env_key:
        return Path(env_key).expanduser()

    # Load from credentials
    creds = load_credentials()
    if creds.github.ssh_key:
        return Path(creds.github.ssh_key).expanduser()

    return None


def _get_user_config_path() -> Path:
    """Get path to user config file."""
    return Path.home() / USER_CONFIG_DIR / "config.toml"


def _load_config() -> Dict[str, Any]:
    """Load config from config.toml file."""
    config_path = _get_user_config_path()
    if not config_path.exists():
        return {}

    if tomllib is None:
        return {}

    try:
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def get_memory_graph_config() -> Dict[str, Any]:
    """Get memory_graph section from config.toml.

    Returns:
        Dict with llm, embedding, and chunking settings.

    .. deprecated::
        Memory backend config is now via environment variables.
        Use LLM_API_KEY, LLM_API_BASE, EMBEDDING_API_KEY, etc. directly.
        See docs/ENVIRONMENT_VARS.md for complete list.
    """
    warnings.warn(
        "get_memory_graph_config() is deprecated. "
        "Use environment variables (LLM_API_KEY, EMBEDDING_API_KEY, etc.) instead. "
        "See docs/ENVIRONMENT_VARS.md",
        DeprecationWarning,
        stacklevel=2,
    )
    # Delegate to unified config system (eliminates duplicate TOML loading)
    try:
        from .config_facade import config
        full_config = config.full()
        return getattr(full_config, "memory_graph", {})
    except Exception as e:
        # Fallback to old behavior if config system unavailable
        logging.debug(f"Failed to load memory_graph config from facade, using fallback: {e}")
        config_data = _load_config()
        return config_data.get("memory_graph", {})


def _get_server_defaults(server_type: str) -> Dict[str, Any]:
    """Get default server config from unified config system.

    This replaces hardcoded localhost defaults with values from the
    unified memory_config module, ensuring env vars are respected.
    """
    try:
        from .memory_config import resolve_llm_config, resolve_embedding_config

        if server_type == "llm":
            llm = resolve_llm_config()
            return {
                "api_base": llm.api_base,
                "model": llm.model,
                "timeout": 120.0,
                "max_tokens": 256,
            }
        elif server_type == "embedding":
            emb = resolve_embedding_config()
            return {
                "api_base": emb.api_base,
                "model": emb.model,
                "timeout": 60.0,
                "batch_size": 32,
            }
    except ImportError:
        pass

    # Fallback to env vars if memory_config not available
    if server_type == "llm":
        return {
            "api_base": os.getenv("LLM_API_BASE", "https://api.openai.com/v1"),
            "model": os.getenv("LLM_MODEL", "gpt-4o-mini"),
            "timeout": 120.0,
            "max_tokens": 256,
        }
    else:  # embedding
        return {
            "api_base": os.getenv("EMBEDDING_API_BASE", "http://localhost:8080/v1"),
            "model": os.getenv("EMBEDDING_MODEL", "bge-m3"),
            "timeout": 60.0,
            "batch_size": 32,
        }


def get_server_config(server_type: str) -> Dict[str, Any]:
    """Get unified server configuration.

    .. deprecated::
        Use environment variables directly (LLM_API_BASE, LLM_MODEL, etc.).
        See docs/ENVIRONMENT_VARS.md for complete list.

    Loads configuration with the following priority:
    1. Environment variables (SERVER_TYPE_SETTING, e.g., LLM_API_BASE)
    2. [servers.{type}] section in config.toml (preferred)
    3. [memory_graph.{type}] section in config.toml (deprecated, for backwards compat)
    4. Built-in defaults for local development

    Args:
        server_type: Either "llm" or "embedding"

    Returns:
        Dict with server configuration (api_base, model, timeout, etc.)

    Example:
        >>> config = get_server_config("llm")
        >>> print(config["api_base"])
        http://localhost:11434/v1
    """
    warnings.warn(
        f"get_server_config('{server_type}') is deprecated. "
        f"Use environment variables ({server_type.upper()}_API_BASE, etc.) directly. "
        "See docs/ENVIRONMENT_VARS.md",
        DeprecationWarning,
        stacklevel=2,
    )
    if server_type not in ("llm", "embedding"):
        raise ValueError(f"Unknown server type: {server_type}. Use 'llm' or 'embedding'.")

    # Start with defaults from unified config
    result = _get_server_defaults(server_type)

    # Load from config file
    config = _load_config()

    # Check new [servers.{type}] section first
    servers_config = config.get("servers", {}).get(server_type, {})

    # Fall back to deprecated [memory_graph.{type}] section
    if not servers_config:
        mg_config = config.get("memory_graph", {})
        servers_config = mg_config.get(server_type, {})

    # Merge config file values
    result.update(servers_config)

    # Environment variables override everything
    env_prefix = server_type.upper()
    env_mappings = {
        "api_base": f"{env_prefix}_API_BASE",
        "model": f"{env_prefix}_MODEL",
        "timeout": f"{env_prefix}_TIMEOUT",
        "max_tokens": f"{env_prefix}_MAX_TOKENS",
        "batch_size": f"{env_prefix}_BATCH_SIZE",
    }

    for key, env_var in env_mappings.items():
        env_val = os.getenv(env_var)
        if env_val:
            # Convert numeric values
            if key in ("timeout", "max_tokens", "batch_size"):
                try:
                    result[key] = float(env_val) if key == "timeout" else int(env_val)
                except ValueError:
                    pass
            else:
                result[key] = env_val

    return result


def get_deepseek_api_key() -> Optional[str]:
    """Get DeepSeek API key from credentials or environment.

    .. deprecated::
        Use LLM_API_KEY environment variable directly.

    Priority: Environment > Credentials file
    """
    warnings.warn(
        "get_deepseek_api_key() is deprecated. Use os.getenv('LLM_API_KEY') instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    # Check environment first
    env_key = os.getenv("DEEPSEEK_API_KEY")
    if env_key:
        return env_key

    # Load from credentials
    creds = load_credentials()
    return creds.deepseek.api_key or None


def get_deepseek_api_base() -> str:
    """Get DeepSeek API base URL from config or environment.

    .. deprecated::
        Use LLM_API_BASE environment variable directly.

    Priority: Environment > config.toml > Default
    """
    warnings.warn(
        "get_deepseek_api_base() is deprecated. Use os.getenv('LLM_API_BASE') instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    # Check environment first
    env_base = os.getenv("LLM_API_BASE")
    if env_base:
        return env_base

    # Load from config.toml (will also warn)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        mg_config = get_memory_graph_config()
    llm_config = mg_config.get("llm", {})
    return llm_config.get("api_base", "https://api.deepseek.com/v1")


def get_deepseek_model() -> str:
    """Get DeepSeek model name from config or environment.

    .. deprecated::
        Use LLM_MODEL environment variable directly.

    Priority: Environment > config.toml > Default

    Available models (as of Dec 2025):
    - deepseek-chat: General purpose chat model
    - deepseek-v3.2: Reasoning-first model for agents
    - deepseek-v3.2-speciale: Extended reasoning capabilities
    """
    warnings.warn(
        "get_deepseek_model() is deprecated. Use os.getenv('LLM_MODEL') instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    # Check environment first
    env_model = os.getenv("DEEPSEEK_MODEL")
    if env_model:
        return env_model

    # Load from config.toml (will also warn)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        mg_config = get_memory_graph_config()
    llm_config = mg_config.get("llm", {})
    return llm_config.get("model", "deepseek-v3.2")


def get_embedding_api_base() -> str:
    """Get embedding API base URL from config or environment.

    .. deprecated::
        Use EMBEDDING_API_BASE environment variable directly.

    Priority: Environment > config.toml > Default
    """
    warnings.warn(
        "get_embedding_api_base() is deprecated. Use os.getenv('EMBEDDING_API_BASE') instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    # Check environment first
    env_base = os.getenv("EMBEDDING_API_BASE")
    if env_base:
        return env_base

    # Load from config.toml (will also warn)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        mg_config = get_memory_graph_config()
    emb_config = mg_config.get("embedding", {})
    return emb_config.get("api_base", "http://localhost:8080/v1")


def get_embedding_api_key() -> Optional[str]:
    """Get embedding API key from credentials or environment.

    .. deprecated::
        Use EMBEDDING_API_KEY environment variable directly.

    Priority: Environment > Credentials file
    """
    warnings.warn(
        "get_embedding_api_key() is deprecated. Use os.getenv('EMBEDDING_API_KEY') instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    # Check environment first
    env_key = os.getenv("EMBEDDING_API_KEY")
    if env_key:
        return env_key

    # Embedding typically doesn't need API key for local servers
    return None
