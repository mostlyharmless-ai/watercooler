"""Unit tests for federation merger module."""

from datetime import datetime

from watercooler_mcp.federation.merger import (
    ScoredResult,
    build_response_envelope,
    merge_results,
)


def _parse_epoch(ts: str) -> float:
    """Parse ISO 8601 timestamp to epoch float for test helpers."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _make_result(
    entry_id: str = "01ABC",
    namespace: str = "cloud",
    ranking_score: float = 0.5,
    raw_score: float = 1.7,
    timestamp: str = "2026-02-01T00:00:00Z",
    **kwargs,
) -> ScoredResult:
    """Helper to create a ScoredResult with sensible defaults."""
    return ScoredResult(
        entry_id=entry_id,
        origin_namespace=namespace,
        raw_score=raw_score,
        normalized_score=kwargs.get("normalized_score", 0.5),
        namespace_weight=kwargs.get("namespace_weight", 1.0),
        recency_decay=kwargs.get("recency_decay", 1.0),
        ranking_score=ranking_score,
        entry_data=kwargs.get("entry_data", {"title": "test"}),
        timestamp=timestamp,
        timestamp_epoch=_parse_epoch(timestamp),
        group_id=kwargs.get("group_id", ""),
    )


class TestMergeResults:
    """Tests for merge_results."""

    def test_sorted_by_ranking_score_descending(self):
        results = {
            "cloud": [
                _make_result("a1", "cloud", ranking_score=0.8),
                _make_result("a2", "cloud", ranking_score=0.3),
            ],
            "site": [
                _make_result("b1", "site", ranking_score=0.6),
            ],
        }
        merged = merge_results(results, "cloud", limit=10)
        scores = [r.ranking_score for r in merged]
        assert scores == [0.8, 0.6, 0.3]

    def test_tiebreak_primary_first(self):
        results = {
            "cloud": [_make_result("a1", "cloud", ranking_score=0.5)],
            "site": [_make_result("b1", "site", ranking_score=0.5)],
        }
        merged = merge_results(results, "cloud", limit=10)
        assert merged[0].origin_namespace == "cloud"
        assert merged[1].origin_namespace == "site"

    def test_tiebreak_newest_timestamp(self):
        results = {
            "cloud": [
                _make_result("a1", "cloud", ranking_score=0.5, timestamp="2026-01-01T00:00:00Z"),
                _make_result("a2", "cloud", ranking_score=0.5, timestamp="2026-02-01T00:00:00Z"),
            ],
        }
        merged = merge_results(results, "cloud", limit=10)
        assert merged[0].entry_id == "a2"  # Newer
        assert merged[1].entry_id == "a1"  # Older

    def test_filter_below_min_score(self):
        results = {
            "cloud": [
                _make_result("a1", "cloud", ranking_score=0.5),
                _make_result("a2", "cloud", ranking_score=0.005),
            ],
        }
        merged = merge_results(results, "cloud", limit=10, min_score=0.01)
        assert len(merged) == 1
        assert merged[0].entry_id == "a1"

    def test_dedup_by_entry_id(self):
        results = {
            "cloud": [_make_result("same-id", "cloud", ranking_score=0.8)],
            "site": [_make_result("same-id", "site", ranking_score=0.6)],
        }
        merged = merge_results(results, "cloud", limit=10)
        assert len(merged) == 1

    def test_dedup_favors_primary_over_secondary(self):
        """When duplicate entry_id exists across namespaces at equal score,
        the primary version must survive dedup regardless of dict iteration order."""
        # Place secondary FIRST in dict to expose iteration-order sensitivity.
        # An OrderedDict isn't needed — CPython 3.7+ preserves insertion order.
        results = {
            "site": [_make_result("dup-id", "site", ranking_score=0.5)],
            "cloud": [_make_result("dup-id", "cloud", ranking_score=0.5)],
        }
        merged = merge_results(results, "cloud", limit=10)
        assert len(merged) == 1
        assert merged[0].origin_namespace == "cloud"

    def test_truncate_to_limit(self):
        results = {
            "cloud": [
                _make_result(f"a{i}", "cloud", ranking_score=1.0 - i * 0.1)
                for i in range(10)
            ],
        }
        merged = merge_results(results, "cloud", limit=3)
        assert len(merged) == 3

    def test_empty_namespace_results(self):
        results = {"cloud": [], "site": []}
        merged = merge_results(results, "cloud", limit=10)
        assert merged == []

    def test_partial_failure_preserves_ordering(self):
        """Missing namespace doesn't affect remaining ordering."""
        results_with_site = {
            "cloud": [
                _make_result("a1", "cloud", ranking_score=0.8),
                _make_result("a2", "cloud", ranking_score=0.3),
            ],
            "site": [
                _make_result("b1", "site", ranking_score=0.6),
            ],
        }
        results_without_site = {
            "cloud": [
                _make_result("a1", "cloud", ranking_score=0.8),
                _make_result("a2", "cloud", ranking_score=0.3),
            ],
        }

        merged_with = merge_results(results_with_site, "cloud", limit=10)
        merged_without = merge_results(results_without_site, "cloud", limit=10)

        cloud_with = [r.entry_id for r in merged_with if r.origin_namespace == "cloud"]
        cloud_without = [r.entry_id for r in merged_without]
        assert cloud_with == cloud_without


class TestBuildResponseEnvelope:
    """Tests for build_response_envelope."""

    def test_basic_envelope(self):
        results = [_make_result("a1", "cloud", ranking_score=0.8)]
        envelope = build_response_envelope(
            results=results,
            primary_namespace="cloud",
            namespace_status={"cloud": {"status": "ok"}},
            queried_namespaces=["cloud"],
            query="test query",
            total_candidates=1,
        )
        assert envelope["schema_version"] == 1
        assert envelope["query"] == "test query"
        assert envelope["primary_namespace"] == "cloud"
        assert envelope["result_count"] == 1
        assert envelope["total_candidates_before_truncation"] == 1
        assert len(envelope["results"]) == 1
        assert envelope["results"][0]["entry_id"] == "a1"

    def test_score_breakdown_in_results(self):
        results = [_make_result("a1", "cloud", ranking_score=0.8, raw_score=1.7)]
        envelope = build_response_envelope(
            results=results,
            primary_namespace="cloud",
            namespace_status={"cloud": {"status": "ok"}},
            queried_namespaces=["cloud"],
            query="test",
            total_candidates=1,
        )
        breakdown = envelope["results"][0]["score_breakdown"]
        assert "raw_score" in breakdown
        assert "normalized_score" in breakdown
        assert "namespace_weight" in breakdown
        assert "recency_decay" in breakdown

    def test_total_candidates_before_truncation(self):
        results = [_make_result("a1", "cloud")]
        envelope = build_response_envelope(
            results=results,
            primary_namespace="cloud",
            namespace_status={"cloud": {"status": "ok"}},
            queried_namespaces=["cloud"],
            query="test",
            total_candidates=15,
        )
        assert envelope["total_candidates_before_truncation"] == 15
        assert envelope["result_count"] == 1

    def test_namespace_status_preserved(self):
        envelope = build_response_envelope(
            results=[],
            primary_namespace="cloud",
            namespace_status={
                "cloud": {"status": "ok"},
                "site": {"status": "timeout"},
                "docs": {"status": "access_denied"},
            },
            queried_namespaces=["cloud", "site", "docs"],
            query="test",
            total_candidates=0,
        )
        assert envelope["namespace_status"]["site"]["status"] == "timeout"
        assert envelope["namespace_status"]["docs"]["status"] == "access_denied"

    def test_group_id_in_envelope_results(self):
        """group_id appears in each result dict."""
        results = [
            _make_result("a1", "cloud", ranking_score=0.8, group_id="cloud_db"),
            _make_result("b1", "my-site", ranking_score=0.6, group_id="my_site"),
        ]
        envelope = build_response_envelope(
            results=results,
            primary_namespace="cloud",
            namespace_status={"cloud": {"status": "ok"}, "my-site": {"status": "ok"}},
            queried_namespaces=["cloud", "my-site"],
            query="test",
            total_candidates=2,
        )
        assert envelope["results"][0]["group_id"] == "cloud_db"
        assert envelope["results"][1]["group_id"] == "my_site"

    def test_group_id_default_empty_string(self):
        """group_id defaults to empty string when not set."""
        results = [_make_result("a1", "cloud", ranking_score=0.8)]
        envelope = build_response_envelope(
            results=results,
            primary_namespace="cloud",
            namespace_status={"cloud": {"status": "ok"}},
            queried_namespaces=["cloud"],
            query="test",
            total_candidates=1,
        )
        assert envelope["results"][0]["group_id"] == ""
