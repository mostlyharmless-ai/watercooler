"""T2 (Graphiti episodes / entities / edges) migration: stdio ↔ hybrid.

T2 is a richer state than T1: per entry, the LLM extracts an Episodic
node + multiple Entity nodes + MENTIONS + RELATES_TO edges with their
own embeddings + temporal metadata. Direct graph-to-graph transport
preserving relationships is non-trivial.

Approach by direction:

- ``stdio_to_hybrid``: thin wrapper around the existing hosted
  ``watercooler_bulk_index`` MCP tool, which enqueues each orphan-branch
  entry for re-extraction on the hosted side. Deterministic chunk-id
  dedup means re-running is idempotent. Cost: hosted LLM extraction
  budget. Wall time depends on hosted queue depth.

- ``hybrid_to_stdio``: NOT IMPLEMENTED in this PR. The canonical path
  is the local ``OPS_T2_REBUILD.md`` runbook: rebuild T2 locally from
  git+T1 via ``watercooler_bulk_index`` against a local-mode MCP. A
  proper transport-style implementation would need a server-side
  ``watercooler_t2_dump`` enumeration tool plus a local
  ``watercooler_t2_restore`` writer with full Episodic/Entity/edge
  semantics and Graphiti-internal-state preservation. Out of scope
  for the v0.4.x cycle; tracked as a follow-on.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from .summary import MigrationSummary
from ._remote import build_premium_client, call_remote_tool

logger = logging.getLogger(__name__)


def _resolve_t2_target_group_id(
    code_path: Optional[str],
    target_group_id: Optional[str],
) -> str:
    """Derive canonical T2 group_id. Database name is server-derived; see t1.py docstring."""
    if target_group_id:
        return target_group_id
    from watercooler.path_resolver import derive_project_group_id
    code_root = Path(code_path or ".").resolve()
    return derive_project_group_id(code_path=code_root)


def migrate_t2_to_hybrid(
    *,
    code_path: Optional[str] = None,
    target_group_id: Optional[str] = None,
    dry_run: bool = False,
    limit: int = 0,
    threads_filter: str = "",
) -> MigrationSummary:
    """Trigger hosted bulk_index for the orphan-branch corpus.

    The hosted side handles enumeration (it reads the orphan branch via
    the GitHub API on its container) and queues each entry through the
    same async pipeline daemons use. Idempotent via deterministic
    chunk-ids; safe to re-run.
    """
    summary = MigrationSummary(
        tier="t2",
        direction="stdio_to_hybrid",
        dry_run=dry_run,
    )
    started = time.monotonic()

    target_group_id_resolved = _resolve_t2_target_group_id(
        code_path, target_group_id
    )
    summary.notes.append(
        f"Target: hosted T2 group_id={target_group_id_resolved} "
        "(server derives database name)"
    )

    if dry_run:
        summary.notes.append(
            "DRY-RUN: would call watercooler_bulk_index "
            f"(threads={threads_filter or 'ALL'}, max_entries={limit or 0}). "
            "No request sent to hosted endpoint."
        )
        summary.elapsed_seconds = round(time.monotonic() - started, 2)
        return summary

    premium = build_premium_client()
    raw = call_remote_tool(
        premium,
        "watercooler_bulk_index",
        # Match the hosted ``_bulk_index_impl`` signature exactly
        # (src/watercooler_mcp/tools/memory.py). The prior call passed
        # a ``confirm=True`` flag that the hosted tool does not accept,
        # causing every real ``migrate t2 --to hybrid`` invocation to
        # fail Pydantic validation with
        # ``Unexpected keyword argument confirm``. Discovered during
        # the test-cjh dogfood after the t1 backfill landed.
        {
            "backend": "graphiti",
            "threads": threads_filter,
            "code_path": "",  # hosted resolves via http_ctx.repo header
            "max_entries": limit or 0,
        },
    )

    try:
        resp = json.loads(raw)
    except json.JSONDecodeError:
        summary.errored += 1
        summary.notes.append(f"Unparseable bulk_index response: {raw[:200]}")
        summary.elapsed_seconds = round(time.monotonic() - started, 2)
        return summary

    if "error" in resp:
        summary.errored += 1
        summary.notes.append(f"bulk_index error: {resp['error']}")
    else:
        # The hosted ``_bulk_index_hosted_impl`` response shape is
        # ``{entries_queued, entries_skipped, already_indexed, errors,
        # ...}`` (src/watercooler_mcp/tools/memory.py around L2350).
        # The legacy local impl uses ``queued`` / ``enqueued`` keys.
        # Accept both so the migration tool works against either path.
        # Use explicit "key in resp" rather than ``int(x) or int(y)``
        # so a legitimate zero count is honoured (a response of
        # ``{"entries_queued": 0, "already_indexed": 50}`` should report
        # pushed=0 / skipped=50, not pushed=50).
        if "entries_queued" in resp:
            summary.pushed = int(resp["entries_queued"])
        elif "queued" in resp:
            summary.pushed = int(resp["queued"])
        elif "enqueued" in resp:
            summary.pushed = int(resp["enqueued"])
        else:
            summary.pushed = 0

        if "already_indexed" in resp:
            summary.skipped_already_present = int(resp["already_indexed"])
        else:
            summary.skipped_already_present = int(resp.get("skipped", 0))

        # ``entries_skipped`` is structurally distinct: those entries had
        # an empty body or missing entry_id and were not queued. Surface
        # the count in notes so a user investigating "pushed=0 but I have
        # entries" knows to check entry validity.
        entries_skipped = int(resp.get("entries_skipped", 0))
        if entries_skipped:
            summary.notes.append(
                f"bulk_index skipped {entries_skipped} entries with missing "
                "entry_id or empty body."
            )

        # ``errors`` (when present) is a list of structured error dicts.
        # Any errors mean the migration is partially failed.
        errors = resp.get("errors") or []
        if errors:
            summary.errored += len(errors)
            summary.notes.append(
                f"bulk_index reported {len(errors)} error(s); see hosted logs."
            )

        if summary.pushed > 0:
            summary.notes.append(
                "bulk_index enqueued tasks; hosted memory queue worker "
                "drains asynchronously. Check progress with "
                "watercooler_memory_task_status or "
                "watercooler_daemon_status(daemon='t2_indexer')."
            )
        elif summary.skipped_already_present > 0 and summary.errored == 0:
            summary.notes.append(
                f"All {summary.skipped_already_present} entries already "
                "indexed (deterministic chunk-id dedup); no new tasks "
                "enqueued."
            )

    summary.elapsed_seconds = round(time.monotonic() - started, 2)
    return summary


def migrate_t2_to_stdio(
    *,
    code_path: Optional[str] = None,
    target_group_id: Optional[str] = None,
    dry_run: bool = False,
    limit: int = 0,
) -> MigrationSummary:
    """Pull hosted T2 down into a local FalkorDB graph.

    Not implemented in this release — see module docstring. The
    canonical path is to rebuild T2 locally from git+T1 via the local
    OPS_T2_REBUILD.md runbook (drop-and-rebuild semantics rather than
    transport semantics).
    """
    summary = MigrationSummary(
        tier="t2",
        direction="hybrid_to_stdio",
        dry_run=dry_run,
    )
    summary.notes.append(
        "T2 hybrid→stdio is not yet implemented as a transport. "
        "The canonical path is to rebuild T2 locally from git+T1 via "
        "docs/OPS_T2_REBUILD.md. A future release will add server-side "
        "watercooler_t2_dump and a local writer that preserves Episodic / "
        "Entity / edge semantics."
    )
    # NOT errored — this is an intentionally deferred path, not a failure.
    # Distinguishing the two so scripts that diff "feature deferred" from
    # "partial migration failure" don't conflate them.
    summary.not_implemented = True
    return summary
