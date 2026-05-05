"""Tests for dual-surface hosted HTTP app (Step 9)."""

from __future__ import annotations

from unittest.mock import patch

import pytest


class TestCreateHttpApp:
    """Test that the HTTP app factory produces a dual-surface FastAPI app."""

    @pytest.fixture
    def http_app(self):
        """Create the HTTP app with mocked auth."""
        with patch.dict("os.environ", {
            "WATERCOOLER_MODE": "hosted",
        }):
            from watercooler_mcp.server_http import create_http_app
            return create_http_app()

    def test_health_endpoint_exists(self, http_app):
        """The /health endpoint should be on the parent app."""
        route_paths = [r.path for r in http_app.routes if hasattr(r, "path")]
        assert "/health" in route_paths

    def test_root_lists_premium(self, http_app):
        """The root endpoint should mention /mcp/premium."""
        route_paths = [r.path for r in http_app.routes if hasattr(r, "path")]
        assert "/" in route_paths

    def test_mcp_mount_exists(self, http_app):
        """Both /mcp and /mcp/premium should be mounted."""
        mount_paths = []
        for route in http_app.routes:
            if hasattr(route, "path"):
                mount_paths.append(route.path)
        # The mount paths show up as routes
        assert any("/mcp" in str(r.path) for r in http_app.routes if hasattr(r, "path"))

    def test_mcp_post_route_injects_request(self, http_app):
        """Request param should be injected by FastAPI, not treated as a query param."""
        for route in http_app.routes:
            if not hasattr(route, "path") or not hasattr(route, "dependant"):
                continue
            if route.path == "/mcp/" and getattr(route, "methods", None) and "POST" in route.methods:
                # request must be recognized as the request parameter
                assert route.dependant.request_param_name == "request", (
                    f"Expected request_param_name='request', got {route.dependant.request_param_name!r}. "
                    f"Query params: {[p.name for p in route.dependant.query_params]}"
                )
                # request must NOT appear as a query parameter
                assert not any(
                    p.name == "request" for p in route.dependant.query_params
                ), "request was misidentified as a query parameter (ForwardRef bug)"
                return
        pytest.fail("POST /mcp/ route not found")

    def test_mcp_post_does_not_422(self, http_app):
        """A valid JSON-RPC POST to /mcp/ must not return 422 (Unprocessable Entity).

        This guards the ForwardRef regression (StarletteRequest alias).
        A 10s signal-based timeout prevents silent hangs in environments
        where TestClient + FastMCP lifespan interaction blocks.
        """
        import signal

        def _timeout_handler(signum, frame):
            raise TimeoutError("test_mcp_post_does_not_422 timed out after 10s")

        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(10)
        try:
            from starlette.testclient import TestClient

            client = TestClient(http_app, raise_server_exceptions=False)
            resp = client.post(
                "/mcp/",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
                headers={
                    "Accept": "application/json",
                    "X-User-ID": "test-user",
                    "X-Request-Signature": "not-verified-in-this-test",
                },
            )
            # The request should reach the handler (not be rejected as 422 by FastAPI).
            # It may fail auth (401) or succeed (200), but never 422.
            assert resp.status_code != 422, (
                f"Got 422 Unprocessable Entity — FastAPI failed to resolve 'request' parameter. "
                f"Response: {resp.text}"
            )
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
