"""Startup utilities for watercooler MCP server.

Contains initialization checks and auto-start logic for external services.
"""

import subprocess
import time
import urllib.error
import urllib.request

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
