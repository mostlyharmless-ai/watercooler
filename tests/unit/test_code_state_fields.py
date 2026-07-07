"""Code-state provenance fields on entry nodes (C3,
thread candidate-research-backend-support).

``code_repo`` and ``code_commit`` — previously only in orphan-branch
commit-message footers — are now stamped onto entry nodes at write time,
next to the existing ``code_branch``, so consumers (the dashboard's synced
graph, the drawer's Code state tab) can answer "was this entry written
against code that still exists in this form?" without git archaeology.

Contracts under test:
- Local writer: fields stamped when supplied; ABSENT when not (legacy and
  context-less entries keep the identical minimal node shape — never
  fabricated).
- MCP write path: context.code_repo / context.code_commit flow into
  commands_graph.say.
- Hosted builder: fields stamped; commit honestly absent when the hosted
  writer has none (no code checkout).
- Graph read model (EntryNode) mirrors the node fields.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from ulid import ULID

from watercooler import commands_graph
from watercooler.baseline_graph.writer import (
    get_entry_node_from_graph,
    init_thread_in_graph,
)
from watercooler_mcp.hosted_ops import _build_per_thread_graph_data


@pytest.fixture
def threads_dir(tmp_path) -> Path:
    d = tmp_path / ".watercooler"
    d.mkdir()
    init_thread_in_graph(d, "topic", title="T", status="OPEN", ball="x")
    return d


class TestLocalWriterStamping:
    def test_fields_stamped_when_supplied(self, threads_dir):
        eid = str(ULID())
        commands_graph.append_entry(
            "topic", threads_dir=threads_dir, agent="Claude", role="implementer",
            title="D", entry_type="Note", body="Spec: implementer\nbody",
            entry_id=eid,
            code_branch="main",
            code_repo="org/code-repo",
            code_commit="abc1234",
        )
        node = get_entry_node_from_graph(threads_dir, eid, "topic")
        assert node["code_branch"] == "main"
        assert node["code_repo"] == "org/code-repo"
        assert node["code_commit"] == "abc1234"

    def test_absent_when_not_supplied_no_fabrication(self, threads_dir):
        eid = str(ULID())
        commands_graph.append_entry(
            "topic", threads_dir=threads_dir, agent="Claude", role="implementer",
            title="D", entry_type="Note", body="Spec: implementer\nbody",
            entry_id=eid,
        )
        node = get_entry_node_from_graph(threads_dir, eid, "topic")
        assert "code_repo" not in node
        assert "code_commit" not in node

    def test_say_wrapper_forwards_fields(self, threads_dir):
        eid = str(ULID())
        commands_graph.say(
            "topic", threads_dir=threads_dir, agent="Claude", role="implementer",
            title="D", entry_type="Note", body="Spec: implementer\nbody",
            entry_id=eid,
            code_repo="org/code-repo",
            code_commit="abc1234",
        )
        node = get_entry_node_from_graph(threads_dir, eid, "topic")
        assert node["code_repo"] == "org/code-repo"
        assert node["code_commit"] == "abc1234"


class TestMcpWritePassThrough:
    def test_say_impl_passes_context_code_state(self, threads_dir):
        from watercooler_mcp.tools import thread_write as tw

        context = MagicMock()
        context.threads_dir = threads_dir
        context.code_branch = "main"
        context.code_repo = "org/code-repo"
        context.code_commit = "abc1234"

        captured: dict = {}

        def fake_say(topic, **kw):
            captured.update(kw)
            return threads_dir / "topic.md"

        with (
            patch.object(tw.validation, "_require_context", return_value=(None, context)),
            patch.object(tw.commands_graph, "say", side_effect=fake_say),
        ):
            tw._say_impl(
                topic="topic",
                title="T",
                body="Spec: implementer\nbody",
                ctx=MagicMock(client_id="test-client"),
                role="implementer",
                agent_func="Claude Code:test-model:implementer",
            )

        assert captured.get("code_branch") == "main"
        assert captured.get("code_repo") == "org/code-repo"
        assert captured.get("code_commit") == "abc1234"


class TestHostedBuilderStamping:
    def _build(self, **kw):
        meta, entries, edges = _build_per_thread_graph_data(
            topic="topic", status="OPEN", ball="x", title="T",
            existing_meta=None, existing_entries=[], existing_edges=[],
            entry_id=str(ULID()), agent="Claude", role="implementer",
            entry_type="Note", entry_title="T", body="b",
            timestamp="2026-07-07T00:00:00Z",
            **kw,
        )
        return entries[-1]

    def test_repo_stamped_commit_honestly_absent(self):
        # Hosted default: the tenant repo is known; the commit is not (no
        # code checkout on the hosted server) — it must never be fabricated.
        node = self._build(code_branch="main", code_repo="org/code-repo")
        assert node["code_repo"] == "org/code-repo"
        assert "code_commit" not in node

    def test_both_stamped_when_caller_supplies(self):
        node = self._build(code_repo="org/code-repo", code_commit="abc1234")
        assert node["code_repo"] == "org/code-repo"
        assert node["code_commit"] == "abc1234"

    def test_legacy_shape_without_fields(self):
        node = self._build()
        assert "code_repo" not in node
        assert "code_commit" not in node


class TestReadModelMirror:
    def test_entry_node_read_model_surfaces_fields(self, threads_dir):
        eid = str(ULID())
        commands_graph.append_entry(
            "topic", threads_dir=threads_dir, agent="Claude", role="implementer",
            title="D", entry_type="Note", body="Spec: implementer\nbody",
            entry_id=eid,
            code_repo="org/code-repo",
            code_commit="abc1234",
        )
        from watercooler.baseline_graph.reader import get_entry_from_graph

        ge = get_entry_from_graph(threads_dir, "topic", entry_id=eid)
        assert ge is not None
        assert ge.code_repo == "org/code-repo"
        assert ge.code_commit == "abc1234"
