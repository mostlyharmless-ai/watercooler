"""Plan v20 Phase 6: unit tests for migrate_splitgraphs.py classification.

The live path talks to FalkorDB and is covered in integration tests. These
unit tests exercise the pure helpers: label-count extraction, graph
classification, and the migrate() dry-run report against a fake client.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts" / "migrate_splitgraphs.py"
)


def _load_script_module():
    import sys
    spec = importlib.util.spec_from_file_location("migrate_splitgraphs", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["migrate_splitgraphs"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_script_module()


class _FakeFalkor:
    """Minimal stand-in for redis client — supports GRAPH.LIST + GRAPH.QUERY.

    Graphs map to ``{"labels": {label: count}, "edges": int}`` so each graph
    can carry both node and edge counts. Legacy tests passing just the
    label dict keep working via the ``_normalize`` path.
    """

    def __init__(self, graphs: Dict[str, Any]):
        self._graphs: Dict[str, Dict[str, Any]] = {
            name: self._normalize(shape) for name, shape in graphs.items()
        }
        self.copied: List[tuple[str, str]] = []
        self.deleted: List[str] = []

    @staticmethod
    def _normalize(shape: Any) -> Dict[str, Any]:
        if isinstance(shape, dict) and "labels" in shape:
            return {
                "labels": dict(shape.get("labels", {})),
                "edges": int(shape.get("edges", 0)),
            }
        # Back-compat: the bare label-count dict shape.
        return {"labels": dict(shape or {}), "edges": 0}

    def execute_command(self, command: str, *args: Any) -> Any:
        if command == "GRAPH.LIST":
            return list(self._graphs.keys())
        if command == "GRAPH.QUERY":
            graph, query, *_ = args
            shape = self._graphs.get(graph, {"labels": {}, "edges": 0})
            if "count(r)" in query:
                return ([], [[shape["edges"]]], [])
            rows = [[[label], count] for label, count in shape["labels"].items()]
            return ([], rows, [])
        if command == "GRAPH.COPY":
            src, dst = args
            self._graphs[dst] = {
                "labels": dict(self._graphs[src]["labels"]),
                "edges": self._graphs[src]["edges"],
            }
            self.copied.append((src, dst))
            return "OK"
        if command == "GRAPH.DELETE":
            name = args[0]
            self._graphs.pop(name, None)
            self.deleted.append(name)
            return "OK"
        raise AssertionError(f"unexpected command {command}")


class TestClassification:
    def test_t1_only_graph(self, mod) -> None:
        client = _FakeFalkor(
            {"watercooler_cloud": {"Entry": 10, "Entry_Embedding": 10}}
        )
        with patch.object(mod, "_connect", return_value=client), \
             patch.object(mod, "_connect_noop", return_value=client):
            report = mod.migrate(
                host="redis://stub",
                repo_slug="mostlyharmless-ai/watercooler",
                execute=False,
                delete_legacy=False,
            )
        assert report["canonical"]["t1_database"] == (
            "mostlyharmless_ai_watercooler_cloud_t1"
        )
        assert report["canonical"]["t2_database"] == (
            "mostlyharmless_ai_watercooler_cloud_t2"
        )
        actions = report["actions"]
        assert len(actions) == 1
        assert actions[0]["source"] == "watercooler_cloud"
        assert actions[0]["target"].endswith("_t1")
        assert actions[0]["classification"] == "t1_only"

    def test_t2_only_graph(self, mod) -> None:
        client = _FakeFalkor({"mostlyharmless_ai_watercooler_cloud": {"Entity": 5, "Episode": 8}})
        with patch.object(mod, "_connect", return_value=client), \
             patch.object(mod, "_connect_noop", return_value=client):
            report = mod.migrate(
                host="redis://stub",
                repo_slug="mostlyharmless-ai/watercooler",
                execute=False,
                delete_legacy=False,
            )
        actions = report["actions"]
        assert len(actions) == 1
        assert actions[0]["classification"] == "t2_only"
        assert actions[0]["target"].endswith("_t2")

    def test_mixed_graph_is_skipped(self, mod) -> None:
        client = _FakeFalkor(
            {"watercooler_cloud": {"Entry": 3, "Entity": 4, "Episode": 2}}
        )
        with patch.object(mod, "_connect", return_value=client), \
             patch.object(mod, "_connect_noop", return_value=client):
            report = mod.migrate(
                host="redis://stub",
                repo_slug="mostlyharmless-ai/watercooler",
                execute=False,
                delete_legacy=False,
            )
        assert report["actions"] == []
        assert len(report["skipped_ambiguous"]) == 1
        assert report["skipped_ambiguous"][0]["name"] == "watercooler_cloud"

    def test_execute_copies_and_optionally_deletes(self, mod) -> None:
        client = _FakeFalkor(
            {"watercooler_cloud": {"Entry": 4, "Entry_Embedding": 4}}
        )
        with patch.object(mod, "_connect", return_value=client), \
             patch.object(mod, "_connect_noop", return_value=client):
            report = mod.migrate(
                host="redis://stub",
                repo_slug="mostlyharmless-ai/watercooler",
                execute=True,
                delete_legacy=True,
            )
        assert client.copied == [(
            "watercooler_cloud",
            "mostlyharmless_ai_watercooler_cloud_t1",
        )]
        assert client.deleted == ["watercooler_cloud"]
        assert report["actions"][0]["executed"] is True
        assert report["actions"][0]["deleted_legacy"] is True

    def test_watercooler_cloud_only_considered_for_its_own_slug(
        self, mod
    ) -> None:
        """PR #654 in-PR review round 6 (MEDIUM): the historical
        ``watercooler_cloud`` fallback must not be added as a candidate
        when migrating a different project, or ``--execute --delete-legacy``
        could delete an unrelated graph that happens to share the name."""
        client = _FakeFalkor(
            {
                # Unrelated graph present on the FalkorDB instance.
                "watercooler_cloud": {
                    "labels": {"Entry": 10},
                    "edges": 4,
                },
                # Target for the unrelated project being migrated.
                "other_org_other_repo_t1": {
                    "labels": {},
                    "edges": 0,
                },
            }
        )
        with patch.object(mod, "_connect", return_value=client), \
             patch.object(mod, "_connect_noop", return_value=client):
            report = mod.migrate(
                host="redis://stub",
                repo_slug="other-org/other-repo",
                execute=True,
                delete_legacy=True,
            )

        # The unrelated watercooler_cloud graph must NOT appear in
        # legacy_candidates or actions.
        candidate_names = [c["name"] for c in report["legacy_candidates"]]
        assert "watercooler_cloud" not in candidate_names
        action_sources = [a["source"] for a in report["actions"]]
        assert "watercooler_cloud" not in action_sources
        # And it survives untouched.
        assert "watercooler_cloud" in client._graphs
        assert "watercooler_cloud" not in client.deleted

    def test_copy_refuses_when_target_already_populated(self, mod) -> None:
        """PR #654 in-PR review round 4 (LOW §3): if the canonical target
        already has content (from a prior partial run), the existing
        post.count >= source.count verification trivially passes, and
        --delete-legacy would then drop the legacy graph. Fail loudly at
        copy time instead."""
        client = _FakeFalkor(
            {
                "watercooler_cloud": {
                    "labels": {"Entry": 10},
                    "edges": 5,
                },
                # Pre-existing canonical target with partial content.
                "mostlyharmless_ai_watercooler_cloud_t1": {
                    "labels": {"Entry": 3},
                    "edges": 1,
                },
            }
        )
        with patch.object(mod, "_connect", return_value=client), \
             patch.object(mod, "_connect_noop", return_value=client):
            report = mod.migrate(
                host="redis://stub",
                repo_slug="mostlyharmless-ai/watercooler",
                execute=True,
                delete_legacy=True,
            )

        action = report["actions"][0]
        assert action["copy_ok"] is False
        assert "target_pre_existing" in (action.get("error") or "")
        assert action.get("target_pre_existing_nodes") == 3
        assert action.get("target_pre_existing_edges") == 1
        # Legacy graph survives.
        assert "watercooler_cloud" in client._graphs
        # No GRAPH.DELETE was issued.
        assert client.deleted == []

    def test_delete_legacy_refuses_when_target_has_extra_nodes(
        self, mod
    ) -> None:
        """PR #654 in-PR review round 11 (MEDIUM): the prior verification
        used ``post >= profile`` on both axes, which passes even if
        GRAPH.COPY silently duplicates nodes. With ``--delete-legacy``
        the source is then dropped on a duplicated target, losing the
        only authoritative copy. Strict equality is the correct
        invariant here."""
        client = _FakeFalkor(
            {
                "watercooler_cloud": {
                    "labels": {"Entry": 4, "Entry_Embedding": 4},
                    "edges": 8,
                }
            }
        )

        # Simulate a buggy copy that duplicates nodes.
        def _dup_copy(c, src, dst):
            src_shape = c._graphs[src]
            c._graphs[dst] = {
                "labels": {
                    label: count * 2 for label, count in src_shape["labels"].items()
                },
                "edges": src_shape["edges"] * 2,
            }
            c.copied.append((src, dst))

        with patch.object(mod, "_connect", return_value=client), \
             patch.object(mod, "_connect_noop", return_value=client), \
             patch.object(mod, "_rename_copy", side_effect=_dup_copy):
            report = mod.migrate(
                host="redis://stub",
                repo_slug="mostlyharmless-ai/watercooler",
                execute=True,
                delete_legacy=True,
            )

        action = report["actions"][0]
        assert action["executed"] is True
        assert action["verified"] is False
        # Legacy must NOT have been deleted.
        assert action.get("deleted_legacy") is not True
        assert action.get("delete_skipped_reason") == "verification_failed"
        assert "watercooler_cloud" in client._graphs

    def test_delete_legacy_refuses_when_post_profile_errors(self, mod) -> None:
        """PR #654 in-PR review round 12 (HIGH): if the post-copy profile
        itself errors (e.g., edge query times out), the dataclass default
        edge_count=0 would pass the verification check on T1 graphs
        (which legitimately have zero edges), and --delete-legacy would
        then drop the source."""
        client = _FakeFalkor(
            {
                "watercooler_cloud": {
                    "labels": {"Entry": 4, "Entry_Embedding": 4},
                    "edges": 0,
                }
            }
        )

        real_execute = client.execute_command
        copied_flag = {"done": False}

        def _fail_post_copy_edges(command, *args):
            if (
                copied_flag["done"]
                and command == "GRAPH.QUERY"
                and args
                and args[0] == "mostlyharmless_ai_watercooler_cloud_t1"
                and "count(r)" in args[1]
            ):
                raise RuntimeError("transient post-copy edge query failure")
            return real_execute(command, *args)

        client.execute_command = _fail_post_copy_edges

        real_copy = mod._rename_copy

        def _tracked_copy(c, src, dst):
            real_copy(c, src, dst)
            copied_flag["done"] = True

        with patch.object(mod, "_connect", return_value=client), \
             patch.object(mod, "_connect_noop", return_value=client), \
             patch.object(mod, "_rename_copy", side_effect=_tracked_copy):
            report = mod.migrate(
                host="redis://stub",
                repo_slug="mostlyharmless-ai/watercooler",
                execute=True,
                delete_legacy=True,
            )

        action = report["actions"][0]
        assert action["verified"] is False
        assert (
            action.get("verification_detail", {}).get("post_profile_error")
        )
        assert action.get("deleted_legacy") is not True
        assert action.get("delete_skipped_reason") == "post_profile_failed"
        assert "watercooler_cloud" in client._graphs
        assert "watercooler_cloud" not in client.deleted

    def test_copy_refuses_when_source_profile_fails(self, mod) -> None:
        """PR #654 in-PR review round 10 (HIGH): symmetric to the
        round-8 target-profile guard. If profiling the SOURCE graph
        fails on the edge query, profile.edge_count=0 and the prior
        code would still proceed; post-copy ``post.edge_count >= 0``
        trivially passed, and ``--delete-legacy`` would drop the
        source on a corrupted baseline."""
        client = _FakeFalkor(
            {
                "watercooler_cloud": {
                    "labels": {"Entry": 10},
                    "edges": 5,
                },
                "mostlyharmless_ai_watercooler_cloud_t1": {
                    "labels": {},
                    "edges": 0,
                },
            }
        )

        real_execute = client.execute_command

        def _fail_on_source_edges(command, *args):
            # Let node-count query succeed, fail the edge-count query on
            # the source. This produces a partial profile with
            # edge_count=0 and profile_error set.
            if (
                command == "GRAPH.QUERY"
                and args
                and args[0] == "watercooler_cloud"
                and "count(r)" in args[1]
            ):
                raise RuntimeError("transient edge count failure")
            return real_execute(command, *args)

        client.execute_command = _fail_on_source_edges

        with patch.object(mod, "_connect", return_value=client), \
             patch.object(mod, "_connect_noop", return_value=client):
            report = mod.migrate(
                host="redis://stub",
                repo_slug="mostlyharmless-ai/watercooler",
                execute=True,
                delete_legacy=True,
            )

        assert len(report["actions"]) == 1
        action = report["actions"][0]
        assert action["copy_ok"] is False
        assert action["source"] == "watercooler_cloud"
        assert "source_profile_failed" in (action.get("error") or "")
        # No mutation happened — the source graph must survive.
        assert client.copied == []
        assert client.deleted == []
        assert "watercooler_cloud" in client._graphs

    def test_copy_refuses_when_target_profile_fails(self, mod) -> None:
        """PR #654 in-PR review round 8 (MEDIUM): a transient GRAPH.QUERY
        failure when checking the target must NOT be treated as 'target is
        empty' — that path would silently GRAPH.COPY into whatever the
        target actually held."""
        client = _FakeFalkor(
            {
                "watercooler_cloud": {
                    "labels": {"Entry": 5},
                    "edges": 2,
                },
                "mostlyharmless_ai_watercooler_cloud_t1": {
                    "labels": {"Entry": 3},
                    "edges": 1,
                },
            }
        )

        real_execute = client.execute_command

        def _fail_on_target_profile(command, *args):
            # Simulate a transient error specifically when profiling the
            # target. Let the source and the list commands pass.
            if (
                command == "GRAPH.QUERY"
                and args
                and args[0] == "mostlyharmless_ai_watercooler_cloud_t1"
                and "RETURN labels(n)" in args[1]
            ):
                raise RuntimeError("transient GRAPH.QUERY failure")
            return real_execute(command, *args)

        client.execute_command = _fail_on_target_profile

        with patch.object(mod, "_connect", return_value=client), \
             patch.object(mod, "_connect_noop", return_value=client):
            report = mod.migrate(
                host="redis://stub",
                repo_slug="mostlyharmless-ai/watercooler",
                execute=True,
                delete_legacy=True,
            )

        action = report["actions"][0]
        assert action["copy_ok"] is False
        err = action.get("error") or ""
        assert "target_profile_failed" in err
        # Neither the legacy nor the target was touched.
        assert client.copied == []
        assert "watercooler_cloud" in client._graphs

    def test_extract_label_unwraps_compact_type_id_prefix(self, mod) -> None:
        """PR #654 in-PR review round 8 (LOW): FalkorDB's compact form
        returns labels as ``[type_id, [label_strings]]``. The prior
        heuristic returned the type-id integer stringified, which never
        matched the T1/T2 hint sets, so every real-FalkorDB migration
        was a no-op."""
        # Compact form: list-type id + list of label strings
        assert mod._extract_label([11, ["Entry"]]) == "Entry"
        # Doubly-wrapped: label in a nested pair
        assert mod._extract_label([11, [[8, "Entry"]]]) == "Entry"
        # Already-plain input still works
        assert mod._extract_label(["Entity"]) == "Entity"
        assert mod._extract_label("Episode") == "Episode"

    def test_dry_run_blocks_mutating_commands(self, mod) -> None:
        """PR #654 in-PR review (MEDIUM): _connect_noop used to equal
        _connect, so a dry-run against ``--host <prod>`` would still be
        able to mutate if any code path leaked through. Now the dry-run
        client is a read-only wrapper that raises on COPY/DELETE."""
        inner = _FakeFalkor(
            {"watercooler_cloud": {"labels": {"Entry": 1}, "edges": 0}}
        )
        ro = mod._ReadOnlyClient(inner)

        # Reads flow through.
        assert ro.execute_command("GRAPH.LIST") == ["watercooler_cloud"]

        # Writes are blocked.
        import pytest as _pytest

        with _pytest.raises(RuntimeError, match="Dry-run mode blocked"):
            ro.execute_command("GRAPH.COPY", "a", "b")
        with _pytest.raises(RuntimeError, match="Dry-run mode blocked"):
            ro.execute_command("GRAPH.DELETE", "a")

    def test_delete_legacy_refuses_when_edges_do_not_survive(self, mod) -> None:
        """PR #654 code-review §6: node-count-only verification is not enough;
        if GRAPH.COPY drops edges, --delete-legacy must refuse."""
        client = _FakeFalkor(
            {
                "watercooler_cloud": {
                    "labels": {"Entry": 5, "Entry_Embedding": 5},
                    "edges": 20,
                }
            }
        )

        # Patch _rename_copy so the target ends up with the same nodes but
        # zero edges — simulating a bad copy that passes the old check.
        def _broken_copy(c, src, dst):
            c._graphs[dst] = {
                "labels": dict(c._graphs[src]["labels"]),
                "edges": 0,
            }
            c.copied.append((src, dst))

        with patch.object(mod, "_connect", return_value=client), \
             patch.object(mod, "_connect_noop", return_value=client), \
             patch.object(mod, "_rename_copy", side_effect=_broken_copy):
            report = mod.migrate(
                host="redis://stub",
                repo_slug="mostlyharmless-ai/watercooler",
                execute=True,
                delete_legacy=True,
            )

        action = report["actions"][0]
        assert action["executed"] is True
        assert action["verified"] is False
        assert action.get("deleted_legacy") is not True
        assert action.get("delete_skipped_reason") == "verification_failed"
        # Legacy graph must survive.
        assert "watercooler_cloud" in client._graphs
        assert "watercooler_cloud" not in client.deleted

    def test_unrelated_graph_ignored(self, mod) -> None:
        client = _FakeFalkor(
            {
                "watercooler_cloud": {"Entry": 4},
                "some_other_project": {"Entry": 99},
            }
        )
        with patch.object(mod, "_connect", return_value=client), \
             patch.object(mod, "_connect_noop", return_value=client):
            report = mod.migrate(
                host="redis://stub",
                repo_slug="mostlyharmless-ai/watercooler",
                execute=False,
                delete_legacy=False,
            )
        action_sources = [a["source"] for a in report["actions"]]
        assert "some_other_project" not in action_sources
        assert "watercooler_cloud" in action_sources
