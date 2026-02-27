"""Deterministic evaluation metrics for watercooler benchmarks.

All functions operate on ranked lists of IDs or text strings.
No external dependencies beyond stdlib + math.
"""
from __future__ import annotations

import math
import re
from typing import Set


def precision_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """Fraction of top-k retrieved that are relevant."""
    if k <= 0:
        return 0.0
    return sum(1 for doc in retrieved[:k] if doc in relevant) / k


def recall_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """Fraction of relevant items in top-k."""
    if not relevant:
        return 1.0
    return len(set(retrieved[:k]) & relevant) / len(relevant)


def f1_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """Harmonic mean of precision@k and recall@k."""
    p = precision_at_k(retrieved, relevant, k)
    r = recall_at_k(retrieved, relevant, k)
    return 2 * p * r / (p + r) if (p + r) > 0 else 0.0


def ndcg_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """Normalized Discounted Cumulative Gain (binary relevance)."""
    def dcg(ranked: list[str], cutoff: int) -> float:
        return sum(
            (1.0 if doc in relevant else 0.0) / math.log2(i + 2)
            for i, doc in enumerate(ranked[:cutoff])
        )
    idcg = dcg(list(relevant), k)
    return dcg(retrieved, k) / idcg if idcg > 0 else 0.0


def average_precision(retrieved: list[str], relevant: set[str]) -> float:
    """Average Precision -- area under precision-recall curve."""
    if not relevant:
        return 0.0
    hits, cum = 0, 0.0
    for rank, doc in enumerate(retrieved, start=1):
        if doc in relevant:
            hits += 1
            cum += hits / rank
    return cum / len(relevant)


def wilson_confidence_interval(
    successes: int, trials: int, confidence: float = 0.95,
) -> tuple[float, float]:
    """Wilson score interval for binomial proportion.

    Preferred over normal approximation: handles small n, extreme p,
    never produces bounds outside [0, 1].
    """
    if trials == 0:
        return (0.0, 1.0)
    z = {0.90: 1.645, 0.95: 1.960, 0.99: 2.576}.get(confidence, 1.960)
    p_hat = successes / trials
    center = (p_hat + z * z / (2 * trials)) / (1 + z * z / trials)
    margin = (z / (1 + z * z / trials)) * math.sqrt(
        p_hat * (1 - p_hat) / trials + z * z / (4 * trials * trials)
    )
    return (max(0.0, center - margin), min(1.0, center + margin))


def keyword_coverage(body: str, summary: str) -> float:
    """Fraction of content-bearing body tokens found in summary."""
    STOP = {"the", "a", "an", "is", "are", "was", "were", "be", "been",
            "have", "has", "had", "do", "does", "did", "and", "or", "but",
            "in", "on", "at", "to", "for", "of", "with", "by", "from"}

    def tokenize(text: str) -> set[str]:
        tokens = re.findall(r"\b[a-z][a-z0-9_-]*\b", text.lower())
        return {t for t in tokens if t not in STOP and len(t) > 2}

    body_t = tokenize(body)
    if not body_t:
        return 1.0
    return len(body_t & tokenize(summary)) / len(body_t)


def named_entity_hallucination_rate(body: str, summary: str) -> float:
    """Fraction of PascalCase/camelCase tokens in summary not in body."""
    def extract(text: str) -> set[str]:
        return set(re.findall(r"\b[A-Z][a-zA-Z0-9]+\b", text)) | \
               set(re.findall(r"\b[a-z]+[A-Z][a-zA-Z0-9]*\b", text))

    summary_ents = extract(summary)
    if not summary_ents:
        return 0.0
    return len(summary_ents - extract(body)) / len(summary_ents)
