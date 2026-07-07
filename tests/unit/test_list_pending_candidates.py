"""Tests for ``watercooler_list_pending_candidates`` (C1,
thread candidate-research-backend-support).

Contracts under test:
- Pending = ``Candidate-Status: needs_human_confirmation`` MINUS a terminal
  disposition. Terminal matching must cover BOTH reference markers in the
  wild: ``Disposition-Target:`` (the MCP promote path,
  ``format_candidate_disposition_body``) and ``Candidate-Entry:`` (the
  dashboard judgment route in watercooler-site) — plus the #886
  ``Promoted-From:`` promoted-entry guard.
- Non-terminal dispositions (keep_exploring, reframe) leave a candidate
  pending by design (§5.4).
- Local mode reads the baseline graph; hosted mode goes through
  ``hosted_ops`` only, honors the topic fast path, and reports
  ``skipped_topics``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from ulid import ULID

from watercooler import commands_graph
from watercooler.baseline_graph.writer import init_thread_in_graph
from watercooler.promotion import candidate_has_terminal_disposition
from watercooler_mcp.errors import ValidationError
from watercooler_mcp.tools import decisions as decisions_mod
from watercooler_mcp.tools.decisions import _list_pending_candidates_impl
from watercooler_mcp.validation import HOSTED_MODE_SENTINEL

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

CAND_A = "01CANDA000000000000000000A"
CAND_B = "01CANDB000000000000000000B"
CAND_C = "01CANDC000000000000000000C"


def candidate_body(source: str | None = None, confidence: int = 4) -> str:
    lines = [
        "Spec: decision-extractor",
        "Candidate-Type: Decision",
        "Candidate-Status: needs_human_confirmation",
        "Surface-Kind: decision",
        f"Confidence: {confidence}/5",
    ]
    if source:
        lines.append(f"Source-Entry: {source}")
    lines += ["", "## Candidate Decision", "Adopt X."]
    return "\n".join(lines)


def candidate_node(entry_id: str, *, index: int = 1, ts: str = "2026-07-01T00:00:00Z",
                   body: str | None = None) -> dict:
    return {
        "id": f"entry:{entry_id}",
        "entry_type": "Note",
        "index": index,
        "title": f"Candidate {entry_id[:6]}",
        "body": body if body is not None else candidate_body(),
        "timestamp": ts,
        "agent": "daemon",
    }


def disposition_node(kind: str, marker: str, target: str, *, index: int = 9) -> dict:
    return {
        "id": f"entry:{str(ULID())}",
        "entry_type": "Note",
        "index": index,
        "title": f"{kind} ({target[:6]})",
        "body": f"Spec: candidate-disposition\nCandidateDisposition: {kind}\n{marker}: {target}\n\nrecorded.",
        "timestamp": "2026-07-02T00:00:00Z",
        "agent": "caleb",
    }


# ---------------------------------------------------------------------------
# candidate_has_terminal_disposition — the predicate both modes share
# ---------------------------------------------------------------------------


class TestTerminalDispositionPredicate:
    def test_disposition_target_promoted_is_terminal(self):
        entries = [disposition_node("promoted", "Disposition-Target", CAND_A)]
        assert candidate_has_terminal_disposition(CAND_A, entries) is True

    def test_dashboard_candidate_entry_rejected_is_terminal(self):
        # The dashboard judgment route writes `Candidate-Entry:`, not
        # `Disposition-Target:` — missing this marker would resurrect
        # dashboard-rejected candidates as pending.
        entries = [disposition_node("rejected", "Candidate-Entry", CAND_A)]
        assert candidate_has_terminal_disposition(CAND_A, entries) is True

    def test_keep_exploring_and_reframe_are_not_terminal(self):
        entries = [
            disposition_node("keep_exploring", "Candidate-Entry", CAND_A),
            disposition_node("reframe", "Candidate-Entry", CAND_A),
        ]
        assert candidate_has_terminal_disposition(CAND_A, entries) is False

    def test_other_candidates_disposition_does_not_match(self):
        entries = [disposition_node("promoted", "Disposition-Target", CAND_B)]
        assert candidate_has_terminal_disposition(CAND_A, entries) is False

    def test_promoted_from_stamped_entry_is_terminal(self):
        # #886: the promotion committed but the disposition Note never landed.
        promoted = {
            "id": f"entry:{str(ULID())}",
            "entry_type": "Decision",
            "body": (
                "Spec: decision-extractor-promoted\n"
                f"Promoted-From: {CAND_A}\n"
                "Authority-Basis: human_promoted\n\ndecision text"
            ),
        }
        assert candidate_has_terminal_disposition(CAND_A, [promoted]) is True


# ---------------------------------------------------------------------------
# Hosted mode
# ---------------------------------------------------------------------------

ENTRIES_BY_TOPIC: dict[str, list[dict]] = {
    # Open candidate (with a source ref) — must be listed.
    "alpha": [candidate_node(CAND_A, body=candidate_body(source=CAND_C))],
    # Candidate rejected via the DASHBOARD marker — must not be listed.
    "beta": [
        candidate_node(CAND_B, ts="2026-06-01T00:00:00Z"),
        disposition_node("rejected", "Candidate-Entry", CAND_B),
    ],
    # No candidates at all.
    "gamma": [
        {
            "id": f"entry:{str(ULID())}",
            "entry_type": "Note",
            "index": 1,
            "title": "plain note",
            "body": "Spec: general\nnothing here",
            "timestamp": "2026-07-03T00:00:00Z",
            "agent": "jay",
        }
    ],
}


def _hosted_context() -> MagicMock:
    ctx = MagicMock()
    ctx.threads_dir = HOSTED_MODE_SENTINEL
    ctx.code_root = None
    ctx.code_repo = "org/demo-threads"
    ctx.code_branch = "main"
    return ctx


def _fake_load_all_entries_hosted(topics=None, max_workers=10):
    if topics is not None:
        return (None, {t: ENTRIES_BY_TOPIC[t] for t in topics if t in ENTRIES_BY_TOPIC})
    return (None, dict(ENTRIES_BY_TOPIC))


def _fake_list_topic_dirs_hosted():
    # `delta` exists as a directory but its entries fail to load —
    # must surface via skipped_topics, never silently.
    return (None, sorted([*ENTRIES_BY_TOPIC.keys(), "delta"]))


class TestListPendingCandidatesHosted:
    def _run(self, **kwargs):
        with (
            patch.object(
                decisions_mod.validation,
                "_require_context",
                return_value=(None, _hosted_context()),
            ),
            patch(
                "watercooler_mcp.hosted_ops.list_topic_dirs_hosted",
                side_effect=_fake_list_topic_dirs_hosted,
            ),
            patch(
                "watercooler_mcp.hosted_ops.load_all_entries_hosted",
                side_effect=_fake_load_all_entries_hosted,
            ),
        ):
            result = _list_pending_candidates_impl(ctx=MagicMock(), **kwargs)
        return json.loads(result.content[0].text)

    def test_lists_open_candidates_excluding_dashboard_rejected(self):
        payload = self._run(code_path="")
        assert payload["schema_version"] == 1
        assert payload["total"] == 1
        ids = [c["entry_id"] for c in payload["candidates"]]
        assert ids == [CAND_A]
        cand = payload["candidates"][0]
        assert cand["topic"] == "alpha"
        assert cand["candidate_type"] == "Decision"
        assert cand["confidence"] == 4
        assert cand["source_entry_id"] == CAND_C

    def test_reports_skipped_topics(self):
        payload = self._run(code_path="")
        assert payload["skipped_topics"] == ["delta"]

    def test_topic_filter_loads_single_topic(self):
        calls: list = []

        def _tracking_load(topics=None, max_workers=10):
            calls.append(topics)
            return _fake_load_all_entries_hosted(topics=topics)

        with (
            patch.object(
                decisions_mod.validation,
                "_require_context",
                return_value=(None, _hosted_context()),
            ),
            patch(
                "watercooler_mcp.hosted_ops.load_all_entries_hosted",
                side_effect=_tracking_load,
            ),
        ):
            result = _list_pending_candidates_impl(
                ctx=MagicMock(), topic="alpha", code_path=""
            )
        payload = json.loads(result.content[0].text)
        # Fast path: exactly the requested topic, no repo-wide fan-out and no
        # directory enumeration.
        assert calls == [["alpha"]]
        assert payload["total"] == 1

    def test_limit_validation(self):
        with pytest.raises(ValidationError):
            _list_pending_candidates_impl(ctx=MagicMock(), limit=0, code_path="")


# ---------------------------------------------------------------------------
# Local mode (real baseline graph on tmp_path)
# ---------------------------------------------------------------------------


class TestListPendingCandidatesLocal:
    def _seed(self, tmp_path: Path) -> Path:
        td = tmp_path / ".watercooler"
        td.mkdir()
        cand_open = str(ULID())
        cand_rejected = str(ULID())

        init_thread_in_graph(td, "alpha", title="Alpha", status="OPEN", ball="x")
        commands_graph.append_entry(
            "alpha", threads_dir=td, agent="daemon", role="scribe",
            title="Open candidate", entry_type="Note",
            body=candidate_body(confidence=5), entry_id=cand_open,
        )

        init_thread_in_graph(td, "beta", title="Beta", status="OPEN", ball="x")
        commands_graph.append_entry(
            "beta", threads_dir=td, agent="daemon", role="scribe",
            title="Rejected candidate", entry_type="Note",
            body=candidate_body(), entry_id=cand_rejected,
        )
        commands_graph.append_entry(
            "beta", threads_dir=td, agent="caleb", role="critic",
            title="Rejected", entry_type="Note",
            body=(
                "Spec: candidate-disposition\n"
                "CandidateDisposition: rejected\n"
                f"Candidate-Entry: {cand_rejected}\n\nframing rejected."
            ),
            entry_id=str(ULID()),
        )
        self.cand_open = cand_open
        return td

    def test_local_scan_lists_only_open(self, tmp_path):
        td = self._seed(tmp_path)
        ctx_obj = MagicMock()
        ctx_obj.threads_dir = td
        with patch.object(
            decisions_mod.validation,
            "_require_context",
            return_value=(None, ctx_obj),
        ), patch.object(decisions_mod, "is_hosted_context", return_value=False):
            result = _list_pending_candidates_impl(ctx=MagicMock(), code_path=str(tmp_path))
        payload = json.loads(result.content[0].text)
        assert payload["total"] == 1
        assert payload["candidates"][0]["entry_id"] == self.cand_open
        assert payload["candidates"][0]["topic"] == "alpha"
        assert payload["candidates"][0]["confidence"] == 5
        # Local calls omit skipped_topics (hosted-only signal).
        assert "skipped_topics" not in payload

    def test_local_topic_filter(self, tmp_path):
        td = self._seed(tmp_path)
        ctx_obj = MagicMock()
        ctx_obj.threads_dir = td
        with patch.object(
            decisions_mod.validation,
            "_require_context",
            return_value=(None, ctx_obj),
        ), patch.object(decisions_mod, "is_hosted_context", return_value=False):
            result = _list_pending_candidates_impl(
                ctx=MagicMock(), topic="beta", code_path=str(tmp_path)
            )
        payload = json.loads(result.content[0].text)
        assert payload["total"] == 0


# ---------------------------------------------------------------------------
# Registration / capability coverage
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_tool_matrix_has_entry(self):
        from watercooler_mcp.capabilities import TOOL_MATRIX

        spec = TOOL_MATRIX["watercooler_list_pending_candidates"]
        assert spec.capability == "baseline_search"
        assert spec.authority == "L1"


class TestSurfaceExposure:
    """Surface contract (PR #1074 review): a baseline_search/L1 dashboard feed
    belongs on the thread-capable surfaces, and must NOT ride along when
    hosted_premium mounts register_decisions_tools as the special case for
    watercooler_list_decisions' remote memory-query leg."""

    @staticmethod
    def _tool_names(surface: str) -> set[str]:
        import asyncio

        from watercooler_mcp.server_factory import build_mcp_server
        from watercooler_mcp.tool_runtime import ToolRuntime

        mcp = build_mcp_server(ToolRuntime(surface=surface))
        return {t.name for t in asyncio.run(mcp.list_tools())}

    @pytest.mark.parametrize("surface", ["local_full", "local_hybrid", "hosted_full"])
    def test_present_on_thread_capable_surfaces(self, surface):
        assert "watercooler_list_pending_candidates" in self._tool_names(surface)

    def test_absent_from_hosted_premium(self):
        names = self._tool_names("hosted_premium")
        assert "watercooler_list_pending_candidates" not in names
        # The special case that mounts this registrar on premium stays intact.
        assert "watercooler_list_decisions" in names
