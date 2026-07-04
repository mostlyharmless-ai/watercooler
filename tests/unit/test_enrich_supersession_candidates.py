"""Unit tests for EnrichSupersessionDaemon earned_edge candidate emission.

Covers the exact candidate BODY contract parsed by
``watercooler_promote_candidate(target_type="Supersession")`` and the monitor-vs-emit
staging gate — hermetic, no live backend and no real thread writes.
"""

import types
from unittest import mock

from watercooler_mcp.daemons.enrich_supersession import (
    EnrichSupersessionDaemon,
    _format_supersession_candidate_body,
)

A = "01KS0JTK0RT4EC0M92PMX19XRA"
B = "01KS0PRRRSBQG6PXQCZJ9KN16Z"


# --- body contract --------------------------------------------------------


def test_body_matches_promote_contract_exactly():
    body = _format_supersession_candidate_body(A, B, "same_source_and_name")
    assert body == (
        "Spec: general-purpose\n"
        "Promotion-Source: earned_edge\n"
        f"Superseded-Entry: {A}\n"
        f"Superseded-By-Entry: {B}\n"
        "Basis: same_source_and_name\n"
        "Authority: none\n"
        "Candidate-Status: needs_human_confirmation\n"
        "\n"
        f"Entry {A} appears superseded by entry {B} "
        "(inferred; basis=same_source_and_name). Confirm via "
        'watercooler_promote_candidate(target_type="Supersession") to ratify.'
    )


def test_body_line_prefixes_are_parseable():
    lines = _format_supersession_candidate_body(A, B, "temporal_only").splitlines()
    assert lines[0] == "Spec: general-purpose"
    assert lines[1] == "Promotion-Source: earned_edge"
    assert lines[2] == f"Superseded-Entry: {A}"
    assert lines[3] == f"Superseded-By-Entry: {B}"
    assert lines[4] == "Basis: temporal_only"
    assert lines[5] == "Authority: none"
    assert lines[6] == "Candidate-Status: needs_human_confirmation"


def test_body_none_basis_renders_unknown():
    body = _format_supersession_candidate_body(A, B, None)
    assert "Basis: unknown" in body
    assert "basis=unknown" in body


# --- monitor vs emit gate -------------------------------------------------


class _FakeBackend:
    def __init__(self, entry_pairs, database="repo_t2"):
        self._entry_pairs = entry_pairs
        self.config = types.SimpleNamespace(database=database)
        self.afforded_calls = []

    def enrich_superseded_by(self, group_id, *, dry_run=False, emit_bases=None):
        return []  # no edge writes this tick; candidate emission is independent

    def afforded_supersession_entry_pairs(self, group_id, *, graph_name=None):
        self.afforded_calls.append(group_id)
        return list(self._entry_pairs)


PAIR = {"superseded_entry": A, "successor_entry": B, "basis": "same_name", "thread": "t"}


def _ok_result():
    return types.SimpleNamespace(written=True, pushed=True, entry_id="X", error=None)


def test_monitor_mode_emits_no_candidates(tmp_path):
    be = _FakeBackend([PAIR])
    d = EnrichSupersessionDaemon(
        backend=be, emit_mode="monitor", code_root=tmp_path
    )
    with mock.patch(
        "watercooler_mcp.daemons.enrich_supersession.daemon_write_entry"
    ) as write:
        d.tick()
    write.assert_not_called()
    assert be.afforded_calls == []  # never even queried in monitor mode


def test_emit_mode_emits_one_candidate_per_pair(tmp_path):
    be = _FakeBackend([PAIR])
    d = EnrichSupersessionDaemon(backend=be, emit_mode="emit", code_root=tmp_path)
    with mock.patch(
        "watercooler_mcp.daemons.enrich_supersession.daemon_write_entry",
        return_value=_ok_result(),
    ) as write, mock.patch(
        "watercooler_mcp.tools.decisions._supersession_is_ratified",
        return_value=False,
    ):
        d.tick()
    assert be.afforded_calls == ["repo_t2"]
    write.assert_called_once()
    kwargs = write.call_args.kwargs
    args = write.call_args.args
    assert args[0] == "t"  # emitted on the pair's thread
    assert kwargs["entry_type"] == "Note"
    assert kwargs["body"] == _format_supersession_candidate_body(A, B, "same_name")


def test_emit_mode_dedups_across_ticks(tmp_path):
    be = _FakeBackend([PAIR])
    d = EnrichSupersessionDaemon(backend=be, emit_mode="emit", code_root=tmp_path)
    with mock.patch(
        "watercooler_mcp.daemons.enrich_supersession.daemon_write_entry",
        return_value=_ok_result(),
    ) as write, mock.patch(
        "watercooler_mcp.tools.decisions._supersession_is_ratified",
        return_value=False,
    ):
        d.tick()
        d.tick()  # same (A,B) — must not re-emit
    write.assert_called_once()


def test_emit_mode_skips_ratified_pair(tmp_path):
    be = _FakeBackend([PAIR])
    d = EnrichSupersessionDaemon(backend=be, emit_mode="emit", code_root=tmp_path)
    with mock.patch(
        "watercooler_mcp.daemons.enrich_supersession.daemon_write_entry",
        return_value=_ok_result(),
    ) as write, mock.patch(
        "watercooler_mcp.tools.decisions._supersession_is_ratified",
        return_value=True,
    ):
        d.tick()
    write.assert_not_called()


def test_emit_mode_skips_pair_with_no_thread(tmp_path):
    pair = {**PAIR, "thread": None}
    be = _FakeBackend([pair])
    d = EnrichSupersessionDaemon(backend=be, emit_mode="emit", code_root=tmp_path)
    with mock.patch(
        "watercooler_mcp.daemons.enrich_supersession.daemon_write_entry",
        return_value=_ok_result(),
    ) as write:
        d.tick()
    write.assert_not_called()


def test_emit_backend_error_is_swallowed(tmp_path):
    class _Boom(_FakeBackend):
        def afforded_supersession_entry_pairs(self, group_id, *, graph_name=None):
            raise RuntimeError("db down")

    d = EnrichSupersessionDaemon(
        backend=_Boom([PAIR]), emit_mode="emit", code_root=tmp_path
    )
    with mock.patch(
        "watercooler_mcp.daemons.enrich_supersession.daemon_write_entry"
    ) as write:
        # Must not raise; write never reached.
        d.tick()
    write.assert_not_called()


def test_hosted_scoped_emission_adopts_full_tenant_identity(tmp_path):
    """Finding 2 (fixed, whole-scope): a hosted-scoped daemon adopts the coordinator scope's
    FULL write identity — the worktree clone (write+push target) AND the tenant repo/branch,
    so the candidate's code_branch tag matches the branch the ratification reads run against.
    Not a code_root write against the server checkout, and not a gated no-op."""
    import types as _types

    be = _FakeBackend([PAIR])
    d = EnrichSupersessionDaemon(backend=be, emit_mode="emit", code_root=tmp_path)
    scope = tmp_path / "tenant-worktree"
    d._threads_dir_override = scope  # coordinator-installed worktree
    d._scope_context = _types.SimpleNamespace(repo="org/tenant", branch="feature-x")
    with mock.patch(
        "watercooler_mcp.daemons.enrich_supersession.daemon_write_entry",
        return_value=_ok_result(),
    ) as write, mock.patch(
        "watercooler_mcp.tools.decisions._supersession_is_ratified", return_value=False
    ):
        d.tick()
    write.assert_called_once()
    kwargs = write.call_args.kwargs
    assert kwargs["threads_dir"] == scope
    assert kwargs["code_repo"] == "org/tenant"
    assert kwargs["code_branch"] == "feature-x"


def test_local_emission_passes_no_scope_override(tmp_path):
    be = _FakeBackend([PAIR])
    d = EnrichSupersessionDaemon(backend=be, emit_mode="emit", code_root=tmp_path)
    with mock.patch(
        "watercooler_mcp.daemons.enrich_supersession.daemon_write_entry",
        return_value=_ok_result(),
    ) as write, mock.patch(
        "watercooler_mcp.tools.decisions._supersession_is_ratified", return_value=False
    ):
        d.tick()
    kwargs = write.call_args.kwargs
    assert kwargs["threads_dir"] is None
    assert kwargs["code_repo"] is None and kwargs["code_branch"] is None


def test_hosted_default_branch_scope_tags_effective_main(tmp_path):
    """A default-branch hosted scope (no X-Branch → branch=None) must tag the candidate
    with the effective branch 'main', not fall back to the server checkout (review #1041)."""
    from watercooler_mcp.context import HttpRequestContext

    be = _FakeBackend([PAIR])
    d = EnrichSupersessionDaemon(backend=be, emit_mode="emit", code_root=tmp_path)
    d._threads_dir_override = tmp_path / "wt"
    d._scope_context = HttpRequestContext(user_id="u", repo="org/tenant", branch=None)
    with mock.patch(
        "watercooler_mcp.daemons.enrich_supersession.daemon_write_entry",
        return_value=_ok_result(),
    ) as write, mock.patch(
        "watercooler_mcp.tools.decisions._supersession_is_ratified", return_value=False
    ):
        d.tick()
    assert write.call_args.kwargs["code_branch"] == "main"
    assert write.call_args.kwargs["code_repo"] == "org/tenant"
