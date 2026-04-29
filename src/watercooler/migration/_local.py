"""Local FalkorDB enumeration / write helpers for migration.

Wraps the existing ``FalkorDBEntryStore`` with simple sync iterators
suitable for migration loops. Uses the FalkorDB Python SDK directly
(no redis-cli subprocess parsing), so output format quirks across
FalkorDB versions / TTY modes don't matter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional

logger = logging.getLogger(__name__)


@dataclass
class LocalEntry:
    """Single Entry-node row pulled from a local FalkorDB graph."""

    entry_id: str
    thread_topic: str
    embedding: list[float]
    group_id: str = ""
    role: str = ""
    entry_type: str = ""
    agent: str = ""
    timestamp: str = ""


def connect_local_falkor(
    *,
    host: str = "localhost",
    port: int = 6379,
    password: Optional[str] = None,
):
    """Open a sync FalkorDB Python client. Caller must close via ``close_local_falkor``."""
    try:
        from falkordb import FalkorDB
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "falkordb Python SDK not installed. "
            "Run `pip install falkordb` (or `uv sync` in this repo)."
        ) from e
    kwargs = {"host": host, "port": port}
    if password:
        kwargs["password"] = password
    return FalkorDB(**kwargs)


def close_local_falkor(client) -> None:
    """Best-effort close of a FalkorDB client's underlying redis-py pool.

    The FalkorDB SDK wraps redis-py; the connection_pool's ``disconnect()``
    is the canonical way to free the pool. Different SDK versions expose
    this at slightly different paths, so we probe a few before giving up.
    Safe to call with ``None``.
    """
    if client is None:
        return
    for attr_chain in (
        ("close",),
        ("connection_pool", "disconnect"),
        ("client", "connection_pool", "disconnect"),
    ):
        try:
            target = client
            for attr in attr_chain:
                target = getattr(target, attr)
            if callable(target):
                target()
                return
        except Exception:
            continue
    # Nothing worked — not fatal, GC will eventually reclaim.
    logger.debug("close_local_falkor: no close() hook found on FalkorDB client")


def list_local_entries(
    client,
    *,
    graph_name: str,
    page_size: int = 500,
) -> Iterator[LocalEntry]:
    """Yield every Entry node in *graph_name* with its embedding.

    Paginated SKIP/LIMIT to avoid materialising the entire result set
    for large graphs. Uses Cypher parameter bindings so graph_name and
    page_size aren't string-interpolated.

    .. note::
       Offset-based pagination is theoretically vulnerable to silent
       truncation if rows are inserted into the local graph mid-scan
       — the same latent risk that the hosted ``list_embeddings_t1``
       was specifically rewritten to avoid via cursor pagination
       (``WHERE n.entry_id > $cursor ORDER BY n.entry_id`` + raw-row-count
       cursor advancement; see ``hosted_semantic.py:list_embeddings_t1``).

       For one-time migration runs against a quiescent local FalkorDB
       this isn't a current bug — the user freshly extracts then
       pushes; concurrent local writes are vanishingly rare. If a
       future caller invokes this while a background daemon is writing
       to the same local graph, switch to cursor pagination matching
       the hosted-side pattern.
    """
    graph = client.select_graph(graph_name)
    skip = 0
    while True:
        res = graph.query(
            "MATCH (n:Entry) "
            "WHERE exists(n.embedding) "
            "RETURN n.entry_id, n.thread_topic, n.embedding, n.group_id "
            "ORDER BY n.entry_id "
            "SKIP $skip LIMIT $limit",
            {"skip": skip, "limit": page_size},
        )
        rows = res.result_set
        if not rows:
            return
        for row in rows:
            entry_id = str(row[0]) if row[0] is not None else ""
            topic = str(row[1]) if row[1] is not None else ""
            embedding = row[2]
            group_id = str(row[3]) if row[3] is not None else ""
            if not entry_id or not embedding:
                continue
            try:
                emb_list = [float(x) for x in embedding]
            except (TypeError, ValueError) as e:
                logger.warning("Skipping %s: bad embedding (%s)", entry_id, e)
                continue
            yield LocalEntry(
                entry_id=entry_id,
                thread_topic=topic,
                embedding=emb_list,
                group_id=group_id,
            )
        if len(rows) < page_size:
            return
        skip += page_size


def upsert_local_entry(
    client,
    *,
    graph_name: str,
    entry: LocalEntry,
) -> None:
    """Idempotent MERGE of a single Entry node into a local FalkorDB graph.

    Mirrors the hosted ``upsert_embedding`` semantics: MERGE keyed on
    entry_id + group_id, SET overwrites the rest. The Vectorf32
    construction uses the ``vecf32(...)`` Cypher function.
    """
    graph = client.select_graph(graph_name)
    graph.query(
        "MERGE (n:Entry {entry_id: $entry_id, group_id: $group_id}) "
        "SET n.thread_topic = $topic, "
        "    n.embedding = vecf32($embedding), "
        "    n.role = $role, "
        "    n.entry_type = $entry_type, "
        "    n.agent = $agent, "
        "    n.timestamp = $timestamp",
        {
            "entry_id": entry.entry_id,
            "group_id": entry.group_id,
            "topic": entry.thread_topic,
            "embedding": entry.embedding,
            "role": entry.role,
            "entry_type": entry.entry_type,
            "agent": entry.agent,
            "timestamp": entry.timestamp,
        },
    )


def ensure_local_indexes(client, *, graph_name: str, dim: int = 1024) -> None:
    """Create the Entry HNSW vector index + range indexes if missing.

    Idempotent: swallows "already exists" errors. Mirrors the hosted-side
    bootstrap in ``hosted_semantic._ensure_entry_indexes``.

    Defense-in-depth: ``dim`` is interpolated into the OPTIONS clause via
    f-string because FalkorDB's CREATE VECTOR INDEX OPTIONS doesn't
    accept Cypher parameter bindings. Cast + range-check defends against
    any future code path that surfaces ``dim`` as a CLI flag / env var
    where a non-integer value could otherwise inject Cypher.
    """
    try:
        dim_int = int(dim)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"ensure_local_indexes: dim must be a positive integer, got {dim!r}"
        ) from e
    if dim_int <= 0 or dim_int > 100_000:
        raise ValueError(
            f"ensure_local_indexes: dim must be in (0, 100000], got {dim_int}"
        )

    graph = client.select_graph(graph_name)
    queries = [
        f"CREATE VECTOR INDEX FOR (n:Entry) ON (n.embedding) "
        f"OPTIONS {{dimension: {dim_int}, similarityFunction: 'cosine'}}",
        "CREATE INDEX FOR (n:Entry) ON (n.entry_id, n.group_id, n.thread_topic)",
    ]
    for q in queries:
        try:
            graph.query(q)
        except Exception as e:
            msg = str(e).lower()
            if "already indexed" in msg or "already exists" in msg:
                continue
            logger.warning("ensure_local_indexes: %s", e)
            raise
