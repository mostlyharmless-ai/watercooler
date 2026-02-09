"""Summarization for memory graph nodes.

Generates concise summaries using DeepSeek API (or compatible OpenAI API).
Used for thread and entry summaries that are then embedded for search.

Summaries are cached to disk to survive pipeline failures and avoid
re-generating expensive API calls.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from .cache import SummaryCache, ThreadSummaryCache

# Try to import httpx for API calls
try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:
    httpx = None  # type: ignore
    HTTPX_AVAILABLE = False


# Default configuration resolved via unified config
# Standard env vars (highest priority):
#   LLM_API_BASE - LLM service endpoint
#   LLM_MODEL - Model name
#   LLM_API_KEY - API key
#
# Resolution is done via watercooler.memory_config which checks:
#   1. Environment variables
#   2. TOML config
#   3. Built-in defaults
DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_TOKENS = 256
DEFAULT_MAX_CONCURRENT = 8


def _get_default_api_base() -> str:
    """Get default LLM API base from unified config."""
    try:
        from watercooler.memory_config import resolve_llm_config
        return resolve_llm_config().api_base
    except ImportError:
        # Fallback if watercooler not available
        return os.environ.get("LLM_API_BASE", "https://api.openai.com/v1")


def _get_default_model() -> str:
    """Get default LLM model from unified config."""
    try:
        from watercooler.memory_config import resolve_llm_config
        return resolve_llm_config().model
    except ImportError:
        return os.environ.get("LLM_MODEL", "gpt-4o-mini")


def _get_default_api_key() -> Optional[str]:
    """Get default LLM API key from unified config."""
    try:
        from watercooler.memory_config import resolve_llm_config
        return resolve_llm_config().api_key or None
    except ImportError:
        return os.environ.get("LLM_API_KEY")


def _get_default_timeout() -> float:
    """Get default LLM timeout from unified config."""
    try:
        from watercooler.memory_config import resolve_llm_config
        return resolve_llm_config().timeout
    except ImportError:
        return float(os.environ.get("LLM_TIMEOUT", DEFAULT_TIMEOUT))


def _get_default_max_tokens() -> int:
    """Get default LLM max_tokens from unified config."""
    try:
        from watercooler.memory_config import resolve_llm_config
        return resolve_llm_config().max_tokens
    except ImportError:
        return int(os.environ.get("LLM_MAX_TOKENS", DEFAULT_MAX_TOKENS))


@dataclass
class SummarizerConfig:
    """Configuration for summary generation.

    Settings are resolved via unified config with priority:
    1. Environment variables (LLM_API_BASE, LLM_MODEL, LLM_API_KEY)
    2. TOML config
    3. Built-in defaults
    """

    api_base: str = field(default_factory=_get_default_api_base)
    model: str = field(default_factory=_get_default_model)
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    api_key: Optional[str] = field(default_factory=_get_default_api_key)

    def __post_init__(self) -> None:
        """Validate config values after initialization."""
        if self.timeout <= 0:
            raise ValueError(f"timeout must be positive, got {self.timeout}")
        if self.max_retries < 1:
            raise ValueError(f"max_retries must be >= 1, got {self.max_retries}")
        if self.max_tokens < 1:
            raise ValueError(f"max_tokens must be >= 1, got {self.max_tokens}")
        if self.max_concurrent < 1:
            raise ValueError(f"max_concurrent must be >= 1, got {self.max_concurrent}")

    @classmethod
    def from_env(cls) -> SummarizerConfig:
        """Create config from unified config system.

        Priority: Environment variables > TOML config > Built-in defaults

        Uses watercooler.memory_config for resolution when available.
        """
        return cls(
            api_base=_get_default_api_base(),
            model=_get_default_model(),
            timeout=_get_default_timeout(),
            max_retries=int(os.environ.get("LLM_MAX_RETRIES", DEFAULT_MAX_RETRIES)),
            max_tokens=_get_default_max_tokens(),
            max_concurrent=int(os.environ.get("LLM_MAX_CONCURRENT", DEFAULT_MAX_CONCURRENT)),
            api_key=_get_default_api_key(),
        )


class SummarizerError(Exception):
    """Error during summary generation."""

    pass


def _ensure_httpx():
    """Ensure httpx is available."""
    if not HTTPX_AVAILABLE:
        raise ImportError(
            "httpx is required for summary generation. "
            "Install with: pip install 'watercooler-cloud[memory]'"
        )


# Prompts for different summary types
ENTRY_SUMMARY_PROMPT = """Summarize this thread entry in 1-2 sentences. Focus on the key action, decision, or insight.

Entry metadata:
- Agent: {agent}
- Role: {role}
- Type: {entry_type}
- Title: {title}

Entry body:
{body}

Summary:"""

THREAD_SUMMARY_PROMPT = """Summarize this watercooler thread in 2-3 sentences. Focus on the main topic, key decisions, and current status.

Thread: {title}
Status: {status}
Entries: {entry_count}

Entry summaries:
{entry_summaries}

Thread summary:"""


def _call_llm(
    prompt: str,
    config: SummarizerConfig,
) -> str:
    """Call LLM API with retry logic."""
    _ensure_httpx()

    url = f"{config.api_base.rstrip('/')}/chat/completions"

    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    payload = {
        "model": config.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": config.max_tokens,
        "temperature": 0.3,  # Low temperature for consistent summaries
    }

    last_error: Optional[Exception] = None

    for attempt in range(config.max_retries):
        try:
            with httpx.Client(timeout=config.timeout) as client:
                response = client.post(url, json=payload, headers=headers)
                response.raise_for_status()

                data = response.json()

                # OpenAI-compatible format
                if "choices" not in data or not data["choices"]:
                    raise SummarizerError(f"Unexpected response format: {data}")

                message = data["choices"][0].get("message", {})
                content = message.get("content", "")

                if not content:
                    raise SummarizerError("Empty response from LLM")

                return content.strip()

        except httpx.HTTPStatusError as e:
            last_error = SummarizerError(
                f"HTTP {e.response.status_code}: {e.response.text}"
            )
        except httpx.RequestError as e:
            last_error = SummarizerError(f"Request failed: {e}")
        except KeyError as e:
            last_error = SummarizerError(f"Missing key in response: {e}")
        except Exception as e:
            last_error = SummarizerError(f"Unexpected error: {e}")

        # Exponential backoff
        if attempt < config.max_retries - 1:
            time.sleep(2**attempt)

    raise last_error or SummarizerError("Summarization failed with unknown error")


def summarize_entry(
    body: str,
    agent: Optional[str] = None,
    role: Optional[str] = None,
    entry_type: Optional[str] = None,
    title: Optional[str] = None,
    config: Optional[SummarizerConfig] = None,
    entry_id: Optional[str] = None,
    use_cache: bool = True,
) -> str:
    """Generate summary for a thread entry.

    Summaries are cached to disk to survive pipeline failures.

    Args:
        body: Entry body text.
        agent: Agent name.
        role: Agent role.
        entry_type: Entry type.
        title: Entry title.
        config: Summarizer configuration.
        entry_id: Unique entry identifier for caching.
        use_cache: Whether to use disk cache.

    Returns:
        Summary string.
    """
    if config is None:
        config = SummarizerConfig.from_env()

    # Short entries don't need summarization
    if len(body) < 200:
        return body.strip()

    # Check cache first
    cache = SummaryCache() if use_cache else None
    cache_key = entry_id or ""

    if cache:
        cached = cache.get(cache_key, body)
        if cached:
            return cached

    prompt = ENTRY_SUMMARY_PROMPT.format(
        agent=agent or "Unknown",
        role=role or "Unknown",
        entry_type=entry_type or "Note",
        title=title or "Untitled",
        body=body[:4000],  # Truncate very long entries
    )

    summary = _call_llm(prompt, config)

    # Save to cache immediately
    if cache:
        cache.set(cache_key, body, summary)

    return summary


def summarize_thread(
    title: str,
    status: str,
    entry_summaries: list[str],
    config: Optional[SummarizerConfig] = None,
    thread_id: Optional[str] = None,
    use_cache: bool = True,
) -> str:
    """Generate summary for a thread.

    Thread summaries are cached to disk.

    Args:
        title: Thread title.
        status: Thread status.
        entry_summaries: List of entry summaries.
        config: Summarizer configuration.
        thread_id: Unique thread identifier for caching.
        use_cache: Whether to use disk cache.

    Returns:
        Thread summary string.
    """
    if config is None:
        config = SummarizerConfig.from_env()

    # Simple threads don't need complex summarization
    if len(entry_summaries) <= 2:
        return " ".join(entry_summaries)

    # Check cache first
    cache = ThreadSummaryCache() if use_cache else None
    entry_count = len(entry_summaries)

    if cache and thread_id:
        cached = cache.get(thread_id, entry_count)
        if cached:
            return cached

    # Combine entry summaries, limiting total length
    combined = "\n".join(f"- {s}" for s in entry_summaries[:20])

    prompt = THREAD_SUMMARY_PROMPT.format(
        title=title,
        status=status,
        entry_count=entry_count,
        entry_summaries=combined[:4000],
    )

    summary = _call_llm(prompt, config)

    # Save to cache immediately
    if cache and thread_id:
        cache.set(thread_id, entry_count, summary)

    return summary


def is_summarizer_available() -> bool:
    """Check if summarizer dependencies are available."""
    return HTTPX_AVAILABLE


# =============================================================================
# ASYNC SUMMARIZATION
# =============================================================================


async def _call_llm_async(
    prompt: str,
    config: SummarizerConfig,
) -> str:
    """Async LLM API call with retry logic."""
    _ensure_httpx()

    url = f"{config.api_base.rstrip('/')}/chat/completions"

    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    payload = {
        "model": config.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": config.max_tokens,
        "temperature": 0.3,
    }

    last_error: Optional[Exception] = None

    for attempt in range(config.max_retries):
        try:
            async with httpx.AsyncClient(timeout=config.timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()

                data = response.json()

                if "choices" not in data or not data["choices"]:
                    raise SummarizerError(f"Unexpected response format: {data}")

                message = data["choices"][0].get("message", {})
                content = message.get("content", "")

                if not content:
                    raise SummarizerError("Empty response from LLM")

                return content.strip()

        except httpx.HTTPStatusError as e:
            last_error = SummarizerError(
                f"HTTP {e.response.status_code}: {e.response.text}"
            )
        except httpx.RequestError as e:
            last_error = SummarizerError(f"Request failed: {e}")
        except KeyError as e:
            last_error = SummarizerError(f"Missing key in response: {e}")
        except Exception as e:
            last_error = SummarizerError(f"Unexpected error: {e}")

        # Exponential backoff
        if attempt < config.max_retries - 1:
            await asyncio.sleep(2**attempt)

    raise last_error or SummarizerError("Summarization failed with unknown error")


async def summarize_entry_async(
    body: str,
    agent: Optional[str] = None,
    role: Optional[str] = None,
    entry_type: Optional[str] = None,
    title: Optional[str] = None,
    config: Optional[SummarizerConfig] = None,
    entry_id: Optional[str] = None,
    use_cache: bool = True,
) -> str:
    """Async version of summarize_entry.

    Args:
        body: Entry body text.
        agent: Agent name.
        role: Agent role.
        entry_type: Entry type.
        title: Entry title.
        config: Summarizer configuration.
        entry_id: Unique entry identifier for caching.
        use_cache: Whether to use disk cache.

    Returns:
        Summary string.
    """
    if config is None:
        config = SummarizerConfig.from_env()

    # Short entries don't need summarization
    if len(body) < 200:
        return body.strip()

    # Check cache first
    cache = SummaryCache() if use_cache else None
    cache_key = entry_id or ""

    if cache:
        cached = cache.get(cache_key, body)
        if cached:
            return cached

    prompt = ENTRY_SUMMARY_PROMPT.format(
        agent=agent or "Unknown",
        role=role or "Unknown",
        entry_type=entry_type or "Note",
        title=title or "Untitled",
        body=body[:4000],
    )

    summary = await _call_llm_async(prompt, config)

    # Save to cache immediately
    if cache:
        cache.set(cache_key, body, summary)

    return summary


