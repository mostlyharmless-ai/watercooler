"""Authentication module for hosted MCP service.

Provides token resolution for GitHub API calls when running as a hosted service.
Tokens are fetched from the watercooler-site token service API, enabling the
MCP server to authenticate with GitHub on behalf of users.

Environment variables:
- WATERCOOLER_TOKEN_API_URL: Base URL of the token service (e.g., https://watercoolerdev.com)
- WATERCOOLER_TOKEN_API_KEY: API key for authenticating with the token service
- WATERCOOLER_AUTH_MODE: "local" (default, use git credentials) or "hosted" (use token service)
- VERCEL_AUTOMATION_BYPASS_SECRET: Optional secret to bypass Vercel preview auth
  (required when token service is on a Vercel preview deployment with auth enabled)

Token Resolution Flow (hosted mode):
    1. Request arrives with user context (user_id or session)
    2. MCP calls token service: GET /api/github/token?userId={user_id}
    3. Token service decrypts and returns GitHub OAuth token
    4. MCP uses token for GitHub API calls

Usage:
    from watercooler_mcp.auth import get_github_token, is_hosted_mode

    if is_hosted_mode():
        token = get_github_token(user_id="user_123")
        if token:
            # Use token for GitHub API calls
            ...
    else:
        # Use local git credentials (ssh key, credential helper, etc.)
        ...
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# Cache for GitHub tokens (user_id -> GitHubTokenInfo)
# Tokens are cached for the lifetime of the process
_github_token_cache: Dict[str, "GitHubTokenInfo"] = {}


@dataclass
class GitHubTokenInfo:
    """GitHub token with metadata."""

    token: str
    user_id: str
    github_username: Optional[str] = None
    scopes: Optional[str] = None
    expires_at: Optional[str] = None


def get_auth_config() -> Dict[str, str]:
    """Get authentication configuration from environment.

    Returns:
        Dict with auth_mode, token_api_url, token_api_key, and vercel_bypass_secret
    """
    return {
        "auth_mode": os.getenv("WATERCOOLER_AUTH_MODE", "local"),
        "token_api_url": os.getenv("WATERCOOLER_TOKEN_API_URL", ""),
        "token_api_key": os.getenv("WATERCOOLER_TOKEN_API_KEY", ""),
        "vercel_bypass_secret": os.getenv("VERCEL_AUTOMATION_BYPASS_SECRET", ""),
    }


def is_hosted_mode() -> bool:
    """Check if running in hosted mode (using token service).

    Returns:
        True if auth_mode is "hosted" and token service is configured
    """
    config = get_auth_config()
    return (
        config["auth_mode"] == "hosted"
        and bool(config["token_api_url"])
        and bool(config["token_api_key"])
    )


def is_token_service_configured() -> bool:
    """Check if the token service is configured.

    Returns:
        True if both API URL and key are set
    """
    config = get_auth_config()
    return bool(config["token_api_url"]) and bool(config["token_api_key"])


def get_github_token(
    user_id: str,
    use_cache: bool = True,
) -> Optional[GitHubTokenInfo]:
    """Fetch GitHub token for a user from the token service.

    Args:
        user_id: User identifier (from session or request context)
        use_cache: Whether to use cached tokens (default: True)

    Returns:
        GitHubTokenInfo if found, None otherwise

    Note:
        If not in hosted mode or token service is not configured,
        returns None without making any API calls.
    """
    if not user_id:
        logger.warning("get_github_token called with empty user_id")
        return None

    # Check cache first
    if use_cache and user_id in _github_token_cache:
        logger.debug(f"Using cached GitHub token for user {user_id}")
        return _github_token_cache[user_id]

    config = get_auth_config()
    api_url = config["token_api_url"]
    api_key = config["token_api_key"]
    vercel_bypass_secret = config["vercel_bypass_secret"]

    if not api_url or not api_key:
        logger.debug("Token service not configured, skipping API call")
        return None

    # Build request URL
    url = f"{api_url.rstrip('/')}/api/github/token?userId={user_id}"

    # Build headers - include Vercel bypass if configured (for preview deployments)
    headers = {
        "x-api-key": api_key,
        "Accept": "application/json",
    }
    if vercel_bypass_secret:
        headers["x-vercel-protection-bypass"] = vercel_bypass_secret
        logger.debug("Including Vercel preview bypass header in token service request")

    try:
        request = urllib.request.Request(
            url,
            headers=headers,
            method="GET",
        )

        with urllib.request.urlopen(request, timeout=10.0) as response:
            data = json.loads(response.read().decode("utf-8"))

        token = data.get("token")
        if token:
            token_info = GitHubTokenInfo(
                token=token,
                user_id=user_id,
                github_username=data.get("githubUsername"),
                scopes=data.get("scopes"),
                expires_at=data.get("expiresAt"),
            )
            # Cache the token
            _github_token_cache[user_id] = token_info
            logger.info(
                f"Retrieved GitHub token for user {user_id} "
                f"(github: {token_info.github_username or 'unknown'})"
            )
            return token_info

        logger.warning(f"Token service returned no token for user {user_id}")
        return None

    except urllib.error.HTTPError as e:
        if e.code == 404:
            logger.info(f"User {user_id} not found in token service")
        elif e.code == 401:
            logger.error("Token service authentication failed - check WATERCOOLER_TOKEN_API_KEY")
        else:
            body = e.read().decode("utf-8") if e.fp else ""
            logger.error(f"Token service HTTP error {e.code}: {body}")
        return None

    except urllib.error.URLError as e:
        logger.error(f"Token service connection error: {e.reason}")
        return None

    except json.JSONDecodeError as e:
        logger.error(f"Token service returned invalid JSON: {e}")
        return None

    except Exception as e:
        logger.error(f"Unexpected error fetching GitHub token: {e}")
        return None


def clear_token_cache() -> None:
    """Clear all cached tokens.

    Useful for testing or when tokens may have been rotated.
    """
    _github_token_cache.clear()
    logger.debug("GitHub token cache cleared")


def invalidate_user_token(user_id: str) -> None:
    """Remove a specific user's token from cache.

    Call this if a token is rejected by GitHub API to force a refresh.

    Args:
        user_id: User ID to invalidate
    """
    if user_id in _github_token_cache:
        del _github_token_cache[user_id]
        logger.debug(f"Invalidated cached token for user {user_id}")


def get_auth_headers(user_id: str) -> Optional[Dict[str, str]]:
    """Get HTTP headers for authenticated GitHub API requests.

    Convenience method that returns properly formatted headers for
    GitHub API authentication.

    Args:
        user_id: User identifier

    Returns:
        Dict with Authorization header, or None if token not available
    """
    token_info = get_github_token(user_id)
    if token_info:
        return {
            "Authorization": f"token {token_info.token}",
            "Accept": "application/vnd.github.v3+json",
        }
    return None


# =============================================================================
# Request Context (for extracting user_id from HTTP requests)
# =============================================================================


@dataclass
class RequestContext:
    """Context extracted from an incoming HTTP request.

    In hosted mode, MCP tools need to know which user is making the request
    to fetch the appropriate GitHub token.
    """

    user_id: Optional[str] = None
    session_id: Optional[str] = None
    repo: Optional[str] = None
    branch: Optional[str] = None


def extract_request_context(
    headers: Dict[str, str],
    query_params: Optional[Dict[str, str]] = None,
) -> RequestContext:
    """Extract request context from HTTP headers and query params.

    The token service expects requests to include user identification:
    - X-User-ID header: User identifier from session
    - X-Session-ID header: Session identifier
    - repo query param: Repository context
    - branch query param: Branch context

    Args:
        headers: HTTP request headers
        query_params: Optional query parameters

    Returns:
        RequestContext with extracted values
    """
    query_params = query_params or {}

    return RequestContext(
        user_id=headers.get("X-User-ID") or headers.get("x-user-id"),
        session_id=headers.get("X-Session-ID") or headers.get("x-session-id"),
        repo=query_params.get("repo"),
        branch=query_params.get("branch"),
    )
