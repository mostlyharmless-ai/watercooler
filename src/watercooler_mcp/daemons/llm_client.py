"""Thin synchronous LLM client for daemon use.

Provides a minimal OpenAI-compatible chat completions client that daemons
can use for LLM-powered analysis. Follows the same patterns as
``baseline_graph.summarizer._call_llm()`` but with daemon-specific config
resolution and concurrency protection.

Key properties:
- Synchronous (httpx), no async — matches daemon thread model
- Graceful degradation: ``is_available()`` probe before calls, None on failure
- ``threading.Semaphore(1)`` for localhost endpoints to protect local llama-server
- Model-aware response parsing via ``models.get_response_field()``
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from watercooler.memory_config import (
    AUTH_SKIP_SENTINELS,
    ResolvedDaemonLLMConfig,
    is_localhost_url,
    is_anthropic_url,
    resolve_daemon_llm_config,
)

logger = logging.getLogger(__name__)

# Per-port semaphores for localhost concurrency protection.
# Different local servers (e.g. LLM on :8000, embeddings on :8080)
# should not block each other.
_localhost_semaphores: dict[str, threading.Semaphore] = {}
_semaphore_lock = threading.Lock()


def _get_localhost_semaphore(api_base: str) -> threading.Semaphore:
    """Get or create a semaphore for a specific localhost endpoint."""
    from urllib.parse import urlparse
    parsed = urlparse(api_base)
    # Default to port 80/443 when implicit
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    key = f"{parsed.hostname}:{port}"
    with _semaphore_lock:
        if key not in _localhost_semaphores:
            _localhost_semaphores[key] = threading.Semaphore(1)
        return _localhost_semaphores[key]


class DaemonLLMClient:
    """Thin synchronous LLM client for daemon use.

    Args:
        config: Resolved daemon LLM config. If None, resolves from
            TOML/env using ``daemon_name``.
        daemon_name: Used for config resolution and logging. Defaults to
            empty string (shared daemon config only).
    """

    def __init__(
        self,
        config: Optional[ResolvedDaemonLLMConfig] = None,
        daemon_name: str = "",
    ) -> None:
        self._daemon_name = daemon_name
        self._config = config or resolve_daemon_llm_config(daemon_name)
        self._is_localhost = is_localhost_url(self._config.api_base)

    @property
    def config(self) -> ResolvedDaemonLLMConfig:
        return self._config

    def is_available(self) -> bool:
        """Probe the configured LLM endpoint for availability.

        For localhost URLs, probes GET /v1/models (llama-server pattern).
        For Anthropic URLs, probes GET /messages (returns 405 = reachable).
        For other remote URLs, probes GET /v1/models.

        Returns:
            True if the endpoint is reachable and responding.
        """
        try:
            import httpx
        except ImportError:
            logger.debug("DAEMON_LLM[%s]: httpx not available", self._daemon_name)
            return False

        api_base = self._config.api_base
        if not api_base:
            return False

        is_anthropic = is_anthropic_url(api_base)
        headers: dict[str, str] = {}

        if is_anthropic:
            headers["anthropic-version"] = "2023-06-01"

        if self._config.api_key and self._config.api_key not in AUTH_SKIP_SENTINELS:
            if is_anthropic:
                headers["x-api-key"] = self._config.api_key
            else:
                headers["Authorization"] = f"Bearer {self._config.api_key}"

        try:
            with httpx.Client(timeout=5.0) as client:
                if is_anthropic:
                    base = api_base.rstrip("/")
                    if base.endswith("/v1"):
                        base = base[:-3]
                    url = f"{base}/v1/messages"
                    response = client.get(url, headers=headers)
                    # 405 = method not allowed (GET on POST endpoint) = reachable
                    if response.status_code in (200, 405):
                        return True
                    if response.status_code in (401, 403):
                        logger.warning(
                            "DAEMON_LLM[%s]: Anthropic endpoint reachable but "
                            "auth failed (HTTP %d) — check api_key",
                            self._daemon_name, response.status_code,
                        )
                    return False
                else:
                    # Normalize: ensure /v1/models regardless of whether
                    # api_base ends with /v1 or not
                    base = api_base.rstrip("/")
                    if not base.endswith("/v1"):
                        base = f"{base}/v1"
                    url = f"{base}/models"
                    response = client.get(url, headers=headers)
                    if 200 <= response.status_code < 300:
                        return True
                    if response.status_code in (401, 403):
                        logger.warning(
                            "DAEMON_LLM[%s]: endpoint reachable but "
                            "auth failed (HTTP %d) — check api_key",
                            self._daemon_name, response.status_code,
                        )
                    return False
        except Exception as e:
            logger.debug(
                "DAEMON_LLM[%s]: endpoint not available at %s: %s",
                self._daemon_name, api_base, e,
            )
            return False

    def complete(
        self,
        prompt: str,
        system: str = "",
        max_tokens: Optional[int] = None,
    ) -> Optional[str]:
        """Send a chat completion request to the configured LLM.

        Args:
            prompt: User message content.
            system: Optional system message.
            max_tokens: Override max_tokens from config.

        Returns:
            LLM response text, or None on any failure (connection,
            timeout, parse error). Never raises.
        """
        try:
            import httpx
        except ImportError:
            logger.warning("DAEMON_LLM[%s]: httpx not available", self._daemon_name)
            return None

        cfg = self._config
        _is_anthropic = is_anthropic_url(cfg.api_base)

        # Model-aware max_tokens — thinking models need a higher floor
        from watercooler.models import get_min_max_tokens
        requested_max_tokens = max_tokens if max_tokens is not None else cfg.max_tokens
        model_min = get_min_max_tokens(cfg.model, requested_max_tokens)
        effective_max_tokens = max(requested_max_tokens, model_min)
        if max_tokens is not None and effective_max_tokens > max_tokens:
            logger.debug(
                "DAEMON_LLM[%s]: max_tokens raised from %d to %d "
                "(model %s minimum for thinking)",
                self._daemon_name, max_tokens, effective_max_tokens, cfg.model,
            )

        # Build request based on API type
        if _is_anthropic:
            # Anthropic base may or may not include /v1 — normalize
            base = cfg.api_base.rstrip("/")
            if base.endswith("/v1"):
                base = base[:-3]
            url = f"{base}/v1/messages"
            payload = {
                "model": cfg.model,
                "max_tokens": effective_max_tokens,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            }
            if system:
                payload["system"] = system
            headers: dict[str, str] = {
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            }
            if cfg.api_key and cfg.api_key not in AUTH_SKIP_SENTINELS:
                headers["x-api-key"] = cfg.api_key
        else:
            # Build OpenAI-compatible messages list
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})

            # Normalize: ensure /v1/chat/completions regardless of
            # whether api_base ends with /v1 or not
            base = cfg.api_base.rstrip("/")
            if not base.endswith("/v1"):
                base = f"{base}/v1"
            url = f"{base}/chat/completions"
            payload = {
                "model": cfg.model,
                "messages": messages,
                "max_tokens": effective_max_tokens,
                "temperature": 0.3,
            }
            headers = {"Content-Type": "application/json"}
            if cfg.api_key and cfg.api_key not in AUTH_SKIP_SENTINELS:
                headers["Authorization"] = f"Bearer {cfg.api_key}"

        # Concurrency protection for local servers.
        # Wait up to 2x the request timeout for the semaphore — long enough
        # to let one in-flight request finish, short enough to avoid
        # unbounded stacking when multiple callers queue up.
        sem = _get_localhost_semaphore(cfg.api_base) if self._is_localhost else None
        acquired = False
        if sem is not None:
            sem_timeout = min((cfg.timeout if cfg.timeout is not None else 60.0) * 2, 120.0)
            acquired = sem.acquire(timeout=sem_timeout)
            if not acquired:
                logger.warning(
                    "DAEMON_LLM[%s]: localhost semaphore timeout after %.1fs",
                    self._daemon_name, sem_timeout,
                )
                return None

        from .telemetry import track_call, SVC_LLM

        try:
            with httpx.Client(timeout=cfg.timeout) as client, \
                 track_call(SVC_LLM) as t:
                response = client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()

                try:
                    if _is_anthropic:
                        # Anthropic response: {"content": [{"text": "..."}], "usage": {...}}
                        content_blocks = data.get("content", [])
                        content = ""
                        for block in content_blocks:
                            if block.get("type") == "text":
                                content += block.get("text", "")
                        content = content.strip()

                        usage = data.get("usage", {})
                        if usage:
                            t.prompt_tokens = usage.get("input_tokens", 0)
                            t.completion_tokens = usage.get("output_tokens", 0)
                            logger.info(
                                "DAEMON_LLM[%s]: model=%s input=%d output=%d",
                                self._daemon_name, cfg.model,
                                t.prompt_tokens, t.completion_tokens,
                            )
                    else:
                        choices = data.get("choices") or []
                        if not choices:
                            raise KeyError("response has no 'choices'")
                        message = choices[0].get("message") or {}
                        if not message:
                            raise KeyError("first choice has no 'message'")

                        usage = data.get("usage", {})
                        if usage:
                            t.prompt_tokens = usage.get("prompt_tokens", 0)
                            t.completion_tokens = usage.get("completion_tokens", 0)
                            logger.info(
                                "DAEMON_LLM[%s]: model=%s prompt=%d completion=%d total=%d",
                                self._daemon_name, cfg.model,
                                t.prompt_tokens, t.completion_tokens,
                                t.prompt_tokens + t.completion_tokens,
                            )

                        # Model-aware response field extraction
                        from watercooler.models import get_response_field
                        response_field = get_response_field(cfg.model)
                        content = message.get(response_field, "").strip()
                        if not content and response_field != "content":
                            content = message.get("content", "").strip()
                except (KeyError, IndexError, TypeError) as parse_err:
                    t.mark_error()
                    logger.warning(
                        "DAEMON_LLM[%s]: API returned 200 but response "
                        "structure invalid: %s (data keys: %s)",
                        self._daemon_name, parse_err,
                        list(data.keys()) if isinstance(data, dict) else type(data).__name__,
                    )
                    return None

                return content if content else None

        except Exception as e:
            # Distinguish auth failures from transient errors
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (401, 403):
                logger.warning(
                    "DAEMON_LLM[%s]: auth failure (HTTP %d) — check api_key: %s",
                    self._daemon_name, status, e,
                )
            elif status == 429:
                logger.warning(
                    "DAEMON_LLM[%s]: rate limited (HTTP 429): %s",
                    self._daemon_name, e,
                )
            else:
                logger.warning(
                    "DAEMON_LLM[%s]: call failed: %s", self._daemon_name, e,
                )
            return None
        finally:
            if acquired and sem is not None:
                sem.release()
