"""FalkorDB storage for entry embeddings.

Stores watercooler entry embeddings in FalkorDB with vector indexing for
efficient similarity search. Replaces the file-based storage in entries.jsonl
and search-index.jsonl.

Architecture:
    - Entry nodes store: entry_id (ULID), thread_topic, group_id, embedding
    - Vector index enables O(log n) similarity search via HNSW
    - Shares FalkorDB instance with Graphiti backend

Node Schema:
    (:Entry {
        entry_id: str,           # ULID - primary key
        thread_topic: str,       # Thread topic for filtering
        group_id: str,           # Project scope (matches Graphiti)
        embedding: vecf32([...]) # 1024-dim vector
    })

Usage:
    from watercooler.baseline_graph.falkordb_entries import FalkorDBEntryStore

    store = FalkorDBEntryStore(group_id="watercooler_cloud")
    await store.connect()
    await store.ensure_index()

    # Store embedding
    await store.store_embedding("01ABC123", "auth-feature", embedding)

    # Search similar
    results = await store.search_similar(query_embedding, limit=10)
"""

from __future__ import annotations

import atexit
import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from falkordb import Graph as FalkorGraph
    from falkordb.asyncio import FalkorDB

logger = logging.getLogger(__name__)

# Default embedding dimension (matches watercooler memory config)
DEFAULT_EMBEDDING_DIM = 1024


@dataclass
class EntrySearchResult:
    """Result from entry similarity search."""

    entry_id: str
    thread_topic: str
    score: float  # Cosine similarity score (0-1, higher is more similar)


class FalkorDBEntryStore:
    """Store and query entry embeddings in FalkorDB.

    Provides vector storage and similarity search for watercooler entry
    embeddings using FalkorDB's HNSW vector index.

    The store connects to the same FalkorDB instance used by Graphiti,
    sharing the connection for efficiency. Entry nodes are scoped by
    group_id to support multi-project deployments.

    Example:
        >>> store = FalkorDBEntryStore(group_id="watercooler_cloud")
        >>> await store.connect()
        >>> await store.ensure_index()
        >>> await store.store_embedding("01ABC", "auth-feature", [0.1] * 1024)
        >>> results = await store.search_similar([0.1] * 1024, limit=5)
        >>> for r in results:
        ...     print(f"{r.entry_id}: {r.score:.3f}")
    """

    def __init__(
        self,
        group_id: str,
        *,
        host: str = "localhost",
        port: int = 6379,
        username: str | None = None,
        password: str | None = None,
        database: str | None = None,
        embedding_dim: int = DEFAULT_EMBEDDING_DIM,
    ) -> None:
        """Initialize entry store.

        Args:
            group_id: Project scope identifier (e.g., "watercooler_cloud").
                Must match the Graphiti group_id for the project.
            host: FalkorDB host. Defaults to localhost.
            port: FalkorDB port. Defaults to 6379.
            username: Optional FalkorDB username.
            password: Optional FalkorDB password.
            database: FalkorDB database name. If None, uses group_id.
            embedding_dim: Embedding vector dimension. Defaults to 1024.
        """
        self.group_id = group_id
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.database = database or group_id
        self.embedding_dim = embedding_dim

        self._client: FalkorDB | None = None
        self._graph: FalkorGraph | None = None
        self._index_created: bool = False

    @classmethod
    def from_config(cls, group_id: str) -> "FalkorDBEntryStore":
        """Create store from watercooler unified configuration.

        Uses the unified config system with proper priority chain:
        1. Environment variables (FALKORDB_HOST, FALKORDB_PORT, etc.)
        2. TOML settings ([memory.database])
        3. Built-in defaults

        Args:
            group_id: Project scope identifier

        Returns:
            FalkorDBEntryStore instance configured from unified config
        """
        from watercooler.memory_config import (
            resolve_database_config,
            resolve_embedding_config,
        )

        db_config = resolve_database_config()
        embedding_config = resolve_embedding_config()

        return cls(
            group_id=group_id,
            host=db_config.host,
            port=db_config.port,
            username=db_config.username if db_config.username else None,
            password=db_config.password if db_config.password else None,
            embedding_dim=embedding_config.dim,
        )

    async def connect(self) -> None:
        """Connect to FalkorDB.

        Creates the client and selects the graph. Safe to call multiple
        times - subsequent calls are no-ops if already connected.

        Raises:
            ImportError: If falkordb package is not installed.
            ConnectionError: If unable to connect to FalkorDB.
        """
        if self._client is not None:
            return

        try:
            from falkordb.asyncio import FalkorDB
        except ImportError as e:
            raise ImportError(
                "falkordb is required for FalkorDBEntryStore. "
                "Install with: pip install falkordb"
            ) from e

        self._client = FalkorDB(
            host=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
        )
        self._graph = self._client.select_graph(self.database)
        logger.debug(f"Connected to FalkorDB at {self.host}:{self.port}, db={self.database}")

    async def close(self) -> None:
        """Close the FalkorDB connection.

        Safe to call multiple times. After close(), connect() must be
        called again before using the store.
        """
        if self._client is not None:
            try:
                if hasattr(self._client, "aclose"):
                    await self._client.aclose()
                elif hasattr(self._client, "connection"):
                    conn = self._client.connection
                    if hasattr(conn, "aclose"):
                        await conn.aclose()
                    elif hasattr(conn, "close"):
                        await conn.close()
            except Exception as e:
                logger.warning(f"Error closing FalkorDB connection: {e}")
            finally:
                self._client = None
                self._graph = None
                self._index_created = False

    async def ensure_index(self) -> None:
        """Create vector index on Entry nodes if not exists.

        Creates:
        - Vector index for similarity search (HNSW, cosine similarity)
        - Range index on entry_id, group_id, thread_topic

        Safe to call multiple times - will not recreate existing indices.

        Raises:
            RuntimeError: If not connected. Call connect() first.
        """
        if self._graph is None:
            raise RuntimeError("Not connected. Call connect() first.")

        if self._index_created:
            return

        # Create vector index for embedding similarity search
        vector_index_query = (
            f"CREATE VECTOR INDEX FOR (n:Entry) ON (n.embedding) "
            f"OPTIONS {{dimension: {self.embedding_dim}, similarityFunction: 'cosine'}}"
        )

        # Create range index for filtering
        range_index_query = (
            "CREATE INDEX FOR (n:Entry) ON (n.entry_id, n.group_id, n.thread_topic)"
        )

        for query in [vector_index_query, range_index_query]:
            try:
                await self._graph.query(query)
                logger.debug(f"Created index: {query[:50]}...")
            except Exception as e:
                # Ignore "already indexed" errors
                if "already indexed" in str(e).lower() or "already exists" in str(e).lower():
                    logger.debug(f"Index already exists: {query[:50]}...")
                else:
                    logger.error(f"Failed to create index: {e}")
                    raise

        self._index_created = True

    async def store_embedding(
        self,
        entry_id: str,
        thread_topic: str,
        embedding: list[float],
    ) -> None:
        """Store or update an entry embedding.

        Creates or updates an Entry node with the given embedding vector.
        Uses MERGE to upsert based on entry_id.

        Args:
            entry_id: Entry ULID (primary key)
            thread_topic: Thread topic for filtering
            embedding: Embedding vector (must match embedding_dim)

        Raises:
            RuntimeError: If not connected.
            ValueError: If embedding dimension doesn't match.
        """
        if self._graph is None:
            raise RuntimeError("Not connected. Call connect() first.")

        if len(embedding) != self.embedding_dim:
            raise ValueError(
                f"Embedding dimension mismatch: got {len(embedding)}, "
                f"expected {self.embedding_dim}"
            )

        # MERGE upserts the Entry node by entry_id
        # SET updates all properties including embedding
        query = """
            MERGE (n:Entry {entry_id: $entry_id})
            SET n.thread_topic = $thread_topic,
                n.group_id = $group_id,
                n.embedding = vecf32($embedding)
            RETURN n.entry_id
        """

        await self._graph.query(
            query,
            {
                "entry_id": entry_id,
                "thread_topic": thread_topic,
                "group_id": self.group_id,
                "embedding": embedding,
            },
        )
        logger.debug(f"Stored embedding for entry {entry_id} in thread {thread_topic}")

    async def search_similar(
        self,
        query_embedding: list[float],
        limit: int = 10,
        threshold: float = 0.0,
        thread_topic: str | None = None,
    ) -> list[EntrySearchResult]:
        """Search for entries similar to a query embedding.

        Uses FalkorDB's HNSW vector index for efficient similarity search.
        Results are filtered by group_id and optionally by thread_topic.

        Args:
            query_embedding: Query vector (must match embedding_dim)
            limit: Maximum number of results to return
            threshold: Minimum similarity score (0-1). Defaults to 0 (no threshold).
            thread_topic: Optional thread topic filter

        Returns:
            List of EntrySearchResult sorted by similarity (highest first)

        Raises:
            RuntimeError: If not connected.
            ValueError: If embedding dimension doesn't match.
        """
        if self._graph is None:
            raise RuntimeError("Not connected. Call connect() first.")

        if len(query_embedding) != self.embedding_dim:
            raise ValueError(
                f"Embedding dimension mismatch: got {len(query_embedding)}, "
                f"expected {self.embedding_dim}"
            )

        # FalkorDB vector search returns (node, score) where score is raw distance
        # We filter by group_id in WHERE clause (not in vector query which doesn't support it)
        # Note: FalkorDB returns distance, we convert to similarity
        if thread_topic:
            query = """
                CALL db.idx.vector.queryNodes('Entry', 'embedding', $limit, vecf32($query_vector))
                YIELD node, score
                WHERE node.group_id = $group_id AND node.thread_topic = $thread_topic
                RETURN node.entry_id AS entry_id, node.thread_topic AS thread_topic, score
                ORDER BY score ASC
            """
            params: dict[str, Any] = {
                "limit": limit * 2,  # Over-fetch for filtering
                "query_vector": query_embedding,
                "group_id": self.group_id,
                "thread_topic": thread_topic,
            }
        else:
            query = """
                CALL db.idx.vector.queryNodes('Entry', 'embedding', $limit, vecf32($query_vector))
                YIELD node, score
                WHERE node.group_id = $group_id
                RETURN node.entry_id AS entry_id, node.thread_topic AS thread_topic, score
                ORDER BY score ASC
            """
            params = {
                "limit": limit * 2,  # Over-fetch for filtering
                "query_vector": query_embedding,
                "group_id": self.group_id,
            }

        result = await self._graph.query(query, params)

        # Convert to results, converting distance to similarity
        # FalkorDB returns cosine distance (0 = identical, 2 = opposite)
        # Similarity = 1 - (distance / 2) for cosine
        results: list[EntrySearchResult] = []
        for row in result.result_set:
            # row format: [entry_id, thread_topic, score]
            entry_id = row[0]
            topic = row[1]
            distance = float(row[2])
            # Convert distance to similarity (0-1 scale)
            similarity = 1.0 - (distance / 2.0)

            if similarity >= threshold:
                results.append(EntrySearchResult(
                    entry_id=entry_id,
                    thread_topic=topic,
                    score=similarity,
                ))

            if len(results) >= limit:
                break

        return results

    async def find_similar_to_entry(
        self,
        entry_id: str,
        limit: int = 10,
        threshold: float = 0.0,
        exclude_same_thread: bool = False,
    ) -> list[EntrySearchResult]:
        """Find entries similar to a given entry.

        Retrieves the embedding for the given entry and searches for
        similar entries, excluding the source entry from results.

        Args:
            entry_id: Source entry ULID
            limit: Maximum number of results (excluding source entry)
            threshold: Minimum similarity score (0-1)
            exclude_same_thread: If True, exclude entries from the same thread

        Returns:
            List of similar entries (excluding the source entry)

        Raises:
            RuntimeError: If not connected.
            ValueError: If entry not found or has no embedding.
        """
        if self._graph is None:
            raise RuntimeError("Not connected. Call connect() first.")

        # Get the source entry's embedding and topic
        query = """
            MATCH (n:Entry {entry_id: $entry_id, group_id: $group_id})
            RETURN n.embedding AS embedding, n.thread_topic AS thread_topic
        """
        result = await self._graph.query(
            query,
            {"entry_id": entry_id, "group_id": self.group_id},
        )

        if not result.result_set:
            raise ValueError(f"Entry not found: {entry_id}")

        row = result.result_set[0]
        embedding = row[0]
        source_topic = row[1]

        if embedding is None:
            raise ValueError(f"Entry has no embedding: {entry_id}")

        # Convert embedding from FalkorDB format if needed
        if isinstance(embedding, str):
            # Comma-separated string format
            embedding = [float(x) for x in embedding.split(",")]

        # Search for similar entries
        results = await self.search_similar(
            query_embedding=embedding,
            limit=limit + 1,  # +1 to account for excluding source
            threshold=threshold,
        )

        # Filter out the source entry and optionally same-thread entries
        filtered: list[EntrySearchResult] = []
        for r in results:
            if r.entry_id == entry_id:
                continue
            if exclude_same_thread and r.thread_topic == source_topic:
                continue
            filtered.append(r)
            if len(filtered) >= limit:
                break

        return filtered

    async def delete_embedding(self, entry_id: str) -> bool:
        """Remove an entry's embedding node.

        Args:
            entry_id: Entry ULID to remove

        Returns:
            True if entry was deleted, False if not found

        Raises:
            RuntimeError: If not connected.
        """
        if self._graph is None:
            raise RuntimeError("Not connected. Call connect() first.")

        query = """
            MATCH (n:Entry {entry_id: $entry_id, group_id: $group_id})
            DELETE n
            RETURN count(*) AS deleted
        """
        result = await self._graph.query(
            query,
            {"entry_id": entry_id, "group_id": self.group_id},
        )

        deleted = result.result_set[0][0] if result.result_set else 0
        if deleted > 0:
            logger.debug(f"Deleted embedding for entry {entry_id}")
            return True
        return False

    async def get_embedding(self, entry_id: str) -> list[float] | None:
        """Get the embedding for an entry.

        Args:
            entry_id: Entry ULID

        Returns:
            Embedding vector or None if not found

        Raises:
            RuntimeError: If not connected.
        """
        if self._graph is None:
            raise RuntimeError("Not connected. Call connect() first.")

        query = """
            MATCH (n:Entry {entry_id: $entry_id, group_id: $group_id})
            RETURN n.embedding AS embedding
        """
        result = await self._graph.query(
            query,
            {"entry_id": entry_id, "group_id": self.group_id},
        )

        if not result.result_set:
            return None

        embedding = result.result_set[0][0]
        if embedding is None:
            return None

        # Convert from FalkorDB format if needed
        if isinstance(embedding, str):
            return [float(x) for x in embedding.split(",")]
        return list(embedding)

    async def count_entries(self, thread_topic: str | None = None) -> int:
        """Count entries in the store.

        Args:
            thread_topic: Optional thread topic filter

        Returns:
            Number of Entry nodes matching criteria

        Raises:
            RuntimeError: If not connected.
        """
        if self._graph is None:
            raise RuntimeError("Not connected. Call connect() first.")

        if thread_topic:
            query = """
                MATCH (n:Entry {group_id: $group_id, thread_topic: $thread_topic})
                RETURN count(n) AS count
            """
            params: dict[str, Any] = {
                "group_id": self.group_id,
                "thread_topic": thread_topic,
            }
        else:
            query = """
                MATCH (n:Entry {group_id: $group_id})
                RETURN count(n) AS count
            """
            params = {"group_id": self.group_id}

        result = await self._graph.query(query, params)
        return result.result_set[0][0] if result.result_set else 0

    async def list_thread_topics(self) -> list[str]:
        """List all thread topics with entries.

        Returns:
            List of unique thread topic strings

        Raises:
            RuntimeError: If not connected.
        """
        if self._graph is None:
            raise RuntimeError("Not connected. Call connect() first.")

        query = """
            MATCH (n:Entry {group_id: $group_id})
            RETURN DISTINCT n.thread_topic AS topic
            ORDER BY topic
        """
        result = await self._graph.query(query, {"group_id": self.group_id})
        return [row[0] for row in result.result_set if row[0]]

    async def health_check(self) -> bool:
        """Check FalkorDB connectivity.

        Returns:
            True if connected and responsive, False otherwise
        """
        if self._graph is None:
            return False

        try:
            await self._graph.query("MATCH (n) RETURN 1 LIMIT 1")
            return True
        except Exception as e:
            logger.warning(f"FalkorDB health check failed: {e}")
            return False


# Convenience function for one-off operations
async def store_entry_embedding(
    entry_id: str,
    thread_topic: str,
    embedding: list[float],
    group_id: str,
    **kwargs: Any,
) -> None:
    """Store a single entry embedding (convenience function).

    Creates a temporary connection, stores the embedding, and closes.
    For batch operations, use FalkorDBEntryStore directly to reuse connections.

    Args:
        entry_id: Entry ULID
        thread_topic: Thread topic
        embedding: Embedding vector
        group_id: Project scope identifier
        **kwargs: Additional args passed to FalkorDBEntryStore
    """
    store = FalkorDBEntryStore(group_id=group_id, **kwargs)
    try:
        await store.connect()
        await store.ensure_index()
        await store.store_embedding(entry_id, thread_topic, embedding)
    finally:
        await store.close()


# =============================================================================
# Sync Wrappers for Integration with Sync Code
# =============================================================================


class _AsyncLoopRunner:
    """Manages a dedicated event loop in a background thread.

    This solves the problem where asyncio.run() creates and closes a new
    event loop on each call, which breaks async clients (like FalkorDB)
    that maintain connections tied to a specific event loop.

    The runner keeps a single event loop alive in a daemon thread, and all
    async operations are submitted to this loop, ensuring connection reuse.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: "threading.Thread | None" = None
        import threading
        self._lock = threading.Lock()

    def _start_loop(self) -> None:
        """Start the event loop in the current thread."""
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _ensure_started(self) -> asyncio.AbstractEventLoop:
        """Ensure the background loop is running."""
        if self._loop is not None and self._loop.is_running():
            return self._loop

        with self._lock:
            # Double-check after acquiring lock
            if self._loop is not None and self._loop.is_running():
                return self._loop

            import threading
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._start_loop,
                daemon=True,
                name="falkordb-async-loop",
            )
            self._thread.start()

            # Wait for loop to start
            import time
            for _ in range(100):  # Up to 1 second
                if self._loop.is_running():
                    break
                time.sleep(0.01)

            return self._loop

    def run(self, coro: Any, timeout: float = 60.0) -> Any:
        """Run a coroutine in the dedicated event loop.

        Args:
            coro: Coroutine to run
            timeout: Maximum time to wait for result

        Returns:
            Result from the coroutine

        Raises:
            TimeoutError: If operation exceeds timeout
            Exception: Any exception raised by the coroutine
        """
        loop = self._ensure_started()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except Exception:
            future.cancel()
            raise

    def stop(self) -> None:
        """Stop the background event loop."""
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=5.0)
            self._loop = None
            self._thread = None


# Module-level async loop runner (singleton)
_async_runner: _AsyncLoopRunner | None = None


def _cleanup_async_runner() -> None:
    """Cleanup handler for module exit."""
    global _async_runner
    if _async_runner is not None:
        _async_runner.stop()
        _async_runner = None


# Register cleanup on module unload
atexit.register(_cleanup_async_runner)


def _get_async_runner() -> _AsyncLoopRunner:
    """Get or create the global async loop runner."""
    global _async_runner
    if _async_runner is None:
        _async_runner = _AsyncLoopRunner()
    return _async_runner


def _run_async(coro: Any) -> Any:
    """Run an async coroutine from sync code.

    Uses a dedicated background event loop to ensure connection reuse
    for async clients like FalkorDB that maintain persistent connections.
    """
    return _get_async_runner().run(coro)


class FalkorDBEntryStoreSync:
    """Synchronous wrapper for FalkorDBEntryStore.

    Provides sync methods that wrap the async FalkorDBEntryStore for use
    in synchronous code paths like writer.py and sync.py.

    Example:
        >>> store = FalkorDBEntryStoreSync(group_id="watercooler_cloud")
        >>> store.connect()
        >>> store.ensure_index()
        >>> store.store_embedding("01ABC", "auth-thread", [0.1] * 1024)
        >>> store.close()
    """

    def __init__(
        self,
        group_id: str,
        *,
        host: str = "localhost",
        port: int = 6379,
        username: str | None = None,
        password: str | None = None,
        database: str | None = None,
        embedding_dim: int = DEFAULT_EMBEDDING_DIM,
    ) -> None:
        """Initialize sync entry store wrapper.

        Args:
            group_id: Project scope identifier
            host: FalkorDB host
            port: FalkorDB port
            username: Optional username
            password: Optional password
            database: Database name (defaults to group_id)
            embedding_dim: Embedding dimension
        """
        self._async_store = FalkorDBEntryStore(
            group_id=group_id,
            host=host,
            port=port,
            username=username,
            password=password,
            database=database,
            embedding_dim=embedding_dim,
        )

    @classmethod
    def from_config(cls, group_id: str) -> "FalkorDBEntryStoreSync":
        """Create store from watercooler unified configuration."""
        async_store = FalkorDBEntryStore.from_config(group_id)
        wrapper = cls.__new__(cls)
        wrapper._async_store = async_store
        return wrapper

    @property
    def group_id(self) -> str:
        return self._async_store.group_id

    @property
    def embedding_dim(self) -> int:
        return self._async_store.embedding_dim

    def connect(self) -> None:
        """Connect to FalkorDB."""
        _run_async(self._async_store.connect())

    def close(self) -> None:
        """Close the connection."""
        _run_async(self._async_store.close())

    def ensure_index(self) -> None:
        """Create indices if not exists."""
        _run_async(self._async_store.ensure_index())

    def store_embedding(
        self,
        entry_id: str,
        thread_topic: str,
        embedding: list[float],
    ) -> None:
        """Store or update an entry embedding."""
        _run_async(
            self._async_store.store_embedding(entry_id, thread_topic, embedding)
        )

    def search_similar(
        self,
        query_embedding: list[float],
        limit: int = 10,
        threshold: float = 0.0,
        thread_topic: str | None = None,
    ) -> list[EntrySearchResult]:
        """Search for similar entries."""
        return _run_async(
            self._async_store.search_similar(
                query_embedding, limit, threshold, thread_topic
            )
        )

    def find_similar_to_entry(
        self,
        entry_id: str,
        limit: int = 10,
        threshold: float = 0.0,
        exclude_same_thread: bool = False,
    ) -> list[EntrySearchResult]:
        """Find entries similar to a given entry."""
        return _run_async(
            self._async_store.find_similar_to_entry(
                entry_id, limit, threshold, exclude_same_thread
            )
        )

    def delete_embedding(self, entry_id: str) -> bool:
        """Delete an entry's embedding."""
        return _run_async(self._async_store.delete_embedding(entry_id))

    def get_embedding(self, entry_id: str) -> list[float] | None:
        """Get an entry's embedding."""
        return _run_async(self._async_store.get_embedding(entry_id))

    def count_entries(self, thread_topic: str | None = None) -> int:
        """Count entries in the store."""
        return _run_async(self._async_store.count_entries(thread_topic))

    def list_thread_topics(self) -> list[str]:
        """List all thread topics with entries."""
        return _run_async(self._async_store.list_thread_topics())

    def health_check(self) -> bool:
        """Check FalkorDB connectivity."""
        return _run_async(self._async_store.health_check())

    def __enter__(self) -> "FalkorDBEntryStoreSync":
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()


# Module-level cache for reusing connections per group_id
_sync_store_cache: dict[str, FalkorDBEntryStoreSync] = {}
_sync_store_cache_lock: "threading.Lock | None" = None


def _get_store_cache_lock() -> "threading.Lock":
    """Get or create the store cache lock."""
    global _sync_store_cache_lock
    if _sync_store_cache_lock is None:
        import threading
        _sync_store_cache_lock = threading.Lock()
    return _sync_store_cache_lock


def get_falkordb_entry_store(group_id: str) -> FalkorDBEntryStoreSync | None:
    """Get or create a FalkorDBEntryStoreSync for embedding storage.

    Returns None if FalkorDB is not available or connection fails.
    Stores are cached per group_id for connection reuse.

    Args:
        group_id: Project scope identifier

    Returns:
        FalkorDBEntryStoreSync or None if unavailable
    """
    # Check cache first (without lock for fast path)
    if group_id in _sync_store_cache:
        return _sync_store_cache[group_id]

    # Acquire lock for creation
    with _get_store_cache_lock():
        # Double-check after acquiring lock
        if group_id in _sync_store_cache:
            return _sync_store_cache[group_id]

        try:
            store = FalkorDBEntryStoreSync.from_config(group_id)
            store.connect()
            store.ensure_index()
            _sync_store_cache[group_id] = store
            logger.info(f"FalkorDB entry store initialized for group_id={group_id}")
            return store
        except ImportError:
            logger.debug("FalkorDB not installed, embedding storage disabled")
            return None
        except Exception as e:
            logger.warning(f"FalkorDB connection failed for {group_id}: {e}")
            return None
