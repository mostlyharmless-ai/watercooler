"""Shared internal utilities for watercooler_memory."""

import hashlib
import os
import time
from datetime import datetime, timezone
from typing import Optional

# Try to import httpx for API calls
try:
    import httpx

    _HTTPX_AVAILABLE = True
except ImportError:
    httpx = None  # type: ignore
    _HTTPX_AVAILABLE = False


def _utc_now_iso() -> str:
    """Return current UTC time in ISO 8601 format with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _generate_chunk_id(text: str, parent_id: str, index: int) -> str:
    """Generate a stable chunk ID based on content hash.

    Args:
        text: Chunk text content.
        parent_id: Parent node ID (entry_id or doc_id).
        index: Position index within parent.

    Returns:
        16-character hex string from SHA-256 hash.
    """
    content = f"{parent_id}:{index}:{text}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _ensure_httpx() -> None:
    """Raise ImportError if httpx is not installed."""
    if not _HTTPX_AVAILABLE:
        raise ImportError(
            "httpx is required for this operation. "
            "Install with: pip install 'watercooler-cloud[memory]'"
        )


def _resolve_embedding_field(attr: str, env_var: str, default):
    """Resolve embedding config: unified config -> env var -> default.

    Args:
        attr: Attribute name on the resolved embedding config object.
        env_var: Environment variable to check as fallback.
        default: Built-in default value. Its type is used for env var casting.
    """
    try:
        from watercooler.memory_config import resolve_embedding_config

        return getattr(resolve_embedding_config(), attr)
    except (ImportError, AttributeError):
        val = os.environ.get(env_var)
        if not val:  # treat empty string same as unset
            return default
        target = type(default)
        if target in (int, float):
            try:
                return target(val)
            except (ValueError, TypeError):
                return default
        return val


def _resolve_llm_field(attr: str, env_var: str, default):
    """Resolve LLM config: unified config -> env var -> default.

    Args:
        attr: Attribute name on the resolved LLM config object.
        env_var: Environment variable to check as fallback.
        default: Built-in default value. Its type is used for env var casting.
    """
    try:
        from watercooler.memory_config import resolve_llm_config

        return getattr(resolve_llm_config(), attr)
    except (ImportError, AttributeError):
        val = os.environ.get(env_var)
        if not val:  # treat empty string same as unset
            return default
        target = type(default)
        if target in (int, float):
            try:
                return target(val)
            except (ValueError, TypeError):
                return default
        return val


def _http_post_with_retry(
    *,
    url: str,
    payload: dict,
    headers: dict,
    timeout: float,
    max_retries: int,
    error_cls: type[Exception],
) -> dict:
    """POST JSON with exponential backoff. Returns parsed JSON response.

    Args:
        url: Target URL.
        payload: JSON request body.
        headers: HTTP headers.
        timeout: Request timeout in seconds.
        max_retries: Maximum number of attempts.
        error_cls: Exception class to wrap errors with.

    Returns:
        Parsed JSON response as dict.

    Raises:
        error_cls: On all retries exhausted.
        ImportError: If httpx is not installed.
    """
    _ensure_httpx()

    last_error: Optional[Exception] = None

    for attempt in range(max_retries):
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            last_error = error_cls(
                f"HTTP {e.response.status_code}: {e.response.text}"
            )
        except httpx.RequestError as e:
            last_error = error_cls(f"Request failed: {e}")
        except ValueError as e:
            # JSON decode failure — response body wasn't valid JSON
            body_snippet = getattr(response, "text", "")[:200]
            last_error = error_cls(
                f"Invalid JSON in response: {e} — body: {body_snippet}"
            )
        except Exception as e:
            last_error = error_cls(f"Unexpected error: {e}")

        if attempt < max_retries - 1:
            time.sleep(2**attempt)

    raise last_error or error_cls("Request failed with unknown error")
