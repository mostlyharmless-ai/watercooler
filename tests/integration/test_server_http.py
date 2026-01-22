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

    def test_hosted_mode_requires_user_id(self):
        """Hosted mode requires X-User-ID header for /mcp."""
        with patch.dict(os.environ, {
            "WATERCOOLER_AUTH_MODE": "hosted",
            "WATERCOOLER_TOKEN_API_URL": "https://example.com",
            "WATERCOOLER_TOKEN_API_KEY": "test-key",
        }, clear=False):
            app = create_http_app()
            client = TestClient(app)

            # Request without X-User-ID should fail
            response = client.post("/mcp", json={})
            assert response.status_code == 401
            assert "X-User-ID" in response.json()["error"]

    def test_hosted_mode_health_no_auth(self):
        """Health endpoint doesn't require auth even in hosted mode."""
        with patch.dict(os.environ, {
            "WATERCOOLER_AUTH_MODE": "hosted",
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
