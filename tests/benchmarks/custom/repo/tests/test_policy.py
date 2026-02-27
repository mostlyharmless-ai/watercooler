from __future__ import annotations

import hashlib

from custom_bench_app.policy import get_policy_code


def test_policy_code_matches_org_decision() -> None:
  expected_sha256 = "2c0a7b6d174e1ea841fd8fcec5e0c038b29e6cb20e604747f6bcbbb10a7e604f"
  actual = get_policy_code()
  assert isinstance(actual, str)
  assert hashlib.sha256(actual.encode("utf-8")).hexdigest() == expected_sha256

