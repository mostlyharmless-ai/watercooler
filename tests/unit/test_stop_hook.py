"""Unit tests for the Stop hook (authority-ladder Phase 1c)."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest

from watercooler import stop_hook


@pytest.fixture
def findings_path(tmp_path, monkeypatch):
    path = tmp_path / "findings.jsonl"
    monkeypatch.setattr(stop_hook, "FINDINGS_PATH", path)
    return path


def _write_finding(path: Path, **fields) -> None:
    rec = {
        "finding_id": fields.get("finding_id", "F"),
        "daemon_name": "decision_extractor",
        "severity": "info",
        "category": fields.get("category", stop_hook.CAT_CANDIDATE_NOTE),
        "topic": fields.get("topic", "thread-x"),
        "entry_id": fields.get("entry_id", "01SOURCE"),
        "message": "msg",
        "details": fields.get("details", {}),
        "created_at": fields["created_at"],
        "acknowledged": False,
        "repo": fields.get("repo", ""),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def test_no_findings_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(stop_hook, "FINDINGS_PATH", tmp_path / "nonexistent.jsonl")
    assert stop_hook._load_findings_since(0.0) == []


def test_filters_by_start_epoch(findings_path):
    _write_finding(findings_path, finding_id="A", created_at=100.0)
    _write_finding(findings_path, finding_id="B", created_at=200.0)
    found = stop_hook._load_findings_since(150.0)
    assert [f["finding_id"] for f in found] == ["B"]


def test_filters_by_category(findings_path):
    _write_finding(
        findings_path,
        finding_id="cand",
        category=stop_hook.CAT_CANDIDATE_NOTE,
        created_at=100.0,
    )
    _write_finding(
        findings_path,
        finding_id="reject",
        category="extraction_rejected",
        created_at=100.0,
    )
    _write_finding(
        findings_path,
        finding_id="cap",
        category=stop_hook.CAT_RATE_CAP,
        created_at=100.0,
    )
    ids = sorted(f["finding_id"] for f in stop_hook._load_findings_since(0.0))
    assert ids == ["cand", "cap"]


def test_scopes_findings_to_active_repo(findings_path, tmp_path):
    """Findings tagged with a different repo are excluded; matching and
    untagged (legacy) findings are kept."""
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    _write_finding(findings_path, finding_id="a", created_at=100.0, repo=str(repo_a))
    _write_finding(findings_path, finding_id="b", created_at=100.0, repo=str(repo_b))
    _write_finding(findings_path, finding_id="legacy", created_at=100.0, repo="")
    ids = sorted(
        f["finding_id"]
        for f in stop_hook._load_findings_since(0.0, session_repo=str(repo_a))
    )
    assert ids == ["a", "legacy"]


def test_no_session_repo_includes_all(findings_path, tmp_path):
    """An empty session repo (no cwd in payload) disables scoping."""
    _write_finding(
        findings_path, finding_id="a", created_at=100.0, repo=str(tmp_path / "x")
    )
    _write_finding(
        findings_path, finding_id="b", created_at=100.0, repo=str(tmp_path / "y")
    )
    found = stop_hook._load_findings_since(0.0, session_repo="")
    assert len(found) == 2


def test_repo_matches_subdirectory_invocation(tmp_path):
    """A session started from a subdirectory still matches its repo root."""
    repo = tmp_path / "repo"
    subdir = repo / "src" / "pkg"
    subdir.mkdir(parents=True)
    assert stop_hook._repo_matches(str(subdir), str(repo)) is True
    assert stop_hook._repo_matches(str(subdir), str(tmp_path / "other")) is False


def test_malformed_lines_skipped(findings_path):
    findings_path.write_text(
        "not json\n"
        + json.dumps(
            {
                "category": stop_hook.CAT_CANDIDATE_NOTE,
                "created_at": 100.0,
                "details": {},
            }
        )
        + "\n"
    )
    assert len(stop_hook._load_findings_since(0.0)) == 1


def test_session_start_from_transcript_timestamp(tmp_path):
    """Real Claude Code transcripts open with untimestamped metadata records;
    the scan must skip them and use the first timestamped record."""
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps({"type": "permission-mode", "permissionMode": "default"})
        + "\n"
        + json.dumps({"type": "file-history-snapshot", "snapshot": {}})
        + "\n"
        + json.dumps({"type": "user", "timestamp": "2026-05-19T18:30:00.000Z"})
        + "\n"
        + json.dumps({"type": "user", "timestamp": "2026-05-19T18:31:00.000Z"})
        + "\n"
    )
    start = stop_hook._session_start_epoch(str(transcript))
    from datetime import datetime, timezone

    expected = datetime(2026, 5, 19, 18, 30, tzinfo=timezone.utc).timestamp()
    assert start == pytest.approx(expected)


def test_session_start_no_timestamped_records_uses_lookback(tmp_path):
    """A transcript with only untimestamped records falls back to the
    lookback window rather than the file mtime."""
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps({"type": "permission-mode"})
        + "\n"
        + json.dumps({"type": "file-history-snapshot"})
        + "\n"
    )
    start = stop_hook._session_start_epoch(str(transcript))
    assert start == pytest.approx(time.time() - stop_hook.FALLBACK_LOOKBACK_S, abs=5)


def test_session_start_missing_transcript_uses_lookback():
    start = stop_hook._session_start_epoch("/nonexistent/path.jsonl")
    assert start <= time.time()
    assert start >= time.time() - stop_hook.FALLBACK_LOOKBACK_S - 5


def test_format_summary_includes_counts_and_entries():
    findings = [
        {
            "category": stop_hook.CAT_CANDIDATE_NOTE,
            "topic": "topic-a",
            "details": {
                "entry_id": "01CAND",
                "source_entry_id": "01SRC",
                "confidence": 5,
                "rejection_reason": "g4_rationale",
            },
        },
        {"category": stop_hook.CAT_RATE_CAP, "topic": "topic-b"},
    ]
    out = stop_hook._format_summary(findings)
    assert "1 candidate Note(s)" in out
    assert "1 rate-cap suppression(s)" in out
    assert "01CAND" in out
    assert "01SRC" in out
    assert "topic-a" in out
    assert "topic-b" in out


def test_main_exits_zero_with_no_findings(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(stop_hook, "FINDINGS_PATH", tmp_path / "missing.jsonl")
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"transcript_path": ""})),
    )
    assert stop_hook.main() == 0
    captured = capsys.readouterr()
    assert captured.err == ""


def test_main_prints_summary_on_findings(monkeypatch, capsys, findings_path):
    _write_finding(
        findings_path,
        finding_id="A",
        created_at=time.time(),
        details={
            "entry_id": "01CAND",
            "source_entry_id": "01SRC",
            "confidence": 4,
            "rejection_reason": "g4_rationale",
        },
    )
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"transcript_path": ""})),
    )
    assert stop_hook.main() == 0
    captured = capsys.readouterr()
    assert "candidate Note" in captured.err
    assert "01CAND" in captured.err


def test_main_swallows_unexpected_errors(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(stop_hook, "_read_payload", boom)
    assert stop_hook.main() == 0


def test_main_handles_empty_stdin(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(stop_hook, "FINDINGS_PATH", tmp_path / "missing.jsonl")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert stop_hook.main() == 0
    assert capsys.readouterr().err == ""
