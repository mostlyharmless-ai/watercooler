"""T1 (entry embedding) migration: stdio ↔ hybrid.

T1 stores per-entry 1024-dim bge-m3 embeddings in FalkorDB Entry nodes.
``stdio_to_hybrid`` ships the local store + orphan-branch metadata up
to hosted; ``hybrid_to_stdio`` pulls the hosted store down to local.

Both directions are idempotent (MERGE on entry_id) and resumable (the
``Checkpoint`` is consulted before each upsert so a kill-and-restart
picks up where it left off).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from .checkpoint import Checkpoint
from .summary import MigrationSummary
from ._local import (
    LocalEntry,
    close_local_falkor,
    connect_local_falkor,
    ensure_local_indexes,
    list_local_entries,
    upsert_local_entry,
)
from ._orphan import discover_threads_dir, scan_orphan_entries
from ._remote import (
    MigrationTransportError,
    RemoteEntry,
    build_premium_client,
    list_remote_embeddings,
    upsert_remote_embedding,
)

logger = logging.getLogger(__name__)


def _iterate_with_transport_guard(iterator, summary):
    """Yield from *iterator*, translating any transport failure into errored.

    The remote enumeration generator raises MigrationTransportError on
    structured transport failure (server error, unparseable response).
    But the underlying premium_client can also raise ConnectionError /
    TimeoutError / generic RuntimeError from the async layer, which
    used to propagate uncaught past this guard, dump a Python traceback
    to the user, and exit with code 1 instead of 2 — defeating the
    JSON-summary contract and leaving any automation parsing it broken.

    Now we catch any Exception (other than StopIteration) and surface
    it via summary.errored + a recovery note, matching the
    `except Exception` discipline used in migrate_t1_to_hybrid's upsert
    loop.
    """
    while True:
        try:
            yield next(iterator)
        except StopIteration:
            return
        except MigrationTransportError as e:
            summary.errored += 1
            summary.notes.append(
                f"Hosted enumeration aborted: {e}. "
                "Pull is incomplete — re-run after resolving the hosted "
                "error to fetch remaining entries."
            )
            logger.warning("MigrationTransportError during pull: %s", e)
            return
        except Exception as e:
            # Any other exception from the transport layer (network blip,
            # async client RuntimeError, etc.). Translate into the same
            # JSON-summary contract so callers parsing stdout don't get
            # a Python traceback and exit code 1.
            summary.errored += 1
            summary.notes.append(
                f"Hosted enumeration failed unexpectedly: {type(e).__name__}: {e}. "
                "Pull is incomplete — re-run after resolving the underlying "
                "transport issue to fetch remaining entries."
            )
            logger.warning("Unexpected %s during pull: %s", type(e).__name__, e)
            return


def _resolve_t1_target_group_id(
    code_path: Optional[str],
    target_group_id: Optional[str],
) -> str:
    """Derive the canonical T1 group_id for the repo.

    The FalkorDB *database name* is derived server-side from the
    group_id (see ``hosted_semantic._derive_database`` — it appends
    ``_t1``); migration callers don't pass the database name. The
    server-side ``_scope_group_id_to_http_ctx`` enforces cross-tenant
    isolation: caller-supplied group_id is overridden by the X-Repo
    header in hybrid hosted mode, accepted as-is in stdio/dev mode.
    """
    if target_group_id:
        return target_group_id
    from watercooler.path_resolver import derive_project_group_id
    code_root = Path(code_path or ".").resolve()
    return derive_project_group_id(code_path=code_root)


def migrate_t1_to_hybrid(
    *,
    code_path: Optional[str] = None,
    local_host: str = "localhost",
    local_port: int = 6379,
    local_password: Optional[str] = None,
    local_graph_name: Optional[str] = None,
    target_group_id: Optional[str] = None,
    embedding_dim: int = 1024,
    checkpoint_path: Optional[Path] = None,
    dry_run: bool = False,
    limit: int = 0,
) -> MigrationSummary:
    """Push local-stored T1 embeddings (+ generate any missing) to hosted T1.

    Algorithm:
      1. Iterate orphan-branch entries (source of truth for entry list)
      2. For each, look up cached embedding in local FalkorDB
      3. Cache hit → push as-is to hosted via premium_client
      4. Cache miss → generate via the configured embedding API, then push
      5. Idempotent + resumable via Checkpoint

    Resume semantics: at-least-once. The checkpoint is written AFTER
    each successful upsert returns. A kill in the (microsecond) window
    between upsert-confirmed and ck.add() leaves the entry upserted but
    unmarked; resume re-pushes that entry. Safe because the hosted
    upsert is MERGE-on-entry_id (idempotent). Pre-marking BEFORE the
    upsert would invert the failure mode — a network failure after the
    pre-mark would silently drop the entry from the resume set, which
    is data loss. At-least-once-with-idempotent-MERGE is the canonical
    pattern for this kind of migration tool.
    """
    from watercooler.baseline_graph.sync import generate_embedding

    summary = MigrationSummary(
        tier="t1",
        direction="stdio_to_hybrid",
        dry_run=dry_run,
    )
    started = time.monotonic()

    target_group_id_resolved = _resolve_t1_target_group_id(
        code_path, target_group_id
    )
    logger.info(
        "Target: hosted T1 group_id=%s (server derives database name)",
        target_group_id_resolved,
    )

    threads_dir = discover_threads_dir(code_path)
    logger.info("Orphan-branch source: %s", threads_dir)

    # Step 1: build cached-embedding map from local FalkorDB.
    # Wrapped in try/finally so the FalkorDB redis-py connection pool is
    # released even if a later step (e.g. build_premium_client) raises.
    # Pre-fix: a build_premium_client() failure on bad config would
    # silently leak a pool connection per invocation, eventually
    # exhausting the local redis-py pool.
    #
    # Default to the canonical group_id-derived graph name so a round-trip
    # `migrate t1 --to stdio` then `--to hybrid` reads from the SAME local
    # graph the pull just wrote to. For users with a legacy-named volume
    # (e.g. the pre-canonical-naming `"watercooler_cloud"` graph that
    # predated Plan v20), pass `--local-graph-name watercooler_cloud`
    # explicitly. Pre-fix: hardcoded `"watercooler_cloud"` here meant
    # round-trip pull-then-push silently re-generated every embedding
    # via the embedding API instead of finding the just-pulled cache.
    local_graph = local_graph_name or target_group_id_resolved
    cache: dict[str, list[float]] = {}
    client = None
    try:
        try:
            client = connect_local_falkor(host=local_host, port=local_port, password=local_password)
            try:
                for entry in list_local_entries(client, graph_name=local_graph):
                    cache[entry.entry_id] = entry.embedding
            except Exception as e:
                summary.notes.append(
                    f"Local FalkorDB enumeration partial (graph={local_graph!r}): {e}"
                )
                logger.warning("Local enumeration failed: %s", e)
        except Exception as e:
            # ``connect_local_falkor`` (in _local.py) wraps the ImportError
            # from `import falkordb` into a RuntimeError with a fixed
            # marker string ("SDK not installed"). Connection failures
            # surface as redis.exceptions.ConnectionError. Discriminate
            # by the marker so the user-facing note doesn't mislabel a
            # missing-dependency as a network problem.
            if "SDK not installed" in str(e):
                kind = "Local FalkorDB SDK not installed"
            else:
                kind = "Local FalkorDB unreachable"
            summary.notes.append(
                f"{kind}: {e} (cache empty; misses will be API-generated)"
            )
            logger.warning("Local FalkorDB cache disabled (%s): %s", kind.lower(), e)

        summary.notes.append(f"Cached embeddings available: {len(cache)}")
        logger.info("Cached embeddings available locally: %d", len(cache))

        # Step 2: build premium client (fail fast if hosted unreachable).
        premium = build_premium_client()
    finally:
        # Always release the local connection pool — even if premium client
        # construction raises (bad config, no hosted endpoint, etc.).
        close_local_falkor(client)

    # Step 3: prepare checkpoint.
    if checkpoint_path is None:
        checkpoint_path = Path("~/.watercooler/migration/t1_to_hybrid_cursor.jsonl").expanduser()
    ck = Checkpoint(checkpoint_path)
    if len(ck) > 0:
        logger.info("Resuming with %d entries already in checkpoint", len(ck))

    # Step 4: iterate orphan-branch entries.
    #
    # Order: ID guard → checkpoint skip → limit guard → increment.
    # This way --limit N counts N entries that we *actually attempt to
    # process* (not N including checkpointed-skips on resume); and
    # total_scanned accurately reflects what was scanned (no off-by-one
    # increment past the limit).
    for entry in scan_orphan_entries(threads_dir):
        eid = entry.get("entry_id")
        if not eid:
            continue
        if eid in ck:
            summary.skipped_already_present += 1
            continue
        if limit and summary.total_scanned >= limit:
            break
        summary.total_scanned += 1

        embedding = cache.get(eid)
        if embedding is not None and len(embedding) == embedding_dim:
            summary.cache_hits += 1
        else:
            summary.api_calls += 1
            if dry_run:
                summary.pushed += 1
                continue
            text = (entry.get("title") or "") + "\n\n" + (entry.get("body") or "")
            # generate_embedding can raise on network blip / rate limit /
            # SDK error. Without this guard the exception escapes the
            # for-loop entirely → reaches cmd_migrate's boundary catch
            # → boundary catch builds a FRESH MigrationSummary →
            # partial counts (potentially thousands of pushed entries)
            # are lost from the user-visible summary. The checkpoint is
            # still sound, but the JSON contract reports pushed=0
            # regardless of how many entries actually landed.
            # Per-entry guard matches the discipline used by every other
            # external-call site in this PR (upsert_remote_embedding,
            # list_remote_embeddings per-row float, upsert_local_entry).
            try:
                embedding = generate_embedding(text)
            except Exception as e:
                summary.errored += 1
                logger.warning(
                    "generate_embedding raised for %s: %s", eid, e,
                )
                continue
            if not embedding or len(embedding) != embedding_dim:
                summary.errored += 1
                logger.warning("Failed embedding for %s", eid)
                continue

        if dry_run:
            summary.pushed += 1
            continue

        remote_entry = RemoteEntry(
            entry_id=eid,
            thread_topic=entry.get("_topic", entry.get("thread_topic", "")),
            embedding=embedding,
            group_id=target_group_id_resolved,
            role=str(entry.get("role") or ""),
            entry_type=str(entry.get("entry_type") or ""),
            agent=str(entry.get("agent") or ""),
            timestamp=str(entry.get("timestamp") or ""),
        )
        try:
            result = upsert_remote_embedding(
                premium,
                target_group_id=target_group_id_resolved,
                entry=remote_entry,
            )
            if result.get("error"):
                summary.errored += 1
                logger.warning("Upsert error for %s: %s", eid, result.get("error"))
                continue
            summary.pushed += 1
            ck.add(eid)
            if summary.pushed % 25 == 0:
                logger.info(
                    "Progress: pushed=%d cache_hits=%d api_calls=%d errored=%d",
                    summary.pushed, summary.cache_hits,
                    summary.api_calls, summary.errored,
                )
        except Exception as e:
            summary.errored += 1
            logger.warning("Upsert raised for %s: %s", eid, e)

    summary.elapsed_seconds = round(time.monotonic() - started, 2)
    return summary


def migrate_t1_to_stdio(
    *,
    code_path: Optional[str] = None,
    local_host: str = "localhost",
    local_port: int = 6379,
    local_password: Optional[str] = None,
    local_graph_name: Optional[str] = None,
    target_group_id: Optional[str] = None,
    embedding_dim: int = 1024,
    checkpoint_path: Optional[Path] = None,
    dry_run: bool = False,
    limit: int = 0,
) -> MigrationSummary:
    """Pull hosted T1 embeddings down into a local FalkorDB graph.

    Resume semantics: same at-least-once-with-idempotent-MERGE as
    to_hybrid (see that docstring). A kill between local_upsert
    confirmation and ck.add() re-writes one entry on resume.

    NOTE: ``cache_hits`` in the summary is meaningless for this
    direction (always 0). It only counts in to_hybrid where a local
    cache might serve an embedding without an API call. There's no
    cache concept in to_stdio — every embedding comes from hosted T1.

    Algorithm:
      1. Build premium_client + iterate hosted T1 via list_remote_embeddings
      2. Open local FalkorDB connection + ensure indexes
      3. For each remote entry: MERGE-upsert into local Entry node
      4. Idempotent + resumable via Checkpoint
    """
    summary = MigrationSummary(
        tier="t1",
        direction="hybrid_to_stdio",
        dry_run=dry_run,
    )
    started = time.monotonic()

    target_group_id_resolved = _resolve_t1_target_group_id(
        code_path, target_group_id
    )
    logger.info(
        "Source: hosted T1 group_id=%s (server derives database name)",
        target_group_id_resolved,
    )

    local_graph = local_graph_name or target_group_id_resolved

    premium = build_premium_client()

    # Mirror the to_hybrid graceful-degradation pattern: catch any local
    # FalkorDB unreachability (server down, refused, DNS, missing SDK)
    # and return a clean MigrationSummary with errored set. Pre-fix the
    # bare connect_local_falkor() crashed with a Python traceback if the
    # local server was down, breaking any script that expected to parse
    # the JSON summary for exit-code decisions.
    local_client = None
    if not dry_run:
        try:
            local_client = connect_local_falkor(
                host=local_host, port=local_port, password=local_password,
            )
            ensure_local_indexes(local_client, graph_name=local_graph, dim=embedding_dim)
        except Exception as e:
            # If connect succeeded but ensure_local_indexes raised
            # (bad dim, transient FalkorDB index-creation error, etc.)
            # we MUST close the open client before the early-return.
            # Pre-fix this branch leaked a redis-py pool connection.
            close_local_falkor(local_client)
            summary.errored += 1
            summary.notes.append(
                f"Local FalkorDB unreachable: {e} "
                f"(host={local_host}:{local_port}). Cannot pull hosted T1 "
                "down without a local target. Start your local FalkorDB "
                "(or pass --local-host/--local-port) and re-run."
            )
            logger.warning("Local FalkorDB connect failed: %s", e)
            summary.elapsed_seconds = round(time.monotonic() - started, 2)
            return summary

    if checkpoint_path is None:
        checkpoint_path = Path("~/.watercooler/migration/t1_to_stdio_cursor.jsonl").expanduser()
    ck = Checkpoint(checkpoint_path)
    if len(ck) > 0:
        logger.info("Resuming with %d entries already in checkpoint", len(ck))

    # Iterate hosted entries. Same checkpoint-then-limit ordering as
    # to_hybrid so resume + --limit semantics stay symmetric. Wrapped in
    # try/finally so the local FalkorDB connection pool is released even
    # if list_remote_embeddings raises mid-stream.
    #
    # MigrationTransportError catch (round-6 review HIGH-1): when the
    # remote enumeration fails mid-pagination, the generator raises
    # instead of silently returning. _iterate_with_transport_guard
    # translates that into a real errored count + descriptive note.
    # Round-7 review MEDIUM-1: the previous outer try/except around
    # list_remote_embeddings(...) was dead code — generator functions
    # don't execute their body until iteration begins, so no exception
    # can fire at the call site. The guard's first-next() catch handles
    # both pre-first-row and mid-stream failures uniformly.
    try:
        iterator = list_remote_embeddings(
            premium,
            target_group_id=target_group_id_resolved,
        )
        for remote in _iterate_with_transport_guard(iterator, summary):
            if remote.entry_id in ck:
                summary.skipped_already_present += 1
                continue
            if limit and summary.total_scanned >= limit:
                break
            summary.total_scanned += 1
            if not remote.embedding or len(remote.embedding) != embedding_dim:
                summary.errored += 1
                logger.warning(
                    "Skipping %s: hosted embedding bad shape (len=%s)",
                    remote.entry_id, len(remote.embedding) if remote.embedding else None,
                )
                continue

            if dry_run:
                summary.pushed += 1
                continue

            local_entry = LocalEntry(
                entry_id=remote.entry_id,
                thread_topic=remote.thread_topic,
                embedding=remote.embedding,
                group_id=target_group_id_resolved,
                role=remote.role,
                entry_type=remote.entry_type,
                agent=remote.agent,
                timestamp=remote.timestamp,
            )
            try:
                upsert_local_entry(local_client, graph_name=local_graph, entry=local_entry)
                summary.pushed += 1
                # NOTE: cache_hits is NOT incremented in to_stdio. It only
                # has meaning for to_hybrid (where a local cache might
                # serve the embedding without an API call). In to_stdio
                # every embedding comes from hosted T1; there's no cache
                # concept. Pre-fix: cache_hits was incremented on every
                # push, making cache_hits == pushed always — a misleading
                # metric for any caller computing a cache-warm rate.
                ck.add(remote.entry_id)
                if summary.pushed % 25 == 0:
                    logger.info("Progress: pushed=%d errored=%d", summary.pushed, summary.errored)
            except Exception as e:
                summary.errored += 1
                logger.warning("Local upsert failed for %s: %s", remote.entry_id, e)
    finally:
        close_local_falkor(local_client)

    summary.elapsed_seconds = round(time.monotonic() - started, 2)
    return summary
