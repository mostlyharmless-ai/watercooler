"""Secrets-handling primitives for Move 4 of the security consolidation.

The flagship export is :class:`Secret`, an opaque non-str wrapper that
fails *loudly* when handled by a naive serializer instead of leaking
its underlying value. See :mod:`watercooler_mcp.secrets.gateway` for
the full surface.
"""

from .gateway import (
    SECRET_PATTERN,
    Secret,
    SecretJSONEncoder,
    load_github_token_secret,
    load_slack_workspace_token_secret,
    redact_object,
    redact_value,
)

__all__ = [
    "SECRET_PATTERN",
    "Secret",
    "SecretJSONEncoder",
    "load_github_token_secret",
    "load_slack_workspace_token_secret",
    "redact_object",
    "redact_value",
]
