"""Unit tests for the Stop hook (authority-ladder Phase 1c)."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest

from watercooler import stop_hook
from watercooler_mcp.daemons.findings_source import FindingsSource


def _patch_single_source(monkeypatch, path: Path, daemon_name: str = "decision_extractor"):
    monkeypatch.setattr(
        stop_hook,
        "_findings_sources",
        lambda: [FindingsSource(daemon_name=daemon_name, findings_path=path)],
    )


@pytest.fixture
def findings_path(tmp_path, monkeypatch):
    path = tmp_path / "findings.jsonl"
    _patch_single_source(monkeypatch, path)
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
    _patch_single_source(monkeypatch, tmp_path / "nonexistent.jsonl")
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
    _patch_single_source(monkeypatch, tmp_path / "missing.jsonl")
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
    _patch_single_source(monkeypatch, tmp_path / "missing.jsonl")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert stop_hook.main() == 0
    assert capsys.readouterr().err == ""


def _write_stance_advisory(
    path: Path,
    *,
    role: str = "critic",
    level: int = 1,
    created_at: float,
    project_salience=(),
    advisory_only: bool = True,
    repo: str = "",
) -> None:
    rec = {
        "finding_id": f"stance-{role}-{created_at}",
        "daemon_name": "decision_stance",
        "severity": "info",
        "category": stop_hook.CAT_STANCE_ADVISORY,
        "topic": f"stance:{role}",
        "entry_id": "",
        "message": f"{role.title()} stance",
        "details": {
            "advisory": {
                "schema_version": 2,
                "role": role,
                "level": level,
                "summary": f"{role.title()} L{level} summary",
                "project_salience": list(project_salience),
                "advisory_only": advisory_only,
            }
        },
        "created_at": created_at,
        "acknowledged": False,
        "repo": repo,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


class TestMultiSourceAndStanceDelivery:
    """Phase 3: generalized findings source + stance_advisory formatting."""

    def test_loads_from_multiple_sources(self, tmp_path, monkeypatch):
        extractor_path = tmp_path / "extractor.jsonl"
        stance_path = tmp_path / "stance.jsonl"
        monkeypatch.setattr(
            stop_hook,
            "_findings_sources",
            lambda: [
                FindingsSource(
                    daemon_name="decision_extractor", findings_path=extractor_path
                ),
                FindingsSource(
                    daemon_name="decision_stance", findings_path=stance_path
                ),
            ],
        )
        _write_finding(extractor_path, finding_id="cand", created_at=100.0)
        _write_stance_advisory(stance_path, created_at=100.0)

        found = stop_hook._load_findings_since(0.0)
        categories = {f["category"] for f in found}
        assert categories == {stop_hook.CAT_CANDIDATE_NOTE, stop_hook.CAT_STANCE_ADVISORY}

    def test_elevated_stance_surfaced_l0_tombstone_not(self):
        elevated = {
            "category": stop_hook.CAT_STANCE_ADVISORY,
            "details": {"advisory": {"role": "critic", "level": 1, "summary": "x"}},
        }
        tombstone = {
            "category": stop_hook.CAT_STANCE_ADVISORY,
            "details": {"advisory": {"role": "critic", "level": 0, "summary": "cleared"}},
        }
        assert stop_hook._elevated_stance([elevated, tombstone]) == [elevated]

    def test_elevated_stance_skips_malformed_advisory_records(self):
        elevated = {
            "category": stop_hook.CAT_STANCE_ADVISORY,
            "details": {"advisory": {"role": "critic", "level": 1, "summary": "x"}},
        }
        malformed = [
            {"category": stop_hook.CAT_STANCE_ADVISORY, "details": {"advisory": None}},
            {
                "category": stop_hook.CAT_STANCE_ADVISORY,
                "details": {"advisory": {"role": "critic", "level": "1"}},
            },
            {"category": stop_hook.CAT_STANCE_ADVISORY, "details": []},
        ]
        assert stop_hook._elevated_stance(malformed + [elevated]) == [elevated]

    def test_malformed_stance_does_not_suppress_candidate_summary(
        self, monkeypatch, capsys, tmp_path
    ):
        """A corrupt stance record should be skipped, not abort the whole hook."""
        findings_path = tmp_path / "findings.jsonl"
        _patch_single_source(monkeypatch, findings_path, daemon_name="decision_stance")
        _write_finding(
            findings_path,
            finding_id="stance-bad",
            category=stop_hook.CAT_STANCE_ADVISORY,
            created_at=time.time(),
            details={"advisory": None},
        )
        _write_finding(
            findings_path,
            finding_id="candidate-good",
            category=stop_hook.CAT_CANDIDATE_NOTE,
            created_at=time.time(),
            details={
                "entry_id": "01CAND",
                "source_entry_id": "01SOURCE",
                "confidence": 4,
                "rejection_reason": "g4_rationale",
            },
        )
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(json.dumps({"transcript_path": ""}))
        )
        assert stop_hook.main() == 0
        err = capsys.readouterr().err
        assert "candidate Note" in err
        assert "01CAND" in err

    def test_main_does_not_print_for_all_tombstone_findings(
        self, monkeypatch, capsys, tmp_path
    ):
        """A non-empty findings list that's entirely L0 tombstones must not
        produce an empty-looking '0 candidate Note(s)' print."""
        stance_path = tmp_path / "stance.jsonl"
        _patch_single_source(monkeypatch, stance_path, daemon_name="decision_stance")
        _write_stance_advisory(stance_path, level=0, created_at=time.time())
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(json.dumps({"transcript_path": ""}))
        )
        assert stop_hook.main() == 0
        assert capsys.readouterr().err == ""

    def test_format_summary_includes_elevated_advisory_and_salience(self):
        findings = [
            {
                "category": stop_hook.CAT_STANCE_ADVISORY,
                "details": {
                    "advisory": {
                        "role": "critic",
                        "level": 2,
                        "summary": "Risk signals elevated",
                        "project_salience": ["watch for hidden authority expansion"],
                        "advisory_only": True,
                    }
                },
            },
        ]
        out = stop_hook._format_summary(findings)
        assert "elevated stance advisory" in out
        assert "[critic] L2" in out
        assert "Risk signals elevated" in out
        assert "advisory-only" in out
        assert "watch for hidden authority expansion" in out

    def test_format_summary_strips_control_chars_from_bullet_and_summary(self):
        """Bullet text ultimately originates from human-promoted Lesson
        notes but the loader permits hand-authored bullets with no
        control-character validation — the Stop hook must not replay raw
        escape sequences to the terminal."""
        findings = [
            {
                "category": stop_hook.CAT_STANCE_ADVISORY,
                "details": {
                    "advisory": {
                        "role": "critic",
                        "level": 1,
                        "summary": "elevated\x1b[31m danger\x1b[0m",
                        "project_salience": ["watch\x1b[2J for X\x07"],
                        "advisory_only": True,
                    }
                },
            },
        ]
        out = stop_hook._format_summary(findings)
        assert "\x1b" not in out
        assert "\x07" not in out
        assert "[31m" not in out and "[0m" not in out and "[2J" not in out
        assert "watch" in out and "for X" in out
        assert "elevated danger" in out

    def test_format_summary_strips_escapes_from_role(self):
        """`role` is interpolated into the terminal line too — it must be
        stripped, not just `summary` (regression: the first hardening pass
        left `role`/`level` raw, carrying a portable \\x1b[ CSI straight to
        the terminal)."""
        findings = [
            {
                "category": stop_hook.CAT_STANCE_ADVISORY,
                "details": {
                    "advisory": {
                        "role": "crit\x1b[2Jic\x1b[31m",
                        "level": 2,
                        "summary": "ok",
                        "advisory_only": True,
                    }
                },
            },
        ]
        out = stop_hook._format_summary(findings)
        assert "\x1b" not in out and "[2J" not in out and "[31m" not in out
        assert "critic" in out  # legitimate role text preserved

    def test_format_summary_strips_single_byte_c1_controls(self):
        """The stripper must cover the C1 range \\x80-\\x9f (single-byte CSI
        `\\x9b`, OSC `\\x9d`, DCS `\\x90...\\x9c`) — UTF-8 terminals decode
        these from \\xc2\\x9x."""
        findings = [
            {
                "category": stop_hook.CAT_STANCE_ADVISORY,
                "details": {
                    "advisory": {
                        "role": "critic",
                        "level": 1,
                        "summary": "a\x9b31mRED b\x9d0;t\x07 c",
                        "project_salience": ["x\x90dcs\x9c y"],
                        "advisory_only": True,
                    }
                },
            },
        ]
        out = stop_hook._format_summary(findings)
        assert not any(0x7F <= ord(c) <= 0x9F for c in out)
        assert "31m" not in out  # single-byte CSI body removed with introducer

    def test_format_summary_strips_escapes_from_candidate_and_rate_cap(self):
        """The candidate-Note / rate-cap print loop interpolates topic,
        entry_id, source, reason raw — these must be stripped too."""
        findings = [
            {
                "category": stop_hook.CAT_CANDIDATE_NOTE,
                "topic": "proj\x1b[2Kevil",
                "details": {
                    "entry_id": "e\x1b[31m1",
                    "source_entry_id": "s\x071",
                    "rejection_reason": "why\x9b2J",
                },
            },
            {
                "category": stop_hook.CAT_RATE_CAP,
                "topic": "cap\x1b[2Jtopic",
            },
        ]
        out = stop_hook._format_summary(findings)
        assert "\x1b" not in out and "\x07" not in out
        assert not any(0x7F <= ord(c) <= 0x9F for c in out)
        assert "[2K" not in out and "[31m" not in out and "[2J" not in out

    def test_format_summary_excludes_l0_tombstone(self):
        findings = [
            {
                "category": stop_hook.CAT_STANCE_ADVISORY,
                "details": {
                    "advisory": {"role": "critic", "level": 0, "summary": "cleared"}
                },
            },
        ]
        out = stop_hook._format_summary(findings)
        assert "elevated stance advisory" not in out
        assert "cleared" not in out

    def test_main_prints_elevated_stance_advisory(
        self, monkeypatch, capsys, tmp_path
    ):
        stance_path = tmp_path / "stance.jsonl"
        _patch_single_source(monkeypatch, stance_path, daemon_name="decision_stance")
        _write_stance_advisory(
            stance_path,
            role="planner",
            level=1,
            created_at=time.time(),
            project_salience=["watch for stalled loops"],
        )
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(json.dumps({"transcript_path": ""}))
        )
        assert stop_hook.main() == 0
        err = capsys.readouterr().err
        assert "[planner] L1" in err
        assert "watch for stalled loops" in err

    def test_repo_scoping_applies_to_stance_findings(self, tmp_path, monkeypatch):
        stance_path = tmp_path / "stance.jsonl"
        _patch_single_source(monkeypatch, stance_path, daemon_name="decision_stance")
        other_repo = tmp_path / "other-repo"
        this_repo = tmp_path / "this-repo"
        other_repo.mkdir()
        this_repo.mkdir()
        _write_stance_advisory(stance_path, created_at=100.0, repo=str(other_repo))
        found = stop_hook._load_findings_since(0.0, session_repo=str(this_repo))
        assert found == []


class TestFindingsSourceFallback:
    def test_falls_back_when_daemons_package_unavailable(self, monkeypatch, tmp_path):
        """If the resolver import fails (and no sidecar exists), the hook
        degrades to the historical decision_extractor-only path rather than
        surfacing nothing."""
        import builtins

        # Ensure the sidecar fast-path is not taken (no file on disk).
        monkeypatch.setattr(
            stop_hook, "_ACTIVE_STANCE_PRODUCER_SIDECAR", tmp_path / "absent"
        )
        real_import = builtins.__import__

        def _blocked_import(name, *args, **kwargs):
            if name == "watercooler_mcp.daemons.findings_source":
                raise ImportError("simulated missing optional dependency")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked_import)
        sources = stop_hook._findings_sources()
        assert len(sources) == 1
        assert sources[0].daemon_name == "decision_extractor"

    def test_fallback_logs_debug_not_silent(self, monkeypatch, caplog, tmp_path):
        """The degradation must leave a trace (debug log), not vanish
        entirely — regression for the strict-namespace silent-disable gap."""
        import builtins
        import logging

        monkeypatch.setattr(
            stop_hook, "_ACTIVE_STANCE_PRODUCER_SIDECAR", tmp_path / "absent"
        )
        real_import = builtins.__import__

        def _blocked_import(name, *args, **kwargs):
            if name == "watercooler_mcp.daemons.findings_source":
                raise ImportError("simulated missing optional dependency")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked_import)
        with caplog.at_level(logging.DEBUG, logger="watercooler.stop_hook"):
            stop_hook._findings_sources()
        assert any("falling back" in r.message for r in caplog.records)


class TestSidecarFastPath:
    """The Stop hook reads the active-stance-producer sidecar directly to
    avoid importing the daemons package + building config on every turn."""

    def _no_import(self, monkeypatch):
        """Fail any daemons-package import so a test proves the fast path did
        NOT fall through to full resolution."""
        import builtins

        real_import = builtins.__import__

        def _blocked(name, *args, **kwargs):
            if name.startswith("watercooler_mcp.daemons"):
                raise AssertionError(f"fast path must not import {name}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked)

    def test_sidecar_with_producer_appends_stance_source(self, monkeypatch, tmp_path):
        sidecar = tmp_path / "active_stance_producer"
        sidecar.write_text("decision_stance", encoding="utf-8")
        monkeypatch.setattr(stop_hook, "_ACTIVE_STANCE_PRODUCER_SIDECAR", sidecar)
        monkeypatch.setattr(stop_hook, "_DAEMONS_DIR", tmp_path)
        self._no_import(monkeypatch)
        names = [s.daemon_name for s in stop_hook._findings_sources()]
        assert names == ["decision_extractor", "decision_stance"]

    def test_sidecar_empty_means_no_stance_source(self, monkeypatch, tmp_path):
        sidecar = tmp_path / "active_stance_producer"
        sidecar.write_text("", encoding="utf-8")
        monkeypatch.setattr(stop_hook, "_ACTIVE_STANCE_PRODUCER_SIDECAR", sidecar)
        monkeypatch.setattr(stop_hook, "_DAEMONS_DIR", tmp_path)
        self._no_import(monkeypatch)
        names = [s.daemon_name for s in stop_hook._findings_sources()]
        assert names == ["decision_extractor"]

    def test_sidecar_unknown_value_ignored(self, monkeypatch, tmp_path):
        """A sidecar value outside the known-producer allowlist is ignored
        (defense against building an out-of-tree findings path)."""
        sidecar = tmp_path / "active_stance_producer"
        sidecar.write_text("../../etc/passwd", encoding="utf-8")
        monkeypatch.setattr(stop_hook, "_ACTIVE_STANCE_PRODUCER_SIDECAR", sidecar)
        monkeypatch.setattr(stop_hook, "_DAEMONS_DIR", tmp_path)
        self._no_import(monkeypatch)
        names = [s.daemon_name for s in stop_hook._findings_sources()]
        assert names == ["decision_extractor"]
