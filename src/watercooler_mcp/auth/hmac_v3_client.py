"""HMAC v3 client signing helper — Sprint 3 caller migration.

Reference Python implementation for callers that sign requests
against the hosted MCP server's v3 verification path. The canonical
string format is defined by ``hmac_keys.build_v3_canonical_string``;
this module is a thin client wrapper that runs the same canonical
construction on outbound requests, computes the HMAC, and returns
the headers to attach to the HTTP request.

Used by:

- ``tests/integration/test_railway_smoke.py`` — exercises the full
  v3 round-trip in local mode.
- Future Sprint 3 caller migrations (premium_client, capture_hook,
  ops scripts).

The dashboard proxy lives in TypeScript at
``watercooler-site/lib/mcpClient.ts`` and reimplements the same
canonical-string format. Keep this module in sync with both
``hmac_keys.build_v3_canonical_string`` (server side) and the
dashboard proxy (cross-language sibling).
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
from typing import Dict, Optional

from .hmac_keys import build_v3_canonical_string

__all__ = ["sign_v3_request", "v3_authorization_header"]


def v3_authorization_header(*, key_id: str, signature_hex: str) -> str:
    """Build the ``Authorization`` header value for a v3 request.

    The format is fixed by ``parse_v3_authorization_header`` in
    ``hmac_keys``: ``HMAC-SHA256 v=3 kid=<key_id> sig=<hex>``.
    """
    return f"HMAC-SHA256 v=3 kid={key_id} sig={signature_hex}"


def sign_v3_request(
    *,
    method: str,
    path: str,
    body: bytes,
    key_id: str,
    secret: bytes,
    user_id: str,
    x_repo: str,
    x_branch: str,
    timestamp: Optional[str] = None,
) -> Dict[str, str]:
    """Sign a v3 request and return the headers to attach.

    Args:
        method: HTTP method. Upper-cased before canonical-string
            construction so callers can pass either case (the server
            also upper-cases on its side, so this is consistent by
            construction).
        path: URL path the request will hit (e.g., ``"/mcp/"``).
            Must match the path Starlette parses on the server.
            Query strings are not part of the canonical string —
            only the path component.
        body: Raw request body bytes. Hashed via SHA-256 before
            inclusion in the canonical string (matching server-side
            ``build_v3_canonical_string``).
        key_id: Registered key identifier; must match a server-side
            registry entry.
        secret: Raw HMAC secret bytes. NOT hex-encoded, NOT the
            UTF-8 of a hex string — the literal bytes used for the
            SHA-256 HMAC computation. Callers loading from env
            vars or hex strings must decode upstream
            (``bytes.fromhex(s)`` for hex,
            ``s.encode("utf-8")`` for UTF-8 secrets matching the
            ``WATERCOOLER_HMAC_KEY_<id>_SECRET`` operator
            convention).
        user_id: Subject the key is signing for. For ``per_user``
            keys this must equal the registered ``bound_user_id``;
            for ``service`` keys with ``no_user_delegation`` it
            must equal ``service_identity``; for ``service`` keys
            with an explicit allow-list it must be in that list.
        x_repo: Canonical ``org/repo`` repo header value. The
            server enforces membership in either the user's
            ``repos`` claim (per_user) or the key's
            ``repo_allow_list`` (service).
        x_branch: Branch header value. Currently only signed
            (not enforced as a separate authorisation surface),
            but tampering still invalidates the signature.
        timestamp: Optional ISO 8601 UTC timestamp. Defaults to the
            current UTC time. Callers can pin this for tests; the
            server enforces a replay window so distant past/future
            timestamps fail verification.

    Returns:
        Dict of headers to attach to the outgoing HTTP request.
        Keys: ``Authorization``, ``X-User-ID``,
        ``X-Request-Timestamp``, ``X-Repo``, ``X-Branch``.

    Raises:
        ValueError: If ``user_id``, ``x_repo``, ``x_branch``, or
            ``path`` contain CR or LF (delegated to
            ``build_v3_canonical_string``'s field-boundary guard).
    """
    if timestamp is None:
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()

    upper_method = method.upper()

    canonical = build_v3_canonical_string(
        method=upper_method,
        path=path,
        timestamp=timestamp,
        key_id=key_id,
        user_id=user_id,
        body=body,
        x_repo=x_repo,
        x_branch=x_branch,
    )
    signature_hex = hmac.new(secret, canonical, hashlib.sha256).hexdigest()

    return {
        "Authorization": v3_authorization_header(
            key_id=key_id, signature_hex=signature_hex
        ),
        "X-User-ID": user_id,
        "X-Request-Timestamp": timestamp,
        "X-Repo": x_repo,
        "X-Branch": x_branch,
    }
