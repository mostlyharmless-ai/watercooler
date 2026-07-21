"""D2 blessed-surface projection + per-leg reconcile (Commons cluster Phase 3).

Decision 01KXQ32Q7Z41F0P7A1JHN0S527 / plan workflow-packs…:85 (review P1-3 +
the approving rereview's boundary check): the projection to `team-lessons` is
idempotent and independently retryable per leg — pointer Note, lesson→pointer
xref, pointer→lesson xref — recoverable from EVERY reachable write boundary,
and never repaired by re-promotion.
"""

from __future__ import annotations

import pytest
from ulid import ULID

from watercooler.blessed_projection import (
    _default_pointer_writer,
    _default_xref_writer,
    find_blessed_pointer,
    format_blessed_pointer_body,
    inspect_blessed_projection,
    reconcile_blessed_projection,
)
from watercooler.commands_graph import append_entry, get_thread_from_graph

SOURCE = "topic-src"
BLESSED = "team-lessons"


def _promoted_lesson_body(candidate_ulid: str) -> str:
    return (
        "Spec: learnings-promoted\n"
        f"Promoted-From: {candidate_ulid}\n"
        f"Source-Thread: {SOURCE}\n"
        "Authority-Source: human\n"
        "Authority-Basis: human_promoted\n"
        "Human-Authorized-By: github:caleb\n"
        "Root-Cause-Canonical: silent-failure@1\n"
        "Confidence: 4/5 (from candidate)\n\n"
        "## Lesson\nAlways verify before asserting.\n\n"
        "## Root cause\nPushError swallowed by caller.\n"
    )


@pytest.fixture
def threads_dir(tmp_path):
    d = tmp_path / ".watercooler"
    d.mkdir()
    return d


@pytest.fixture
def lesson_id(threads_dir):
    """Seed: source thread with a genuine promoted lesson + the blessed thread."""
    candidate = str(ULID())
    lid = str(ULID())
    append_entry(
        SOURCE, threads_dir=threads_dir, agent="Caleb", role="pm",
        title="Always verify before asserting", entry_type="Note",
        body=_promoted_lesson_body(candidate), ball="Jay", status="OPEN",
        entry_id=lid,
    )
    append_entry(
        BLESSED, threads_dir=threads_dir, agent="Caleb", role="pm",
        title="Charter", entry_type="Note", body="charter", ball="Caleb",
        status="OPEN", entry_id=str(ULID()),
    )
    return lid


def _reconcile(threads_dir, lesson_id, **kw):
    return reconcile_blessed_projection(
        threads_dir, SOURCE, lesson_id, blessed_topic=BLESSED,
        actor="Test Projection", **kw
    )


def _blessed_entries(threads_dir):
    from watercooler.baseline_graph import storage
    from watercooler.baseline_graph.storage import get_graph_dir

    return list(storage.load_thread_entries(get_graph_dir(threads_dir), BLESSED))


class TestReconcileFromEachBoundary:
    def test_from_nothing_creates_all_three_legs(self, threads_dir, lesson_id):
        result = _reconcile(threads_dir, lesson_id)
        assert (result.pointer, result.xref_lesson, result.xref_pointer) == (
            "created", "created", "created"
        )
        assert result.complete
        pointer = find_blessed_pointer(_blessed_entries(threads_dir), lesson_id)
        assert pointer == result.pointer_entry_id

    def test_from_pointer_only_creates_only_xrefs(self, threads_dir, lesson_id):
        # Boundary: pointer committed, both xref writes lost.
        writer = _default_pointer_writer(threads_dir, BLESSED, "Test Projection")
        legs = inspect_blessed_projection(
            threads_dir, SOURCE, lesson_id, blessed_topic=BLESSED
        )
        writer(
            legs.lesson_title,
            format_blessed_pointer_body(
                lesson_entry_id=lesson_id, source_topic=SOURCE,
                source_index=legs.source_index,
                candidate_entry_id=legs.candidate_entry_id,
                lesson_summary=legs.lesson_summary,
                root_cause_canonical=legs.root_cause_canonical,
            ),
        )
        result = _reconcile(threads_dir, lesson_id)
        assert (result.pointer, result.xref_lesson, result.xref_pointer) == (
            "present", "created", "created"
        )

    def test_from_pointer_plus_lesson_xref(self, threads_dir, lesson_id):
        # Boundary: crash after the first xref.
        first = _reconcile(
            threads_dir, lesson_id,
            xref_writer=_OneShotXref(threads_dir),  # writes only the FIRST xref
        )
        assert (first.pointer, first.xref_lesson, first.xref_pointer) == (
            "created", "created", "failed"
        )
        second = _reconcile(threads_dir, lesson_id)
        assert (second.pointer, second.xref_lesson, second.xref_pointer) == (
            "present", "present", "created"
        )

    def test_from_pointer_plus_pointer_side_xref_only(self, threads_dir, lesson_id):
        # Odd ordering (review #1131 P2: CONSTRUCT the state, don't approximate
        # it): pointer + the BLESSED-side xref exist, the source-side xref was
        # lost. The retry must create ONLY the missing source-side leg without
        # duplicating the pointer or the existing xref.
        writer = _default_pointer_writer(threads_dir, BLESSED, "Test Projection")
        legs = inspect_blessed_projection(
            threads_dir, SOURCE, lesson_id, blessed_topic=BLESSED
        )
        pointer_id = writer(
            legs.lesson_title,
            format_blessed_pointer_body(
                lesson_entry_id=lesson_id, source_topic=SOURCE,
                source_index=legs.source_index,
                candidate_entry_id=legs.candidate_entry_id,
                lesson_summary=legs.lesson_summary,
                root_cause_canonical=legs.root_cause_canonical,
            ),
        )
        # Only the blessed-side (pointer → lesson) xref lands.
        _default_xref_writer(threads_dir, "Test Projection")(
            BLESSED, pointer_id, lesson_id
        )
        before = inspect_blessed_projection(
            threads_dir, SOURCE, lesson_id, blessed_topic=BLESSED
        )
        assert before.pointer_entry_id == pointer_id
        assert before.xref_pointer_present and not before.xref_lesson_present

        result = _reconcile(threads_dir, lesson_id)
        assert (result.pointer, result.xref_lesson, result.xref_pointer) == (
            "present", "created", "present"
        )
        after = inspect_blessed_projection(
            threads_dir, SOURCE, lesson_id, blessed_topic=BLESSED
        )
        assert after.pointer_entry_id == pointer_id  # no duplicate pointer
        assert after.xref_lesson_present and after.xref_pointer_present

    def test_reconcile_through_injected_readers_first_write_and_retry(
        self, threads_dir, lesson_id
    ):
        # Review #1131 P1 coverage: the hosted path injects entry/xref READERS
        # (no local filesystem). Drive the reconciler entirely through injected
        # readers backed by in-memory state — first write creates all legs,
        # the idempotent retry no-ops.
        from watercooler.baseline_graph import storage
        from watercooler.baseline_graph.storage import get_graph_dir

        graph_dir = get_graph_dir(threads_dir)
        pointer_store: list[dict] = []
        xref_store: dict[tuple, list] = {}

        def entries_loader(topic):
            if topic == BLESSED:
                return list(pointer_store)  # hosted blessed thread, in-memory
            return list(storage.load_thread_entries(graph_dir, topic))

        def xrefs_loader(topic, target):
            return list(xref_store.get((topic, target), []))

        def pointer_writer(title, body):
            eid = f"01POINTERHOSTED{len(pointer_store):011d}"
            pointer_store.append(
                {"id": eid, "entry_id": eid, "entry_type": "Note",
                 "title": title, "body": body}
            )
            return eid

        def xref_writer(topic, target, value):
            xref_store.setdefault((topic, target), []).append(value)
            return True

        kw = dict(
            blessed_topic=BLESSED, actor="Hosted Test",
            pointer_writer=pointer_writer, xref_writer=xref_writer,
            entries_loader=entries_loader, xrefs_loader=xrefs_loader,
        )
        first = reconcile_blessed_projection(threads_dir, SOURCE, lesson_id, **kw)
        assert (first.pointer, first.xref_lesson, first.xref_pointer) == (
            "created", "created", "created"
        )
        retry = reconcile_blessed_projection(threads_dir, SOURCE, lesson_id, **kw)
        assert (retry.pointer, retry.xref_lesson, retry.xref_pointer) == (
            "present", "present", "present"
        )
        assert len(pointer_store) == 1  # idempotent: no duplicate pointer

    def test_complete_state_is_a_noop(self, threads_dir, lesson_id):
        _reconcile(threads_dir, lesson_id)
        before = len(_blessed_entries(threads_dir))
        again = _reconcile(threads_dir, lesson_id)
        assert (again.pointer, again.xref_lesson, again.xref_pointer) == (
            "present", "present", "present"
        )
        assert len(_blessed_entries(threads_dir)) == before  # no duplicate pointer


class _OneShotXref:
    """Xref writer that succeeds once then fails — simulates a mid-legs crash."""

    def __init__(self, threads_dir):
        self._inner = _default_xref_writer(threads_dir, "Test Projection")
        self._used = False

    def __call__(self, topic, target, value):
        if self._used:
            raise RuntimeError("simulated crash after first xref")
        self._used = True
        return self._inner(topic, target, value)


class TestGuardsAndFailureIsolation:
    def test_missing_or_non_genuine_lesson_fails_cleanly(self, threads_dir, lesson_id):
        result = _reconcile(threads_dir, str(ULID()))  # no such lesson
        assert result.pointer == "failed" and result.errors

    def test_plain_note_is_not_projectable(self, threads_dir, lesson_id):
        plain = str(ULID())
        append_entry(
            SOURCE, threads_dir=threads_dir, agent="Caleb", role="pm",
            title="not a lesson", entry_type="Note",
            body="## Lesson\nlooks like one but carries no promotion markers\n",
            entry_id=plain,
        )
        result = _reconcile(threads_dir, plain)
        assert result.pointer == "failed"

    def test_pointer_writer_exception_never_raises(self, threads_dir, lesson_id):
        def boom(title, body):
            raise RuntimeError("pointer write exploded")

        result = _reconcile(threads_dir, lesson_id, pointer_writer=boom)
        assert result.pointer == "failed"
        assert not result.complete  # reported, not raised

    def test_pointer_write_preserves_blessed_thread_ball(self, threads_dir, lesson_id):
        before = get_thread_from_graph(threads_dir, BLESSED)
        _reconcile(threads_dir, lesson_id)
        after = get_thread_from_graph(threads_dir, BLESSED)
        assert after["ball"] == before["ball"] == "Caleb"
        assert after["status"] == before["status"]


class TestRepairFinding:
    def test_incomplete_projection_persists_repair_finding(self, monkeypatch):
        # Review #1131 P2: the synchronous promote response can be dropped —
        # an incomplete projection must leave a durable, discoverable record
        # naming the exact repair command.
        from watercooler.blessed_projection import ReconcileResult
        from watercooler_mcp.tools import promotion as promo

        captured = {}

        def fake_append(daemon_name, findings, namespace="", **kw):
            captured["daemon"] = daemon_name
            captured["findings"] = findings

        import watercooler_mcp.daemons.state as state_mod

        monkeypatch.setattr(state_mod, "append_findings", fake_append)
        result = ReconcileResult(
            pointer="created", xref_lesson="failed", xref_pointer="failed",
            pointer_entry_id="01P", errors=["source xref failed: boom"],
        )
        cmd = "watercooler reconcile-blessed-projection --topic t --lesson-id 01L"
        promo._persist_blessed_repair_finding(
            topic="t", lesson_entry_id="01L", result=result, repair_cmd=cmd
        )
        assert captured["daemon"] == "blessed_projection"
        f = captured["findings"][0]
        assert f.category == "blessed_projection_incomplete"
        assert f.entry_id == "01L" and f.topic == "t"
        assert cmd in f.message
        assert f.details["legs"]["xref_lesson"] == "failed"
        assert f.details["repair_command"] == cmd

    def test_finding_persist_failure_never_raises(self, monkeypatch):
        from watercooler.blessed_projection import ReconcileResult
        from watercooler_mcp.tools import promotion as promo
        import watercooler_mcp.daemons.state as state_mod

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(state_mod, "append_findings", boom)
        promo._persist_blessed_repair_finding(
            topic="t", lesson_entry_id="01L",
            result=ReconcileResult(errors=["x"]), repair_cmd="cmd",
        )  # must not raise — promotion stays independent of bookkeeping


class TestScopedRepairFindings:
    """Rereview #1131 P1: tenant isolation + ordinary-listing discoverability."""

    @pytest.fixture
    def daemons_dir(self, tmp_path, monkeypatch):
        import watercooler_mcp.daemons.state as state_mod

        monkeypatch.setattr(state_mod, "_DEFAULT_DAEMONS_DIR", tmp_path / "daemons")
        return tmp_path / "daemons"

    @staticmethod
    def _finding(ulid_suffix: str):
        from ulid import ULID

        from watercooler_mcp.daemons.state import Finding

        return Finding(
            finding_id=str(ULID()),
            daemon_name="blessed_projection",
            severity="warning",
            category="blessed_projection_incomplete",
            topic=f"t-{ulid_suffix}",
        )

    def test_two_scope_isolation(self, daemons_dir):
        from watercooler_mcp.daemons.state import append_findings, load_findings

        append_findings("blessed_projection", [self._finding("a")], namespace="nsA")
        append_findings("blessed_projection", [self._finding("b")], namespace="nsB")
        a = load_findings("blessed_projection", namespace="nsA")
        b = load_findings("blessed_projection", namespace="nsB")
        assert [f.topic for f in a] == ["t-a"]
        assert [f.topic for f in b] == ["t-b"]

    def test_persist_uses_auth_scope_namespace_when_resolvable(self, monkeypatch):
        from watercooler.blessed_projection import ReconcileResult
        from watercooler_mcp.tools import promotion as promo
        import watercooler_mcp.auth.scope as scope_mod
        import watercooler_mcp.daemons.state as state_mod

        class FakeScope:
            namespace = "deadbeef" * 4
            scope_id = "user:org/repo"
            user_id = "user"
            repo = "org/repo"

        captured = {}

        def fake_append(daemon_name, findings, namespace="", **kw):
            captured.update(
                namespace=namespace, unscoped=kw.get("_allow_unscoped", False),
                finding=findings[0],
            )

        monkeypatch.setattr(
            scope_mod, "resolve_scope_or_off_hosted", lambda: FakeScope()
        )
        monkeypatch.setattr(state_mod, "append_findings", fake_append)
        promo._persist_blessed_repair_finding(
            topic="t", lesson_entry_id="01L",
            result=ReconcileResult(errors=["x"]), repair_cmd="cmd",
        )
        assert captured["namespace"] == FakeScope.namespace
        assert captured["unscoped"] is False
        assert captured["finding"].scope_id == "user:org/repo"

    def test_persist_falls_back_to_unscoped_locally(self, monkeypatch):
        from watercooler.blessed_projection import ReconcileResult
        from watercooler_mcp.tools import promotion as promo
        import watercooler_mcp.auth.scope as scope_mod
        import watercooler_mcp.daemons.state as state_mod

        captured = {}

        def fake_append(daemon_name, findings, namespace="", **kw):
            captured.update(namespace=namespace, unscoped=kw.get("_allow_unscoped"))

        monkeypatch.setattr(scope_mod, "resolve_scope_or_off_hosted", lambda: None)
        monkeypatch.setattr(state_mod, "append_findings", fake_append)
        promo._persist_blessed_repair_finding(
            topic="t", lesson_entry_id="01L",
            result=ReconcileResult(errors=["x"]), repair_cmd="cmd",
        )
        assert captured["namespace"] == "" and captured["unscoped"] is True

    def test_persist_fails_closed_when_hosted_scope_is_broken(self, monkeypatch):
        # Rereview #1131 P1 (round 2): a hosted context that is present but
        # unresolvable must NOT fall through to the global unscoped store —
        # the record is lost (logged), never written cross-tenant.
        from watercooler.blessed_projection import ReconcileResult
        from watercooler_mcp.tools import promotion as promo
        import watercooler_mcp.auth.scope as scope_mod
        import watercooler_mcp.daemons.state as state_mod

        def broken_scope():
            raise scope_mod.ScopeResolutionError("auth context missing user_id")

        calls = []
        monkeypatch.setattr(scope_mod, "resolve_scope_or_off_hosted", broken_scope)
        monkeypatch.setattr(
            state_mod, "append_findings", lambda *a, **k: calls.append((a, k))
        )
        # Must neither raise (best-effort contract) nor write anywhere.
        promo._persist_blessed_repair_finding(
            topic="t", lesson_entry_id="01L",
            result=ReconcileResult(errors=["x"]), repair_cmd="cmd",
        )
        assert calls == []

    def test_hosted_runtime_with_absent_context_fails_closed(
        self, daemons_dir, monkeypatch
    ):
        # Rereview #1131 P1 (round 4): a live HostedDaemonCoordinator with
        # BOTH contextvars absent (middleware failed to install/retain them)
        # is NOT local mode. With a populated global findings file, none of
        # persist / list / acknowledge may touch the unscoped store.
        import json as json_mod
        from unittest.mock import MagicMock

        from watercooler.blessed_projection import ReconcileResult
        from watercooler_mcp.daemons.state import append_findings, load_findings
        from watercooler_mcp.tools import promotion as promo
        from watercooler_mcp.tools.daemon import (
            _acknowledge_finding_impl,
            _aux_source_findings,
        )
        import watercooler_mcp.daemons as daemons_pkg
        import watercooler_mcp.daemons.state as state_mod

        # Hosted process signal; no HTTP/worker contextvars are set in tests,
        # so resolve_scope() raises naturally — exactly the failure mode.
        monkeypatch.setattr(
            daemons_pkg, "get_hosted_coordinator", lambda: object()
        )
        global_finding = self._finding("global")
        append_findings(
            "blessed_projection", [global_finding],
            namespace="", _allow_unscoped=True,
        )

        # Persist: writes nowhere (and does not raise — best-effort contract).
        writes = []
        monkeypatch.setattr(
            state_mod, "append_findings",
            lambda *a, **k: writes.append((a, k)),
        )
        promo._persist_blessed_repair_finding(
            topic="t", lesson_entry_id="01L",
            result=ReconcileResult(errors=["x"]), repair_cmd="cmd",
        )
        assert writes == []

        # Listing: refuses the global store.
        assert _aux_source_findings(
            daemon_filter=None, severity=None, category=None, topic=None,
            limit=50, unacknowledged_only=False,
        ) == []

        # Acknowledge: errors instead of acking in the global store.
        result = json_mod.loads(
            _acknowledge_finding_impl(
                MagicMock(), "blessed_projection",
                finding_id=global_finding.finding_id,
            )
        )
        assert result["status"] == "error"
        assert "scope resolution failed" in result["message"]
        assert [
            f.acknowledged
            for f in load_findings(
                "blessed_projection", namespace="", _allow_unscoped=True
            )
        ] == [False]

    def test_public_listing_fails_closed_without_scope(self, daemons_dir, monkeypatch):
        # Rereview #1131 P1 (round 5): scope_id=None means "ALL scopes" to
        # HostedDaemonCoordinator.get_findings — the PUBLIC listing tool with
        # a live coordinator, findings in TWO scopes, and absent auth context
        # must error, never aggregate other tenants' registered-daemon
        # findings. With a resolved scope, it returns only that scope's.
        import json as json_mod
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from watercooler_mcp.daemons.hosted_coordinator import (
            HostedDaemonCoordinator,
        )
        from watercooler_mcp.tools.daemon import _daemon_findings_impl
        import watercooler_mcp.context as ctx_mod
        import watercooler_mcp.daemons as daemons_pkg

        class FakeManager:
            def __init__(self, findings):
                self._findings = findings

            def get_all_findings(self, **kw):
                return list(self._findings)

        coord = HostedDaemonCoordinator()
        coord._scopes = {
            "userA:org/repo": SimpleNamespace(
                manager=FakeManager([self._finding("scope-a")])
            ),
            "userB:org/repo": SimpleNamespace(
                manager=FakeManager([self._finding("scope-b")])
            ),
        }
        monkeypatch.setattr(daemons_pkg, "get_daemon_runtime", lambda: coord)
        monkeypatch.setattr(daemons_pkg, "get_hosted_coordinator", lambda: coord)
        monkeypatch.setattr(
            daemons_pkg,
            "ensure_hosted_scope_for_current_context",
            lambda reason="": None,
        )

        # Absent context → error, zero findings from either scope.
        monkeypatch.setattr(ctx_mod, "get_effective_context", lambda: None)
        out = json_mod.loads(_daemon_findings_impl(MagicMock()))
        assert out["status"] == "error"
        assert out["findings"] == []

        # Resolved scope → that tenant's findings only.
        monkeypatch.setattr(
            ctx_mod,
            "get_effective_context",
            lambda: SimpleNamespace(scope_id="userA:org/repo"),
        )
        out = json_mod.loads(_daemon_findings_impl(MagicMock()))
        assert [f["topic"] for f in out["findings"]] == ["t-scope-a"]

        # Resolved-but-UNREGISTERED scope (e.g. reaped between ensure_scope
        # and the read) → nothing, never the all-scope aggregate (round 6).
        monkeypatch.setattr(
            ctx_mod,
            "get_effective_context",
            lambda: SimpleNamespace(scope_id="ghost:org/repo"),
        )
        out = json_mod.loads(_daemon_findings_impl(MagicMock()))
        assert out["findings"] == []

        # Same contract directly at the coordinator.
        assert coord.get_findings(scope_id="ghost:org/repo") == []

    def test_persist_hosted_hint_with_absent_context_fails_closed(
        self, monkeypatch
    ):
        # The per-request hint (is_hosted_context) alone must also fail
        # closed when the auth contextvars are gone — no coordinator needed.
        from watercooler.blessed_projection import ReconcileResult
        from watercooler_mcp.tools import promotion as promo
        import watercooler_mcp.daemons.state as state_mod

        writes = []
        monkeypatch.setattr(
            state_mod, "append_findings",
            lambda *a, **k: writes.append((a, k)),
        )
        promo._persist_blessed_repair_finding(
            topic="t", lesson_entry_id="01L",
            result=ReconcileResult(errors=["x"]), repair_cmd="cmd",
            hosted_hint=True,
        )
        assert writes == []

    def test_listing_fails_closed_when_hosted_scope_is_broken(
        self, daemons_dir, monkeypatch
    ):
        from watercooler_mcp.daemons.state import append_findings
        from watercooler_mcp.tools.daemon import _aux_source_findings
        import watercooler_mcp.auth.scope as scope_mod

        # A finding exists in the global unscoped store; a broken hosted
        # request must not be able to read it.
        append_findings(
            "blessed_projection", [self._finding("global")],
            namespace="", _allow_unscoped=True,
        )

        def broken_scope():
            raise scope_mod.ScopeResolutionError("auth context missing repo")

        monkeypatch.setattr(scope_mod, "resolve_scope_or_off_hosted", broken_scope)
        assert _aux_source_findings(
            daemon_filter=None, severity=None, category=None, topic=None,
            limit=50, unacknowledged_only=False,
        ) == []

    def test_ordinary_listing_merges_aux_source(self, daemons_dir, monkeypatch):
        # The all-daemon listing includes blessed_projection findings for the
        # caller's own scope, and an OTHER-daemon filter excludes them.
        from watercooler_mcp.daemons.state import append_findings
        from watercooler_mcp.tools.daemon import _aux_source_findings
        import watercooler_mcp.auth.scope as scope_mod

        append_findings("blessed_projection", [self._finding("mine")], namespace="nsA")
        append_findings("blessed_projection", [self._finding("theirs")], namespace="nsB")

        class ScopeA:
            namespace = "nsA"

        monkeypatch.setattr(scope_mod, "resolve_scope_or_off_hosted", lambda: ScopeA())
        # No filter → aux included, caller's scope only.
        rows = _aux_source_findings(
            daemon_filter=None, severity=None, category=None, topic=None,
            limit=50, unacknowledged_only=False,
        )
        assert [f.topic for f in rows] == ["t-mine"]
        # Explicit aux-source filter → same result.
        rows = _aux_source_findings(
            daemon_filter="blessed_projection", severity=None, category=None,
            topic=None, limit=50, unacknowledged_only=False,
        )
        assert [f.topic for f in rows] == ["t-mine"]
        # A different daemon filter excludes aux sources entirely.
        assert _aux_source_findings(
            daemon_filter="learnings", severity=None, category=None, topic=None,
            limit=50, unacknowledged_only=False,
        ) == []

    def test_aux_acknowledge_round_trip_in_scoped_mode(self, daemons_dir, monkeypatch):
        # Rereview #1131 P2: list → acknowledge → filtered-list. The ack must
        # land in the SAME auth-derived namespace the finding was written to
        # (the coordinator's daemon-namespace recovery can't apply to an
        # unregistered aux source).
        import json as json_mod
        from unittest.mock import MagicMock

        from watercooler_mcp.daemons.state import append_findings, load_findings
        from watercooler_mcp.tools.daemon import (
            _acknowledge_finding_impl,
            _aux_source_findings,
        )
        import watercooler_mcp.auth.scope as scope_mod

        finding = self._finding("mine")
        append_findings("blessed_projection", [finding], namespace="nsA")

        class ScopeA:
            namespace = "nsA"

        monkeypatch.setattr(scope_mod, "resolve_scope_or_off_hosted", lambda: ScopeA())
        listed = _aux_source_findings(
            daemon_filter=None, severity=None, category=None, topic=None,
            limit=50, unacknowledged_only=True,
        )
        assert [f.finding_id for f in listed] == [finding.finding_id]

        result = json_mod.loads(
            _acknowledge_finding_impl(
                MagicMock(), "blessed_projection", finding_id=finding.finding_id
            )
        )
        assert result["status"] == "ok"
        assert result["acknowledged"] == [finding.finding_id]

        # Gone from the unacknowledged view; still in the namespace's store.
        assert _aux_source_findings(
            daemon_filter=None, severity=None, category=None, topic=None,
            limit=50, unacknowledged_only=True,
        ) == []
        assert [
            f.acknowledged for f in load_findings("blessed_projection", namespace="nsA")
        ] == [True]

    def test_aux_acknowledge_fails_closed_when_scope_is_broken(self, monkeypatch):
        import json as json_mod
        from unittest.mock import MagicMock

        from watercooler_mcp.tools.daemon import _acknowledge_finding_impl
        import watercooler_mcp.auth.scope as scope_mod

        def broken_scope():
            raise scope_mod.ScopeResolutionError("auth context missing user_id")

        monkeypatch.setattr(scope_mod, "resolve_scope_or_off_hosted", broken_scope)
        result = json_mod.loads(
            _acknowledge_finding_impl(MagicMock(), "blessed_projection", finding_id="01F")
        )
        assert result["status"] == "error"
        assert "scope resolution failed" in result["message"]


class TestPointerBody:
    def test_pointer_markers_and_provenance(self, threads_dir, lesson_id):
        result = _reconcile(threads_dir, lesson_id)
        entries = _blessed_entries(threads_dir)
        from watercooler.blessed_projection import _bare_id

        pointer = next(
            e for e in entries
            if _bare_id(e.get("id") or "") == result.pointer_entry_id
        )
        body = pointer["body"]
        assert f"Blessed-Lesson: {lesson_id}" in body
        assert f"Blessed-Source-Topic: {SOURCE}" in body
        assert "Root-Cause-Canonical: silent-failure@1" in body
        assert "reconcile-blessed-projection" in body
        # Not promotion-guard bait: no Authority-Basis, Spec not *-promoted.
        assert "Authority-Basis" not in body
