"""LeanRAG backend adapter for entity extraction and hierarchical graph RAG."""

from __future__ import annotations

import asyncio
import json
import os
# subprocess removed - now using native build_hierarchical_graph()
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from . import (
    BackendError,
    Capabilities,
    ChunkPayload,
    ConfigError,
    CorpusPayload,
    HealthStatus,
    IndexResult,
    MemoryBackend,
    PrepareResult,
    QueryPayload,
    QueryResult,
    TransientError,
)

# Thread-safe lock for os.chdir() operations
# os.chdir() changes the process-wide current directory, which is not thread-safe.
# This lock ensures that only one thread can change directories at a time.
_chdir_lock = threading.Lock()


@dataclass
class LeanRAGConfig:
    """Configuration for LeanRAG backend."""

    # LeanRAG submodule location
    leanrag_path: Path = Path("external/LeanRAG")

    # Database configuration
    falkordb_host: str = "localhost"
    falkordb_port: int = 6379

    # LLM configuration (optional)
    llm_api_base: str | None = None
    llm_model: str | None = None

    # Embedding configuration (optional)
    embedding_api_base: str | None = None
    embedding_model: str | None = None

    # Working directory for exports
    work_dir: Path | None = None

    # Test mode: Add pytest__ prefix to database names for isolation
    test_mode: bool = False

    # Max workers for graph building
    max_workers: int = 8

    @classmethod
    def from_unified(cls) -> "LeanRAGConfig":
        """Create LeanRAGConfig from unified watercooler configuration.

        Uses the unified config system with proper priority chain:
        1. Environment variables (highest)
        2. Backend-specific TOML overrides ([memory.leanrag])
        3. Shared TOML settings ([memory.llm], [memory.embedding], [memory.database])
        4. Built-in defaults (lowest)

        Returns:
            LeanRAGConfig instance with all settings resolved

        Example:
            >>> config = LeanRAGConfig.from_unified()
            >>> backend = LeanRAGBackend(config)
        """
        from watercooler.memory_config import (
            resolve_llm_config,
            resolve_embedding_config,
            resolve_database_config,
            get_leanrag_max_workers,
        )

        llm = resolve_llm_config("leanrag")
        embedding = resolve_embedding_config("leanrag")
        db = resolve_database_config()

        return cls(
            llm_api_base=llm.api_base if llm.api_base != "https://api.openai.com/v1" else None,
            llm_model=llm.model,
            embedding_api_base=embedding.api_base if embedding.api_base != "https://api.openai.com/v1" else None,
            embedding_model=embedding.model,
            falkordb_host=db.host,
            falkordb_port=db.port,
            max_workers=get_leanrag_max_workers(),
        )


class LeanRAGBackend(MemoryBackend):
    """
    LeanRAG adapter implementing MemoryBackend contract.

    LeanRAG provides:
    - Entity and relation extraction
    - Hierarchical semantic clustering (GMM + UMAP)
    - Multi-layer knowledge graph construction
    - Reduced redundancy (~46% vs flat baselines)

    This adapter uses native LeanRAG Python APIs and maps to/from canonical payloads.

    Performance Considerations:
    - Current implementation creates new FalkorDB connections per request
    - Future Enhancement: Connection pooling for high-throughput scenarios
      - Recommended for production deployments handling >100 QPS
      - Consider using a connection pool (e.g., redis-py connection pool)
      - Pool size should be tuned based on concurrent request volume
      - Trade-off: Memory overhead vs connection setup latency
    """

    def __init__(self, config: LeanRAGConfig | None = None) -> None:
        self.config = config or LeanRAGConfig()
        self._validate_config()

    def _validate_config(self) -> None:
        """Validate configuration and LeanRAG availability."""
        if not self.config.leanrag_path.exists():
            raise ConfigError(
                f"LeanRAG not found at {self.config.leanrag_path}. "
                "Run: git submodule update --init external/LeanRAG"
            )

        process_script = self.config.leanrag_path / "leanrag/pipelines/process.py"
        if not process_script.exists():
            raise ConfigError(
                f"LeanRAG process script not found at {process_script}. "
                "Ensure LeanRAG submodule is properly initialized."
            )

    def _normalize_entity_name(self, name: str | None) -> str | None:
        """Normalize entity name by stripping quotes and whitespace.

        LeanRAG stores entity names in different ways:
        - Milvus: Raw names with quotes (e.g., '"OAUTH2"')
        - FalkorDB: Stripped names without quotes (e.g., 'OAUTH2')

        This normalization ensures consistency across backends by matching
        FalkorDB's normalization pattern (see falkordb.py:161).

        Args:
            name: Entity name to normalize (may be None for optional fields)

        Returns:
            Normalized entity name, or None if input was None

        Example:
            >>> backend._normalize_entity_name('"OAUTH2"  ')
            'OAUTH2'
        """
        if name is None:
            return None
        return name.strip().strip('"').strip()

    def _apply_test_prefix(self, work_dir: Path) -> Path:
        """Apply pytest__ prefix to work_dir basename if test_mode is enabled.

        LeanRAG uses os.path.basename(work_dir) as the FalkorDB database name.
        When test_mode=True, we prepend 'pytest__' to the directory name to
        ensure test databases are isolated and can be cleaned up separately.

        Args:
            work_dir: Original working directory path

        Returns:
            Path with pytest__ prefix applied to basename if test_mode=True,
            otherwise returns original path unchanged
        """
        if not self.config.test_mode:
            return work_dir

        # Get parent and basename
        parent = work_dir.parent
        basename = work_dir.name

        # Prepend pytest__ if not already present
        if not basename.startswith("pytest__"):
            basename = f"pytest__{basename}"

        return parent / basename

    def prepare(self, corpus: CorpusPayload) -> PrepareResult:
        """
        Prepare corpus for LeanRAG ingestion.

        Maps canonical payload to LeanRAG's expected JSON format:
        - documents.json: Entries with content and metadata
        - threads.json: Thread metadata
        - threads_chunk.json: Chunks generated from provided entries
        - manifest.json: Export metadata
        """
        if self.config.work_dir:
            work_dir = self.config.work_dir
        else:
            work_dir = Path(tempfile.mkdtemp(prefix="leanrag-prepare-"))

        # Convert to absolute path for reliability
        work_dir = work_dir.resolve()

        # Apply pytest__ prefix if in test mode
        work_dir = self._apply_test_prefix(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            documents = [
                {
                    "id": entry.get("id"),
                    "thread_id": entry.get("thread_id"),
                    "title": entry.get("title"),
                    "content": entry.get("body", entry.get("content", "")),
                    "agent": entry.get("agent"),
                    "role": entry.get("role"),
                    "type": entry.get("type"),
                    "timestamp": entry.get("timestamp"),
                }
                for entry in corpus.entries
            ]

            threads = [
                {
                    "id": thread.get("id"),
                    "topic": thread.get("topic"),
                    "status": thread.get("status"),
                    "ball": thread.get("ball"),
                    "entry_count": thread.get("entry_count"),
                    "title": thread.get("title"),
                }
                for thread in corpus.threads
            ]

            chunks = self._extract_chunks_from_entries(corpus)

            (work_dir / "documents.json").write_text(json.dumps(documents, indent=2))
            (work_dir / "threads.json").write_text(json.dumps(threads, indent=2))
            (work_dir / "threads_chunk.json").write_text(json.dumps(chunks, indent=2))

            manifest = {
                "format": "leanrag-corpus",
                "version": "1.0",
                "source": "watercooler-cloud",
                "memory_payload_version": corpus.manifest_version,
                "chunker": {
                    "name": corpus.chunker_name,
                    "params": corpus.chunker_params,
                },
                "statistics": {
                    "threads": len(corpus.threads),
                    "entries": len(corpus.entries),
                    "chunks": len(chunks),
                },
                "files": {
                    "documents": "documents.json",
                    "threads": "threads.json",
                    "chunks": "threads_chunk.json",
                },
            }
            (work_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

            return PrepareResult(
                manifest_version=corpus.manifest_version,
                prepared_count=len(documents),
                message=f"Prepared corpus at {work_dir}",
            )
        except Exception as exc:
            raise BackendError(f"Failed to prepare corpus: {exc}") from exc

    def _extract_chunks_from_entries(
        self, corpus: CorpusPayload
    ) -> list[dict[str, Any]]:
        """
        Extract chunks from all entries for LeanRAG query pipeline.

        Creates threads_chunk.json with format:
        [{"hash_code": "...", "text": "..."}, ...]
        """
        import hashlib

        chunks: list[dict[str, Any]] = []
        for entry in corpus.entries:
            entry_chunks = entry.get("chunks", [])

            if not entry_chunks:
                full_text = f"# {entry.get('title', '')}\n\n{entry.get('body', '')}"
                hash_code = hashlib.md5(full_text.encode()).hexdigest()
                chunks.append({"hash_code": hash_code, "text": full_text})
                continue

            for chunk in entry_chunks:
                if isinstance(chunk, str):
                    chunk_text = chunk
                elif isinstance(chunk, dict):
                    chunk_text = chunk.get("text", chunk.get("content", str(chunk)))
                else:
                    chunk_text = str(chunk)

                hash_code = hashlib.md5(chunk_text.encode()).hexdigest()
                chunks.append({"hash_code": hash_code, "text": chunk_text})

        return chunks

    def _ensure_chunk_file(self, work_dir: Path, chunks: ChunkPayload) -> Path:
        """Ensure threads_chunk.json exists in the working directory."""
        chunk_file = work_dir / "threads_chunk.json"
        if chunk_file.exists():
            return chunk_file

        serialized = []
        for item in chunks.chunks:
            text = item.get("text", item.get("content", ""))
            hash_code = item.get("hash_code") or item.get("id") or item.get("chunk_id")
            if not hash_code:
                import hashlib

                hash_code = hashlib.md5(text.encode()).hexdigest()
            serialized.append({"hash_code": hash_code, "text": text})

        chunk_file.write_text(json.dumps(serialized, indent=2))
        return chunk_file

    def index(self, chunks: ChunkPayload) -> IndexResult:
        """
        Run LeanRAG entity extraction and graph building.

        Executes LeanRAG pipeline:
        1. triple_extraction (bypasses LeanRAG chunking)
        2. build_hierarchical_graph() to construct hierarchical graph (native)
        """
        import logging

        logger = logging.getLogger(__name__)

        work_dir = self.config.work_dir or Path(tempfile.mkdtemp(prefix="leanrag-index-"))

        # Convert to absolute path so it works when we change directories
        work_dir = work_dir.resolve()

        # Apply pytest__ prefix if in test mode
        work_dir = self._apply_test_prefix(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            chunk_file = self._ensure_chunk_file(work_dir, chunks)

            leanrag_abspath = self.config.leanrag_path.resolve()

            with open(chunk_file, "r") as fh:
                corpus = json.load(fh)
            chunks_dict = {item["hash_code"]: item["text"] for item in corpus}

            # Thread-safe directory change AND sys.path modification
            with _chdir_lock:
                # Add LeanRAG to path inside lock to prevent race conditions
                if str(leanrag_abspath) not in sys.path:
                    sys.path.insert(0, str(leanrag_abspath))

                original_cwd = os.getcwd()
                try:
                    os.chdir(str(self.config.leanrag_path))

                    from leanrag.extraction.chunk import triple_extraction
                    from leanrag.core.llm import generate_text_async

                    asyncio.run(
                        triple_extraction(
                            chunks_dict, generate_text_async, str(work_dir), save_filtered=False
                        )
                    )
                finally:
                    os.chdir(original_cwd)

            # Native graph building (replaces subprocess call to build.py)
            logger.info(f"Running LeanRAG graph building (native) at {work_dir}")

            # Import inside lock context to ensure sys.path is set
            with _chdir_lock:
                if str(leanrag_abspath) not in sys.path:
                    sys.path.insert(0, str(leanrag_abspath))

                original_cwd = os.getcwd()
                try:
                    os.chdir(str(self.config.leanrag_path))

                    from leanrag.pipelines.build_native import build_hierarchical_graph

                    build_result = build_hierarchical_graph(
                        working_dir=str(work_dir),
                        max_workers=8,  # Default parallelism
                        fresh_start=False,  # Allow checkpoint resume
                    )
                finally:
                    os.chdir(original_cwd)

            if build_result.errors:
                logger.warning(f"Graph building completed with errors: {build_result.errors}")

            logger.info(
                f"Graph building complete: {build_result.entries_processed} entities, "
                f"{build_result.clusters_created} clusters, "
                f"{build_result.duration_seconds:.1f}s"
            )

            return IndexResult(
                manifest_version=chunks.manifest_version,
                indexed_count=len(chunks.chunks),
                message=(
                    f"Indexed {len(chunks.chunks)} chunks via LeanRAG at {work_dir}. "
                    f"Built {build_result.clusters_created} clusters in {build_result.duration_seconds:.1f}s"
                ),
            )
        except FileNotFoundError as exc:
            raise ConfigError(
                f"Required LeanRAG files not found: {exc}. "
                "Ensure LeanRAG submodule is initialized and prepared has been run."
            ) from exc
        except ValueError as exc:
            # Checkpoint corruption or other value errors from build_native
            raise BackendError(f"LeanRAG build error: {exc}") from exc
        except Exception as exc:
            raise BackendError(f"Unexpected error during index: {exc}") from exc

    def query(self, query: QueryPayload) -> QueryResult:
        """
        Execute queries against LeanRAG graph by calling query_graph directly.
        """
        work_dir = self.config.work_dir or Path(tempfile.mkdtemp(prefix="leanrag-query-"))
        # Convert to absolute path for reliability
        work_dir = work_dir.resolve()
        # Apply pytest__ prefix if in test mode (consistent with prepare/index)
        work_dir = self._apply_test_prefix(work_dir)

        if not (work_dir / "threads_chunk.json").exists():
            raise ConfigError(
                f"threads_chunk.json not found in {work_dir}. "
                "Run prepare() and index() before querying."
            )

        try:
            leanrag_abspath = self.config.leanrag_path.resolve()

            # Thread-safe directory change AND sys.path modification
            with _chdir_lock:
                # Add LeanRAG to path inside lock to prevent race conditions
                if str(leanrag_abspath) not in sys.path:
                    sys.path.insert(0, str(leanrag_abspath))

                original_cwd = os.getcwd()
                try:
                    os.chdir(str(self.config.leanrag_path))

                    from leanrag.pipelines.query import query_graph
                    from leanrag.core.llm import embedding, generate_text

                    results: list[dict[str, Any]] = []
                    for q in query.queries:
                        query_text = q.get("query", q.get("text", ""))
                        topk = q.get("limit", q.get("topk", 5))
                        if not query_text:
                            continue

                        global_config = {
                            "working_dir": str(work_dir),
                            "chunks_file": str(work_dir / "threads_chunk.json"),
                            "embeddings_func": embedding,
                            "use_llm_func": generate_text,
                            "topk": topk,
                            "level_mode": 1,
                        }

                        context, answer = query_graph(global_config, None, query_text)
                        results.append(
                            {
                                "query": query_text,
                                "answer": answer,
                                "context": context,
                                "topk": topk,
                            }
                        )

                        if answer:
                            print(f"Query answer: {answer[:200]}...")
                finally:
                    os.chdir(original_cwd)

            return QueryResult(
                manifest_version=query.manifest_version,
                results=results,
                message=f"Executed {len(results)} queries via LeanRAG",
            )
        except Exception as exc:
            raise BackendError(f"Unexpected error during query: {exc}") from exc

    def healthcheck(self) -> HealthStatus:
        """
        Check LeanRAG and database health.
        """
        try:
            self._validate_config()

            try:
                import redis

                redis.Redis(
                    host=self.config.falkordb_host,
                    port=self.config.falkordb_port,
                    socket_connect_timeout=2,
                ).ping()
                db_status = "FalkorDB: connected"
            except ImportError:
                db_status = "FalkorDB: redis-py not installed"
            except (redis.ConnectionError, redis.TimeoutError) as exc:
                db_status = f"FalkorDB: unreachable ({exc})"

            return HealthStatus(
                ok=True,
                details=f"LeanRAG available at {self.config.leanrag_path}, {db_status}",
            )
        except ConfigError as exc:
            return HealthStatus(ok=False, details=str(exc))
        except Exception as exc:
            return HealthStatus(ok=False, details=f"Health check failed: {exc}")

    def search_nodes(
        self,
        query: str,
        group_ids: Sequence[str] | None = None,
        max_results: int = 10,
        entity_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search for entity nodes using vector similarity search.

        Args:
            query: Search query string
            group_ids: Optional list of group IDs to filter by (ignored - LeanRAG uses separate databases)
            max_results: Maximum number of results to return
            entity_types: Optional list of entity types to filter by (not implemented in LeanRAG)

        Returns:
            List of normalized CoreResult dictionaries with node data

        Raises:
            ConfigError: If work_dir not set or database not indexed
            BackendError: If search fails
            TransientError: If database connection fails
        """
        work_dir = self.config.work_dir
        if not work_dir:
            raise ConfigError("work_dir must be set before searching")

        work_dir = work_dir.resolve()
        if not (work_dir / "threads_chunk.json").exists():
            raise ConfigError(
                f"Database not indexed. threads_chunk.json not found in {work_dir}"
            )

        try:
            # Add LeanRAG to path
            leanrag_abspath = self.config.leanrag_path.resolve()

            # Thread-safe directory change AND sys.path modification
            with _chdir_lock:
                # Add LeanRAG to path inside lock to prevent race conditions
                if str(leanrag_abspath) not in sys.path:
                    sys.path.insert(0, str(leanrag_abspath))

                original_cwd = os.getcwd()
                try:
                    os.chdir(str(self.config.leanrag_path))

                    # Import LeanRAG functions
                    from leanrag.core.llm import embedding
                    from leanrag.database.vector import search_vector_search

                    # Convert text query to embedding vector
                    query_embedding = embedding(query)

                    # Execute vector search (level_mode=2 means all levels: base + clusters)
                    results = search_vector_search(
                        str(work_dir),
                        query_embedding,
                        topk=max_results,
                        level_mode=2
                    )

                    # Normalize to CoreResult format
                    normalized_results = []
                    for entity_name, parent, description, source_id in results:
                        # Normalize entity names to match FalkorDB pattern
                        normalized_name = self._normalize_entity_name(entity_name)
                        normalized_parent = self._normalize_entity_name(parent)

                        normalized_results.append({
                            "id": normalized_name,  # Required by CoreResult
                            "name": normalized_name,
                            "summary": description,
                            "score": 0.0,  # Milvus doesn't return scores in current API
                            "backend": "leanrag",  # Required by CoreResult
                            "content": None,  # Entities don't have content
                            "source": source_id,  # Chunk hash where entity was found
                            "metadata": {
                                "parent": normalized_parent,  # Hierarchical parent (for clusters)
                            },
                            "extra": {
                                "corpus": str(work_dir),
                            },
                        })

                    return normalized_results

                finally:
                    os.chdir(original_cwd)

        except ImportError as e:
            raise TransientError(f"Failed to import LeanRAG modules: {e}") from e
        except Exception as e:
            raise BackendError(f"Entity search failed: {e}") from e

    def search_facts(
        self,
        query: str,
        group_ids: Sequence[str] | None = None,
        max_results: int = 10,
        center_node_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search for facts/relationships via entity search + hierarchical edge traversal.

        This implements LeanRAG's reasoning chain pattern from query.py:
        1. Find relevant entities via vector search
        2. Get hierarchical paths for each entity (entity → parent → grandparent → root)
        3. Search for relationships between all entities in those hierarchical paths

        Args:
            query: Search query string
            group_ids: Optional list of group IDs to filter by (ignored - LeanRAG uses separate databases)
            max_results: Maximum number of results to return
            center_node_id: Optional entity name to center search around (not yet implemented)

        Returns:
            List of normalized CoreResult dictionaries with fact/edge data

        Raises:
            ConfigError: If work_dir not set or database not indexed
            BackendError: If search fails
            TransientError: If database connection fails
        """
        work_dir = self.config.work_dir
        if not work_dir:
            raise ConfigError("work_dir must be set before searching")

        work_dir = work_dir.resolve()
        if not (work_dir / "threads_chunk.json").exists():
            raise ConfigError(
                f"Database not indexed. threads_chunk.json not found in {work_dir}"
            )

        try:
            # Add LeanRAG to path
            leanrag_abspath = self.config.leanrag_path.resolve()

            # Thread-safe directory change AND sys.path modification
            with _chdir_lock:
                # Add LeanRAG to path inside lock to prevent race conditions
                if str(leanrag_abspath) not in sys.path:
                    sys.path.insert(0, str(leanrag_abspath))

                original_cwd = os.getcwd()
                try:
                    os.chdir(str(self.config.leanrag_path))

                    # Import LeanRAG functions
                    from leanrag.database.adapter import search_nodes_link, find_tree_root
                    from itertools import combinations
                    import logging

                    logger = logging.getLogger(__name__)

                    # Strategy: Find relevant entities via vector search, then traverse
                    # hierarchical relationships (matches LeanRAG query.py get_reasoning_chain)

                    # 1. Find relevant entities (more than requested to increase relationship discovery)
                    entities = self.search_nodes(query, max_results=max_results * 2)

                    # 2. Get hierarchical paths for each entity
                    # find_tree_root returns [entity, parent, grandparent, ..., root]
                    db_name = work_dir.name
                    entity_paths = []
                    for entity in entities:
                        entity_name = entity["id"]
                        path = find_tree_root(db_name, entity_name)
                        if path:
                            entity_paths.append(path)

                    # Performance cap: Limit entity paths to prevent combinatorial explosion
                    # For max_results=10, limit to top 10 paths to keep combinations manageable
                    MAX_ENTITY_PATHS = max(10, max_results)
                    if len(entity_paths) > MAX_ENTITY_PATHS:
                        logger.debug(
                            f"Performance cap hit: Limiting entity paths from {len(entity_paths)} to {MAX_ENTITY_PATHS} "
                            f"(max_results={max_results}). Combinatorics capped to avoid explosion."
                        )
                    entity_paths = entity_paths[:MAX_ENTITY_PATHS]

                    # 3. For each pair of entity paths, search for relationships
                    # between all entities in those paths
                    facts = []
                    seen_edges = set()  # Deduplicate edges (bidirectional)

                    # Performance cap: Limit entities per path pair
                    MAX_ENTITIES_PER_PAIR = 20

                    for path1, path2 in combinations(entity_paths, 2):
                        # Get all unique entities from both paths
                        all_entities = list(set(path1 + path2))

                        # Cap entities per pair to prevent explosion
                        if len(all_entities) > MAX_ENTITIES_PER_PAIR:
                            logger.debug(
                                f"Performance cap hit: Limiting entities per path pair from {len(all_entities)} to {MAX_ENTITIES_PER_PAIR}. "
                                f"Prevents combinatorial explosion in relationship search."
                            )
                        all_entities = all_entities[:MAX_ENTITIES_PER_PAIR]

                        # Search for relationships between all pairs of entities
                        for e1, e2 in combinations(all_entities, 2):
                            if e1 == e2:
                                continue

                            # Early exit if we have enough results
                            # Collect extra to allow for scoring/ranking
                            if len(facts) >= max_results * 3:
                                break

                            # Deduplicate using bidirectional key (frozenset treats {A,B} == {B,A})
                            edge_key = frozenset([e1, e2])
                            if edge_key in seen_edges:
                                continue

                            try:
                                # search_nodes_link returns (src, tgt, description, weight, level)
                                link = search_nodes_link(e1, e2, str(work_dir), level=None)

                                if link:
                                    seen_edges.add(edge_key)
                                    # Preserve original directionality from search_nodes_link
                                    src, tgt, description, weight, level = link
                                    facts.append({
                                        "id": f"{src}||{tgt}",  # Synthetic ID with original direction
                                        "source_node_id": src,  # Original source
                                        "target_node_id": tgt,  # Original target
                                        "summary": description,  # Relationship description
                                        "score": float(weight) if weight else 0.0,
                                        "backend": "leanrag",
                                        "content": None,  # Facts don't have content
                                        "source": None,  # Not applicable to edges
                                        "metadata": {
                                            "level": level,  # Hierarchy level for downstream ranking
                                        },
                                        "extra": {
                                            "corpus": str(work_dir),
                                        },
                                    })
                            except Exception as e:
                                # Log failed link lookups for debugging (may be expected if no relationship exists)
                                logger.debug(f"Link lookup failed for ({e1}, {e2}): {e}")
                                continue

                        # Early exit at path pair level too
                        if len(facts) >= max_results * 3:
                            break

                    # Return top-scored results (sort before truncate ensures best quality)
                    # This is critical: sorting before slicing ensures we return the highest-scored
                    # relationships, not just the first N found (which would be order-dependent)
                    facts.sort(key=lambda x: x["score"], reverse=True)
                    return facts[:max_results]

                finally:
                    os.chdir(original_cwd)

        except ImportError as e:
            raise TransientError(f"Failed to import LeanRAG modules: {e}") from e
        except Exception as e:
            raise BackendError(f"Fact search failed: {e}") from e

    def search_episodes(
        self,
        query: str,
        group_ids: Sequence[str] | None = None,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """Episodes are not supported by LeanRAG.

        LeanRAG chunks are static document segments without provenance information
        (who created/modified content, when changes occurred). Episodes require this
        temporal and actor context which LeanRAG doesn't provide.

        Args:
            query: Search query string (unused)
            group_ids: Optional group IDs (unused)
            max_results: Maximum results (unused)

        Returns:
            Never returns - always raises UnsupportedOperationError

        Raises:
            UnsupportedOperationError: Always raised - LeanRAG doesn't support episodes
        """
        from . import UnsupportedOperationError
        raise UnsupportedOperationError(
            "LeanRAG backend does not support episode search. "
            "Episodes require provenance (who/when) which LeanRAG chunks lack. "
            "LeanRAG chunks are static document segments without actor/time context."
        )

    def get_node(
        self,
        node_id: str,
        group_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get entity node by name (LeanRAG uses names, not UUIDs).

        Args:
            node_id: Entity name to retrieve (e.g., "OAUTH2", "JWT_TOKENS")
            group_id: Optional group ID (ignored - LeanRAG uses separate databases)

        Returns:
            Normalized CoreResult dictionary with node data, or None if not found

        Raises:
            IdNotSupportedError: If node_id is UUID-style (LeanRAG uses entity names)
            ConfigError: If work_dir not set or database not indexed
            BackendError: If retrieval fails
            TransientError: If database connection fails
        """
        # Validate ID format - LeanRAG uses entity names, not UUIDs
        if self._looks_like_uuid(node_id):
            from . import IdNotSupportedError
            raise IdNotSupportedError(
                f"LeanRAG get_node() requires entity names, not UUIDs. "
                f"Received: {node_id[:20]}..."
            )

        work_dir = self.config.work_dir
        if not work_dir:
            raise ConfigError("work_dir must be set before retrieving nodes")

        work_dir = work_dir.resolve()
        if not (work_dir / "threads_chunk.json").exists():
            raise ConfigError(
                f"Database not indexed. threads_chunk.json not found in {work_dir}"
            )

        try:
            # Add LeanRAG to path
            leanrag_abspath = self.config.leanrag_path.resolve()

            # Thread-safe directory change AND sys.path modification
            with _chdir_lock:
                # Add LeanRAG to path inside lock to prevent race conditions
                if str(leanrag_abspath) not in sys.path:
                    sys.path.insert(0, str(leanrag_abspath))

                original_cwd = os.getcwd()
                try:
                    os.chdir(str(self.config.leanrag_path))

                    # Import FalkorDB connection function
                    from leanrag.database.falkordb import get_falkordb_connection

                    # Query entity at ANY level (not just level=0)
                    graph_name = work_dir.name
                    db, graph = get_falkordb_connection(graph_name)

                    try:
                        # Query for entity at any level
                        query = """
                        MATCH (n:Entity {entity_name: $entity_name})
                        RETURN n.entity_name, n.description, n.source_id, n.degree, n.parent, n.level
                        LIMIT 1
                        """

                        result = graph.query(query, params={'entity_name': node_id})

                        if not result.result_set:
                            return None

                        row = result.result_set[0]
                        entity_name, description, source_id, degree, parent, level = row

                        return {
                            "id": entity_name,  # Required by CoreResult
                            "name": entity_name,
                            "summary": description,
                            "score": None,  # Not applicable for direct retrieval
                            "backend": "leanrag",
                            "content": None,  # Entities don't have content
                            "source": source_id,  # Chunk hash where entity was found
                            "metadata": {
                                "parent": parent,  # Hierarchical parent
                                "degree": degree,  # Graph connectivity
                                "level": level,  # Hierarchy level
                            },
                            "extra": {
                                "corpus": str(work_dir),
                            },
                        }
                    finally:
                        db.close()  # Always close database connection

                finally:
                    os.chdir(original_cwd)

        except ImportError as e:
            raise TransientError(f"Failed to import LeanRAG modules: {e}") from e
        except Exception as e:
            raise BackendError(f"Node retrieval failed: {e}") from e

    def _looks_like_uuid(self, value: str) -> bool:
        """Check if a string looks like a UUID or ULID.
        
        Args:
            value: String to check
            
        Returns:
            True if value resembles a UUID/ULID format
        """
        if not value:
            return False
        
        # UUID: 8-4-4-4-12 hex digits with hyphens
        # ULID: 26 alphanumeric characters (base32)
        # Check length and character patterns
        if len(value) == 36 and value.count('-') == 4:
            # Looks like UUID
            return True
        elif len(value) == 26 and value.isalnum() and value.isupper():
            # Looks like ULID
            return True
        
        return False

    def get_edge(
        self,
        edge_id: str,
        group_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get edge/relationship by synthetic ID (SOURCE||TARGET format).

        LeanRAG doesn't have native edge IDs. This method expects edge_id
        in the format "SOURCE||TARGET" where SOURCE and TARGET are entity names.

        Args:
            edge_id: Synthetic edge ID in format "SOURCE||TARGET"
            group_id: Optional group ID (ignored - LeanRAG uses separate databases)

        Returns:
            Normalized CoreResult dictionary with edge data, or None if not found

        Raises:
            IdNotSupportedError: If edge_id format is invalid (must be SOURCE||TARGET)
            ConfigError: If work_dir not set or database not indexed
            BackendError: If retrieval fails
            TransientError: If database connection fails
        """
        # Validate edge_id format
        if "||" not in edge_id or not edge_id.strip():
            from . import IdNotSupportedError
            raise IdNotSupportedError(
                f"LeanRAG get_edge() requires synthetic edge IDs in format SOURCE||TARGET. "
                f"Received: {edge_id[:50]}"
            )

        # Validate that split produces non-empty entity names
        parts = edge_id.split("||", 1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            from . import IdNotSupportedError
            raise IdNotSupportedError(
                f"LeanRAG get_edge() requires synthetic edge IDs in format SOURCE||TARGET. "
                f"Received: {edge_id[:50]}"
            )

        work_dir = self.config.work_dir
        if not work_dir:
            raise ConfigError("work_dir must be set before retrieving edges")

        work_dir = work_dir.resolve()
        if not (work_dir / "threads_chunk.json").exists():
            raise ConfigError(
                f"Database not indexed. threads_chunk.json not found in {work_dir}"
            )

        try:
            # Add LeanRAG to path
            leanrag_abspath = self.config.leanrag_path.resolve()

            # Thread-safe directory change AND sys.path modification
            with _chdir_lock:
                # Add LeanRAG to path inside lock to prevent race conditions
                if str(leanrag_abspath) not in sys.path:
                    sys.path.insert(0, str(leanrag_abspath))

                original_cwd = os.getcwd()
                try:
                    os.chdir(str(self.config.leanrag_path))

                    # Import LeanRAG function
                    from leanrag.database.adapter import search_nodes_link

                    # Parse synthetic ID (already validated above)
                    source, target = parts

                    # Retrieve relationship
                    # search_nodes_link returns (src, tgt, description, weight, level)
                    result = search_nodes_link(source, target, str(work_dir), level=None)

                    if not result:
                        return None

                    src, tgt, description, weight, level = result

                    return {
                        "id": edge_id,  # Required by CoreResult
                        "source_node_id": src,
                        "target_node_id": tgt,
                        "summary": description,
                        "score": float(weight) if weight else 0.0,
                        "backend": "leanrag",
                        "content": None,  # Edges don't have content
                        "source": None,  # Not applicable to edges
                        "metadata": {
                            "level": level,
                        },
                        "extra": {
                            "corpus": str(work_dir),
                        },
                    }

                finally:
                    os.chdir(original_cwd)

        except ImportError as e:
            raise TransientError(f"Failed to import LeanRAG modules: {e}") from e
        except Exception as e:
            raise BackendError(f"Edge retrieval failed: {e}") from e

    def get_capabilities(self) -> Capabilities:
        """Return LeanRAG capabilities with Phase 1 protocol extensions."""
        return Capabilities(
            # Existing capabilities
            embeddings=bool(self.config.embedding_api_base),
            entity_extraction=True,
            graph_query=True,
            rerank=False,
            schema_versions=["1.0.0"],
            supports_falkor=True,
            supports_milvus=bool(self.config.embedding_api_base),
            supports_neo4j=False,
            max_tokens=1024,
            
            # Phase 1 protocol extensions
            supports_nodes=True,       # ✅ Via Milvus vector search on entity embeddings
            supports_facts=True,       # ✅ Via entity search + relationship traversal
            supports_episodes=False,   # ❌ No provenance (chunks are static segments)
            supports_chunks=False,     # ❌ Not yet implemented (will be added in future phase)
            supports_edges=True,       # ✅ Via synthetic SOURCE||TARGET ID format
            
            # ID modality (how LeanRAG identifies entities/edges)
            node_id_type="name",       # Entity names (e.g., "OAUTH2", "JWT_TOKENS")
            edge_id_type="synthetic",  # SOURCE||TARGET format (no native edge IDs)
        )
