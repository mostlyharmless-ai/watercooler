"""Category 2: Memory Recall Benchmark Tests.

Tests that the baseline graph search correctly retrieves entries
matching natural-language queries. All tests are deterministic
(no LLM calls) except test_semantic_vs_keyword which requires
a live embedding server.
"""
from __future__ import annotations

import pytest

from watercooler.baseline_graph.search import SearchQuery, search_graph

from .ground_truth import RelevanceSet, build_recall_goldens
from .metrics import (
    average_precision,
    f1_at_k,
    keyword_coverage,
    named_entity_hallucination_rate,
    precision_at_k,
    recall_at_k,
    wilson_confidence_interval,
)

# Minimum acceptable thresholds for keyword-only search over 5 goldens.
# With file-based search (no embeddings), keyword matching is inherently
# limited — 0.4 mean F1@5 and 0.5 top-1 hit rate represent a baseline
# that should hold as long as entry titles/bodies contain query terms.
MIN_MEAN_F1_AT_5 = 0.4
MIN_TOP1_HIT_RATE = 0.5

_RECALL_GOLDENS = build_recall_goldens()


@pytest.mark.benchmark
@pytest.mark.parametrize("golden", _RECALL_GOLDENS, ids=lambda g: g.category)
def test_retrieval_quality(benchmark_graph, golden: RelevanceSet):
    """Evaluate recall@5 for each golden query."""
    K = 5
    query = SearchQuery(
        query=golden.query,
        limit=K,
        include_entries=True,
        include_threads=False,
    )
    results = search_graph(benchmark_graph, query)
    retrieved = [r.node_id for r in results.results]

    r = recall_at_k(retrieved, golden.relevant_entry_ids, K)
    assert r >= 0.5, (
        f"Recall@{K} for '{golden.query}': {r:.2f}\n"
        f"Retrieved: {retrieved}\n"
        f"Expected: {golden.relevant_entry_ids}"
    )


@pytest.mark.benchmark
def test_aggregate_recall_metrics(benchmark_graph, recall_goldens):
    """Compute mean F1@5 and top-1 hit rate across the full gold dataset."""
    K = 5
    f1_scores, binary_hits = [], []

    for golden in recall_goldens:
        query = SearchQuery(
            query=golden.query,
            limit=K,
            include_entries=True,
            include_threads=False,
        )
        results = search_graph(benchmark_graph, query)
        retrieved = [r.node_id for r in results.results]
        f1_scores.append(f1_at_k(retrieved, golden.relevant_entry_ids, K))
        binary_hits.append(
            1 if retrieved and retrieved[0] in golden.relevant_entry_ids else 0
        )

    mean_f1 = sum(f1_scores) / len(f1_scores)
    hit_rate = sum(binary_hits) / len(binary_hits)
    ci_lo, ci_hi = wilson_confidence_interval(sum(binary_hits), len(binary_hits))

    assert mean_f1 >= MIN_MEAN_F1_AT_5, f"Mean F1@5: {mean_f1:.3f}"
    assert hit_rate >= MIN_TOP1_HIT_RATE, (
        f"Top-1 hit rate: {hit_rate:.2f} (CI: [{ci_lo:.2f}, {ci_hi:.2f}])"
    )


@pytest.mark.benchmark
def test_stale_fact_resolution(benchmark_graph, superseded_decisions):
    """Newer decisions outrank older superseded ones.

    Note: keyword search has no timestamp recency boost, so ranking
    depends on field-match scoring. The newer decision should match
    more fields (title + body + summary) than the older one.
    """
    for pair in superseded_decisions:
        query = SearchQuery(
            query=pair["query"],
            limit=10,
            include_entries=True,
            include_threads=False,
        )
        results = search_graph(benchmark_graph, query)
        ids = [r.node_id for r in results.results]
        old_pos = next(
            (i for i, x in enumerate(ids) if x == pair["older_id"]), None
        )
        new_pos = next(
            (i for i, x in enumerate(ids) if x == pair["newer_id"]), None
        )
        if old_pos is not None and new_pos is not None:
            assert new_pos < old_pos, (
                f"Superseded decision ranked higher: "
                f"newer@{new_pos} older@{old_pos}"
            )


@pytest.mark.benchmark
def test_summary_quality(entries_with_summaries):
    """Graph summaries accurately represent entry content."""
    for entry in entries_with_summaries:
        coverage = keyword_coverage(entry["body"], entry["summary"])
        halluc = named_entity_hallucination_rate(entry["body"], entry["summary"])
        compression = len(entry["summary"]) / max(len(entry["body"]), 1)

        assert coverage >= 0.3, (
            f"Low coverage ({coverage:.2f}) for entry {entry['id']}"
        )
        assert halluc <= 0.5, (
            f"High hallucination ({halluc:.2f}) for entry {entry['id']}"
        )
        assert compression <= 0.5, (
            f"Summary too long ({compression:.2f}) for entry {entry['id']}"
        )


@pytest.mark.benchmark
@pytest.mark.needs_embedding
def test_semantic_vs_keyword(benchmark_graph, paraphrase_pairs):
    """Semantic search finds entries that keyword search misses."""
    semantic_only_hits = 0
    total_semantic_results = 0

    for pair in paraphrase_pairs:
        kw_q = SearchQuery(
            query=pair.query,
            semantic=False,
            limit=5,
            include_entries=True,
            include_threads=False,
        )
        sem_q = SearchQuery(
            query=pair.query,
            semantic=True,
            limit=5,
            include_entries=True,
            include_threads=False,
        )
        kw_ids = {r.node_id for r in search_graph(benchmark_graph, kw_q).results}
        sem_results = search_graph(benchmark_graph, sem_q).results
        sem_ids = {r.node_id for r in sem_results}
        total_semantic_results += len(sem_results)
        if pair.relevant_entry_ids & sem_ids and not (
            pair.relevant_entry_ids & kw_ids
        ):
            semantic_only_hits += 1

    # If semantic search returned nothing at all, embeddings aren't indexed
    if total_semantic_results == 0:
        pytest.skip("No embeddings indexed in benchmark graph")

    assert semantic_only_hits > 0, (
        "Semantic search found nothing that keyword missed"
    )
