"""Unified configuration facade for watercooler-cloud.

Single entry point for all configuration access:
- Path resolution (threads_dir, templates_dir)
- TOML config loading (user + project)
- Credential management
- Environment variable access
- Runtime context (for MCP)

Usage:
    from watercooler.config_facade import config

    # Simple paths (lightweight, stdlib-only)
    threads_dir = config.paths.threads_dir

    # Full config (lazy-loads TOML + Pydantic)
    cfg = config.full()
    log_level = cfg.mcp.logging.level

    # Runtime context (MCP)
    ctx = config.context(code_root="/path/to/repo")

    # Environment access (centralized with type helpers)
    level = config.env.get("WATERCOOLER_LOG_LEVEL", "INFO")
    enabled = config.env.get_bool("WATERCOOLER_FEATURE", True)

Architecture:
    - Lazy loading: Config components loaded only when accessed
    - Thread-safe: Uses existing locks in underlying modules
    - Backward compatible: Old imports continue working
    - Testing support: Easy reset for test isolation
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .config_schema import WatercoolerConfig


@dataclass(frozen=True)
class PathConfig:
    """Resolved filesystem paths.

    Attributes:
        threads_dir: Directory for thread markdown files
        templates_dir: Directory containing _TEMPLATE_*.md files
        code_root: Code repository root (if available)
    """
    threads_dir: Path
    templates_dir: Path
    code_root: Optional[Path] = None


class Config:
    """Unified configuration facade.

    Provides single entry point for all configuration access,
    consolidating the previously fragmented config system.

    All properties use lazy loading - config is only loaded when accessed.
    Thread-safety is provided by underlying modules (config_loader, credentials).

    Example:
        from watercooler.config_facade import config

        # Lightweight path access
        print(config.paths.threads_dir)

        # Full config with TOML loading
        cfg = config.full()
        print(cfg.mcp.logging.level)

        # Environment variables with type coercion
        debug = config.env.get_bool("DEBUG", False)

        # Credentials
        token = config.get_github_token()

        # Testing support
        config.reset()  # Clear cached state
    """

    def __init__(self):
        self._full_config: Optional[Any] = None
        self._credentials: Optional[Any] = None
        self._paths: Optional[PathConfig] = None

    @property
    def paths(self) -> PathConfig:
        """Get resolved paths (lightweight, stdlib-only).

        Paths are resolved using git-aware discovery:
        - threads_dir: CLI > env > git-aware default
        - templates_dir: CLI > env > project > bundled

        Returns:
            PathConfig with resolved paths

        Example:
            threads = config.paths.threads_dir
            templates = config.paths.templates_dir
        """
        if self._paths is None:
            from .path_resolver import resolve_threads_dir, resolve_templates_dir
            self._paths = PathConfig(
                threads_dir=resolve_threads_dir(),
                templates_dir=resolve_templates_dir()
            )
        return self._paths

    def full(self, project_path: Optional[Path] = None, force_reload: bool = False) -> WatercoolerConfig:
        """Get full configuration (lazy-loads TOML + Pydantic).

        Loads and merges configuration from:
        1. Built-in defaults (config_schema.py)
        2. User config (~/.watercooler/config.toml)
        3. Project config (.watercooler/config.toml)
        4. Environment variable overrides

        Args:
            project_path: Optional project directory (default: cwd)
            force_reload: Force reload even if cached

        Returns:
            WatercoolerConfig instance with full configuration

        Example:
            cfg = config.full()
            log_level = cfg.mcp.logging.level
            sync_enabled = cfg.mcp.sync.async_sync
        """
        if force_reload or self._full_config is None:
            from .config_loader import get_config
            self._full_config = get_config(project_path, force_reload=force_reload)
        return self._full_config

    @property
    def credentials(self):
        """Get credentials (lazy-loads ~/.watercooler/credentials.toml).

        Loads credential information including:
        - GitHub tokens and SSH keys
        - Dashboard session secrets
        - API keys (DeepSeek, etc.)

        Returns:
            Credentials instance

        Example:
            creds = config.credentials
            github_token = creds.github.token
            ssh_key = creds.github.ssh_key
        """
        if self._credentials is None:
            from .credentials import load_credentials
            self._credentials = load_credentials()
        return self._credentials

    def context(self, code_root: Optional[Path] = None):
        """Resolve runtime thread context (MCP).

        Discovers git repository information and resolves:
        - Code repository details (root, branch, commit)
        - Threads directory and repository URL
        - Thread slug for repository naming

        Args:
            code_root: Code repository root (default: cwd)

        Returns:
            ThreadContext with full runtime context

        Example:
            ctx = config.context()
            print(ctx.code_branch)
            print(ctx.threads_dir)
            print(ctx.threads_repo_url)
        """
        from watercooler_mcp.config import resolve_thread_context
        return resolve_thread_context(code_root)

    # Centralized environment variable access
    class EnvVars:
        """Environment variable helpers with type coercion.

        Provides type-safe access to environment variables with
        sensible defaults and automatic parsing.
        """

        @staticmethod
        def get(key: str, default: Any = None) -> Any:
            """Get environment variable.

            Args:
                key: Environment variable name
                default: Default value if not set

            Returns:
                Environment variable value or default

            Example:
                level = config.env.get("WATERCOOLER_LOG_LEVEL", "INFO")
            """
            return os.getenv(key, default)

        @staticmethod
        def get_bool(key: str, default: bool = False) -> bool:
            """Get boolean environment variable.

            Treats "1", "true", "yes", "on" as True (case-insensitive).
            Empty or missing values return the default.

            Args:
                key: Environment variable name
                default: Default value if not set

            Returns:
                Boolean value

            Example:
                debug = config.env.get_bool("DEBUG", False)
                auto = config.env.get_bool("WATERCOOLER_AUTO_PROVISION", True)
            """
            val = os.getenv(key, "").lower()
            if not val:
                return default
            return val in ("1", "true", "yes", "on")

        @staticmethod
        def get_int(key: str, default: int = 0) -> int:
            """Get integer environment variable.

            Args:
                key: Environment variable name
                default: Default value if not set or invalid

            Returns:
                Integer value or default if parsing fails

            Example:
                port = config.env.get_int("WATERCOOLER_PORT", 8080)
                max_size = config.env.get_int("MAX_SIZE", 1000)
            """
            val = os.getenv(key)
            if val:
                try:
                    return int(val)
                except ValueError:
                    pass
            return default

        @staticmethod
        def get_float(key: str, default: float = 0.0) -> float:
            """Get float environment variable.

            Args:
                key: Environment variable name
                default: Default value if not set or invalid

            Returns:
                Float value or default if parsing fails

            Example:
                timeout = config.env.get_float("TIMEOUT", 30.0)
            """
            val = os.getenv(key)
            if val:
                try:
                    return float(val)
                except ValueError:
                    pass
            return default

        @staticmethod
        def get_path(key: str, default: Optional[Path] = None) -> Optional[Path]:
            """Get path environment variable with expansion.

            Expands ~ (home) and environment variables in the path.

            Args:
                key: Environment variable name
                default: Default value if not set

            Returns:
                Path with expansions applied, or default

            Example:
                data_dir = config.env.get_path("DATA_DIR", Path("/tmp/data"))
            """
            val = os.getenv(key)
            if val:
                return Path(os.path.expanduser(os.path.expandvars(val)))
            return default

    env = EnvVars()

    # Helper methods for common patterns

    def get_threads_dir(self, cli_value: Optional[str] = None,
                       code_root: Optional[Path] = None) -> Path:
        """Resolve threads directory with precedence: CLI > env > git-aware default.

        Convenience method that accepts CLI override.

        Args:
            cli_value: Explicit directory from CLI argument
            code_root: Code repository root for context

        Returns:
            Resolved threads directory path

        Example:
            # From CLI argument
            threads_dir = config.get_threads_dir("/path/to/threads")

            # Auto-discovery
            threads_dir = config.get_threads_dir()
        """
        from .path_resolver import resolve_threads_dir
        return resolve_threads_dir(cli_value, code_root)

    def get_github_token(self) -> Optional[str]:
        """Get GitHub token from env or credentials file.

        Precedence: GITHUB_TOKEN env > GH_TOKEN env > credentials.toml

        Returns:
            GitHub token or None

        Example:
            token = config.get_github_token()
            if token:
                # Use for GitHub API calls
                pass
        """
        # Environment variables take precedence
        token = self.env.get("GITHUB_TOKEN") or self.env.get("GH_TOKEN")
        if token:
            return token

        # Fall back to credentials file
        try:
            return self.credentials.github.token or None
        except AttributeError as e:
            logging.debug(f"Failed to load GitHub token from credentials: {e}")
            return None

    def reset(self) -> None:
        """Reset cached state (for testing).

        Clears all cached configuration, forcing fresh loads
        on next access. Useful for test isolation.

        Example:
            def test_config():
                config.reset()  # Start with clean state
                # ... test code ...
                config.reset()  # Clean up
        """
        self._full_config = None
        self._credentials = None
        self._paths = None
        # Also clear the config_loader cache to ensure env vars are re-read
        try:
            from .config_loader import clear_config_cache
            clear_config_cache()
        except ImportError:
            pass  # config_loader not available (shouldn't happen)


# Global singleton instance
config = Config()
