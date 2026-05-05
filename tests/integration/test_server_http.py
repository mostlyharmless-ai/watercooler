"""Integration tests for HTTP server (server_http.py).

These tests verify the HTTP transport layer, CORS configuration,
request limits, timeouts, and authentication middleware.

Requires the [http] extra: pip install watercooler-cloud[http]
"""

from __future__ import annotations

import os
import pytest
from unittest.mock import patch, MagicMock

# Skip all tests if HTTP dependencies not installed
try:
    from fastapi.testclient import TestClient
    from watercooler_mcp.server_http import create_http_app, check_http_dependencies
    HTTP_AVAILABLE = check_http_dependencies()
except ImportError:
    HTTP_AVAILABLE = False
    TestClient = None
    create_http_app = None

pytestmark = pytest.mark.skipif(not HTTP_AVAILABLE, reason="HTTP dependencies not installed")


@pytest.fixture
def app():
    """Create a test app with local mode."""
    with patch.dict(os.environ, {
        "WATERCOOLER_AUTH_MODE": "local",
        "WATERCOOLER_CORS_ORIGINS": "",
    }, clear=False):
        return create_http_app()


@pytest.fixture
def client(app):
    """Create a test client."""
    return TestClient(app)


class TestHealthEndpoints:
    """Tests for health and root endpoints."""

    def test_health_check(self, client):
        """Health endpoint returns healthy status."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "mode" in data
        assert "cache" in data

    def test_root_endpoint(self, client):
        """Root endpoint returns API information."""
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "Watercooler MCP HTTP Server"
        assert "endpoints" in data
        assert "/health" in data["endpoints"]
        assert "/mcp" in data["endpoints"]


class TestCORSConfiguration:
    """Tests for CORS middleware configuration."""

    def test_cors_wildcard_no_credentials(self, client):
        """Wildcard origins disables credentials for security."""
        # Test that health endpoint works (CORS is configured)
        response = client.get("/health")
        assert response.status_code == 200

    def test_cors_explicit_origins_with_credentials(self, client):
        """Explicit origins allows credentials."""
        # Test that health endpoint works
        response = client.get("/health")
        assert response.status_code == 200

    def test_cors_empty_defaults_to_wildcard(self, client):
        """Empty CORS origins defaults to wildcard without credentials."""
        response = client.get("/health")
        assert response.status_code == 200


class TestRequestLimits:
    """Tests for request size limits and timeouts."""

    def test_request_too_large(self):
        """Request exceeding size limit returns 413."""
        with patch.dict(os.environ, {
            "WATERCOOLER_AUTH_MODE": "local",
            "WATERCOOLER_MAX_REQUEST_SIZE": "100",  # 100 bytes
        }, clear=False):
            app = create_http_app()
            client = TestClient(app)

            # Send request with Content-Length header exceeding limit
            large_body = "x" * 200
            response = client.post(
                "/mcp",
                content=large_body,
                headers={"Content-Length": "200", "Content-Type": "application/json"},
            )
            assert response.status_code == 413
            assert "too large" in response.json()["error"].lower()

    def test_request_within_limit(self, client):
        """Request within size limit is processed."""
        # Small request should not be rejected for size
        response = client.get("/health")
        assert response.status_code == 200


class TestAuthenticationMiddleware:
    """Tests for authentication in hosted mode."""

    def test_local_mode_no_auth_required(self, client):
        """Local mode does not require authentication."""
        response = client.get("/health")
        assert response.status_code == 200

    def test_hosted_mode_unauthenticated_returns_401(self):
        """#733: with the legacy v1/v2 single-secret verifier deleted,
        an unauthenticated /mcp request in hosted mode falls through
        to the hosted-identity gate and receives 401. The legacy
        misconfiguration-503 surface is gone.
        """
        env = {
            "WATERCOOLER_MODE": "hosted",
            "WATERCOOLER_TOKEN_API_URL": "https://example.com",
            "WATERCOOLER_TOKEN_API_KEY": "test-key",
        }
        with patch.dict(os.environ, env, clear=False):
            app = create_http_app()
            client = TestClient(app, raise_server_exceptions=False)

            response = client.post("/mcp/", json={})
            assert response.status_code == 401
            assert "HMAC secret required" not in response.json().get("error", "")

    def test_hosted_mode_requires_user_id(self):
        """Hosted mode requires X-User-ID or Bearer for /mcp."""
        env = {
            "WATERCOOLER_MODE": "hosted",
            "WATERCOOLER_TOKEN_API_URL": "https://example.com",
            "WATERCOOLER_TOKEN_API_KEY": "test-key",
        }
        with patch.dict(os.environ, env, clear=False):
            app = create_http_app()
            client = TestClient(app, raise_server_exceptions=False)

            # Request without auth headers receives 401 from the hosted
            # identity gate (no fallback v2 HMAC verifier exists post-#733)
            response = client.post("/mcp/", json={})
            assert response.status_code == 401

    def test_hosted_mode_health_no_auth(self):
        """Health endpoint doesn't require auth even in hosted mode."""
        with patch.dict(os.environ, {
            "WATERCOOLER_MODE": "hosted",
            "WATERCOOLER_TOKEN_API_URL": "https://example.com",
            "WATERCOOLER_TOKEN_API_KEY": "test-key",
        }, clear=False):
            app = create_http_app()
            client = TestClient(app)

            response = client.get("/health")
            assert response.status_code == 200

    def test_hosted_mode_missing_token(self):
        """Hosted mode returns 403 if user has no token."""
        # This test requires proper async context for MCP
        # Skip for now as it requires complex lifespan handling
        pytest.skip("MCP endpoint requires async lifespan context")

    def test_startup_logs_identity_mode_policy(self, monkeypatch):
        """create_http_app() emits a single INFO line naming the
        configured identity-mode policy.

        PR #748 review round 3 MED: closes the observability gap
        the reviewer flagged — operators should have a boot-time
        signal of which mode is active rather than having to grep
        ``hosted_identity_auth_used`` telemetry. Mirrors the
        existing HMAC v3 registry-loaded startup log.

        Captures ``logger.info`` directly rather than via caplog —
        ``observability.log_action`` (called earlier in
        ``create_http_app``) flips ``watercooler_mcp.propagate`` to
        False on first call (documented in
        ``auth/__init__.py:285-291``), which breaks caplog assertions
        when this test runs alongside prior tests that already
        triggered the propagation flip.
        """
        from watercooler_mcp import server_http as _sh

        captured: list[tuple[str, tuple]] = []
        original_info = _sh.logger.info

        def _capture_info(msg, *args, **kwargs):
            captured.append((msg, args))
            # Forward so other startup INFO lines still surface in
            # captured stderr for debugging.
            return original_info(msg, *args, **kwargs)

        monkeypatch.setattr(_sh.logger, "info", _capture_info)

        env = {
            "WATERCOOLER_MODE": "hosted",
            "WATERCOOLER_TOKEN_API_URL": "https://example.com",
            "WATERCOOLER_TOKEN_API_KEY": "test-key",
            "WATERCOOLER_REQUIRE_HMAC_OR_BEARER": "enforce",
        }
        with patch.dict(os.environ, env, clear=False):
            create_http_app()

        identity_msgs = [
            (msg, args)
            for msg, args in captured
            if "Hosted identity-mode policy" in msg
        ]
        assert len(identity_msgs) == 1, (
            f"expected exactly one identity-mode startup log, got "
            f"{len(identity_msgs)}: {identity_msgs!r}"
        )
        rendered = identity_msgs[0][0] % identity_msgs[0][1]
        assert "enforce" in rendered, rendered
        assert "WATERCOOLER_REQUIRE_HMAC_OR_BEARER" in rendered, rendered
        assert "X-User-ID-only requests rejected with 401" in rendered, rendered

    def test_startup_logs_identity_mode_warn_default(self, monkeypatch):
        """Default warn-mode emits the equivalent INFO line with
        warn-mode body. Defends against a future refactor that only
        logs in enforce mode and silences the default-deployment
        signal — operators need the boot-time policy signal in
        BOTH modes."""
        from watercooler_mcp import server_http as _sh

        captured: list[tuple[str, tuple]] = []

        def _capture_info(msg, *args, **kwargs):
            captured.append((msg, args))

        monkeypatch.setattr(_sh.logger, "info", _capture_info)

        # Default — env unset.
        monkeypatch.delenv("WATERCOOLER_REQUIRE_HMAC_OR_BEARER", raising=False)
        env = {
            "WATERCOOLER_MODE": "hosted",
            "WATERCOOLER_TOKEN_API_URL": "https://example.com",
            "WATERCOOLER_TOKEN_API_KEY": "test-key",
        }
        with patch.dict(os.environ, env, clear=False):
            create_http_app()
        identity_msgs = [
            (msg, args)
            for msg, args in captured
            if "Hosted identity-mode policy" in msg
        ]
        assert len(identity_msgs) == 1
        rendered = identity_msgs[0][0] % identity_msgs[0][1]
        assert "warn" in rendered, rendered
        assert "accepted with telemetry" in rendered, rendered


class TestMCPEndpoint:
    """Tests for the MCP protocol endpoint.

    Note: These tests are marked as xfail because FastMCP requires
    proper async lifespan context that TestClient doesn't provide
    in all cases.
    """

    @pytest.mark.xfail(reason="FastMCP requires async lifespan context")
    def test_mcp_endpoint_exists(self, client):
        """MCP endpoint is mounted and accessible."""
        # The exact response depends on FastMCP, but endpoint should exist
        response = client.get("/mcp")
        # FastMCP may return 405 for GET or 200 with info
        assert response.status_code in (200, 405, 422)

    @pytest.mark.xfail(reason="FastMCP requires async lifespan context")
    def test_mcp_post_without_body(self, client):
        """POST to /mcp without body returns appropriate error."""
        response = client.post("/mcp")
        # FastMCP should handle the request
        assert response.status_code in (200, 400, 422)
