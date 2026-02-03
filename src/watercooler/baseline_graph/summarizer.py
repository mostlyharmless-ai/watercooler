"""Summarizer for baseline graph using local LLM.

Uses OpenAI-compatible API for local LLM inference (llama-server, OpenAI, etc.).
Returns empty string when LLM is unavailable (no fallback to extractive).
"""

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from watercooler.memory_config import is_anthropic_url

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
    4. Built-in defaults (localhost:8000 for llama-server)
    """

    # LLM settings (resolved via unified config by default)
    api_base: str = field(default_factory=_get_default_api_base)
    model: str = field(default_factory=_get_default_model)
    api_key: str = field(default_factory=_get_default_api_key)
    timeout: float = 30.0
    max_tokens: int = 256

    # Prompt configuration (auto-detected from model if empty)
    system_prompt: str = ""  # Empty means auto-detect based on model
    prompt_prefix: str = ""  # Empty means auto-detect (e.g., "/no_think" for Qwen3)

    # Summary prompts (configurable via [memory.llm])
    summary_prompt: str = field(default_factory=_get_default_summary_prompt)
    thread_summary_prompt: str = field(default_factory=_get_default_thread_summary_prompt)

    # Few-shot example for format compliance
    summary_example_input: str = "Implemented OAuth2 authentication with JWT tokens. Added refresh token rotation and secure cookie storage."
    summary_example_output: str = "OAuth2 authentication implemented with JWT tokens, refresh rotation, and secure cookie storage.\ntags: #authentication #OAuth2 #JWT #security"

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
            system_prompt=llm.get("system_prompt", llm_defaults.system_prompt),
            prompt_prefix=llm.get("prompt_prefix", llm_defaults.prompt_prefix),
            summary_prompt=llm.get("summary_prompt", llm_defaults.summary_prompt),
            thread_summary_prompt=llm.get("thread_summary_prompt", llm_defaults.thread_summary_prompt),
            summary_example_input=llm.get("summary_example_input", llm_defaults.summary_example_input),
            summary_example_output=llm.get("summary_example_output", llm_defaults.summary_example_output),
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
            system_prompt=llm_config.system_prompt,
            prompt_prefix=llm_config.prompt_prefix,
            summary_prompt=llm_config.summary_prompt,
            thread_summary_prompt=llm_config.thread_summary_prompt,
            summary_example_input=llm_config.summary_example_input,
            summary_example_output=llm_config.summary_example_output,
            prefer_extractive=os.environ.get("BASELINE_GRAPH_EXTRACTIVE_ONLY", "").lower() in ("1", "true", "yes"),
        )


def is_llm_service_available(config: Optional[SummarizerConfig] = None) -> bool:
    """Check if LLM service is available.

    Args:
        config: Summarizer configuration (uses env defaults if None)

    Returns:
        True if the LLM service responds to a health check request
    """
    config = config or SummarizerConfig.from_env()

    try:
        import httpx
    except ImportError:
        logger.debug("httpx not available, cannot check LLM service")
        return False

    try:
        api_base = config.api_base or ""
        is_anthropic = is_anthropic_url(api_base)
        headers = {}

        # Add auth header for external APIs (not needed for local llama-server)
        if config.api_key and config.api_key not in ("", "local"):
            if is_anthropic:
                # Anthropic uses x-api-key header
                headers["x-api-key"] = config.api_key
                headers["anthropic-version"] = "2023-06-01"
            else:
                headers["Authorization"] = f"Bearer {config.api_key}"

        with httpx.Client(timeout=5.0) as client:
            if is_anthropic:
                # Anthropic doesn't have /models endpoint. Use GET on /messages
                # which returns 405 Method Not Allowed - confirms API is reachable
                # without triggering actual completions (avoids rate limits/charges)
                url = f"{api_base.rstrip('/')}/messages"
                response = client.get(url, headers=headers)
                # 405 = API reachable, method not allowed (expected for GET on POST endpoint)
                # 400 = API reachable, bad request (also acceptable)
                return response.status_code in (200, 400, 405)
            else:
                url = f"{api_base.rstrip('/')}/models"
                response = client.get(url, headers=headers)
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


def _extract_tags(text: str) -> List[str]:
    """Extract hashtags from text.

    Looks for tags in formats:
    - "tags: #foo #bar #baz"
    - "#foo #bar" (standalone hashtags)

    Args:
        text: Text that may contain tags

    Returns:
        List of tag strings (without # prefix), deduplicated
    """
    if not text:
        return []

    tags = set()

    # Pattern 1: "tags: #foo #bar" line
    tags_line_match = re.search(r"tags:\s*((?:#\w+\s*)+)", text, re.IGNORECASE)
    if tags_line_match:
        tag_str = tags_line_match.group(1)
        for match in re.finditer(r"#(\w+)", tag_str):
            tags.add(match.group(1).lower())

    # Pattern 2: Standalone hashtags (if no tags: line found)
    if not tags:
        for match in re.finditer(r"#(\w+)", text):
            tags.add(match.group(1).lower())

    return sorted(tags)


def _strip_tags_from_summary(text: str) -> str:
    """Remove the tags line from a summary.

    Args:
        text: Summary text that may end with "tags: #foo #bar"

    Returns:
        Text with tags line removed and trailing whitespace stripped
    """
    if not text:
        return ""

    # Remove "tags: ..." line (typically at the end)
    result = re.sub(r"\n?tags:\s*(?:#\w+\s*)+\s*$", "", text, flags=re.IGNORECASE)
    return result.strip()


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
    messages: List[Dict[str, str]],
    config: SummarizerConfig,
) -> Optional[str]:
    """Call local LLM via OpenAI-compatible API.

    Args:
        messages: List of message dicts with "role" and "content" keys
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
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.3,  # Low temp for factual summaries
    }

    headers = {
        "Content-Type": "application/json",
    }
    # Add authorization header for non-local endpoints (local llama-server doesn't need it)
    if config.api_key and config.api_key not in ("", "local"):
        headers["Authorization"] = f"Bearer {config.api_key}"

    try:
        with httpx.Client(timeout=config.timeout) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            message = data["choices"][0]["message"]

            # Log token usage if available (OpenAI API returns this)
            usage = data.get("usage", {})
            if usage:
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
                total_tokens = usage.get("total_tokens", 0)
                logger.info(
                    f"LLM usage: model={config.model} "
                    f"prompt={prompt_tokens} completion={completion_tokens} total={total_tokens}"
                )

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


def _build_summary_messages(
    entry_body: str,
    entry_title: Optional[str],
    entry_type: Optional[str],
    config: SummarizerConfig,
) -> List[Dict[str, str]]:
    """Build chat messages for summarization with model-aware prompting.

    Constructs a message list with:
    - Optional system prompt (from config or auto-detected by model family)
    - User message with optional prefix, few-shot example, and entry content

    Args:
        entry_body: Entry body text
        entry_title: Optional entry title
        entry_type: Optional entry type
        config: Summarizer configuration

    Returns:
        List of message dicts for the LLM API
    """
    from watercooler.models import get_model_prompt_defaults

    # Get model-specific defaults
    model_defaults = get_model_prompt_defaults(config.model)

    # Resolve system prompt (config > auto-detect)
    system_prompt = config.system_prompt or model_defaults.get("system_prompt", "")

    # Resolve prompt prefix (config > auto-detect)
    prompt_prefix = config.prompt_prefix or model_defaults.get("prompt_prefix", "")

    # Build entry context
    context = ""
    if entry_title:
        context += f"Title: {entry_title}\n"
    if entry_type:
        context += f"Type: {entry_type}\n"

    content = _truncate_text(entry_body, 2000)

    # Build the user prompt with few-shot example
    user_prompt_parts = []

    # Add prefix if needed (e.g., "/no_think" for Qwen3)
    if prompt_prefix:
        user_prompt_parts.append(prompt_prefix.rstrip())

    # Add instruction from config (or default)
    instruction = config.summary_prompt
    if not instruction or "{context}" in instruction or "{content}" in instruction:
        # Use simple default if template-style or empty
        instruction = "Summarize the entry in 1-2 sentences, then add relevant tags."
    user_prompt_parts.append(instruction)

    # Add few-shot example
    if config.summary_example_input and config.summary_example_output:
        user_prompt_parts.append(
            f"\nExample input:\n\"{config.summary_example_input}\"\n\n"
            f"Example output:\n{config.summary_example_output}"
        )

    # Add the actual entry to summarize
    if context:
        user_prompt_parts.append(f"\nNow summarize this entry:\n{context}\n{content}")
    else:
        user_prompt_parts.append(f"\nNow summarize this entry:\n{content}")

    user_content = "\n".join(user_prompt_parts)

    # Build messages list
    messages: List[Dict[str, str]] = []

    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    messages.append({"role": "user", "content": user_content})

    return messages


def summarize_entry(
    entry_body: str,
    entry_title: Optional[str] = None,
    entry_type: Optional[str] = None,
    config: Optional[SummarizerConfig] = None,
) -> str:
    """Summarize a single thread entry.

    Uses LLM for summarization with model-aware prompting.
    Returns empty string if LLM unavailable.

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

    # Build messages with model-aware prompting
    messages = _build_summary_messages(entry_body, entry_title, entry_type, config)

    result = _call_llm(messages, config)

    if result is None:
        logger.warning(
            "LLM unavailable for entry summarization - returning empty summary. "
            f"Check LLM service at {config.api_base}"
        )
        return ""

    return result


def summarize_thread(
    entries: List[Dict[str, Any]],
    thread_title: Optional[str] = None,
    config: Optional[SummarizerConfig] = None,
) -> str:
    """Summarize an entire thread from its entries.

    Generates a prose summary of the thread and aggregates tags from all
    entry summaries. Tags are extracted deterministically and deduplicated,
    then appended to the thread summary.

    Args:
        entries: List of entry dicts with 'body', 'title', 'type' keys.
            May also include 'summary' with pre-computed entry summary.
        thread_title: Optional thread title
        config: Summarizer configuration

    Returns:
        Thread summary string with aggregated tags
    """
    config = config or SummarizerConfig()

    if not entries:
        return ""

    # Collect entry summaries and aggregate tags
    entry_summaries = []
    all_tags: set[str] = set()

    for entry in entries[:config.max_thread_entries]:
        body = entry.get("body", "")
        title = entry.get("title", "")
        entry_summary = entry.get("summary", "")

        # Extract tags from entry summary if available
        if entry_summary:
            tags = _extract_tags(entry_summary)
            all_tags.update(tags)
            # Use summary without tags for prose generation
            short = _strip_tags_from_summary(entry_summary)[:100]
        elif body:
            short = extractive_summary(body, max_chars=100, include_headers=False)
        else:
            continue

        if title:
            entry_summaries.append(f"- {title}: {short}")
        else:
            entry_summaries.append(f"- {short}")

    combined = "\n".join(entry_summaries)

    # Use extractive if forced
    if config.prefer_extractive:
        prose = extractive_summary(
            combined,
            max_chars=config.extractive_max_chars * 2,
            include_headers=False,
        )
        # Append aggregated tags
        if all_tags:
            tag_line = "tags: " + " ".join(f"#{t}" for t in sorted(all_tags))
            return f"{prose}\n{tag_line}"
        return prose

    # Build LLM messages using model-aware prompting
    from watercooler.models import get_model_prompt_defaults

    title = thread_title or "Development Discussion"
    model_defaults = get_model_prompt_defaults(config.model)

    # Resolve system prompt and prefix
    system_prompt = config.system_prompt or model_defaults.get("system_prompt", "")
    prompt_prefix = config.prompt_prefix or model_defaults.get("prompt_prefix", "")

    # Build user message - ask for prose only, we'll add tags separately
    base_prompt = config.thread_summary_prompt
    # Modify prompt to exclude tags (we aggregate them separately)
    prose_prompt = base_prompt.replace("then add relevant tags", "").replace(
        "include relevant tags", ""
    ).strip()
    if not prose_prompt:
        prose_prompt = "Summarize this development thread in 2-3 sentences. Include the main topic, key decisions, and outcome if any."

    if "{title}" in prose_prompt or "{entries}" in prose_prompt:
        user_content = prose_prompt.format(title=title, entries=combined)
    else:
        user_content = f"""{prose_prompt}

Thread: {title}

Entries:
{combined}

Summary:"""

    # Add prefix if needed
    if prompt_prefix:
        user_content = f"{prompt_prefix.rstrip()} {user_content}"

    # Build messages
    messages: List[Dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_content})

    result = _call_llm(messages, config)

    if result is None:
        logger.warning(
            "LLM unavailable for thread summarization - returning empty summary. "
            f"Check LLM service at {config.api_base}"
        )
        return ""

    # Strip any tags the LLM may have added (we aggregate our own)
    prose = _strip_tags_from_summary(result)

    # Append deterministically aggregated tags
    if all_tags:
        tag_line = "tags: " + " ".join(f"#{t}" for t in sorted(all_tags))
        return f"{prose}\n{tag_line}"

    return prose


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
