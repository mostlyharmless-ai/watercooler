"""Tests for _sanitize_response_text in watercooler_memory._utils."""

from unittest.mock import MagicMock, patch

import pytest

from watercooler_memory._utils import (
    _MAX_ERROR_TEXT_LENGTH,
    _sanitize_response_text,
)


class TestSanitizeResponseText:
    """Unit tests for _sanitize_response_text."""

    def test_short_text_passes_through(self):
        """Short safe text is returned unchanged."""
        assert _sanitize_response_text("Bad request") == "Bad request"

    def test_none_returns_empty(self):
        assert _sanitize_response_text(None) == ""

    def test_empty_string_returns_empty(self):
        assert _sanitize_response_text("") == ""

    def test_exactly_max_length_not_truncated(self):
        text = "x" * _MAX_ERROR_TEXT_LENGTH
        result = _sanitize_response_text(text)
        assert result == text
        assert "truncated" not in result

    def test_over_max_length_truncated(self):
        text = "x" * (_MAX_ERROR_TEXT_LENGTH + 100)
        result = _sanitize_response_text(text)
        assert result.endswith("...[truncated]")
        assert len(result) == _MAX_ERROR_TEXT_LENGTH + len("...[truncated]")

    def test_redacts_bearer_token(self):
        text = "Error: Bearer sk-abc123xyz is invalid"
        result = _sanitize_response_text(text)
        assert "sk-abc123xyz" not in result
        assert "[REDACTED]" in result

    def test_redacts_authorization_header(self):
        text = "Authorization: Basic dXNlcjpwYXNz"
        result = _sanitize_response_text(text)
        assert "dXNlcjpwYXNz" not in result
        assert "[REDACTED]" in result

    def test_redacts_openai_key(self):
        text = "Invalid key: sk-proj-abcdefghijklmnop"
        result = _sanitize_response_text(text)
        assert "sk-proj-abcdefghijklmnop" not in result
        assert "[REDACTED]" in result

    def test_redacts_github_pat(self):
        text = "token ghp_ABC123DEF456 expired"
        result = _sanitize_response_text(text)
        assert "ghp_ABC123DEF456" not in result
        assert "[REDACTED]" in result

    def test_redacts_github_oauth_token(self):
        text = "gho_secrettoken123 is not authorized"
        result = _sanitize_response_text(text)
        assert "gho_secrettoken123" not in result
        assert "[REDACTED]" in result

    def test_redacts_github_app_token(self):
        text = "invalid ghs_apptokensecret"
        result = _sanitize_response_text(text)
        assert "ghs_apptokensecret" not in result
        assert "[REDACTED]" in result

    def test_redacts_generic_key_prefix(self):
        text = "key-myapikey12345 not found"
        result = _sanitize_response_text(text)
        assert "key-myapikey12345" not in result
        assert "[REDACTED]" in result

    def test_combined_redaction_and_truncation(self):
        """Long text with secrets gets both redacted and truncated."""
        secret = "Bearer sk-supersecretkey123"
        text = secret + " " + "x" * 1000
        result = _sanitize_response_text(text)
        assert "sk-supersecretkey123" not in result
        assert "Bearer" not in result
        assert result.endswith("...[truncated]")

    def test_multiple_secrets_all_redacted(self):
        text = "key1=sk-abc123 key2=Bearer tok456 key3=ghp_xyz789"
        result = _sanitize_response_text(text)
        assert "sk-abc123" not in result
        assert "tok456" not in result
        assert "ghp_xyz789" not in result
        assert result.count("[REDACTED]") >= 3

    def test_non_string_coerced(self):
        """Non-string inputs are coerced via str()."""
        assert _sanitize_response_text(42) == "42"
        assert _sanitize_response_text(False) == "False"

    def test_case_insensitive_redaction(self):
        text = "BEARER mytoken123"
        result = _sanitize_response_text(text)
        assert "mytoken123" not in result
        assert "[REDACTED]" in result


class TestHttpPostWithRetrySanitization:
    """Integration: verify _http_post_with_retry uses _sanitize_response_text."""

    @pytest.fixture(autouse=True)
    def _ensure_httpx(self):
        """Skip if httpx is not installed."""
        pytest.importorskip("httpx")

    def test_http_error_message_is_sanitized(self):
        """HTTPStatusError response text goes through sanitization."""
        import httpx
        from watercooler_memory._utils import _http_post_with_retry

        secret_body = "Error: Bearer sk-leaked-secret-key-value"
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = secret_body
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            message="Unauthorized",
            request=MagicMock(),
            response=mock_response,
        )
        mock_response.json.side_effect = Exception("should not be called")

        with patch("httpx.Client") as MockClient:
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=ctx)
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.post.return_value = mock_response
            MockClient.return_value = ctx

            with pytest.raises(ValueError, match=r"HTTP 401"):
                _http_post_with_retry(
                    url="https://api.example.com/v1/test",
                    payload={},
                    headers={},
                    timeout=5.0,
                    max_retries=1,
                    error_cls=ValueError,
                )

    def test_http_error_secret_not_in_message(self):
        """Secrets in HTTPStatusError responses are redacted in the exception."""
        import httpx
        from watercooler_memory._utils import _http_post_with_retry

        secret_body = "Invalid API key: sk-supersecretapikey123456"
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = secret_body
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            message="Forbidden",
            request=MagicMock(),
            response=mock_response,
        )

        with patch("httpx.Client") as MockClient:
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=ctx)
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.post.return_value = mock_response
            MockClient.return_value = ctx

            with pytest.raises(ValueError) as exc_info:
                _http_post_with_retry(
                    url="https://api.example.com/v1/test",
                    payload={},
                    headers={},
                    timeout=5.0,
                    max_retries=1,
                    error_cls=ValueError,
                )
            assert "sk-supersecretapikey123456" not in str(exc_info.value)
            assert "[REDACTED]" in str(exc_info.value)

    def test_json_decode_error_body_is_sanitized(self):
        """ValueError (JSON decode) path uses sanitized body snippet."""
        from watercooler_memory._utils import _http_post_with_retry

        long_html = "<html>" + "x" * 1000 + "</html>"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = long_html
        mock_response.raise_for_status.return_value = None
        mock_response.json.side_effect = ValueError("Expecting value")

        with patch("httpx.Client") as MockClient:
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=ctx)
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.post.return_value = mock_response
            MockClient.return_value = ctx

            with pytest.raises(RuntimeError) as exc_info:
                _http_post_with_retry(
                    url="https://api.example.com/v1/test",
                    payload={},
                    headers={},
                    timeout=5.0,
                    max_retries=1,
                    error_cls=RuntimeError,
                )
            msg = str(exc_info.value)
            assert "truncated" in msg
            # Original 1000+ char body should not appear in full
            assert long_html not in msg
