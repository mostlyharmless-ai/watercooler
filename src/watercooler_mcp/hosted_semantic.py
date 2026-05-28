"""Plan v20 Phase 8: hosted-side T1 semantic access.

Pure functions over the hosted FalkorDB — `<org>_<repo>_t1` — exposing:

- ``upsert_embedding(database, entry_id, topic, embedding, group_id)`` —
  called by the hosted ``watercooler_semantic`` MCP tool (``action="upsert"``).
- ``delete_embedding(database, entry_id, group_id)`` — hosted delete.
- ``search_semantic_entries(database, group_id, query_embedding, ...)`` —
  hosted semantic-entry search backing ``watercooler_search(mode="entries",
  semantic=True)`` on the hosted surface.
- ``find_similar_t1(database, entry_id, group_id, limit, threshold)`` —
  hosted ``watercooler_find_similar`` backing (was ``not_supported_hosted``
  before Phase 8).

Schema (matches ``watercooler/baseline_graph/falkordb_entries.py``):

- ``(:Entry {entry_id, thread_topic, group_id, embedding})`` — embedding
  stored directly on the Entry node as a vecf32.
- Vector HNSW index on ``Entry.embedding`` (cosine similarity).
- Range index on ``Entry(entry_id, group_id, thread_topic)``.
- Queried via ``CALL db.idx.vector.queryNodes('Entry', 'embedding', …)``.

All helpers open a short-lived connection; the hosted MCP runtime does not
cache FalkorDB clients the way the local worker does.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

FALKOR_URL_ENV = "FALKORDB_URL"
FALKOR_HOST_ENV = "FALKORDB_HOST"
FALKOR_PORT_ENV = "FALKORDB_PORT"
FALKOR_USERNAME_ENV = "FALKORDB_USERNAME"
FALKOR_PASSWORD_ENV = "FALKORDB_PASSWORD"
DEFAULT_FALKOR_URL = "redis://localhost:6379"

# PR #654 in-PR review round 7 (HIGH): the prior implementation created a
# fresh ``FalkorDB(...)`` client (with its own redis-py ``ConnectionPool``)
# on every call. Long-running hosted deployments would leak a pool per
# upsert/delete/search, exhausting FalkorDB TCP connections under any real
# hybrid traffic and causing silent write failures (submit_failed receipts
# with no hosted success). Keep a process-wide singleton keyed on the
# resolved URL + credentials so every hosted request reuses the same pool.
#
# Round 13/18 (MEDIUM): the (client, key) pair is stored as one tuple
# so a hot-path reader sees either the old tuple or the new one, never
# a half-updated (old_key, new_client) mismatch. Under CPython with
# the GIL this works because a module-level attribute read is a single
# bytecode (LOAD_GLOBAL). Under free-threaded CPython (PEP703, 3.13+)
# attribute-load atomicity is NOT guaranteed — two concurrent readers
# could in principle see a torn pointer — so the fallback below always
# re-reads under ``_FALKOR_CLIENT_LOCK`` before writing, and the hot
# path's stale read is acceptable (it just falls through to the locked
# slow path). The practical risk is low today; the guarantee we rely
# on is "no torn (key, client) pair is observable", which the single-
# tuple storage gives us regardless of thread model.
_FALKOR_CLIENT_LOCK = threading.Lock()
_FALKOR_CLIENT_STATE: Optional[tuple] = None  # (key_tuple, client) or None


def _parse_falkor_url(url: str) -> Dict[str, Any]:
    """Split ``redis://[user:pass@]host[:port]`` into FalkorDB() kwargs."""
    parsed = urlparse(url)
    kwargs: Dict[str, Any] = {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 6379,
    }
    if parsed.username:
        kwargs["username"] = parsed.username
    if parsed.password:
        kwargs["password"] = parsed.password
    return kwargs


def _resolve_falkor_kwargs() -> Dict[str, Any]:
    """Resolve FalkorDB connection kwargs from env vars.

    Honors the canonical ``FALKORDB_HOST`` / ``FALKORDB_PORT`` /
    ``FALKORDB_USERNAME`` / ``FALKORDB_PASSWORD`` env-var contract used
    by the rest of the system (see ``watercooler/memory_config.py`` and
    ``watercooler/config_loader.py``). Falls back to ``FALKORDB_URL``
    parsing for legacy configurations, then to ``DEFAULT_FALKOR_URL``.

    Why: hosted Graphiti reads ``FALKORDB_HOST`` and successfully
    resolves the live Railway hostname; ``hosted_semantic`` was the
    only consumer using the divergent ``FALKORDB_URL`` convention,
    which silently fell back to a stale hardcoded Railway internal
    hostname after the FalkorDB service was renamed — breaking
    ``find_similar`` / hosted semantic upsert+search end-to-end.
    """
    host = os.environ.get(FALKOR_HOST_ENV, "").strip()
    if host:
        port_raw = os.environ.get(FALKOR_PORT_ENV, "").strip()
        try:
            port = int(port_raw) if port_raw else 6379
        except ValueError:
            port = 6379
        kwargs: Dict[str, Any] = {"host": host, "port": port}
        username = os.environ.get(FALKOR_USERNAME_ENV, "").strip()
        password = os.environ.get(FALKOR_PASSWORD_ENV, "").strip()
        if username:
            kwargs["username"] = username
        if password:
            kwargs["password"] = password
        return kwargs

    url = os.environ.get(FALKOR_URL_ENV, DEFAULT_FALKOR_URL)
    return _parse_falkor_url(url)


def _get_falkor_client() -> Any:
    """Return the process-wide FalkorDB client, creating it lazily.

    Reusing a single client across calls means the underlying redis-py
    ``ConnectionPool`` is shared, which is the idiomatic way to bound the
    number of live TCP connections to FalkorDB.

    (client, key) is stored as one tuple and replaced atomically so the
    hot-path read cannot observe a torn state under free-threaded
    CPython (PEP703, 3.13+).
    """
    global _FALKOR_CLIENT_STATE
    try:
        from falkordb import FalkorDB  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "falkordb SDK not installed on the hosted side — "
            "install 'falkordb' to enable hosted semantic access"
        ) from e

    kwargs = _resolve_falkor_kwargs()
    # Round 19 (MEDIUM): the cache key needs to detect credential
    # rotation but MUST NOT retain the password in plaintext in RAM
    # for the process lifetime. A SHA-256 digest of (username,
    # password) is a one-way token: equal iff creds unchanged, but
    # reveals nothing about the password to a heap dump or debug
    # introspection.
    import hashlib as _hashlib
    cred_material = (
        f"{kwargs.get('username') or ''}\x00{kwargs.get('password') or ''}"
    )
    cred_digest = _hashlib.sha256(cred_material.encode("utf-8")).hexdigest()
    key = (
        kwargs.get("host"),
        kwargs.get("port"),
        cred_digest,
    )

    # Single attribute read → the (key, client) pair cannot mismatch.
    state = _FALKOR_CLIENT_STATE
    if state is not None and state[0] == key:
        return state[1]

    with _FALKOR_CLIENT_LOCK:
        state = _FALKOR_CLIENT_STATE
        if state is not None and state[0] == key:
            return state[1]
        # Round 15 (MEDIUM): close the previous client's connection pool
        # before replacing it so a credential rotation doesn't leak TCP
        # sockets that outlive GC.
        if state is not None:
            _close_client_quietly(state[1])
        client = FalkorDB(**kwargs)
        _FALKOR_CLIENT_STATE = (key, client)
        logger.debug(
            "HOSTED_T1: initialised singleton FalkorDB client (host=%s port=%s)",
            kwargs.get("host"), kwargs.get("port"),
        )
        return client


def _close_client_quietly(client: Any) -> None:
    """Best-effort close of a FalkorDB client's connection pool.

    The FalkorDB SDK wraps redis-py; the underlying ``connection_pool``
    has ``disconnect()``. Different SDK versions expose this at different
    paths, so we probe a couple before giving up.
    """
    for attr_chain in (
        ("close",),
        ("connection_pool", "disconnect"),
        ("client", "connection_pool", "disconnect"),
    ):
        try:
            target: Any = client
            for attr in attr_chain:
                target = getattr(target, attr)
            if callable(target):
                target()
                return
        except Exception:
            continue
    # Nothing worked — not fatal, GC will eventually reclaim.
    logger.debug("HOSTED_T1: no close() hook found on rotated FalkorDB client")


def _reset_falkor_client_for_tests() -> None:
    """Test-only helper to drop the singleton."""
    global _FALKOR_CLIENT_STATE
    with _FALKOR_CLIENT_LOCK:
        if _FALKOR_CLIENT_STATE is not None:
            _close_client_quietly(_FALKOR_CLIENT_STATE[1])
        _FALKOR_CLIENT_STATE = None


import re as _re

# Round 20 (MEDIUM): database names are the sole tenant-scope axis at
# the FalkorDB layer. Anything that routes a request into
# ``select_graph(<caller-supplied>)`` without validating the string
# matches the canonical ``<alnum/_/-/.>_t[12]`` shape is a scope bypass
# waiting to happen — a stdio/dev caller (no http_ctx guard) could pass
# any string as ``group_id``, which reaches us here. The bash admin
# wrapper already has ``_is_safe_graph_name``; this is its Python peer.
_SAFE_DATABASE_NAME_RE = _re.compile(r"^[a-z0-9_]+_t[12]$")


def _assert_safe_database_name(database: str) -> None:
    """Raise if ``database`` contains characters we don't want shipped to FalkorDB."""
    if not database or not _SAFE_DATABASE_NAME_RE.match(database):
        raise ValueError(
            f"Refusing to use FalkorDB database name {database!r}: must "
            "match [A-Za-z0-9_.-]+"
        )


def _select_graph(database: str) -> Any:
    """Return a FalkorDB Graph bound to ``database`` using the sync SDK.

    Uses the process-wide singleton client so the underlying redis-py
    ``ConnectionPool`` is reused across upsert / delete / search /
    find_similar calls. See :func:`_get_falkor_client` for the rationale.
    """
    _assert_safe_database_name(database)
    client = _get_falkor_client()
    return client.select_graph(database)


# Process-scoped cache: databases whose Entry vector + range indexes
# we've already attempted to create (success OR "already exists" — both
# are fine). Keeps the hot path cheap.
#
# PR #656 review (MEDIUM): mirror the locking discipline used by
# ``_FALKOR_CLIENT_STATE``. Under free-threaded CPython (PEP 703) the
# membership-test + mutation here are not atomic; two concurrent
# first-touches for the same database could both pass the cache check
# and issue duplicate ``CREATE INDEX`` queries. Under standard CPython
# the GIL makes this benign (the "already exists" swallow handles it),
# but inconsistency with the rest of the module's threading discipline
# is a latent gap we close here.
_ENSURED_INDEXES: set[str] = set()
_ENSURED_INDEXES_LOCK = threading.Lock()


def _ensure_entry_indexes(graph: Any, database: str) -> None:
    """Create the vector + range indexes on :Entry if they don't exist.

    Plan v20 Phase 8 previously relied on ``db.idx.vector.queryNodes``
    against an index that was never bootstrapped on the hosted side —
    first query returned ``Invalid arguments for procedure
    'db.idx.vector.queryNodes'`` because no vector index existed on
    ``(:Entry).embedding``. Fix: call this at the start of every write
    AND read path so the index is present the first time a query runs.
    The module-level ``_ENSURED_INDEXES`` cache keeps the fast path
    cheap after bootstrap.

    PR #656 review (MEDIUM, both reviewers): originally called only
    from ``upsert_embedding``. A fresh hosted database that received
    its first access via ``search_semantic_entries`` /
    ``find_similar_t1`` would still hit "Invalid arguments". The fix
    now calls this from every entry point that issues a vector-index
    query.

    Idempotent by design: FalkorDB raises on duplicate index creation
    and we swallow "already indexed / already exists" errors. Any
    other failure is logged and re-raised so the caller gets a
    ``falkor_error`` structured result.

    The dimension is read from ``EMBEDDING_DIM`` (the same env var
    everything else uses to size vectors) with a 1024 fallback matching
    the local ``falkordb_entries.FalkorDBEntryStore.ensure_index``
    default.
    """
    # Fast path: hot read under the lock so a free-threaded reader can't
    # observe a torn ``set`` during ``add()``.
    with _ENSURED_INDEXES_LOCK:
        if database in _ENSURED_INDEXES:
            return

    try:
        dim = int(os.environ.get("EMBEDDING_DIM", "1024"))
    except ValueError:
        dim = 1024

    vector_q = (
        f"CREATE VECTOR INDEX FOR (n:Entry) ON (n.embedding) "
        f"OPTIONS {{dimension: {dim}, similarityFunction: 'cosine'}}"
    )
    range_q = (
        "CREATE INDEX FOR (n:Entry) ON (n.entry_id, n.group_id, n.thread_topic)"
    )

    for q in (vector_q, range_q):
        try:
            graph.query(q)
        except Exception as e:
            msg = str(e).lower()
            if "already indexed" in msg or "already exists" in msg:
                continue
            logger.warning(
                "HOSTED_T1: ensure_entry_indexes: %s (query=%s)", e, q[:60]
            )
            raise

    with _ENSURED_INDEXES_LOCK:
        _ENSURED_INDEXES.add(database)


def upsert_embedding(
    *,
    database: str,
    entry_id: str,
    topic: str,
    embedding: List[float],
    group_id: str = "",
    role: str = "",
    entry_type: str = "",
    agent: str = "",
    timestamp: str = "",
) -> Dict[str, Any]:
    """Upsert the HNSW-indexed embedding for ``entry_id`` in the hosted T1.

    Codex re-review 01KPZ47AYVR56NF0PTNAK4NQWH §1: materialises role /
    entry_type / agent / timestamp on the Entry node so hosted semantic
    search can filter with parity against the hosted keyword path.
    """
    # Guard order: database → entry_id → group_id → embedding.
    # Symmetric with delete_embedding and find_similar_t1 so operators
    # see a consistent error code when an argument is missing.
    if not database:
        return {"success": False, "error": "missing_database"}
    if not entry_id:
        return {"success": False, "error": "missing_entry_id"}
    if not group_id:
        # MERGE keys on entry_id only, so a call with ``group_id=""``
        # would unconditionally overwrite an existing entry's tenant
        # scope with the empty string, stripping its searchability.
        return {"success": False, "error": "missing_group_id"}
    if not embedding:
        return {"success": False, "error": "missing_embedding"}

    try:
        graph = _select_graph(database)
    except Exception as e:
        return {"success": False, "error": f"connect_failed: {e}"}

    # Plan v20 Phase 8: ensure the :Entry vector + range indexes exist
    # before the first write lands. First upsert against a fresh hosted
    # graph previously produced nodes with no queryable index, so
    # subsequent ``db.idx.vector.queryNodes`` calls failed with
    # "Invalid arguments". Module-level cache skips the CREATE after
    # bootstrap.
    try:
        _ensure_entry_indexes(graph, database)
    except Exception as e:
        return {"success": False, "error": f"ensure_index_failed: {e}"}

    query = (
        "MERGE (n:Entry {entry_id: $entry_id}) "
        "SET n.thread_topic = $thread_topic, "
        "    n.group_id = $group_id, "
        "    n.role = $role, "
        "    n.entry_type = $entry_type, "
        "    n.agent = $agent, "
        "    n.timestamp = $timestamp, "
        "    n.embedding = vecf32($embedding) "
        "RETURN n.entry_id"
    )
    params = {
        "entry_id": entry_id,
        "thread_topic": topic,
        "group_id": group_id,
        "role": role or "",
        "entry_type": entry_type or "",
        "agent": agent or "",
        # PR #654 in-PR review round 4 (LOW §2): canonicalise to
        # ``YYYY-MM-DDTHH:MM:SSZ`` UTC so range comparisons sort
        # correctly regardless of input format.
        "timestamp": _canonicalize_timestamp(timestamp),
        "embedding": list(embedding),
    }

    # Round 22 (LOW): if the caller passed a non-empty timestamp that
    # couldn't be parsed, the stored value falls back to "" and the
    # entry will be invisible to ``start_time`` / ``end_time`` range
    # queries. Surface that to the caller in the response.
    timestamp_unparseable = bool(timestamp) and params["timestamp"] == ""

    try:
        graph.query(query, params)
    except Exception as e:
        logger.warning("HOSTED_T1: upsert failed for %s: %s", entry_id, e)
        return {"success": False, "error": f"falkor_error: {e}"}

    result: Dict[str, Any] = {
        "success": True,
        "status": "upserted",
        "entry_id": entry_id,
    }
    if timestamp_unparseable:
        result["timestamp_unparseable"] = timestamp
        result["warning"] = (
            "timestamp was not parseable as ISO-8601; stored as empty. "
            "Range-filtered searches will not match this entry."
        )
    return result


def delete_embedding(
    *,
    database: str,
    entry_id: str,
    group_id: str = "",
    topic: str = "",
) -> Dict[str, Any]:
    if not database:
        return {"success": False, "error": "missing_database"}
    if not entry_id:
        return {"success": False, "error": "missing_entry_id"}
    # Round 22 (MEDIUM): require group_id on the delete path, matching
    # the upsert/search guards. Without it, a caller could delete any
    # tenant's entry with this entry_id. ULID collision is improbable,
    # but the sibling functions already enforce the scope; the delete
    # contract should too.
    if not group_id:
        return {"success": False, "error": "missing_group_id"}

    try:
        graph = _select_graph(database)
    except Exception as e:
        return {"success": False, "error": f"connect_failed: {e}"}

    # Scope to (entry_id, group_id, [thread_topic]) to avoid cross-project
    # / cross-thread collisions. group_id is now required (round 22);
    # topic is optional since some delete paths genuinely don't know it.
    clauses: List[str] = [
        "n.entry_id = $entry_id",
        "n.group_id = $group_id",
    ]
    params: Dict[str, Any] = {
        "entry_id": entry_id,
        "group_id": group_id,
    }
    if topic:
        clauses.append("n.thread_topic = $thread_topic")
        params["thread_topic"] = topic

    where = " AND ".join(clauses)
    # PR #654 in-PR review round 5 (HIGH §2): the prior form did
    # ``SET n.embedding = null`` which left the Entry node in place with
    # a null vector. Every delete would leak a ghost node, inflating node
    # counts (and Railway memory) with no recovery path. Match the local
    # path's semantics (falkordb_entries.delete_embedding:499) and DELETE
    # the node. T1 Entry has no outgoing/incoming relationships in the
    # canonical T1 graph, so DELETE is safe; use DETACH DELETE as a
    # defense-in-depth against future schema additions.
    query = f"MATCH (n:Entry) WHERE {where} DETACH DELETE n"

    try:
        graph.query(query, params)
    except Exception as e:
        return {"success": False, "error": f"falkor_error: {e}"}

    return {"success": True, "status": "deleted", "entry_id": entry_id}


def search_semantic_entries(
    *,
    database: str,
    query_embedding: List[float],
    group_id: str,
    limit: int = 10,
    similarity_threshold: float = 0.0,
    thread_topic: Optional[str] = None,
    role: Optional[str] = None,
    entry_type: Optional[str] = None,
    agent: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> Dict[str, Any]:
    """HNSW KNN search against ``Entry.embedding``.

    The local reader (falkordb_entries.search_similar) converts FalkorDB
    cosine distance (0..2) to similarity as ``1 - distance / 2``. We do the
    same here so clients can uniformly filter on ``similarity_threshold``
    in [0, 1].

    Filter parity with the hosted keyword path (``search_entries_hosted``
    at ``src/watercooler_mcp/hosted_ops.py:3093-3106``):

    - ``role`` — case-insensitive EXACT match (``toLower == toLower``)
    - ``entry_type`` — case-insensitive EXACT match
    - ``agent`` — case-insensitive SUBSTRING match (``CONTAINS``)
    - ``start_time`` / ``end_time`` — raw ISO-8601 string comparison
    - ``thread_topic`` — exact match (case-sensitive, as with the
      keyword path via GitHub's indexer)

    The Entry node now materialises ``role``, ``entry_type``, ``agent``,
    ``timestamp`` at upsert time (Codex re-review §1), so these filters
    land in the Cypher ``WHERE`` and still return non-null matches. The
    query over-fetches (3x) to absorb filter-induced loss before the
    final ``LIMIT``.
    """
    if not database:
        return {"error": "missing_database", "results": []}
    if not group_id:
        return {"error": "missing_group_id", "results": []}
    if not query_embedding:
        return {"error": "missing_query_embedding", "results": []}
    # Cap at 51 (not 50) so ``find_similar_t1`` — which over-fetches by
    # one to filter out the source entry — can still return the
    # caller-requested 50. Above 51 is a runaway-query guard.
    limit = max(1, min(limit, 51))

    try:
        graph = _select_graph(database)
    except Exception as e:
        return {"error": f"connect_failed: {e}", "results": []}

    # PR #656 review: also bootstrap on the read path. A search-first
    # access on a fresh database (e.g., post-restart with a stale cache,
    # or post-migration) would otherwise return "Invalid arguments for
    # procedure 'db.idx.vector.queryNodes'" because the index was never
    # created.
    try:
        _ensure_entry_indexes(graph, database)
    except Exception as e:
        return {"error": f"ensure_index_failed: {e}", "results": []}

    # Base over-fetch: 3x so post-KNN filtering (role/entry_type/agent/
    # timestamps) still has room to satisfy ``limit``.
    k = limit * 3

    clauses: List[str] = ["node.group_id = $group_id"]
    params: Dict[str, Any] = {
        "k": k,
        "q": list(query_embedding),
        "group_id": group_id,
    }
    if thread_topic:
        clauses.append("node.thread_topic = $thread_topic")
        params["thread_topic"] = thread_topic
    if role:
        # Case-insensitive EXACT match (parity with hosted_ops.py:3094-3096).
        clauses.append("toLower(node.role) = toLower($role)")
        params["role"] = role
    if entry_type:
        # Case-insensitive EXACT match (parity with hosted_ops.py:3097-3099).
        clauses.append("toLower(node.entry_type) = toLower($entry_type)")
        params["entry_type"] = entry_type
    if agent:
        # Case-insensitive SUBSTRING match (parity with hosted_ops.py:3100-3102).
        clauses.append("toLower(node.agent) CONTAINS toLower($agent)")
        params["agent"] = agent
    # Query-side canonicalisation matches the upsert-side form so range
    # bounds sort correctly regardless of the format the caller uses.
    # PR #654 in-PR review round 6 (LOW): drop the clause rather than
    # poison the result set when canonicalisation fails.
    # Round 16 (MEDIUM): surface the drop to the caller via
    # ``filters_dropped`` so a client passing an invalid timestamp
    # doesn't get back a same-shape response silently missing the
    # filter.
    filters_dropped: List[str] = []
    if start_time:
        canonical_start = _canonicalize_timestamp(start_time)
        if canonical_start:
            clauses.append("node.timestamp >= $start_time")
            params["start_time"] = canonical_start
        else:
            logger.warning(
                "HOSTED_T1: start_time=%r is not parseable as ISO-8601; "
                "dropping clause rather than returning wrong results.",
                start_time,
            )
            filters_dropped.append("start_time")
    if end_time:
        canonical_end = _canonicalize_timestamp(end_time)
        if canonical_end:
            clauses.append("node.timestamp <= $end_time")
            params["end_time"] = canonical_end
        else:
            logger.warning(
                "HOSTED_T1: end_time=%r is not parseable as ISO-8601; "
                "dropping clause rather than returning wrong results.",
                end_time,
            )
            filters_dropped.append("end_time")

    where = " AND ".join(clauses)
    query = (
        "CALL db.idx.vector.queryNodes('Entry', 'embedding', $k, "
        "vecf32($q)) YIELD node, score "
        f"WHERE {where} "
        "RETURN node.entry_id AS entry_id, node.thread_topic AS topic, "
        "node.role AS role, node.entry_type AS entry_type, "
        "node.agent AS agent, node.timestamp AS timestamp, score "
        "ORDER BY score ASC LIMIT $k"
    )

    try:
        result = graph.query(query, params)
    except Exception as e:
        return {"error": f"falkor_error: {e}", "results": []}

    # Materialise rows ONCE. Some SDK versions expose ``result.result_set``
    # as a single-shot iterator; calling _rows(result) twice would yield
    # an empty second pass and break the ``knn_budget_exhausted`` signal
    # below.
    #
    # Cypher ``ORDER BY score ASC`` ordering: FalkorDB's
    # ``db.idx.vector.queryNodes`` returns cosine DISTANCE (0 = identical,
    # 2 = opposite). Ascending distance → most-similar first → correct
    # contract for the caller. We also sort by similarity DESC below as
    # belt-and-braces against any SDK layer that might reorder rows on
    # the way back.
    raw_rows = list(_rows(result))
    results = []
    for row in raw_rows:
        try:
            entry_id = str(row[0])
            topic = str(row[1])
            r_role = row[2]
            r_entry_type = row[3]
            r_agent = row[4]
            r_timestamp = row[5]
            distance = float(row[6])
        except (TypeError, ValueError, IndexError):
            continue
        similarity = max(0.0, 1.0 - (distance / 2.0))
        if similarity < similarity_threshold:
            continue
        results.append(
            {
                "entry_id": entry_id,
                "topic": topic,
                "role": r_role,
                "entry_type": r_entry_type,
                "agent": r_agent,
                "timestamp": r_timestamp,
                "similarity": similarity,
            }
        )
        if len(results) >= limit:
            break

    # Defensive final sort: most-similar first. Belt-and-braces against
    # any SDK layer that might disturb the Cypher-side ordering.
    results.sort(key=lambda r: r.get("similarity", 0.0), reverse=True)

    response: Dict[str, Any] = {
        "count": len(results),
        "method": "hosted_t1_hnsw",
        "threshold": similarity_threshold,
        "results": results,
    }
    if filters_dropped:
        # Round 16 (MEDIUM): surface silently-dropped filters so the
        # caller can distinguish "applied filter yielded N" from
        # "unparseable filter was ignored, returning N unfiltered".
        response["filters_dropped"] = filters_dropped
    # Round 20 (LOW): metadata filters use ``toLower(node.role) =
    # toLower($role)`` which evaluates falsy when the node property is
    # NULL. Pre-Phase-8 entries (upserted before role/entry_type/agent/
    # timestamp were materialised) are silently excluded from filtered
    # searches. No migration backfills these fields. Surface the fact
    # that a metadata filter was applied so callers can decide whether
    # to accept the possibly-undercounted result or re-run without
    # filters for a fuller view. ``thread_topic`` predates Phase 8 and
    # has always been materialised, so it doesn't count as a
    # metadata-era filter.
    metadata_filters_applied = [
        name for name, value in (
            ("role", role),
            ("entry_type", entry_type),
            ("agent", agent),
            ("start_time", start_time),
            ("end_time", end_time),
        )
        if value
    ]
    if metadata_filters_applied:
        response["metadata_filters_applied"] = metadata_filters_applied
        response["metadata_filter_note"] = (
            "Entries upserted before Phase 8 materialised metadata "
            "properties will be silently excluded. Re-run without "
            "these filters for a pre-Phase-8-inclusive view."
        )
    # Round 18 (MEDIUM): if FalkorDB returned the full over-fetch worth
    # of rows AND we still couldn't fill ``limit``, the KNN budget was
    # the bottleneck. Reuses the materialised ``raw_rows`` from above.
    if len(raw_rows) >= k and len(results) < limit:
        response["knn_budget_exhausted"] = True
    return response


def find_similar_t1(
    *,
    database: str,
    entry_id: str,
    group_id: str,
    limit: int = 5,
    similarity_threshold: float = 0.5,
) -> Dict[str, Any]:
    """Hosted ``find_similar``: KNN from ``entry_id``'s stored embedding."""
    # Cap the caller-visible ``limit`` to 50 so ``limit + 1`` (the
    # over-fetch we use to filter out the source entry) fits inside
    # ``search_semantic_entries``'s 51 cap. Defensive even though the
    # MCP wrapper already validates limit; direct callers exist too.
    limit = max(1, min(limit, 50))
    # Guard order mirrors upsert/delete: database → entry_id → group_id.
    if not database:
        return {
            "error": "missing_database",
            "source_entry_id": entry_id,
            "results": [],
        }
    if not entry_id:
        return {
            "error": "missing_entry_id",
            "source_entry_id": entry_id,
            "results": [],
        }
    if not group_id:
        return {
            "error": "missing_group_id",
            "source_entry_id": entry_id,
            "results": [],
        }

    try:
        graph = _select_graph(database)
    except Exception as e:
        return {
            "error": f"connect_failed: {e}",
            "source_entry_id": entry_id,
            "results": [],
        }

    # PR #656 review: bootstrap on the read path too — search-first access
    # to a fresh database would otherwise hit "Invalid arguments for
    # procedure 'db.idx.vector.queryNodes'".
    try:
        _ensure_entry_indexes(graph, database)
    except Exception as e:
        return {
            "error": f"ensure_index_failed: {e}",
            "source_entry_id": entry_id,
            "results": [],
        }

    fetch = (
        "MATCH (n:Entry {entry_id: $entry_id, group_id: $group_id}) "
        "RETURN n.embedding LIMIT 1"
    )
    try:
        result = graph.query(fetch, {"entry_id": entry_id, "group_id": group_id})
    except Exception as e:
        return {
            "error": f"falkor_error: {e}",
            "source_entry_id": entry_id,
            "results": [],
        }

    rows = list(_rows(result))
    if not rows:
        return {
            "error": "no_embedding",
            "message": (
                f"Hosted T1 has no embedding for entry {entry_id} in group "
                f"{group_id}; upsert via watercooler_semantic (action=upsert) "
                "first."
            ),
            "source_entry_id": entry_id,
            "results": [],
        }

    vector = _coerce_vector(rows[0][0])
    if vector is None:
        return {
            "error": "invalid_embedding",
            "source_entry_id": entry_id,
            "results": [],
        }

    # Over-fetch by 1 so we can drop the source entry itself.
    search = search_semantic_entries(
        database=database,
        query_embedding=vector,
        group_id=group_id,
        limit=limit + 1,
        similarity_threshold=similarity_threshold,
    )
    search["source_entry_id"] = entry_id
    search["results"] = [
        r for r in search.get("results", []) if r.get("entry_id") != entry_id
    ][:limit]
    search["count"] = len(search["results"])
    return search


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rows(result: Any) -> Any:
    """Extract the row iterable from a FalkorDB SDK query result.

    The falkordb SDK's ``QueryResult`` exposes the matrix of values on
    ``.result_set`` (a ``list[list[Any]]``). We accept either the SDK
    object or a raw ``[headers, rows, stats]`` triple so unit tests can
    keep using a simple list stub.
    """
    if hasattr(result, "result_set"):
        return result.result_set or []
    try:
        return result[1] or []
    except (IndexError, TypeError):
        return []


def _coerce_vector(value: Any) -> Optional[List[float]]:
    if isinstance(value, list):
        try:
            return [float(v) for v in value]
        except (TypeError, ValueError):
            return None
    return None


def _canonicalize_timestamp(raw: Any) -> str:
    """Normalise an ISO-8601 timestamp to ``YYYY-MM-DDTHH:MM:SSZ`` (UTC).

    PR #654 in-PR review round 4 (LOW §2): ``hosted_semantic`` stores and
    compares timestamps as raw strings, so mixed input formats cause wrong
    ordering on range queries. Canonicalising at both write- and query-
    time guarantees a single lexicographic format.

    Falls back to an empty string on unparseable input (matches prior
    behaviour of silently writing whatever arrived — callers that pass
    ``""`` explicitly continue to get ``""``).
    """
    if raw in (None, ""):
        return ""
    from datetime import datetime, timezone

    s = str(raw).strip()
    # ``fromisoformat`` (3.11+) accepts ``Z``; earlier versions don't.
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s[:-1]).replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(s)
    except ValueError:
        # Last-ditch: try the common offset-less form with a T separator.
        try:
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return ""
    if dt.tzinfo is None:
        # Treat naive timestamps as UTC — it's the least-surprise default
        # for a server that receives headers with no offset.
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Enumeration: paginated dump of all Entry nodes for migration tooling
# ---------------------------------------------------------------------------

def list_embeddings_t1(
    *,
    database: str,
    group_id: str,
    cursor: str = "",
    limit: int = 200,
) -> Dict[str, Any]:
    """Paginated enumeration of Entry nodes in the hosted T1 graph.

    Used by the ``watercooler migrate t1 --to stdio`` migration to pull
    all hosted embeddings down into a local FalkorDB instance.

    Cursor-style pagination keyed on entry_id (deterministic ordering)
    so concurrent writes during the pull return a consistent snapshot
    per page.

    Args:
        database: FalkorDB database name (``<group_id>_t1`` form).
        group_id: Project group_id (without ``_t1`` suffix).
        cursor: ``entry_id`` of the last row from the previous page.
            Empty starts at the beginning.
        limit: Page size (capped to 1000).

    Returns:
        Dict with ``entries`` (list of {entry_id, thread_topic, embedding,
        group_id, role, entry_type, agent, timestamp}) and ``next_cursor``
        (the last entry_id of this page; empty if exhausted).
    """
    limit = max(1, min(int(limit) if limit else 200, 1000))
    if not database:
        return {"error": "missing_database", "entries": [], "next_cursor": ""}
    if not group_id:
        return {"error": "missing_group_id", "entries": [], "next_cursor": ""}

    try:
        graph = _select_graph(database)
    except Exception as e:
        return {"error": f"select_graph_failed: {e}", "entries": [], "next_cursor": ""}

    # Use a strict greater-than predicate so the cursor's own row is not
    # re-emitted on the next page. ``""`` (empty string) sorts before all
    # ULIDs lexicographically, so the first page starts at the true beginning.
    try:
        result = graph.query(
            "MATCH (n:Entry {group_id: $group_id}) "
            "WHERE n.entry_id > $cursor AND exists(n.embedding) "
            "RETURN n.entry_id, n.thread_topic, n.embedding, n.role, "
            "       n.entry_type, n.agent, n.timestamp "
            "ORDER BY n.entry_id "
            "LIMIT $limit",
            {"group_id": group_id, "cursor": cursor, "limit": limit},
        )
    except Exception as e:
        return {"error": f"query_failed: {e}", "entries": [], "next_cursor": ""}

    entries: List[Dict[str, Any]] = []
    # Track the last RAW entry_id (regardless of whether it passes the
    # embedding filter). This is the cursor — it determines where the
    # next page picks up. If we used the post-filter `last_entry_id`,
    # any row dropped by the filter would cause re-fetch / silent
    # truncation: a raw page of `limit` rows with one filtered out
    # would compute `len(entries) < limit` (looks like end-of-stream)
    # AND set the cursor to a stale earlier entry, dropping every entry
    # past the gap.
    raw_count = len(result.result_set)
    last_raw_entry_id = ""
    for row in result.result_set:
        entry_id = str(row[0]) if row[0] is not None else ""
        if entry_id:
            last_raw_entry_id = entry_id  # advance cursor regardless of filter
        if not entry_id:
            continue
        embedding = row[2]
        if not embedding:
            continue
        try:
            emb_list = [float(x) for x in embedding]
        except (TypeError, ValueError):
            continue
        entries.append({
            "entry_id": entry_id,
            "thread_topic": str(row[1]) if row[1] is not None else "",
            "embedding": emb_list,
            "group_id": group_id,
            "role": str(row[3]) if row[3] is not None else "",
            "entry_type": str(row[4]) if row[4] is not None else "",
            "agent": str(row[5]) if row[5] is not None else "",
            "timestamp": str(row[6]) if row[6] is not None else "",
        })

    # End-of-stream test uses the RAW row count (FalkorDB returned fewer
    # than `limit` rows → no more data). Cursor is the last raw entry_id
    # so the next page resumes correctly even if mid-page rows were
    # filtered out.
    next_cursor = last_raw_entry_id if raw_count == limit else ""
    return {
        "entries": entries,
        "next_cursor": next_cursor,
        # Renamed from "page_size" — that was the post-filter entry count
        # which a future caller might compare against `limit` to detect
        # end-of-stream, getting false positives whenever rows were
        # filtered. The unambiguous name is `entries_returned`; for the
        # raw row count (page-fullness signal) callers should consult
        # `next_cursor` semantics: empty cursor = no more pages.
        "entries_returned": len(entries),
    }
