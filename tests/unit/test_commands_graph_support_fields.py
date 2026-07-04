"""``support_fields`` forwarding through the say/ack command wrappers (#896 Leg 2).

The §6 tether read-model (#896 Leg 2 / #932) added a ``support_fields`` kwarg to
``append_entry``, ``say`` and ``daemon_write_entry`` — but the ``ack`` wrapper was
missed. Because ``daemon_write_entry`` always forwards ``support_fields=`` to
``ack`` (even as ``None``), every daemon candidate write raised ``ack() got an
unexpected keyword argument 'support_fields'`` — silently breaking both the
ExtractDecisionsDaemon §6 emission and the ExtractLearningsDaemon Phase-2 candidate
harvest. The unit suites missed it because they mock the writer. These tests pin the
wrapper-level forwarding so the drift cannot recur.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import patch

from ulid import ULID

from watercooler import commands_graph
from watercooler.baseline_graph.writer import (
    get_entries_for_thread,
    init_thread_in_graph,
)

_SUPPORT = {
    "dominant_tether": "source",
    "support_counts": {"source": 2},
    "thin_support": True,
    "thin_support_reason": "single_quote",
    "support_evidence": ["> verbatim quote"],
    "bogus_key": "should-be-dropped",
}


def _thread(tmp_path: Path) -> Path:
    td = tmp_path / ".watercooler"
    td.mkdir()
    init_thread_in_graph(td, "topic", title="T", status="OPEN", ball="x")
    return td


class TestAckForwardsSupportFields:
    def test_ack_accepts_and_persists_support_fields(self, tmp_path):
        td = _thread(tmp_path)
        # The regression: this call raised TypeError before ack grew the kwarg.
        commands_graph.ack(
            "topic",
            threads_dir=td,
            agent="ExtractLearningsDaemon",
            role="scribe",
            title="Learning candidate",
            entry_type="Note",
            body="Spec: learnings\nbody",
            entry_id=str(ULID()),
            support_fields=_SUPPORT,
        )
        node = list(get_entries_for_thread(td, "topic"))[-1]
        assert node["dominant_tether"] == "source"
        assert node["thin_support"] is True
        assert "bogus_key" not in node  # whitelist drops unknowns

    def test_ack_support_fields_none_is_accepted(self, tmp_path):
        # daemon_write_entry always passes support_fields=, frequently None.
        td = _thread(tmp_path)
        commands_graph.ack(
            "topic",
            threads_dir=td,
            agent="A",
            role="scribe",
            title="N",
            entry_type="Note",
            body="Spec: scribe\nbody",
            entry_id=str(ULID()),
            support_fields=None,
        )
        node = list(get_entries_for_thread(td, "topic"))[-1]
        assert "dominant_tether" not in node


class TestWrappersExposeSupportFields:
    def test_say_and_ack_signatures_carry_support_fields(self):
        for fn in (commands_graph.say, commands_graph.ack):
            assert "support_fields" in inspect.signature(fn).parameters, fn.__name__


class TestDaemonWriteAckSeam:
    """End-to-end pin on the exact seam that drifted: daemon_write_entry → ack.

    The unit suites mocked the writer, so the missing-kwarg break went unnoticed
    until a daemon actually emitted. Here only run_with_sync is patched (to invoke
    the write thunk without real git) — the REAL ack() runs. Pre-fix this returned
    written=False (the TypeError was caught by daemon_write_entry's never-raise
    contract); post-fix it persists the support field.
    """

    def _ctx(self, threads_dir: Path):
        from watercooler_mcp.config import ThreadContext

        return ThreadContext(
            code_root=threads_dir.parent,
            threads_dir=threads_dir,
            code_repo="test/repo",
            code_branch="main",
            code_commit="abc1234",
            code_remote="origin",
            explicit_dir=False,
        )

    @staticmethod
    def _run_sync_calls_thunk(*args, **kwargs):
        # args[2] is the _do_write thunk; call it so the real ack() executes.
        args[2]()
        kwargs["sync_status"].update(
            {"operation_completed": True, "committed": True, "pushed": True, "error": None}
        )
        return None

    def test_daemon_write_entry_forwards_support_fields_through_ack(self, tmp_path):
        from watercooler_mcp.daemons.daemon_write import daemon_write_entry

        td = _thread(tmp_path)
        ctx = self._ctx(td)
        with patch(
            "watercooler_mcp.config.resolve_thread_context", return_value=ctx
        ), patch(
            "watercooler_mcp.middleware.run_with_sync",
            side_effect=self._run_sync_calls_thunk,
        ):
            result = daemon_write_entry(
                "topic",
                code_root=td.parent,
                title="Learning candidate",
                body="Spec: learnings\nbody",
                agent="ExtractLearningsDaemon",
                role="scribe",
                entry_type="Note",
                support_fields=_SUPPORT,
            )

        assert result.written is True and result.pushed is True
        node = list(get_entries_for_thread(td, "topic"))[-1]
        assert node["dominant_tether"] == "source"
        assert "bogus_key" not in node
