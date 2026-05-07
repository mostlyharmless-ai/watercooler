"""Unit tests for the atomic-commit refactor in ``hosted_ops``.

The load-bearing claim of the refactor is: one logical ``say`` call
produces ONE git commit (and therefore ONE webhook delivery) rather
than the prior 5 separate commits (3 graph put_files + 1 md put_file
+ 1 enrichment put_file). The 5-event burst was overwhelming the
dashboard's webhook receiver — most events were dropped at the edge,
leaving ``ConnectedRepo.graphNodes`` stale and the dashboard list out
of sync with the orphan branch.

These tests pin the burst-reduction property by counting calls into
the GitHub client. They mock ``client.commit_files`` and
``client.put_file`` so the tests are pure unit tests with no network
or LLM dependencies.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from watercooler_mcp.hosted_ops import (
    _generate_entry_enrichment,
    _write_per_thread_atomic,
    _write_per_thread_graph,
)


def _fake_client() -> MagicMock:
    """A GitHubClient mock that records ``commit_files`` / ``put_file``
    calls and returns plausible blob/commit SHAs.

    The ``commit_files`` side-effect transparently accepts (and
    ignores) the ``expected_blob_shas`` kwarg so tests focused on
    burst-reduction don't have to thread conflict-check state through
    a mock filesystem. Tests that specifically exercise the conflict
    check should override the side-effect.
    """
    client = MagicMock()
    client.repo = "owner/repo"
    client.branch = "watercooler/threads"

    def _commit_files_side_effect(files, message, **kwargs):
        return ("commit-sha-aaaa", {path: f"blob-{i}" for i, (path, _c) in enumerate(files)})
    client.commit_files.side_effect = _commit_files_side_effect
    client.put_file.return_value = "putfile-sha"
    return client


def test_write_per_thread_graph_uses_one_commit() -> None:
    """``_write_per_thread_graph`` must produce exactly ONE
    ``commit_files`` call regardless of how many graph files it
    writes. Previously this was 3 ``put_file`` calls = 3 commits;
    now all 3 files land in 1 commit.
    """
    client = _fake_client()

    meta = {"id": "thread:demo", "type": "thread", "topic": "demo"}
    entries = [{"id": "entry:e1", "type": "entry", "thread_topic": "demo", "index": 0}]
    edges = [{"source_id": "thread:demo", "target_id": "entry:e1"}]

    _write_per_thread_graph(
        client,
        topic="demo",
        meta=meta,
        entries=entries,
        edges=edges,
        meta_sha=None,
        entries_sha=None,
        edges_sha=None,
        commit_message="[watercooler] demo: test",
    )

    assert client.commit_files.call_count == 1
    assert client.put_file.call_count == 0

    # Verify all 3 graph files are in the single commit.
    files_arg = client.commit_files.call_args.kwargs.get(
        "files"
    ) or client.commit_files.call_args.args[0]
    paths = {p for p, _c in files_arg}
    assert paths == {
        "graph/baseline/threads/demo/meta.json",
        "graph/baseline/threads/demo/entries.jsonl",
        "graph/baseline/threads/demo/edges.jsonl",
    }


def test_write_per_thread_atomic_bundles_md_into_same_commit() -> None:
    """With ``project_md=True``, ``_write_per_thread_atomic`` rolls
    the ``.md`` projection into the SAME commit as the graph files.
    No separate ``put_file`` for the markdown.
    """
    client = _fake_client()

    meta = {"id": "thread:demo", "type": "thread", "topic": "demo", "title": "Demo"}
    entries = [
        {
            "id": "entry:e1",
            "type": "entry",
            "thread_topic": "demo",
            "entry_id": "e1",
            "index": 0,
            "agent": "Test",
            "role": "implementer",
            "entry_type": "Note",
            "title": "First",
            "body": "hello",
            "timestamp": "2026-05-05T00:00:00Z",
        }
    ]
    edges: list[dict] = []

    commit_sha, info = _write_per_thread_atomic(
        client,
        topic="demo",
        meta=meta,
        entries=entries,
        edges=edges,
        commit_message="[watercooler] demo: First",
        project_md=True,
        # No enrichment in this test — keeps the assertions about call
        # count tight; enrichment is exercised in a separate test.
    )

    assert client.commit_files.call_count == 1
    assert client.put_file.call_count == 0
    assert info["md_projected"] is True

    files_arg = client.commit_files.call_args.kwargs.get(
        "files"
    ) or client.commit_files.call_args.args[0]
    paths = {p for p, _c in files_arg}
    assert "threads/demo.md" in paths
    assert len(paths) == 4  # meta + entries + edges + md


def test_write_per_thread_atomic_with_enrichment_still_one_commit() -> None:
    """When enrichment runs, the summary + embedding are merged into
    the entries list IN MEMORY before the single commit. The atomic
    write therefore still produces one ``commit_files`` call, not a
    follow-up ``put_file`` for the enriched ``entries.jsonl``.
    """
    client = _fake_client()

    meta = {"id": "thread:demo", "type": "thread", "topic": "demo", "title": "Demo"}
    entries = [
        {
            "id": "entry:e1",
            "type": "entry",
            "thread_topic": "demo",
            "entry_id": "e1",
            "index": 0,
            "agent": "Test",
            "role": "implementer",
            "entry_type": "Note",
            "title": "First",
            "body": "hello world",
            "timestamp": "2026-05-05T00:00:00Z",
        }
    ]
    edges: list[dict] = []

    # Stub the enrichment generator so the test doesn't need an LLM
    # / embedding service. The point of this test is the burst-count
    # property, not the enrichment internals.
    with patch(
        "watercooler_mcp.hosted_ops._generate_entry_enrichment",
        return_value=("a short summary", [0.1, 0.2, 0.3]),
    ) as mock_enrich:
        commit_sha, info = _write_per_thread_atomic(
            client,
            topic="demo",
            meta=meta,
            entries=entries,
            edges=edges,
            commit_message="[watercooler] demo: First",
            project_md=True,
            enrich_entry_id="e1",
            enrich_body="hello world",
            enrich_title="First",
            enrich_entry_type="Note",
        )

    mock_enrich.assert_called_once()
    assert client.commit_files.call_count == 1
    assert client.put_file.call_count == 0
    assert info["enriched"] is True
    assert info["md_projected"] is True

    # Confirm the enrichment merged into the entries list before the
    # commit by inspecting the entries.jsonl content sent to commit_files.
    files_arg = client.commit_files.call_args.kwargs.get(
        "files"
    ) or client.commit_files.call_args.args[0]
    entries_content = next(
        c for p, c in files_arg if p.endswith("/entries.jsonl")
    )
    assert "a short summary" in entries_content
    # The embedding is rendered as a JSON list — check one element.
    assert "0.1" in entries_content


def test_write_per_thread_atomic_skips_enrichment_when_generator_returns_none() -> None:
    """If both summary and embedding generators return ``None`` (e.g.
    services unavailable) the atomic write still succeeds in one
    commit and ``info["enriched"]`` is False. The entry is written
    without summary/embedding fields rather than failing the whole
    write.
    """
    client = _fake_client()

    meta = {"id": "thread:demo", "type": "thread", "topic": "demo"}
    entries = [
        {
            "id": "entry:e1",
            "type": "entry",
            "thread_topic": "demo",
            "entry_id": "e1",
            "index": 0,
            "agent": "Test",
            "role": "implementer",
            "entry_type": "Note",
            "title": "First",
            "body": "hello",
            "timestamp": "2026-05-05T00:00:00Z",
        }
    ]

    with patch(
        "watercooler_mcp.hosted_ops._generate_entry_enrichment",
        return_value=(None, None),
    ):
        _commit_sha, info = _write_per_thread_atomic(
            client,
            topic="demo",
            meta=meta,
            entries=entries,
            edges=[],
            commit_message="[watercooler] demo: First",
            project_md=True,
            enrich_entry_id="e1",
            enrich_body="hello",
            enrich_title="First",
            enrich_entry_type="Note",
        )

    assert client.commit_files.call_count == 1
    assert info["enriched"] is False


def test_write_per_thread_atomic_propagates_conflict() -> None:
    """``GitHubConflictError`` from ``commit_files`` (branch ref moved
    between read and update) propagates so the caller's retry loop
    runs. This preserves the per-file SHA contract that the prior
    ``_write_per_thread_graph`` exposed.
    """
    from watercooler_mcp.github_api import GitHubConflictError

    client = _fake_client()
    client.commit_files.side_effect = GitHubConflictError(
        "branch moved", status_code=422
    )

    with pytest.raises(GitHubConflictError):
        _write_per_thread_atomic(
            client,
            topic="demo",
            meta={"topic": "demo"},
            entries=[],
            edges=[],
            commit_message="[watercooler] demo: test",
            project_md=False,
        )


def test_write_per_thread_atomic_forwards_expected_blob_shas() -> None:
    """Caller-supplied ``meta_sha`` / ``entries_sha`` / ``edges_sha``
    must reach ``commit_files`` as ``expected_blob_shas`` so the
    per-file conflict check spans the full caller transaction.

    This is the regression test for PR #775's reviewer-flagged
    lost-write window: callers always passed these SHAs through to
    the prior ``_write_per_thread_graph`` (where they were the
    ``sha=`` arg on ``put_file``), the v1 atomic refactor silently
    dropped them, and a follow-up CR pointed out that two concurrent
    ``say_hosted`` calls could both succeed with the second
    overwriting the first. This test pins that the SHAs are now
    forwarded so the conflict check actually runs.
    """
    client = _fake_client()

    _write_per_thread_atomic(
        client,
        topic="demo",
        meta={"topic": "demo"},
        entries=[],
        edges=[],
        commit_message="[watercooler] demo: test",
        project_md=False,
        meta_sha="parent-meta-sha",
        entries_sha="parent-entries-sha",
        edges_sha=None,  # caller saw "edges.jsonl absent at parent"
    )

    kwargs = client.commit_files.call_args.kwargs
    expected = kwargs.get("expected_blob_shas")
    assert expected is not None, "expected_blob_shas must be forwarded"
    assert expected["graph/baseline/threads/demo/meta.json"] == "parent-meta-sha"
    assert expected["graph/baseline/threads/demo/entries.jsonl"] == "parent-entries-sha"
    # ``None`` entry encodes "caller expects this file to be absent at
    # the parent commit" — commit_files must check for that case (as a
    # creation conflict if it actually exists).
    assert expected["graph/baseline/threads/demo/edges.jsonl"] is None


def test_write_per_thread_atomic_blob_sha_mismatch_raises_conflict() -> None:
    """If ``commit_files`` rejects the caller's expected SHAs (e.g.
    a concurrent writer landed an entry between the caller's read
    and our write) the conflict propagates so the caller's retry
    loop refreshes from the post-concurrent-write state. This is
    end-to-end coverage of the lost-write fix at the
    ``_write_per_thread_atomic`` layer.
    """
    from watercooler_mcp.github_api import GitHubConflictError

    client = _fake_client()
    # Simulate commit_files detecting a parent-tree blob mismatch.
    client.commit_files.side_effect = GitHubConflictError(
        "blob SHA mismatch for entries.jsonl", status_code=409
    )

    with pytest.raises(GitHubConflictError):
        _write_per_thread_atomic(
            client,
            topic="demo",
            meta={"topic": "demo"},
            entries=[],
            edges=[],
            commit_message="[watercooler] demo: test",
            project_md=False,
            entries_sha="stale-entries-sha-from-T1",
        )
