"""Plan v5.1 Move 3 Sprint 4 — daemon-internal namespace propagation tests.

Background
----------
Move 3 (`WATERCOOLER_FINDINGS_STRICT_NAMESPACE`) flips the contract on the
findings store: under strict mode, calling ``load_findings`` /
``save_checkpoint`` / ``append_findings`` with an empty ``namespace``
raises ``ValueError`` rather than silently routing to a shared root path.
PR #726 wired the predicate; this test pins the migration that all
daemon-internal callers correctly propagate ``self.state_namespace``
through to the state functions, so the strict-mode flip does not break
legitimate same-scope dedup-resync and checkpoint paths.

The test has two parts:

1. **Strict-mode contract.** When the flag is on, a state-fn call with
   empty ``namespace`` raises; with a real namespace it succeeds. When
   the flag is off, the un-namespaced call is lenient (preserves
   single-tenant local-mode behaviour).

2. **Static AST scan of daemon source files.** Every call to
   ``load_findings`` / ``save_checkpoint`` / ``append_findings`` /
   ``acknowledge_finding`` inside a daemon file must include either:
   - a ``namespace=…`` keyword argument, OR
   - an explicit ``_allow_unscoped=True`` keyword argument (admin-path
     escape hatch).

   This catches regressions where a future refactor introduces a new
   call without the namespace argument — preventing the cross-tenant
   read leak the migration closes.

Cross-tenant impact (the security argument): without proper
propagation, each daemon's dedup-resync reads findings from ALL
tenants when looking up "what have I already emitted." Each daemon's
dedup state therefore depends on the union of findings across all
tenants — a confidentiality and correctness violation that's been
latent until the strict flag is flipped.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


_DAEMONS_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "watercooler_mcp"
    / "daemons"
)
assert _DAEMONS_DIR.is_dir(), f"daemons dir missing: {_DAEMONS_DIR}"


_GATED_FNS = {
    "load_findings",
    "append_findings",
    "save_checkpoint",
    "load_checkpoint",
    "acknowledge_finding",
}


# Files that legitimately call these functions without a namespace
# argument because they are NOT daemon-internal call sites — e.g.,
# the state module itself defines them; the finding_store wraps them
# but already takes its own namespace; the hosted_coordinator passes
# scope_id directly. Any addition here requires explicit security
# review (the flag exists to catch unintentional misses).
#
# PR #730 round 1 MED #1: ``decision_extractor.py`` was previously
# listed here with a false justification. It only contains two
# ``load_findings`` calls (both already correctly carry
# ``namespace=self.state_namespace``); the file should be covered by
# the AST gate so future regressions are caught.
#
# ``manager.py`` remains in the indirect set because its sole
# unscoped call uses ``_allow_unscoped=True`` — the documented
# escape hatch for the cross-process-fallback contract (see
# ``manager.py:_get_findings`` for the local-single-tenant rationale).
# The AST gate's ``_has_allow_unscoped_kwarg`` check would catch
# that anyway; keeping it in the indirect set is belt-and-suspenders
# documentation of intent.
_SCOPE_INDIRECT_FILES = {
    "state.py",          # defines the functions
    "finding_store.py",  # wraps them with self._namespace
    "hosted_coordinator.py",  # passes scope_id namespace explicitly
    "manager.py",        # cross-process fallback uses _allow_unscoped=True
    "base.py",           # base class — uses self.state_namespace
    "__init__.py",
}


def _iter_daemon_modules() -> list[Path]:
    """Yield daemon-module paths whose internal calls are gated."""
    return sorted(
        p
        for p in _DAEMONS_DIR.glob("*.py")
        if p.name not in _SCOPE_INDIRECT_FILES
    )


def _calls_in(module_source: str, fn_names: set[str]) -> list[ast.Call]:
    """Return every ``ast.Call`` whose called name is in *fn_names*."""
    tree = ast.parse(module_source)
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Direct: ``load_findings(...)``
        if isinstance(node.func, ast.Name) and node.func.id in fn_names:
            calls.append(node)
            continue
        # Attribute: ``state.load_findings(...)`` (rarely but possible)
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in fn_names
        ):
            calls.append(node)
    return calls


def _has_namespace_kwarg(call: ast.Call) -> bool:
    """Check for ``namespace=…`` kwarg with a non-empty literal or
    non-literal value.

    PR #730 round 1 LOW: rejecting ``namespace=""`` (empty-string
    literal) is the security-meaningful contract — the gate exists
    to catch dropped scope, and an explicit empty namespace is the
    same as a missing one. Non-literal values (``self.state_namespace``,
    function calls, variables) pass because the gate cannot prove
    them empty statically; runtime safety for those falls to the
    M3 strict-mode flag's ``_findings_strict_namespace`` check.
    """
    for kw in call.keywords:
        if kw.arg != "namespace":
            continue
        # Non-literal value (Name, Attribute, Call, etc.): assume
        # caller provides a real namespace at runtime.
        if not isinstance(kw.value, ast.Constant):
            return True
        # Constant value: only non-empty strings count.
        if isinstance(kw.value.value, str) and kw.value.value:
            return True
        # Empty string, None, or other literal values — reject.
        return False
    return False


def _has_allow_unscoped_kwarg(call: ast.Call) -> bool:
    return any(
        kw.arg == "_allow_unscoped"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is True
        for kw in call.keywords
    )


# ---------------------------------------------------------------------- #
# Static AST scan — every call must propagate namespace
# ---------------------------------------------------------------------- #


class TestDaemonCallSitesPropagateNamespace:
    """Walks every daemon module's AST and asserts every gated state
    function call carries ``namespace=…`` (or ``_allow_unscoped=True``).
    """

    @pytest.mark.parametrize(
        "module_path",
        _iter_daemon_modules(),
        ids=lambda p: p.name,
    )
    def test_module_calls_propagate_namespace(self, module_path: Path) -> None:
        source = module_path.read_text(encoding="utf-8")
        calls = _calls_in(source, _GATED_FNS)
        if not calls:
            pytest.skip(f"{module_path.name} has no gated calls")

        offenders: list[str] = []
        for call in calls:
            fn_label = (
                call.func.id  # type: ignore[attr-defined]
                if isinstance(call.func, ast.Name)
                else call.func.attr  # type: ignore[attr-defined]
            )
            if _has_namespace_kwarg(call) or _has_allow_unscoped_kwarg(call):
                continue
            offenders.append(
                f"{module_path.name}:{call.lineno} {fn_label}() — "
                "missing namespace= and _allow_unscoped=True"
            )

        assert not offenders, (
            "Daemon call sites that don't propagate namespace= would "
            "leak findings across tenants under STRICT_NAMESPACE=1:\n  "
            + "\n  ".join(offenders)
        )


class TestASTGateRejectsEmptyNamespaceLiteral:
    """PR #730 round 1 LOW: the AST gate must reject a literal
    ``namespace=""`` argument — semantically the same as a missing
    argument. Pin both halves of the contract so a future loosening
    is caught.
    """

    def test_gate_rejects_empty_string_literal(self) -> None:
        source = 'load_findings("daemon", limit=10, namespace="")'
        call = ast.parse(source, mode="eval").body
        assert isinstance(call, ast.Call)
        assert _has_namespace_kwarg(call) is False

    def test_gate_rejects_none_literal(self) -> None:
        source = 'load_findings("daemon", limit=10, namespace=None)'
        call = ast.parse(source, mode="eval").body
        assert isinstance(call, ast.Call)
        assert _has_namespace_kwarg(call) is False

    def test_gate_accepts_non_empty_string_literal(self) -> None:
        source = 'load_findings("daemon", limit=10, namespace="u1:org/repo")'
        call = ast.parse(source, mode="eval").body
        assert isinstance(call, ast.Call)
        assert _has_namespace_kwarg(call) is True

    def test_gate_accepts_self_attribute(self) -> None:
        source = "load_findings('daemon', limit=10, namespace=self.state_namespace)"
        call = ast.parse(source, mode="eval").body
        assert isinstance(call, ast.Call)
        assert _has_namespace_kwarg(call) is True

    def test_gate_accepts_variable_reference(self) -> None:
        source = "load_findings('daemon', limit=10, namespace=ns)"
        call = ast.parse(source, mode="eval").body
        assert isinstance(call, ast.Call)
        assert _has_namespace_kwarg(call) is True


# ---------------------------------------------------------------------- #
# Strict-mode contract — runtime behaviour of the flag
# ---------------------------------------------------------------------- #


class TestStrictModeContract:
    """Pins the M3 strict-mode contract that the migration enables."""

    def test_strict_mode_raises_on_empty_namespace(
        self, monkeypatch, tmp_path,
    ) -> None:
        """Under strict mode, an empty namespace is a hard error.

        This is the cross-tenant defense — any daemon-internal call
        site that drops ``namespace=`` while the flag is on will trip
        this guard at runtime.
        """
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path,
        )
        monkeypatch.setenv("WATERCOOLER_FINDINGS_STRICT_NAMESPACE", "1")

        from watercooler_mcp.daemons.state import load_findings

        with pytest.raises(ValueError, match="empty namespace"):
            load_findings("auditor", limit=10)

    def test_strict_mode_accepts_namespaced_call(
        self, monkeypatch, tmp_path,
    ) -> None:
        """Under strict mode, a namespaced call succeeds — the
        migration's positive case."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path,
        )
        monkeypatch.setenv("WATERCOOLER_FINDINGS_STRICT_NAMESPACE", "1")

        from watercooler_mcp.daemons.state import load_findings

        result = load_findings(
            "auditor", limit=10, namespace="u1:org/example-repo",
        )
        assert result == []

    def test_strict_mode_accepts_allow_unscoped_call(
        self, monkeypatch, tmp_path,
    ) -> None:
        """Under strict mode, an ``_allow_unscoped=True`` call still
        succeeds — preserves the admin / diagnostic escape hatch."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path,
        )
        monkeypatch.setenv("WATERCOOLER_FINDINGS_STRICT_NAMESPACE", "1")

        from watercooler_mcp.daemons.state import load_findings

        result = load_findings("auditor", limit=10, _allow_unscoped=True)
        assert result == []

    def test_strict_mode_lenient_when_flag_off(
        self, monkeypatch, tmp_path,
    ) -> None:
        """Default (flag off) accepts empty namespace — preserves
        single-tenant local mode behaviour. The migration must not
        break local-mode users who never set the flag."""
        monkeypatch.setattr(
            "watercooler_mcp.daemons.state._DEFAULT_DAEMONS_DIR", tmp_path,
        )
        monkeypatch.delenv("WATERCOOLER_FINDINGS_STRICT_NAMESPACE", raising=False)

        from watercooler_mcp.daemons.state import load_findings

        result = load_findings("auditor", limit=10)
        assert result == []
