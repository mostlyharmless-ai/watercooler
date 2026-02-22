"""Category 3: Sync Awareness Benchmark Tests.

Tests that watercooler's thread structure enables detection of
concurrent changes, branch-scoped reads, and entry provenance.
All tests are deterministic -- no live agents needed.
"""
from __future__ import annotations

import pytest

from watercooler.baseline_graph.reader import read_thread_from_graph
from watercooler.baseline_graph.search import SearchQuery, search_graph


@pytest.mark.benchmark
def test_change_detection(benchmark_graph):
    """Entries about file changes are discoverable by module name."""
    query = SearchQuery(
        query="authentication",
        limit=5,
        include_entries=True,
        include_threads=False,
    )
    results = search_graph(benchmark_graph, query)
    assert results.count > 0, "No change entries found for 'authentication'"


@pytest.mark.benchmark
def test_code_branch_filtering(benchmark_graph, branch_entries):
    """code_branch metadata enables branch-scoped reads."""
    result = read_thread_from_graph(
        benchmark_graph,
        branch_entries["topic"],
        code_branch=branch_entries["branch"],
    )
    assert result is not None, (
        f"Thread '{branch_entries['topic']}' not found in benchmark graph"
    )
    _, entries = result
    assert len(entries) > 0, "No entries found for branch filter"

    for e in entries:
        assert e.code_branch == branch_entries["branch"], (
            f"Entry {e.entry_id} has wrong branch: "
            f"{e.code_branch} (expected {branch_entries['branch']})"
        )


@pytest.mark.benchmark
def test_entry_provenance(benchmark_graph, entries_with_commits):
    """Entries with code_commit metadata are retrievable by title."""
    for entry_info in entries_with_commits:
        query = SearchQuery(
            query=entry_info["title"],
            limit=3,
            include_entries=True,
            include_threads=False,
        )
        results = search_graph(benchmark_graph, query)
        found = any(
            r.node_id == entry_info["entry_id"]
            for r in results.results
        )
        assert found, (
            f"Entry {entry_info['entry_id']} with commit "
            f"{entry_info['commit']} not found by title search"
        )
