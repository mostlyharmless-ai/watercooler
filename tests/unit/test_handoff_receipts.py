"""Tests for handoff-receipt error summarization and test isolation.

Covers ``summarize_remote_error`` (PR: fix/handoff-receipt-error-detail) —
receipts must carry the remote HTTP status/body detail that
``premium_client.call_tool_text`` attaches to ``remote_call_failed``
envelopes, instead of the bare tag observed in production receipts.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from watercooler_mcp.handoff_receipts import (
    DEFAULT_HANDOFF_RECEIPTS_FILE,
    _receipts_file,
    append_handoff_receipt,
    summarize_remote_error,
)


class TestSummarizeRemoteError:
    def test_plain_error_passthrough(self) -> None:
        assert summarize_remote_error({"error": "rejected"}) == "rejected"

    def test_status_field_fallback(self) -> None:
        assert summarize_remote_error({"status": "denied"}) == "denied"

    def test_empty_payload_defaults_rejected(self) -> None:
        assert summarize_remote_error({}) == "rejected"

    def test_message_detail_appended(self) -> None:
        out = summarize_remote_error(
            {"error": "remote_call_failed", "message": "Client error '403 Forbidden'"}
        )
        assert out == "remote_call_failed: Client error '403 Forbidden'"

    def test_remote_error_preferred_over_message_with_status(self) -> None:
        out = summarize_remote_error(
            {
                "error": "remote_call_failed",
                "message": "Client error '403 Forbidden' for url ...",
                "remote_error": (
                    "repo_claim_mismatch: X-Repo 'org/b' not in authorised set"
                ),
                "status_code": 403,
            }
        )
        assert out == (
            "remote_call_failed: http=403: "
            "repo_claim_mismatch: X-Repo 'org/b' not in authorised set"
        )

    def test_detail_identical_to_error_not_duplicated(self) -> None:
        out = summarize_remote_error(
            {"error": "scope_resolution_failed", "message": "scope_resolution_failed"}
        )
        assert out == "scope_resolution_failed"

    def test_detail_truncated_to_bound(self) -> None:
        out = summarize_remote_error(
            {"error": "remote_call_failed", "remote_error": "x" * 2000}
        )
        # "remote_call_failed: " prefix + 500-char detail cap
        assert out == "remote_call_failed: " + "x" * 500


class TestReceiptsIsolation:
    def test_autouse_fixture_repoints_receipts_file(self) -> None:
        # The autouse conftest fixture must keep every test away from the
        # operator's live receipts file.
        assert _receipts_file() != DEFAULT_HANDOFF_RECEIPTS_FILE

    def test_append_lands_in_isolated_file(self) -> None:
        append_handoff_receipt(
            backend="graphiti", stage="submit_failed", entry_id="E1", error="boom"
        )
        lines = _receipts_file().read_text().strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["error"] == "boom"


class TestReceiptCarriesRemoteDetail:
    def test_t1_upsert_rejection_receipt_includes_remote_body(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from watercooler_mcp import t1_hybrid

        receipts = tmp_path / "receipts.jsonl"
        monkeypatch.setenv("WATERCOOLER_HANDOFF_RECEIPTS_FILE", str(receipts))
        premium = MagicMock()
        premium.call_tool_text = AsyncMock(
            return_value=json.dumps(
                {
                    "success": False,
                    "error": "remote_call_failed",
                    "status_code": 403,
                    "remote_error": "repo_claim_mismatch: connect the repo",
                }
            )
        )

        ok = t1_hybrid._submit_t1_upsert(
            premium=premium,
            threads_dir=tmp_path / "repo-threads",
            entry_id="E1",
            topic="t",
            embedding=[0.1],
        )
        assert ok is False
        record = json.loads(receipts.read_text().strip().splitlines()[-1])
        assert record["stage"] == "submit_failed"
        assert "http=403" in record["error"]
        assert "repo_claim_mismatch: connect the repo" in record["error"]
