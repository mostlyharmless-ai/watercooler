"""Summarizer for baseline graph using local LLM.

Uses OpenAI-compatible API for local LLM inference (Ollama, llama.cpp).
Returns empty string when LLM is unavailable (no fallback to extractive).
"""

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _get_default_api_base() -> str:
    """Get default API base from unified config (checks env vars first)."""
    from watercooler.memory_config import resolve_baseline_graph_llm_config
    return resolve_baseline_graph_llm_config().api_base


def _get_default_model() -> str:
    """Get default model from unified config (checks env vars first)."""
    from watercooler.memory_config import resolve_baseline_graph_llm_config
    return resolve_baseline_graph_llm_config().model


def _get_default_api_key() -> str:
    """Get default API key from unified config (checks env vars first)."""
    from watercooler.memory_config import resolve_baseline_graph_llm_config
    return resolve_baseline_graph_llm_config().api_key


def _get_default_summary_prompt() -> str:
    """Get default summary prompt from unified config (checks env vars first)."""
    from watercooler.memory_config import resolve_baseline_graph_llm_config
    return resolve_baseline_graph_llm_config().summary_prompt


def _get_default_thread_summary_prompt() -> str:
    """Get default thread summary prompt from unified config (checks env vars first)."""
    from watercooler.memory_config import resolve_baseline_graph_llm_config
    return resolve_baseline_graph_llm_config().thread_summary_prompt


@dataclass
class SummarizerConfig:
    """Configuration for the summarizer.

    LLM settings are resolved via unified config with priority:
    1. Environment variables (LLM_API_BASE, LLM_MODEL, LLM_API_KEY)
    2. Legacy env vars (BASELINE_GRAPH_API_BASE, etc.)
    3. TOML config ([memory.llm])
    4. Built-in defaults (localhost:11434 for Ollama)
    """

    # LLM settings (resolved via unified config by default)
    api_base: str = field(default_factory=_get_default_api_base)
    model: str = field(default_factory=_get_default_model)
    api_key: str = field(default_factory=_get_default_api_key)
    timeout: float = 30.0
    max_tokens: int = 256

    # Summary prompts (configurable via [memory.llm])
    summary_prompt: str = field(default_factory=_get_default_summary_prompt)
    thread_summary_prompt: str = field(default_factory=_get_default_thread_summary_prompt)

    # Extractive fallback settings
    extractive_max_chars: int = 200
    include_headers: bool = True
    max_headers: int = 3

    # Thread summarization
    max_thread_entries: int = 10  # Max entries to include in thread summary

    # Behavior
    prefer_extractive: bool = False  # Force extractive mode
    retry_on_failure: bool = True

    @classmethod
    def from_config_dict(cls, config: Dict[str, Any]) -> "SummarizerConfig":
        """Create config from dictionary (e.g., from config.toml)."""
        from watercooler.memory_config import resolve_baseline_graph_llm_config
        llm_defaults = resolve_baseline_graph_llm_config()

        llm = config.get("llm", {})
        extractive = config.get("extractive", {})

        return cls(
            api_base=llm.get("api_base", llm_defaults.api_base),
            model=llm.get("model", llm_defaults.model),
            api_key=llm.get("api_key", llm_defaults.api_key),
            timeout=llm.get("timeout", cls.timeout),
            max_tokens=llm.get("max_tokens", cls.max_tokens),
            summary_prompt=llm.get("summary_prompt", llm_defaults.summary_prompt),
            thread_summary_prompt=llm.get("thread_summary_prompt", llm_defaults.thread_summary_prompt),
            extractive_max_chars=extractive.get("max_chars", cls.extractive_max_chars),
            include_headers=extractive.get("include_headers", cls.include_headers),
            max_headers=extractive.get("max_headers", cls.max_headers),
            max_thread_entries=config.get("max_thread_entries", cls.max_thread_entries),
            prefer_extractive=config.get("prefer_extractive", cls.prefer_extractive),
        )

    @classmethod
    def from_env(cls) -> "SummarizerConfig":
        """Create config from environment variables.

        Uses unified config with priority:
        1. LLM_API_BASE, LLM_MODEL, LLM_API_KEY (preferred)
        2. BASELINE_GRAPH_API_BASE, etc. (legacy, backward compatible)
        3. TOML config
        4. Built-in defaults
        """
        from watercooler.memory_config import resolve_baseline_graph_llm_config
        llm_config = resolve_baseline_graph_llm_config()

        # Parse numeric values with fallback to defaults on invalid input
        timeout = cls.timeout
        if timeout_str := os.environ.get("BASELINE_GRAPH_TIMEOUT"):
            try:
                timeout = float(timeout_str)
            except ValueError:
                logger.warning(f"Invalid BASELINE_GRAPH_TIMEOUT value: {timeout_str!r}, using default")

        max_tokens = cls.max_tokens
        if max_tokens_str := os.environ.get("BASELINE_GRAPH_MAX_TOKENS"):
            try:
                max_tokens = int(max_tokens_str)
            except ValueError:
                logger.warning(f"Invalid BASELINE_GRAPH_MAX_TOKENS value: {max_tokens_str!r}, using default")

        return cls(
            api_base=llm_config.api_base,
            model=llm_config.model,
            api_key=llm_config.api_key,
            timeout=timeout,
            max_tokens=max_tokens,
            summary_prompt=llm_config.summary_prompt,
            thread_summary_prompt=llm_config.thread_summary_prompt,
            prefer_extractive=os.environ.get("BASELINE_GRAPH_EXTRACTIVE_ONLY", "").lower() in ("1", "true", "yes"),
        )


def is_llm_service_available(config: Optional[SummarizerConfig] = None) -> bool:
    """Check if LLM service is available.

    Args:
        config: Summarizer configuration (uses env defaults if None)

    Returns:
        True if the LLM service responds to a models list request
    """
    config = config or SummarizerConfig.from_env()

    try:
        import httpx
    except ImportError:
        logger.debug("httpx not available, cannot check LLM service")
        return False

    try:
        url = f"{config.api_base.rstrip('/')}/models"
        with httpx.Client(timeout=5.0) as client:
            response = client.get(url)
            return response.status_code == 200
    except Exception as e:
        logger.debug(f"LLM service not available at {config.api_base}: {e}")
        return False


def _extract_headers(text: str, max_headers: int = 3) -> List[str]:
    """Extract markdown headers from text.

    Args:
        text: Markdown text to extract headers from
        max_headers: Maximum number of headers to return

    Returns:
        List of header strings (without # prefix)
    """
    headers = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("#"):
            # Remove leading #s and whitespace
            header = re.sub(r"^#+\s*", "", line)
            if header:
                headers.append(header)
            if len(headers) >= max_headers:
                break
    return headers


def _truncate_text(text: str, max_chars: int) -> str:
    """Truncate text to max_chars, preferring sentence boundaries.

    Args:
        text: Text to truncate
        max_chars: Maximum characters

    Returns:
        Truncated text with ellipsis if needed
    """
    if len(text) <= max_chars:
        return text

    # Try to break at sentence boundary
    truncated = text[:max_chars]
    last_period = truncated.rfind(".")
    last_newline = truncated.rfind("\n")

    break_point = max(last_period, last_newline)
    if break_point > max_chars * 0.5:  # Only use if we keep at least half
        return truncated[: break_point + 1].strip()

    # Fall back to word boundary
    last_space = truncated.rfind(" ")
    if last_space > max_chars * 0.7:
        return truncated[:last_space].strip() + "..."

    return truncated.strip() + "..."


def extractive_summary(
    text: str,
    max_chars: int = 200,
    include_headers: bool = True,
    max_headers: int = 3,
) -> str:
    """Generate extractive summary from text.

    Extractive summarization extracts key portions without using an LLM:
    - First N characters of content
    - Optionally includes markdown headers

    Args:
        text: Text to summarize
        max_chars: Maximum characters for main summary
        include_headers: Whether to include headers
        max_headers: Maximum number of headers to include

    Returns:
        Extractive summary string
    """
    if not text or not text.strip():
        return ""

    parts = []

    # Extract headers if requested
    if include_headers:
        headers = _extract_headers(text, max_headers)
        if headers:
            parts.append("Topics: " + ", ".join(headers))

    # Get first paragraph or truncated content
    # Skip any leading headers for the content portion
    content_lines = []
    in_header = True
    for line in text.split("\n"):
        if in_header and line.strip().startswith("#"):
            continue
        in_header = False
        if line.strip():
            content_lines.append(line.strip())

    content = " ".join(content_lines)
    if content:
        truncated = _truncate_text(content, max_chars)
        parts.append(truncated)

    return " | ".join(parts) if parts else text[:max_chars]


def _validate_api_base(api_base: str) -> bool:
    """Validate api_base URL format and warn about security concerns.

    Args:
        api_base: The API base URL to validate

    Returns:
        True if URL is valid, False otherwise
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(api_base)

        # Must have scheme and netloc
        if not parsed.scheme or not parsed.netloc:
            logger.warning(f"Invalid api_base URL format: {api_base}")
            return False

        # Must be http or https
        if parsed.scheme not in ("http", "https"):
            logger.warning(f"api_base must use http or https: {api_base}")
            return False

        # Warn about non-localhost URLs (potential SSRF)
        host = parsed.netloc.split(":")[0].lower()
        localhost_hosts = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
        if host not in localhost_hosts:
            logger.warning(
                f"api_base points to non-localhost ({host}). "
                "Ensure this is intentional for your LLM backend."
            )

        return True
    except Exception as e:
        logger.warning(f"Failed to parse api_base URL: {e}")
        return False


def _call_llm(
    prompt: str,
    config: SummarizerConfig,
) -> Optional[str]:
    """Call local LLM via OpenAI-compatible API.

    Args:
        prompt: Prompt to send
        config: Summarizer configuration

    Returns:
        LLM response text or None on failure
    """
    try:
        import httpx
    except ImportError:
        logger.warning("httpx not available, falling back to extractive")
        return None

    # Validate api_base URL
    if not _validate_api_base(config.api_base):
        return None

    url = f"{config.api_base.rstrip('/')}/chat/completions"

    # Ensure max_tokens is sufficient for thinking models
    from watercooler.models import get_min_max_tokens
    max_tokens = max(config.max_tokens, get_min_max_tokens(config.model, config.max_tokens))

    payload = {
        "model": config.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.3,  # Low temp for factual summaries
    }

    headers = {
        "Content-Type": "application/json",
    }
    if config.api_key and config.api_key != "ollama":
        headers["Authorization"] = f"Bearer {config.api_key}"

    try:
        with httpx.Client(timeout=config.timeout) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            message = data["choices"][0]["message"]

            # Get response field based on model (e.g., "reasoning" for qwen3)
            from watercooler.models import get_response_field
            response_field = get_response_field(config.model)

            # Try configured field first, fall back to content
            content = message.get(response_field, "").strip()
            if not content and response_field != "content":
                content = message.get("content", "").strip()

            return content
    except httpx.ConnectError:
        logger.warning(f"Cannot connect to LLM at {config.api_base}")
        return None
    except httpx.TimeoutException:
        logger.warning(f"LLM request timed out after {config.timeout}s")
        return None
    except Exception as e:
        logger.warning(f"LLM call failed: {e}")
        return None


def summarize_entry(
    entry_body: str,
    entry_title: Optional[str] = None,
    entry_type: Optional[str] = None,
    config: Optional[SummarizerConfig] = None,
) -> str:
    """Summarize a single thread entry.

    Uses LLM for summarization. Returns empty string if LLM unavailable.

    Args:
        entry_body: Entry body text
        entry_title: Optional entry title
        entry_type: Optional entry type (Note, Plan, Decision, etc.)
        config: Summarizer configuration

    Returns:
        Summary string
    """
    config = config or SummarizerConfig()

    # Use extractive if forced or text is short
    if config.prefer_extractive or len(entry_body) < config.extractive_max_chars:
        return extractive_summary(
            entry_body,
            max_chars=config.extractive_max_chars,
            include_headers=config.include_headers,
            max_headers=config.max_headers,
        )

    # Build LLM prompt
    context = ""
    if entry_title:
        context += f"Title: {entry_title}\n"
    if entry_type:
        context += f"Type: {entry_type}\n"

    content = _truncate_text(entry_body, 2000)

    # Use configurable prompt with {context} and {content} placeholders
    base_prompt = config.summary_prompt
    if "{context}" in base_prompt or "{content}" in base_prompt:
        # Template-style prompt
        prompt = base_prompt.format(context=context, content=content)
    else:
        # Simple instruction prompt - wrap with context and content
        prompt = f"""{base_prompt}

{context}
Content:
{content}

Summary:"""

    result = _call_llm(prompt, config)

    if result is None:
        logger.debug("LLM unavailable, no summary generated")
        return ""

    return result


def summarize_thread(
    entries: List[Dict[str, Any]],
    thread_title: Optional[str] = None,
    config: Optional[SummarizerConfig] = None,
) -> str:
    """Summarize an entire thread from its entries.

    Args:
        entries: List of entry dicts with 'body', 'title', 'type' keys
        thread_title: Optional thread title
        config: Summarizer configuration

    Returns:
        Thread summary string
    """
    config = config or SummarizerConfig()

    if not entries:
        return ""

    # Concatenate entry summaries for context
    entry_summaries = []
    for entry in entries[:config.max_thread_entries]:
        body = entry.get("body", "")
        title = entry.get("title", "")
        if body:
            short = extractive_summary(body, max_chars=100, include_headers=False)
            if title:
                entry_summaries.append(f"- {title}: {short}")
            else:
                entry_summaries.append(f"- {short}")

    combined = "\n".join(entry_summaries)

    # Use extractive if forced
    if config.prefer_extractive:
        return extractive_summary(
            combined,
            max_chars=config.extractive_max_chars * 2,
            include_headers=False,
        )

    # Build LLM prompt using configurable template
    title = thread_title or "Development Discussion"

    base_prompt = config.thread_summary_prompt
    if "{title}" in base_prompt or "{entries}" in base_prompt:
        # Template-style prompt
        prompt = base_prompt.format(title=title, entries=combined)
    else:
        # Simple instruction prompt - wrap with context
        prompt = f"""{base_prompt}

Thread: {title}

Entries:
{combined}

Summary:"""

    result = _call_llm(prompt, config)

    if result is None:
        logger.debug("LLM unavailable, no thread summary generated")
        return ""

    return result


def get_baseline_graph_config() -> Dict[str, Any]:
    """Load baseline_graph section from config.toml.

    Returns:
        Dict with baseline_graph settings, empty dict if not configured.
    """
    try:
        from watercooler.credentials import _load_config
        config = _load_config()
        return config.get("baseline_graph", {})
    except (ImportError, FileNotFoundError, PermissionError, OSError) as e:
        # Expected failures when config not found/accessible
        logger.debug(f"Could not load config: {e}")
        return {}
    except (KeyError, TypeError, ValueError) as e:
        # Config structure issues - likely bugs in TOML format
        logger.warning(f"Malformed baseline_graph config: {e}")
        return {}
    except Exception as e:
        # Unexpected errors - log and continue
        logger.warning(f"Unexpected error loading baseline_graph config: {e}")
        return {}


def create_summarizer_config() -> SummarizerConfig:
    """Create SummarizerConfig from config.toml and environment.

    Priority:
    1. Environment variables (highest)
    2. config.toml [baseline_graph] section
    3. Built-in defaults (lowest)

    Returns:
        Configured SummarizerConfig
    """
    # Start with config.toml
    config_dict = get_baseline_graph_config()
    config = SummarizerConfig.from_config_dict(config_dict)

    # Override with environment
    if os.environ.get("BASELINE_GRAPH_API_BASE"):
        config.api_base = os.environ["BASELINE_GRAPH_API_BASE"]
    if os.environ.get("BASELINE_GRAPH_MODEL"):
        config.model = os.environ["BASELINE_GRAPH_MODEL"]
    if os.environ.get("BASELINE_GRAPH_API_KEY"):
        config.api_key = os.environ["BASELINE_GRAPH_API_KEY"]
    if os.environ.get("BASELINE_GRAPH_EXTRACTIVE_ONLY", "").lower() in ("1", "true", "yes"):
        config.prefer_extractive = True

    return config
