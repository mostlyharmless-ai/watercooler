from __future__ import annotations


def get_region_retention_policy() -> str:
  """Return the current region retention policy string.

  This value is governed by org decisions, not derivable from the codebase.
  """
  return ""


def get_region_retention_policy_citations() -> list[str]:
  """Return the Watercooler entry IDs that justify the current policy."""
  return []

