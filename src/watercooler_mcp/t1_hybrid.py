"""Plan v20 Phase 8: hybrid T1 routing for embedding writes.

The MCP layer registers ``upsert_embedding`` / ``delete_embedding`` callbacks
with :mod:`watercooler.baseline_graph.sync`. In hybrid mode those callbacks
forward to the hosted ``watercooler_semantic`` tool (``action="upsert"`` /
``action="delete"``) via ``premium_client`` and append a Stage-A handoff
receipt.

Keeping this in a dedicated module (rather than :mod:`memory_sync`) makes
the T1 vs T2 separation explicit in the MCP tree, mirroring the split-surface
health model from Phase 1.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, List, Optional

from .handoff_receipts import append_handoff_receipt

logger = logging.getLogger(__name__)


# Round 17 (MEDIUM): one shared implementation lives in _async_utils.
from ._async_utils import run_coro_in_fresh_loop as _run_coro_in_fresh_loop


def install_hybrid_callbacks(runtime: Any) -> None:
    """Register hybrid T1 upsert/delete callbacks when the surface is hybrid.

    Idempotent: if the runtime is not ``local_hybrid`` or has no
    ``premium_client``, callbacks are cleared (so re-installing in a
    non-hybrid runtime does not leave stale routing in place).
    """
    from watercooler.baseline_graph import sync as bg_sync

    if (
        runtime is None
        or getattr(runtime, "surface", None) != "local_hybrid"
        or getattr(runtime, "premium_client", None) is None
    ):
        bg_sync.register_t1_remote_embedding_callbacks(upsert=None, delete=None)
        return

    premium = runtime.premium_client

    def _upsert(
        threads_dir: Path,
        entry_id: str,
        topic: str,
        embedding: List[float],
        role: str = "",
        entry_type: str = "",
        agent: str = "",
        timestamp: str = "",
    ) -> bool:
        return _submit_t1_upsert(
            premium=premium,
            threads_dir=threads_dir,
            entry_id=entry_id,
            topic=topic,
            embedding=embedding,
            role=role,
            entry_type=entry_type,
            agent=agent,
            timestamp=timestamp,
        )

    def _delete(threads_dir: Path, entry_id: str, topic: str = "") -> bool:
        return _submit_t1_delete(
            premium=premium,
            threads_dir=threads_dir,
            entry_id=entry_id,
            topic=topic,
        )

    bg_sync.register_t1_remote_embedding_callbacks(upsert=_upsert, delete=_delete)


def _derive_group_id(threads_dir: Path) -> str:
    """Return the canonical ``<org>_<repo>`` project group id.

    Codex review: source the slug from the git remote so the T1 hybrid
    write lands in the canonical hosted database rather than a repo-only
    fallback name.

    PR #654 in-PR review round 7 (MEDIUM): both fallback paths log a
    WARNING now so a misconfigured client that ends up with
    ``group_id="unknown"`` is visible in operator logs rather than
    silently routing writes to an ``unknown_t1`` database. The
    cross-tenant guard in ``_scope_group_id_to_http_ctx`` still rescues
    authenticated HTTP traffic, but stdio / test fixtures / misconfigured
    hybrid clients that bypass that guard now emit a signal.
    """
    try:
        from watercooler.path_resolver import (
            derive_project_group_id,
            derive_repo_slug,
        )

        try:
            repo_slug = derive_repo_slug(threads_dir=threads_dir)
        except Exception as e:
            logger.warning(
                "T1_HYBRID: could not read git remote for %s (%s); "
                "falling back to repo-only slug.",
                threads_dir, e,
            )
            repo_slug = None
        return derive_project_group_id(
            repo_slug=repo_slug, threads_dir=threads_dir
        )
    except Exception as e:
        # Round 14 + round 20 (MEDIUM): fail closed. The prior code had a
        # ``threads_dir.endswith("-threads")`` fallback that returned the
        # bare stem (e.g. "watercooler-cloud" → "watercooler_cloud_t1"),
        # silently routing T1 upserts to a non-canonical database when
        # the git remote was unreadable. Non-canonical is worse than
        # failure: semantic queries would miss those entries and there's
        # no backfill path. Raise and let ``_submit_t1_upsert`` record a
        # submit_failed receipt; operators fix the underlying
        # ``derive_repo_slug`` failure (git remote / threads_dir shape).
        logger.error(
            "T1_HYBRID: canonical group_id derivation failed (%s); "
            "refusing to write to a non-canonical database. Fix the git "
            "remote or resolve the threads_dir path (%r).",
            e, str(threads_dir),
        )
        raise


def _submit_t1_upsert(
    *,
    premium: Any,
    threads_dir: Path,
    entry_id: str,
    topic: str,
    embedding: List[float],
    role: str = "",
    entry_type: str = "",
    agent: str = "",
    timestamp: str = "",
) -> bool:
    try:
        group_id = _derive_group_id(threads_dir)
    except Exception as e:
        # Round 14 (MEDIUM): _derive_group_id now raises rather than
        # returning "unknown" when it cannot resolve the canonical
        # target. Record a submit_failed receipt and refuse — better
        # a missing embedding than one in the wrong database.
        append_handoff_receipt(
            backend="t1_semantic",
            stage="submit_failed",
            entry_id=entry_id,
            topic=topic,
            error=f"group_id_unresolved: {e}",
        )
        return False
    args = {
        "entry_id": entry_id,
        "topic": topic,
        "group_id": group_id,
        "embedding": embedding,
        "role": role,
        "entry_type": entry_type,
        "agent": agent,
        "timestamp": timestamp,
    }
    try:
        text = _run_coro_in_fresh_loop(
            premium.call_tool_text(
                "watercooler_semantic", {"action": "upsert", **args}
            )
        )
    except Exception as e:
        logger.warning(
            "T1_HYBRID: upsert RPC failed for entry %s: %s", entry_id, e
        )
        append_handoff_receipt(
            backend="t1_semantic",
            stage="submit_failed",
            entry_id=entry_id,
            topic=topic,
            group_id=group_id,
            error=f"rpc_failed: {e}",
        )
        return False

    payload = _safe_json(text)
    if not payload.get("success", False):
        append_handoff_receipt(
            backend="t1_semantic",
            stage="submit_failed",
            entry_id=entry_id,
            topic=topic,
            group_id=group_id,
            error=str(payload.get("error") or payload.get("status") or "rejected"),
        )
        return False

    append_handoff_receipt(
        backend="t1_semantic",
        stage="submitted",
        entry_id=entry_id,
        topic=topic,
        group_id=group_id,
        remote_task_id=str(payload.get("remote_task_id") or payload.get("task_id") or ""),
        submission_status=str(payload.get("status") or "upserted"),
    )
    return True


def _submit_t1_delete(
    *,
    premium: Any,
    threads_dir: Path,
    entry_id: str,
    topic: str = "",
) -> bool:
    try:
        group_id = _derive_group_id(threads_dir)
    except Exception as e:
        append_handoff_receipt(
            backend="t1_semantic",
            stage="submit_failed",
            entry_id=entry_id,
            topic=topic,
            error=f"group_id_unresolved: {e}",
            extra={"op": "delete"},
        )
        return False
    args = {"entry_id": entry_id, "group_id": group_id, "topic": topic}
    try:
        text = _run_coro_in_fresh_loop(
            premium.call_tool_text(
                "watercooler_semantic", {"action": "delete", **args}
            )
        )
    except Exception as e:
        logger.warning(
            "T1_HYBRID: delete RPC failed for entry %s: %s", entry_id, e
        )
        append_handoff_receipt(
            backend="t1_semantic",
            stage="submit_failed",
            entry_id=entry_id,
            topic=topic,
            group_id=group_id,
            error=f"rpc_failed: {e}",
            extra={"op": "delete"},
        )
        return False

    payload = _safe_json(text)
    ok = bool(payload.get("success", False))
    append_handoff_receipt(
        backend="t1_semantic",
        stage="submitted" if ok else "submit_failed",
        entry_id=entry_id,
        topic=topic,
        group_id=group_id,
        submission_status=str(payload.get("status") or ("deleted" if ok else "rejected")),
        error=str(payload.get("error", "")) if not ok else "",
        extra={"op": "delete"},
    )
    return ok


def _safe_json(text: str) -> dict:
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return {"success": False, "error": "non_json_response"}
    if not isinstance(payload, dict):
        return {"success": False, "error": "unexpected_payload_shape"}
    return payload
