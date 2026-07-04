"""Summarizer for baseline graph using local LLM.

Uses OpenAI-compatible API for local LLM inference (llama-server, OpenAI, etc.).
Returns empty string when LLM is unavailable (no fallback to extractive).
"""

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from watercooler.constants import ENTRY_TYPES
from watercooler.memory_config import is_anthropic_url, AUTH_SKIP_SENTINELS

logger = logging.getLogger(__name__)

# Thread-summary schema version. Bump when the summary-generation contract changes in a
# way that should invalidate previously stored summaries. v2 (#878) adds the schema-aware
# authority-language guard; v1 summaries are laundering-prone and are treated as stale so
# enrichment/recovery paths regenerate them. v3 (#902/#788) replaces the OAuth2/JWT
# few-shot example and adds the security-fabrication grounding guard; v2 summaries can
# carry the fabricated-auth bleed and are treated as stale.
SUMMARY_SCHEMA_VERSION = 3


def summary_is_stale(meta: Dict[str, Any]) -> bool:
    """True if a thread's stored summary predates the current summary schema.

    Args:
        meta: Thread meta dict, which may carry ``summary_schema_version``. A missing or
            unparseable version is treated as v1 (pre-#878), hence stale.
    """
    raw = meta.get("summary_schema_version", 1)
    # Accept only genuine ints (bool is an int subclass but never a valid version).
    # Anything missing or malformed (str/float/bool/None) is treated as stale so the
    # summary is regenerated rather than silently blessed as current.
    if isinstance(raw, bool) or not isinstance(raw, int):
        return True
    return raw < SUMMARY_SCHEMA_VERSION


def stamp_summary_version(meta: Dict[str, Any]) -> None:
    """Record that ``meta['summary']`` was generated under the current summary schema."""
    meta["summary_schema_version"] = SUMMARY_SCHEMA_VERSION


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

    # Few-shot example for format compliance. Deliberately neutral (a refactor,
    # not auth): the prior OAuth2/JWT example bled its subject matter into
    # unrelated security-themed summaries on weak local models (#902/#788). Keep
    # it domain-free so any residual contamination is harmless.
    summary_example_input: str = "Refactored the date-parsing helper to accept ISO-8601 offsets and added unit tests for the boundary cases."
    summary_example_output: str = "Refactored the date-parsing helper to accept ISO-8601 offsets, with new boundary-case unit tests.\ntags: #refactor #date-parsing #tests"

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
        if config.api_key and config.api_key not in AUTH_SKIP_SENTINELS:
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
    if config.api_key and config.api_key not in AUTH_SKIP_SENTINELS:
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
    grounding_guard: str = "",
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

    # Grounding constraint (#902/#788): the example above is format-only; stay
    # strictly within the entry text and invent no mechanisms not present.
    user_prompt_parts.append(f"\n{_ENTRY_GROUNDING_CLAUSE}")
    if grounding_guard:
        user_prompt_parts.append(grounding_guard)

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

    # Grounding guard (#902/#788): if the summary names an auth/credential mechanism
    # absent from the entry (few-shot bleed / topic extrapolation), regenerate once
    # with a hardened grounding instruction, then fall back to the deterministic
    # extractive summary (which can only contain source text, so it cannot fabricate).
    source = f"{entry_title or ''}\n{entry_body}"
    if _fabricates_security(result, source):
        logger.info(
            "Entry summary named an ungrounded security mechanism; regenerating "
            "with hardened grounding guard"
        )
        hardened = _build_summary_messages(
            entry_body, entry_title, entry_type, config,
            grounding_guard=_HARDENED_GROUNDING_GUARD,
        )
        retry = _call_llm(hardened, config)
        if retry and not _fabricates_security(retry, source):
            result = retry
        else:
            result = extractive_summary(
                entry_body,
                max_chars=config.extractive_max_chars,
                include_headers=config.include_headers,
                max_headers=config.max_headers,
            )

    return result


# Catch-all bucket for entries whose type cannot be resolved to a known type.
UNKNOWN_ENTRY_TYPE = "Unknown"
_ENTRY_TYPE_LOOKUP = {t.lower(): t for t in ENTRY_TYPES}
# Types that grant decision/outcome language permission. These may only be assigned
# from graph-sourced entry_type/type fields, never inferred from untrusted body prose.
_AUTHORITY_ENTRY_TYPES = frozenset({"Decision", "Closure"})

# Thread-level authority assertions. These target claims that *the thread reached*
# a decision or outcome, not any mention of the tokens "decision"/"resolved" (a Note
# may legitimately reference a prior decision). Only consulted when the summary window
# contains zero Decision / zero Closure entries, and only to trigger a deterministic
# regenerate-or-extractive fallback - never to rewrite prose in place.
_DECISION_ASSERTION_RE = re.compile(
    r"\b("
    r"key decisions?"
    r"|decisions?\s+(?:include|includes|are|were|made|reached|taken)"
    r"|(?:we|the team|the group|participants?)\s+decided"
    r"|decided\s+(?:to|that|on)"
    # Singular/label/passive forms. "the decision is/was ...", "a decision was made",
    # "a decision has been reached", "the decision has been made" are all assertions;
    # "the prior decision to defer" (a reference) is deliberately not matched because
    # "the decision to" requires neither is/was nor a passive made/reached verb.
    r"|the\s+decision\s+(?:is|was)"
    r"|(?:a|an|the)\s+decision\s+(?:was|were|is|has\s+been|have\s+been|had\s+been)"
    r"\s+(?:made|reached|taken|agreed|approved|finalized)"
    r"|decision:"
    r")",
    re.IGNORECASE,
)
_OUTCOME_ASSERTION_RE = re.compile(
    r"\b("
    r"the\s+outcome\s+(?:is|was|of)"
    r"|outcome:"
    r"|the\s+resolution\s+(?:is|was)"
    r"|resolution:"
    # Thread-level "resolved" only. A bare "X was resolved" matches ordinary factual
    # Notes ("the merge conflict was resolved by rebasing"), so it is deliberately
    # excluded; the subject must be the thread/discussion itself.
    r"|the\s+(?:thread|discussion|debate)\s+(?:was|is|has\s+been)\s+resolved"
    r"|reached\s+(?:a\s+)?(?:consensus|resolution|conclusion)"
    r")",
    re.IGNORECASE,
)

_HARDENED_AUTHORITY_GUARD = (
    "IMPORTANT: the previous summary implied decisions or outcomes this thread does not "
    "contain. Re-summarize strictly as discussion or analysis. Do not use the words "
    '"decision", "decided", "outcome", "resolved", or "resolution" in any form.'
)

# Grounding (#902/#788): the few-shot example demonstrates FORMAT only. Weak local
# models can echo its subject matter or extrapolate generic mechanisms (e.g. invent
# "OAuth2/JWT auth" for a security-themed entry with no auth code), which is
# especially dangerous for security topics. This clause is appended to every summary
# prompt; the deterministic detector below is the backstop.
_GROUNDING_CLAUSE = (
    "Summarize ONLY what the source text explicitly states. Do not introduce "
    "technologies, protocols, frameworks, or security mechanisms (authentication "
    "schemes, tokens, etc.) that are not literally present in it."
)
# Entry prompts carry a few-shot example, so they also warn against echoing it.
_ENTRY_GROUNDING_CLAUSE = (
    "The example above illustrates output format only — ignore its subject matter. "
    + _GROUNDING_CLAUSE
)

_HARDENED_GROUNDING_GUARD = (
    "IMPORTANT: the previous summary named a technology or security mechanism that "
    "does not appear in the entry text. Re-summarize using ONLY terms present in the "
    "entry. Do not mention any authentication scheme, token, or protocol unless the "
    "entry itself names it."
)

# Credential/auth-mechanism vocabulary whose *fabrication* (present in the summary but
# absent from the source text) is the observed, dangerous failure mode (#902/#788).
# A grounded summary whose source actually contains the term never trips this — the
# detector fires only on terms missing from the source. The broad terms
# (authentication/authorization) can over-fire on a legitimate permissions thread that
# paraphrases (summary "authorization", source "permission") — that regenerates and, if
# still ungrounded, falls back to grounded extractive prose, so it trades a little
# summary polish for never asserting an auth control that isn't there.
_SECURITY_FABRICATION_RE = re.compile(
    r"\b("
    r"oauth\s?2?|jwt|json\s+web\s+tokens?|saml|sso|oidc|openid(?:\s+connect)?"
    r"|mfa|2fa|two[-\s]factor|multi[-\s]factor"
    r"|refresh\s+tokens?|access\s+tokens?|bearer\s+tokens?|session\s+tokens?"
    r"|secure\s+cookies?|authentication|authorization|authenticate"
    r")\b",
    re.IGNORECASE,
)


# Negation cue immediately preceding a term, so a source that says "no JWT" /
# "without authentication" does NOT count as positively grounding that term — a
# summary that then asserts it as present is still a fabrication (#788).
_NEGATION_RE = re.compile(
    r"\b(no|not|without|never|lacks?|lacking|absent|none|free\s+of|"
    r"does\s+not\s+\w+|doesn't\s+\w+)\b[\w\s,'-]{0,24}$"
)


def _grounding_pattern(term: str) -> str:
    """Word-boundary regex that decides whether ``term`` is grounded in the source.

    Anchored on ``\\b`` so a security term is not "grounded" by an unrelated word
    that merely contains it ("sso" in "assorted", "authentication" in
    "reauthentication") — being loose here is a false-negative in the dangerous
    direction (a real fabrication would be missed). The oauth/oauth2 family is
    treated as one term (a bare ``\\b`` would split them on the digit).
    """
    if term.startswith("oauth"):
        return r"\boauth\s?2?\b"
    return rf"\b{re.escape(term)}\b"


def _term_positively_grounded(term: str, source_l: str) -> bool:
    """True if ``term`` appears in ``source_l`` in at least one non-negated context."""
    for match in re.finditer(_grounding_pattern(term), source_l):
        # Negation is leading-context only (a 32-char lookback ending at the term):
        # this catches "no JWT" / "without authentication" (#788) but not trailing
        # forms like "JWT-less"; a miss there degrades safely to regenerate ->
        # extractive rather than to a fabricated claim.
        preceding = source_l[max(0, match.start() - 32):match.start()]
        if not _NEGATION_RE.search(preceding):
            return True
    return False


def _fabricates_security(summary: str, source: str) -> bool:
    """True if ``summary`` asserts a credential/auth mechanism not positively present
    in ``source``.

    The detector for the #902/#788 few-shot bleed: a weak model echoing the example
    or extrapolating from a topic name produces auth/token claims the entry never
    made. A term is "grounded" only if the source mentions it in a non-negated
    context — so an entry that says "no JWT, no authentication" does not license a
    summary that asserts JWT/auth as implemented.
    """
    if not summary:
        return False
    source_l = re.sub(r"\s+", " ", source.lower())
    for match in _SECURITY_FABRICATION_RE.finditer(summary):
        # Normalize internal whitespace so "JSON  Web Token" matches "json web token".
        term = re.sub(r"\s+", " ", match.group(0).lower())
        if not _term_positively_grounded(term, source_l):
            return True
    return False


def _normalize_entry_type(entry: Dict[str, Any]) -> str:
    """Resolve an entry dict to a canonical entry type or ``Unknown``.

    Checks the canonical ``entry_type`` field, then the parser-produced ``type`` field
    (which on raw graph nodes is the node *kind* ``"entry"`` and must be ignored), then a
    ``Type:`` header line in the body. Any value outside :data:`ENTRY_TYPES` - including
    missing values and the node-kind ``"entry"`` - resolves to ``Unknown`` and grants no
    authority-language permission.

    Args:
        entry: Entry dict with some combination of ``entry_type``/``type``/``body`` keys.

    Returns:
        A value from :data:`ENTRY_TYPES` or :data:`UNKNOWN_ENTRY_TYPE`.
    """
    for key in ("entry_type", "type"):
        value = entry.get(key)
        if isinstance(value, str):
            canonical = _ENTRY_TYPE_LOOKUP.get(value.strip().lower())
            if canonical:
                return canonical

    # Fall back to a `Type:` header in the body, but NEVER trust body prose to assert
    # an authority type: a `Type: Decision` line in untrusted, contributor-writable
    # body text would otherwise grant decision/outcome permission and poison the badge.
    # Body-derived authority types resolve to Unknown; only the canonical
    # entry_type/type fields (graph-sourced) may classify an entry as Decision/Closure.
    body = entry.get("body", "") or ""
    match = re.search(r"(?im)^\s*Type:\s*([A-Za-z]+)\s*$", body)
    if match:
        canonical = _ENTRY_TYPE_LOOKUP.get(match.group(1).strip().lower())
        if canonical and canonical not in _AUTHORITY_ENTRY_TYPES:
            return canonical

    return UNKNOWN_ENTRY_TYPE


def entry_type_counts(entries: List[Dict[str, Any]]) -> Dict[str, int]:
    """Count entries by canonical entry type (deterministic, no LLM).

    Returns a dict with a key for every type in :data:`ENTRY_TYPES` plus ``Unknown``,
    suitable for a non-LLM entry-mix badge.

    Args:
        entries: Entry dicts to classify via :func:`_normalize_entry_type`.

    Returns:
        Mapping of entry type to count.
    """
    counts: Dict[str, int] = {t: 0 for t in ENTRY_TYPES}
    counts[UNKNOWN_ENTRY_TYPE] = 0
    for entry in entries:
        counts[_normalize_entry_type(entry)] += 1
    return counts


def format_entry_mix(counts: Dict[str, int]) -> str:
    """Render an entry-type count dict as a compact badge string.

    Always shows the canonical types (including zeros) so the badge does not hide thread
    shape; appends ``Unknown`` only when present. Example: ``"10 Note, 0 Plan, 0 Decision,
    0 PR, 0 Closure"``.

    Args:
        counts: Mapping produced by :func:`entry_type_counts`.

    Returns:
        Human-readable entry-mix string.
    """
    parts = [f"{counts.get(t, 0)} {t}" for t in ENTRY_TYPES]
    if counts.get(UNKNOWN_ENTRY_TYPE, 0):
        parts.append(f"{counts[UNKNOWN_ENTRY_TYPE]} {UNKNOWN_ENTRY_TYPE}")
    return ", ".join(parts)


def _launders_authority(prose: str, allow_decision: bool, allow_outcome: bool) -> bool:
    """True if ``prose`` asserts decision/outcome authority it is not permitted to.

    Args:
        prose: Generated thread summary prose.
        allow_decision: Whether the summary window contained a ``Decision`` entry.
        allow_outcome: Whether the summary window contained a ``Closure`` entry.
    """
    if not prose:
        return False
    if not allow_decision and _DECISION_ASSERTION_RE.search(prose):
        return True
    if not allow_outcome and _OUTCOME_ASSERTION_RE.search(prose):
        return True
    return False


def _authority_language_guard(allow_decision: bool, allow_outcome: bool) -> str:
    """Build the constraint clause appended to the prompt when authority is unsupported."""
    clauses = []
    if not allow_decision:
        clauses.append(
            "This thread contains NO Decision entries: do not use decision language "
            '(no "key decisions", "we decided", "the decision is"). Describe it as '
            "discussion or analysis."
        )
    if not allow_outcome:
        clauses.append(
            "This thread contains NO Closure entries: do not claim an outcome or "
            'resolution (no "the outcome is", "resolved", "resolution"). Describe the '
            "current state as ongoing or open."
        )
    if not clauses:
        return ""
    return "\n\nConstraints:\n- " + "\n- ".join(clauses)


def _thread_messages(system_prompt: str, user_content: str) -> List[Dict[str, str]]:
    """Assemble the chat message list for a thread summary call."""
    messages: List[Dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_content})
    return messages


def _scrub_authority(prose: str, allow_decision: bool, allow_outcome: bool) -> str:
    """Drop clauses from extractive prose that assert unsupported authority.

    This operates only on deterministic *extractive* prose (concatenated entry
    snippets), never on generative LLM output, so removing offending fragments is safe
    here even though in-place rewriting of LLM prose is not. A Note whose own body
    contains "we decided to ship" must not reappear as the thread's summary on a
    Decision-free window.
    """
    units = re.split(r"(?<=[.!?])\s+|\s*\|\s*|\n+", prose)
    kept = [
        u for u in units
        if u.strip() and not _launders_authority(u, allow_decision, allow_outcome)
    ]
    return " ".join(kept).strip()


def _extractive_thread_prose(
    combined: str,
    config: SummarizerConfig,
    allow_decision: bool = True,
    allow_outcome: bool = True,
) -> str:
    """Deterministic extractive prose for a thread (the safe fallback).

    When the summary window contains no Decision/Closure entry, the extracted prose is
    re-checked and scrubbed: entry bodies can themselves contain authority phrasing, and
    the #878 invariant is about the summary *surface*, not just LLM-fabricated text.
    """
    prose = extractive_summary(
        combined,
        max_chars=config.extractive_max_chars * 2,
        include_headers=False,
    )
    if _launders_authority(prose, allow_decision, allow_outcome):
        prose = _scrub_authority(prose, allow_decision, allow_outcome)
    return prose


def _finalize_thread_summary(prose: str, all_tags: "set[str]") -> str:
    """Append deterministically aggregated tags to thread prose."""
    if all_tags:
        tag_line = "tags: " + " ".join(f"#{t}" for t in sorted(all_tags))
        return f"{prose}\n{tag_line}"
    return prose


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

    # Deterministic entry-type accounting (no LLM). Authority-language permission is
    # based on the summary *window* (the entries actually sent to the LLM after
    # `max_thread_entries` truncation), not the full thread: otherwise the model could
    # assert a Decision it never saw. The full-thread mix drives the badge metadata
    # (see entry_type_counts / format_entry_mix used by the read/list surfaces).
    window = entries[: config.max_thread_entries]
    window_counts = entry_type_counts(window)
    allow_decision_language = window_counts["Decision"] > 0
    allow_outcome_language = window_counts["Closure"] > 0

    # Collect entry summaries and aggregate tags
    entry_summaries = []
    all_tags: set[str] = set()

    for entry in window:
        body = entry.get("body", "")
        title = entry.get("title", "")
        entry_summary = entry.get("summary", "")
        etype = _normalize_entry_type(entry)

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

        # Prefix each entry with its type so the LLM can see thread shape.
        prefix = f"[{etype}] "
        if title:
            entry_summaries.append(f"- {prefix}{title}: {short}")
        else:
            entry_summaries.append(f"- {prefix}{short}")

    combined = "\n".join(entry_summaries)

    # Use extractive if forced. Extractive prose is built from entry content, which can
    # itself contain authority phrasing, so it is scrubbed against the window's permission.
    if config.prefer_extractive:
        return _finalize_thread_summary(
            _extractive_thread_prose(
                combined, config, allow_decision_language, allow_outcome_language
            ),
            all_tags,
        )

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
        prose_prompt = "Summarize this development thread in 2-3 sentences."

    entry_mix = format_entry_mix(window_counts)
    guard = _authority_language_guard(allow_decision_language, allow_outcome_language)
    grounding = f"\n\n{_GROUNDING_CLAUSE}"

    if "{title}" in prose_prompt or "{entries}" in prose_prompt:
        user_content = prose_prompt.format(title=title, entries=combined)
        user_content = f"{user_content}\n\nEntry mix: {entry_mix}{guard}{grounding}"
    else:
        user_content = f"""{prose_prompt}

Thread: {title}
Entry mix: {entry_mix}{guard}{grounding}

Entries:
{combined}

Summary:"""

    # Add prefix if needed
    if prompt_prefix:
        user_content = f"{prompt_prefix.rstrip()} {user_content}"

    result = _call_llm(_thread_messages(system_prompt, user_content), config)

    if result is None:
        logger.warning(
            "LLM unavailable for thread summarization - returning empty summary. "
            f"Check LLM service at {config.api_base}"
        )
        return ""

    # Strip any tags the LLM may have added (we aggregate our own)
    prose = _strip_tags_from_summary(result)

    # Deterministic guards: a Decision/Closure-free window must never produce
    # decision/outcome language (#878), and the summary must not name an auth/
    # credential mechanism absent from the entries (#902/#788 grounding). Detect ->
    # regenerate once with the matching hardened guard(s) -> fall back to extractive.
    # We never rewrite prose in place (a keyword strip cannot tell laundering from a
    # legitimate reference). Extractive prose is built from the entries themselves,
    # so it can neither launder authority (it is scrubbed) nor fabricate a mechanism.
    launders = _launders_authority(prose, allow_decision_language, allow_outcome_language)
    fabricates = _fabricates_security(prose, combined)
    if launders or fabricates:
        logger.info(
            "Thread summary failed a guard for %r (authority=%s, fabrication=%s); "
            "regenerating with hardened guard",
            title, launders, fabricates,
        )
        extra = []
        if launders:
            extra.append(_HARDENED_AUTHORITY_GUARD)
        if fabricates:
            extra.append(_HARDENED_GROUNDING_GUARD)
        hardened = f"{user_content}\n\n" + "\n\n".join(extra)
        retry = _call_llm(_thread_messages(system_prompt, hardened), config)
        retry_prose = _strip_tags_from_summary(retry) if retry else ""
        if (
            retry_prose
            and not _launders_authority(
                retry_prose, allow_decision_language, allow_outcome_language
            )
            and not _fabricates_security(retry_prose, combined)
        ):
            prose = retry_prose
        else:
            prose = _extractive_thread_prose(
                combined, config, allow_decision_language, allow_outcome_language
            )

    return _finalize_thread_summary(prose, all_tags)


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
