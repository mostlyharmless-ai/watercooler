from __future__ import annotations

import hashlib

from custom_bench_app.decision_trace import (
  get_region_retention_policy,
  get_region_retention_policy_citations,
)


def test_region_retention_policy_matches_decision_trace() -> None:
  expected_sha256 = "5f4d126812f5711bc0db4eb9c2e2357f7b2bc44f0f4e51e0b8e8dab16e71028b"
  actual = get_region_retention_policy()
  assert isinstance(actual, str)
  assert hashlib.sha256(actual.encode("utf-8")).hexdigest() == expected_sha256


def test_region_retention_policy_citations_match_decision_trace() -> None:
  expected_sha256 = "2613483ad7be500339b8bf2c76e54c7f2c3c20a6d5933a707e7fd686295f596c"
  citations = get_region_retention_policy_citations()
  assert isinstance(citations, list)
  assert citations, "Citations must be non-empty"
  assert all(isinstance(x, str) for x in citations)
  joined = ",".join(sorted(citations))
  assert hashlib.sha256(joined.encode("utf-8")).hexdigest() == expected_sha256

