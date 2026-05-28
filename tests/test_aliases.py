"""Tests for the non-discoverable MCP tool alias-forwarding machinery.

The alias forwarder shipped inert in PR1a; later consolidation PRs populate
``TOOL_ALIASES`` (PR3b onward). Tests that need a synthetic alias register it
via the ``temp_alias`` fixture, which snapshots and restores the registry so
the real aliases are not disturbed.
"""

import asyncio
import json
import warnings

import pytest

from watercooler_mcp.aliases import (
    TOOL_ALIASES,
    AliasForwardingMiddleware,
    ToolAlias,
    resolve_alias,
)


class _Msg:
    """Minimal stand-in for an MCP CallToolRequestParams (mutable name/args)."""

    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _Ctx:
    def __init__(self, message):
        self.message = message


def _call(middleware, name, arguments):
    """Run the middleware once; return (result, forwarded_name, forwarded_args)."""
    seen: dict = {}

    async def call_next(ctx):
        seen["name"] = ctx.message.name
        seen["arguments"] = dict(ctx.message.arguments or {})
        return "RESULT"

    ctx = _Ctx(_Msg(name, arguments))
    result = asyncio.run(middleware.on_call_tool(ctx, call_next))
    return result, seen.get("name"), seen.get("arguments")


def test_role_details_alias_registered():
    """PR3b — watercooler_role_details forwards to watercooler_roles."""
    alias = resolve_alias("watercooler_role_details")
    assert alias is not None
    assert alias.canonical == "watercooler_roles"
    # A non-aliased name still resolves to None.
    assert resolve_alias("watercooler_anything") is None


def test_non_aliased_call_passes_through_untouched():
    mw = AliasForwardingMiddleware()
    result, name, args = _call(mw, "watercooler_search", {"query": "x"})
    assert result == "RESULT"
    assert name == "watercooler_search"
    assert args == {"query": "x"}


@pytest.fixture
def temp_alias():
    """Register a temporary alias and restore TOOL_ALIASES afterward.

    Snapshots/restores rather than clear()-ing — the registry now ships
    populated (PR3b+), so a blunt clear() would strip real aliases and
    pollute other tests.
    """
    saved = dict(TOOL_ALIASES)

    def _register(name: str, alias: ToolAlias) -> None:
        TOOL_ALIASES[name] = alias

    yield _register
    TOOL_ALIASES.clear()
    TOOL_ALIASES.update(saved)


def test_alias_rewrites_name_and_warns(temp_alias):
    mw = AliasForwardingMiddleware()
    temp_alias(
        "watercooler_test_legacy",
        ToolAlias(canonical="watercooler_test_new", since="PRtest"),
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result, name, args = _call(mw, "watercooler_test_legacy", {"a": 1})
    assert result == "RESULT"
    assert name == "watercooler_test_new"
    assert args == {"a": 1}
    # Filter to the alias deprecation specifically — unrelated
    # DeprecationWarnings (e.g. a dependency's) may also be recorded.
    depr = [
        w
        for w in caught
        if issubclass(w.category, DeprecationWarning)
        and "watercooler_test_legacy" in str(w.message)
    ]
    assert len(depr) == 1
    assert "watercooler_test_new" in str(depr[0].message)


def test_alias_inject_args_override_caller_values(temp_alias):
    """inject_args is the collapsed-tool selector that defines the retired
    name's operation — it overrides a caller-supplied value of the same key,
    so a stale call passing that key cannot redirect the retired tool
    (PR5 review)."""
    mw = AliasForwardingMiddleware()
    temp_alias(
        "watercooler_test_legacy",
        ToolAlias(
            canonical="watercooler_test_new", inject_args={"action": "add"}
        ),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        # Injected when the caller did not supply the argument.
        _, name, args = _call(mw, "watercooler_test_legacy", {"topic": "t"})
        assert name == "watercooler_test_new"
        assert args == {"topic": "t", "action": "add"}
        # A caller-supplied value of the injected key is OVERRIDDEN — the
        # retired name keeps its meaning even if a stale call passes action=.
        _, _, args2 = _call(mw, "watercooler_test_legacy", {"action": "remove"})
        assert args2 == {"action": "add"}


def test_alias_guard_short_circuits_on_error(temp_alias):
    """A guard returning an error dict short-circuits — call_next never runs
    and the error payload is returned as the tool result."""
    mw = AliasForwardingMiddleware()
    temp_alias(
        "watercooler_test_strict",
        ToolAlias(
            canonical="watercooler_test_new",
            guard=lambda args: (
                {"error": "x_required"} if not args.get("x") else None
            ),
        ),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        result, name, _ = _call(mw, "watercooler_test_strict", {})
        assert name is None  # call_next never ran — the call short-circuited
        assert json.loads(result.content[0].text) == {"error": "x_required"}
        # Guard passes when the precondition is met → the call forwards.
        result2, name2, _ = _call(mw, "watercooler_test_strict", {"x": 1})
        assert name2 == "watercooler_test_new"
        assert result2 == "RESULT"


def test_alias_renames_args(temp_alias):
    """rename_args re-binds a legacy parameter name to its canonical name;
    a caller-supplied canonical key always wins and the legacy key never
    survives to the forwarded call."""
    mw = AliasForwardingMiddleware()
    temp_alias(
        "watercooler_test_legacy",
        ToolAlias(
            canonical="watercooler_test_new",
            rename_args={"old_a": "new_a"},
        ),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        # Legacy key is renamed; it does not survive to the canonical call.
        _, name, args = _call(
            mw, "watercooler_test_legacy", {"old_a": 1, "b": 2}
        )
        assert name == "watercooler_test_new"
        assert args == {"new_a": 1, "b": 2}
        # A caller-supplied canonical key wins; the legacy key is dropped.
        _, _, args2 = _call(
            mw, "watercooler_test_legacy", {"old_a": 1, "new_a": 9}
        )
        assert args2 == {"new_a": 9}


def test_get_thread_entry_range_alias_renames_and_guards():
    """PR3c — watercooler_get_thread_entry_range forwards to
    watercooler_get_thread_entry, renaming start_index/end_index to
    index/to_index; the guard rejects open-ended legacy calls (no
    end_index), which the unified tool does not express."""
    mw = AliasForwardingMiddleware()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        # Explicit range → args renamed and forwarded.
        _, name, args = _call(
            mw,
            "watercooler_get_thread_entry_range",
            {"topic": "t", "start_index": 0, "end_index": 9},
        )
        assert name == "watercooler_get_thread_entry"
        assert args == {"topic": "t", "index": 0, "to_index": 9}
        # Open-ended legacy call (no end_index) → guard short-circuits.
        result, name2, _ = _call(
            mw,
            "watercooler_get_thread_entry_range",
            {"topic": "t", "start_index": 1},
        )
        assert name2 is None
        assert (
            json.loads(result.content[0].text)["error"] == "end_index_required"
        )


def test_role_details_alias_preserves_role_required():
    """Regression (PR3b review): watercooler_role_details had a stricter
    contract than the catalog tool — a missing `role` was an error, not a
    catalog dump. The alias guard preserves it for callers of the retired
    name."""
    mw = AliasForwardingMiddleware()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        # No role → guard short-circuits with the legacy error; not forwarded.
        result, name, _ = _call(mw, "watercooler_role_details", {})
        assert name is None
        assert json.loads(result.content[0].text) == {"error": "role_required"}
        # With a role → forwards to watercooler_roles, role passes through.
        _, name2, args2 = _call(
            mw, "watercooler_role_details", {"role": "critic"}
        )
        assert name2 == "watercooler_roles"
        assert args2 == {"role": "critic"}


def test_migration_preflight_alias_injects_preflight_only():
    """PR4a — watercooler_migration_preflight forwards to watercooler_bulk_index
    with preflight_only=True injected; code_path/backend pass through."""
    mw = AliasForwardingMiddleware()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        _, name, args = _call(
            mw,
            "watercooler_migration_preflight",
            {"code_path": ".", "backend": "graphiti"},
        )
    assert name == "watercooler_bulk_index"
    assert args == {
        "code_path": ".",
        "backend": "graphiti",
        "preflight_only": True,
    }


def test_leanrag_run_pipeline_alias_injects_run_pipeline():
    """PR4a — watercooler_leanrag_run_pipeline forwards to watercooler_bulk_index
    with run_pipeline=True injected; legacy pipeline args pass through."""
    mw = AliasForwardingMiddleware()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        _, name, args = _call(
            mw,
            "watercooler_leanrag_run_pipeline",
            {"group_id": "g", "dry_run": True, "incremental": False},
        )
    assert name == "watercooler_bulk_index"
    assert args == {
        "group_id": "g",
        "dry_run": True,
        "incremental": False,
        "run_pipeline": True,
    }


def test_migrate_to_memory_backend_alias_renames_and_guards():
    """PR4a — watercooler_migrate_to_memory_backend forwards to
    watercooler_bulk_index (topics → threads). The guard rejects migrate's
    dry-run default and any migrate-only knob — bulk_index's idempotent queue
    has no preview/checkpoint/chunk equivalent."""
    mw = AliasForwardingMiddleware()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        # Plain explicit call forwards; topics renamed to threads.
        _, name, args = _call(
            mw,
            "watercooler_migrate_to_memory_backend",
            {
                "code_path": ".",
                "backend": "graphiti",
                "dry_run": False,
                "topics": "auth,memory",
            },
        )
        assert name == "watercooler_bulk_index"
        assert args == {
            "code_path": ".",
            "backend": "graphiti",
            "dry_run": False,
            "threads": "auth,memory",
        }
        # Bare call — migrate's dry_run defaults True → guard short-circuits.
        result, name2, _ = _call(
            mw, "watercooler_migrate_to_memory_backend", {}
        )
        assert name2 is None
        assert (
            json.loads(result.content[0].text)["error"]
            == "migrate_to_memory_backend_retired"
        )
        # A migrate-only knob also short-circuits.
        result3, name3, _ = _call(
            mw,
            "watercooler_migrate_to_memory_backend",
            {"dry_run": False, "rechunk": True},
        )
        assert name3 is None
        assert (
            json.loads(result3.content[0].text)["error"]
            == "migrate_to_memory_backend_retired"
        )


def test_reindex_alias_forwards_to_list_threads():
    """PR4b — watercooler_reindex forwards to the graph-first
    watercooler_list_threads; the no-arg call maps straight through."""
    mw = AliasForwardingMiddleware()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        _, name, args = _call(mw, "watercooler_reindex", {"code_path": "."})
    assert name == "watercooler_list_threads"
    assert args == {"code_path": "."}


def test_whoami_alias_injects_identity_detail():
    """PR4b — watercooler_whoami forwards to
    watercooler_health(detail="identity")."""
    mw = AliasForwardingMiddleware()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        _, name, args = _call(mw, "watercooler_whoami", {})
    assert name == "watercooler_health"
    assert args == {"detail": "identity"}


def test_acknowledge_finding_alias_folds_into_daemon_findings():
    """PR5 D1 — watercooler_acknowledge_finding forwards to
    watercooler_daemon_findings(action="acknowledge"); the legacy daemon_name
    arg is renamed to the findings tool's `daemon` arg."""
    mw = AliasForwardingMiddleware()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        _, name, args = _call(
            mw,
            "watercooler_acknowledge_finding",
            {"daemon_name": "project_coordinator", "finding_id": "f1"},
        )
    assert name == "watercooler_daemon_findings"
    assert args == {
        "daemon": "project_coordinator",
        "finding_id": "f1",
        "action": "acknowledge",
    }


def test_find_similar_alias_folds_into_search():
    """PR6 D4 — watercooler_find_similar forwards to watercooler_search;
    entry_id → seed_entry_id, similarity_threshold → semantic_threshold."""
    mw = AliasForwardingMiddleware()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        _, name, args = _call(
            mw,
            "watercooler_find_similar",
            {"entry_id": "01ABC", "similarity_threshold": 0.7, "limit": 5},
        )
    assert name == "watercooler_search"
    assert args == {
        "seed_entry_id": "01ABC",
        "semantic_threshold": 0.7,
        "limit": 5,
    }


def test_find_similar_alias_preserves_use_embeddings():
    """use_embeddings passes straight through — search's seeded mode keeps the
    heuristic-fallback behavior find_similar had, so the retired name's
    contract is not narrowed (PR6 review)."""
    mw = AliasForwardingMiddleware()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        _, name, args = _call(
            mw,
            "watercooler_find_similar",
            {"entry_id": "01ABC", "use_embeddings": False},
        )
    assert name == "watercooler_search"
    assert args == {"seed_entry_id": "01ABC", "use_embeddings": False}


def test_federated_search_alias_folds_into_search():
    """PR6 D5 — watercooler_federated_search forwards to
    watercooler_search(federated=True); query/namespaces pass through."""
    mw = AliasForwardingMiddleware()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        _, name, args = _call(
            mw,
            "watercooler_federated_search",
            {"query": "auth", "namespaces": "ns1,ns2"},
        )
    assert name == "watercooler_search"
    assert args == {
        "query": "auth",
        "namespaces": "ns1,ns2",
        "federated": True,
    }
