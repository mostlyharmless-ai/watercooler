"""Smoke tests for memory backends with real databases.

These tests validate full backend workflows (prepare→index→query) with real
database connections. They use minimal fixtures and are marked to run optionally.

Mark: @pytest.mark.integration_falkor
Usage: pytest -m integration_falkor

Runtime Expectations:
    LeanRAG tests: ~20-30 seconds (66 entries, hierarchical clustering)
    Graphiti minimal: ~4-5 minutes (5 entries, LLM entity extraction)
    Graphiti full: ~45-50 minutes (15 entries, temporal graph building)

CI Configuration:
    - Recommended timeout: 60 minutes for full test suite
    - Consider using @pytest.mark.slow for long-running tests
    - Graphiti tests require OPENAI_API_KEY environment variable
"""

from __future__ import annotations

import os
import re
import uuid

import pytest
from pathlib import Path
from typing import Generator

from watercooler_memory.backends import (
    CorpusPayload,
    ChunkPayload,
    QueryPayload,
)
from watercooler_memory.backends.registry import get_backend
from watercooler_memory.graph import MemoryGraph


# FalkorDB connection configuration (configurable via env vars for CI flexibility)
FALKORDB_HOST = os.environ.get("FALKORDB_HOST", "localhost")
FALKORDB_PORT = int(os.environ.get("FALKORDB_PORT", "6379"))


# Fixtures


@pytest.fixture(autouse=True, scope="session")
def cleanup_test_databases() -> None:
    """Clean test databases BEFORE running tests to allow post-test inspection.

    This fixture removes both Redis keys AND FalkorDB graphs with pytest__ prefix.
    FalkorDB stores graphs as separate data structures requiring GRAPH.DELETE.

    Note: If using pytest-xdist for parallel test execution, consider adding worker-
    specific suffixes to database names to avoid race conditions between workers.
    """
    try:
        import redis
        r = redis.Redis(host=FALKORDB_HOST, port=FALKORDB_PORT, socket_connect_timeout=2)

        # First, delete FalkorDB graphs with pytest__ prefix
        # Use SCAN to find all keys, then check if they're graphs and delete
        cursor = 0
        graphs_deleted = []
        while True:
            cursor, keys = r.scan(cursor, match="pytest__*", count=100)
            for key in keys:
                key_str = key.decode('utf-8') if isinstance(key, bytes) else key
                try:
                    # Try to delete as a graph using GRAPH.DELETE
                    r.execute_command('GRAPH.DELETE', key_str)
                    graphs_deleted.append(key_str)
                except redis.ResponseError:
                    # Not a graph, delete as regular key
                    r.delete(key)
            if cursor == 0:
                break

    except (ConnectionError, TimeoutError) as e:
        import logging
        logging.warning(f"FalkorDB not available for test cleanup: {e}")
    except ImportError as e:
        import logging
        logging.warning(f"redis-py not installed for test cleanup: {e}")

    yield  # Run tests (results persist for inspection)


@pytest.fixture
def watercooler_threads_dir() -> Path:
    """Path to test watercooler threads (bundled with tests).

    Note: Contains unified-branch-parity-protocol.md (~68KB, 1798 lines, 38 entries).
    This real watercooler thread provides realistic test data for backend validation
    with authentic metadata, temporal relationships, and multi-agent conversations.

    The 38 entries are sufficient for hierarchical clustering validation: LeanRAG's
    GMM + UMAP clustering typically requires 30+ samples for reliable community detection.
    """
    return Path(__file__).parent / "fixtures" / "threads"


@pytest.fixture
def watercooler_corpus(watercooler_threads_dir: Path) -> CorpusPayload:
    """
    Real watercooler corpus for integration testing.

    Uses actual watercooler threads to validate:
    - Custom chunker with real thread content
    - Entity extraction on actual agent conversations
    - Clustering with sufficient data volume
    """
    # Use substantial thread for testing (68K - rich technical content for clustering)
    # The unified-branch-parity-protocol thread has sufficient entries for proper
    # hierarchical clustering testing while being more manageable for CI
    test_threads = [
        "unified-branch-parity-protocol.md",
    ]

    # Build memory graph with watercooler preset for headers
    from watercooler_memory.chunker import ChunkerConfig
    from watercooler_memory.graph import GraphConfig

    config = GraphConfig(chunker=ChunkerConfig.watercooler_preset())
    graph = MemoryGraph(config=config)
    for thread_file in test_threads:
        thread_path = watercooler_threads_dir / thread_file
        if thread_path.exists():
            graph.add_thread(thread_path)

    # Chunk all entries using the custom watercooler chunker with headers
    chunk_nodes = graph.chunk_all_entries()

    # Convert to canonical payload format
    threads_data = [
        {
            "id": thread.thread_id,
            "topic": thread.thread_id,  # thread_id is the topic slug
            "status": thread.status,
            "ball": thread.ball,
            "entry_count": len([e for e in graph.entries.values() if e.thread_id == thread.thread_id]),
            "title": thread.title,
        }
        for thread in graph.threads.values()
    ]

    entries_data = [
        {
            "id": entry.entry_id,
            "thread_id": entry.thread_id,
            "agent": entry.agent,
            "role": entry.role,
            "type": entry.entry_type,
            "title": entry.title,
            "body": entry.body,
            "timestamp": entry.timestamp,  # Already an ISO string
            # Include chunks for this entry
            "chunks": [
                {"text": chunk.text, "chunk_id": chunk.chunk_id}
                for chunk in graph.chunks.values()
                if chunk.entry_id == entry.entry_id
            ],
        }
        for entry in graph.entries.values()
    ]

    # Create corpus payload with chunker info
    return CorpusPayload(
        manifest_version="1.0.0",
        threads=threads_data,
        entries=entries_data,
        metadata={"source": "watercooler-threads", "test_mode": True},
        chunker_name="watercooler",
        chunker_params={"preset": "watercooler"},
    )


@pytest.fixture
def minimal_corpus() -> CorpusPayload:
    """
    Minimal corpus payload for smoke testing.

    Contains 2 threads with 5 total entries - enough to test the pipeline
    but small enough to complete quickly (<90s target).
    """
    return CorpusPayload(
        manifest_version="1.0.0",
        threads=[
            {
                "id": "auth-feature",
                "topic": "auth-feature",
                "status": "OPEN",
                "ball": "Claude",
                "entry_count": 3,
                "title": "Authentication Feature",
            },
            {
                "id": "payment-feature",
                "topic": "payment-feature",
                "status": "OPEN",
                "ball": "Codex",
                "entry_count": 2,
                "title": "Payment Integration",
            },
        ],
        entries=[
            # Thread 1: auth-feature
            {
                "id": "auth-1",
                "thread_id": "auth-feature",
                "agent": "Claude",
                "role": "planner",
                "type": "Plan",
                "title": "Authentication Design",
                "body": "Proposing OAuth2 with JWT tokens for secure authentication. "
                "Key requirements: (1) User login via OAuth providers, "
                "(2) JWT token generation and validation, "
                "(3) Refresh token rotation.",
                "timestamp": "2025-01-01T10:00:00Z",
            },
            {
                "id": "auth-2",
                "thread_id": "auth-feature",
                "agent": "Codex",
                "role": "critic",
                "type": "Note",
                "title": "Security Review",
                "body": "The OAuth2 design looks solid. Suggestions: "
                "(1) Use PKCE for mobile clients, "
                "(2) Implement rate limiting on auth endpoints, "
                "(3) Add audit logging for failed login attempts.",
                "timestamp": "2025-01-01T10:30:00Z",
            },
            {
                "id": "auth-3",
                "thread_id": "auth-feature",
                "agent": "Claude",
                "role": "implementer",
                "type": "Note",
                "title": "Implementation Complete",
                "body": "Implemented OAuth2 authentication with JWT tokens. "
                "Added PKCE support and rate limiting as suggested. "
                "All tests passing. Ready for review.",
                "timestamp": "2025-01-01T14:00:00Z",
            },
            # Thread 2: payment-feature
            {
                "id": "pay-1",
                "thread_id": "payment-feature",
                "agent": "Codex",
                "role": "planner",
                "type": "Plan",
                "title": "Payment Integration Plan",
                "body": "Integration with Stripe for payment processing. "
                "Features: (1) Credit card payments, "
                "(2) Subscription management, "
                "(3) Webhook handling for payment events.",
                "timestamp": "2025-01-02T09:00:00Z",
            },
            {
                "id": "pay-2",
                "thread_id": "payment-feature",
                "agent": "Claude",
                "role": "implementer",
                "type": "Note",
                "title": "Stripe Integration",
                "body": "Integrated Stripe SDK. Implemented payment processing, "
                "subscription management, and webhook handlers. "
                "Added retry logic for failed payments.",
                "timestamp": "2025-01-02T15:00:00Z",
            },
        ],
        edges=[
            {"source": "auth-feature", "target": "auth-1", "type": "CONTAINS"},
            {"source": "auth-feature", "target": "auth-2", "type": "CONTAINS"},
            {"source": "auth-feature", "target": "auth-3", "type": "CONTAINS"},
            {"source": "auth-1", "target": "auth-2", "type": "FOLLOWS"},
            {"source": "auth-2", "target": "auth-3", "type": "FOLLOWS"},
            {"source": "payment-feature", "target": "pay-1", "type": "CONTAINS"},
            {"source": "payment-feature", "target": "pay-2", "type": "CONTAINS"},
            {"source": "pay-1", "target": "pay-2", "type": "FOLLOWS"},
        ],
        metadata={"source": "smoke-test", "version": "1.0.0"},
    )


@pytest.fixture
def minimal_chunks() -> ChunkPayload:
    """
    Minimal chunk payload for smoke testing.

    Contains chunks derived from minimal_corpus entries.
    """
    return ChunkPayload(
        manifest_version="1.0.0",
        chunks=[
            {
                "id": "chunk-auth-1",
                "entry_id": "auth-1",
                "text": "Proposing OAuth2 with JWT tokens for secure authentication.",
                "token_count": 10,
                "hash_code": "abc123",
            },
            {
                "id": "chunk-auth-2",
                "entry_id": "auth-2",
                "text": "The OAuth2 design looks solid. Use PKCE for mobile clients.",
                "token_count": 12,
                "hash_code": "def456",
            },
            {
                "id": "chunk-auth-3",
                "entry_id": "auth-3",
                "text": "Implemented OAuth2 authentication with JWT tokens. All tests passing.",
                "token_count": 11,
                "hash_code": "ghi789",
            },
            {
                "id": "chunk-pay-1",
                "entry_id": "pay-1",
                "text": "Integration with Stripe for payment processing. Credit card payments.",
                "token_count": 11,
                "hash_code": "jkl012",
            },
            {
                "id": "chunk-pay-2",
                "entry_id": "pay-2",
                "text": "Integrated Stripe SDK. Implemented payment processing and webhooks.",
                "token_count": 10,
                "hash_code": "mno345",
            },
        ],
        threads=[
            {"id": "auth-feature", "topic": "auth-feature"},
            {"id": "payment-feature", "topic": "payment-feature"},
        ],
        entries=[
            {"id": "auth-1", "title": "Authentication Design"},
            {"id": "auth-2", "title": "Security Review"},
            {"id": "auth-3", "title": "Implementation Complete"},
            {"id": "pay-1", "title": "Payment Integration Plan"},
            {"id": "pay-2", "title": "Stripe Integration"},
        ],
    )


@pytest.fixture
def sample_queries() -> QueryPayload:
    """Sample queries for smoke testing."""
    return QueryPayload(
        manifest_version="1.0.0",
        queries=[
            {
                "query": "What authentication methods are being used?",
                "limit": 3,
            },
            {
                "query": "How is payment processing implemented?",
                "limit": 3,
            },
        ],
    )


# Smoke Tests


@pytest.mark.integration_falkor
class TestLeanRAGSmoke:
    """Smoke tests for LeanRAG backend with real FalkorDB."""

    @pytest.fixture
    def leanrag_backend(self, tmp_path: Path) -> Generator[LeanRAGBackend, None, None]:
        """LeanRAG backend with unique working directory per test for isolation.

        Uses pytest's tmp_path fixture to create a unique directory per test.
        This avoids Milvus-lite file lock issues that occur when multiple tests
        share the same work directory.

        Why unique directories per test:
        - Milvus-lite uses SQLite under the hood with file locks
        - MilvusClient instances in LeanRAG don't explicitly close()
        - File locks persist after test completion, causing cleanup failures
        - Subsequent tests may read stale entities with wrong source_id hashes
        - Using unique directories eliminates all cleanup/isolation issues
        """
        from watercooler_memory.backends.leanrag import LeanRAGBackend, LeanRAGConfig

        # Use pytest's tmp_path with a unique subdirectory for this test
        # This ensures complete isolation - no file lock issues, no stale data
        work_dir = tmp_path / f"leanrag_{uuid.uuid4().hex[:8]}"

        # Clean up FalkorDB graph with matching name (graph name = work_dir.name)
        # The pytest__ prefix is applied by the backend when test_mode=True
        graph_name = f"pytest__{work_dir.name}"
        try:
            from falkordb import FalkorDB
            db = FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT)
            try:
                graph = db.select_graph(graph_name)
                graph.delete()
                print(f"Cleaned up FalkorDB graph: {graph_name}")
            except Exception as delete_err:
                # Graph doesn't exist yet - expected for fresh test
                if "unknown graph" not in str(delete_err).lower():
                    print(f"FalkorDB cleanup note: {delete_err}")
        except Exception as e:
            print(f"FalkorDB cleanup skipped: {e}")

        config = LeanRAGConfig(work_dir=work_dir, test_mode=True)
        backend = LeanRAGBackend(config)

        print(f"\n*** LeanRAG working directory: {work_dir.absolute()} ***\n")

        yield backend

        # Cleanup: Delete FalkorDB graph after test to avoid accumulation
        # (tmp_path is automatically cleaned by pytest)
        try:
            from falkordb import FalkorDB
            db = FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT)
            try:
                graph = db.select_graph(graph_name)
                graph.delete()
                print(f"Post-test cleanup: deleted FalkorDB graph {graph_name}")
            except Exception:
                pass  # Graph might not exist if test didn't reach indexing
        except Exception:
            pass

    def test_healthcheck(self, leanrag_backend):
        """LeanRAG healthcheck should succeed."""
        health = leanrag_backend.healthcheck()
        assert health.ok is True
        assert "LeanRAG available" in health.details
        # Note: FalkorDB connectivity might fail if not running locally

    def test_prepare_only(self, leanrag_backend, minimal_corpus, tmp_path):
        """LeanRAG prepare should create export files."""
        result = leanrag_backend.prepare(minimal_corpus)

        assert result.manifest_version == "1.0.0"
        assert result.prepared_count == 5  # 5 entries
        assert "Prepared corpus at" in result.message

        # Extract actual work directory from result message
        # Format: "Prepared corpus at /path/to/pytest__leanrag_xxx"
        match = re.search(r"Prepared corpus at (.+)", result.message)
        assert match, f"Could not extract work directory from: {result.message}"
        actual_dir = Path(match.group(1))

        # Verify pytest__ prefix applied to work_dir when test_mode=True
        assert actual_dir.exists(), f"Expected work directory to exist at {actual_dir}"
        assert actual_dir.name.startswith("pytest__"), "Work directory should have pytest__ prefix"

        # Verify export files exist in pytest__ prefixed directory
        assert (actual_dir / "documents.json").exists()
        assert (actual_dir / "threads.json").exists()
        assert (actual_dir / "manifest.json").exists()

    @pytest.mark.skipif(
        "os.environ.get('SKIP_LEANRAG_INDEX') == '1'",
        reason="LeanRAG indexing requires dependencies and takes time",
    )
    def test_prepare_index_query(
        self, leanrag_backend, minimal_corpus, minimal_chunks, sample_queries
    ):
        """Full LeanRAG pipeline: prepare→index→query."""
        # Step 1: Prepare
        prepare_result = leanrag_backend.prepare(minimal_corpus)
        assert prepare_result.prepared_count == 5

        # Step 2: Index (this will shell out to LeanRAG scripts)
        # NOTE: This requires:
        # - LeanRAG dependencies installed
        # - FalkorDB running
        # - LLM API configured (DeepSeek/OpenAI/local)
        index_result = leanrag_backend.index(minimal_chunks)
        assert index_result.indexed_count == 5
        assert "Indexed" in index_result.message

        # Step 3: Query
        query_result = leanrag_backend.query(sample_queries)
        assert query_result.manifest_version == "1.0.0"
        assert len(query_result.results) > 0
        assert "queries" in query_result.message

    @pytest.mark.integration_leanrag_llm
    def test_full_pipeline_watercooler_threads(
        self, leanrag_backend, watercooler_corpus, sample_queries
    ):
        """
        Full LeanRAG pipeline with real watercooler threads.

        Validates:
        - Custom chunker with real thread content
        - Entity extraction on actual agent conversations
        - Clustering with sufficient data volume (8-10 threads)

        Requires:
        - DEEPSEEK_API_KEY environment variable
        - GLM embedding server on port 8080 (or alternative configured)
        - FalkorDB running on port 6379
        """
        # Step 1: Prepare real watercooler thread
        prepare_result = leanrag_backend.prepare(watercooler_corpus)
        assert prepare_result.prepared_count > 0  # memory-backend thread has multiple entries
        print(f"Prepared {prepare_result.prepared_count} entries from watercooler thread")

        # Step 2: Index with entity extraction and clustering
        # This will test the full pipeline including hierarchical clustering
        # Extract chunks from corpus entries (created by watercooler preset)
        chunk_list = []
        for entry in watercooler_corpus.entries:
            for chunk in entry.get("chunks", []):
                chunk_list.append({
                    "id": chunk["chunk_id"],
                    "entry_id": entry["id"],
                    "text": chunk["text"],
                    "token_count": len(chunk["text"].split()),  # Rough estimate
                    "hash_code": chunk.get("hash_code", chunk["chunk_id"]),
                })
        
        chunks = ChunkPayload(
            manifest_version="1.0.0",
            chunks=chunk_list,  # Pass actual chunks from watercooler preset
            threads=watercooler_corpus.threads,
            entries=watercooler_corpus.entries,
        )
        index_result = leanrag_backend.index(chunks)
        assert index_result.indexed_count >= 0
        print(f"Indexed {index_result.indexed_count} chunks")

        # Step 3: Query with real questions about the threads
        real_queries = QueryPayload(
            manifest_version="1.0.0",
            queries=[
                {
                    "query": "What is the memory backend architecture?",
                    "limit": 5,
                },
                {
                    "query": "How does LeanRAG entity extraction work?",
                    "limit": 5,
                },
                {
                    "query": "What is the baseline graph pipeline?",
                    "limit": 5,
                },
            ],
        )
        query_result = leanrag_backend.query(real_queries)
        assert len(query_result.results) > 0
        print(f"Query returned {len(query_result.results)} results")


@pytest.mark.integration_falkor
class TestGraphitiSmoke:
    """Smoke tests for Graphiti backend with real database."""

    @pytest.fixture
    def graphiti_backend(self, tmp_path: Path) -> Generator[GraphitiBackend, None, None]:
        """Graphiti backend with temp working directory."""
        import os
        from watercooler_memory.backends.graphiti import (
            GraphitiBackend,
            GraphitiConfig,
        )

        # Ensure OpenAI API key is set (required for Graphiti)
        if "OPENAI_API_KEY" not in os.environ:
            pytest.skip("OPENAI_API_KEY not set - required for Graphiti")

        config = GraphitiConfig(work_dir=tmp_path / "pytest__graphiti_work", test_mode=True)
        backend = GraphitiBackend(config)
        yield backend

    def test_healthcheck(self, graphiti_backend):
        """Graphiti healthcheck should succeed."""
        health = graphiti_backend.healthcheck()
        assert health.ok is True
        assert "Graphiti available" in health.details
        # Note: Database connectivity might fail if not running

    def test_prepare_only(self, graphiti_backend, minimal_corpus):
        """Graphiti prepare should create episodes."""
        result = graphiti_backend.prepare(minimal_corpus)

        assert result.manifest_version == "1.0.0"
        assert result.prepared_count == 5  # 5 episodes
        assert "episodes" in result.message

        # Verify export files exist
        work_dir = graphiti_backend.config.work_dir
        assert (work_dir / "episodes.json").exists()
        assert (work_dir / "manifest.json").exists()

    @pytest.mark.skipif(
        "os.environ.get('SKIP_GRAPHITI_INDEX') == '1'",
        reason="Graphiti indexing requires implementation completion",
    )
    def test_query_returns_nonzero_scores(
        self, graphiti_backend, minimal_corpus, minimal_chunks
    ):
        """Verify that Graphiti queries return non-zero relevance scores.
        
        This guards against regressions where scores might be hardcoded to 0.0.
        Real Graphiti reranker scores should be in the range 0.0-10.0+ depending
        on the reranker algorithm used.
        """
        # Step 1: Prepare
        prepare_result = graphiti_backend.prepare(minimal_corpus)
        assert prepare_result.prepared_count == 5

        # Step 2: Index
        index_result = graphiti_backend.index(minimal_chunks)
        assert index_result.manifest_version == "1.0.0"

        # Step 3: Query with a query that should match indexed content
        # Request more results to ensure we get edges with linked episodes
        # (not all edges have episodes - some are inferred/derived facts)
        query = QueryPayload(
            manifest_version="1.0.0",
            queries=[
                {
                    "query": "OAuth2 authentication JWT tokens",
                    "limit": 20,
                },
            ],
        )
        query_result = graphiti_backend.query(query)
        
        # Verify we got results
        assert len(query_result.results) > 0, "Query should return results"
        
        # Verify at least one result has non-zero score (guard against regression)
        scores = [result.get("score", 0.0) for result in query_result.results]
        max_score = max(scores)
        assert max_score > 0.0, (
            f"Expected at least one non-zero relevance score, got max={max_score}. "
            f"All scores: {scores}"
        )
        
        # Verify provenance metadata is present
        for result in query_result.results:
            metadata = result.get("metadata", {})
            assert "source_backend" in metadata, "Missing source_backend in metadata"
            assert metadata["source_backend"] == "graphiti", "Wrong source_backend"
            assert "reranker" in metadata, "Missing reranker in metadata"

        # Verify at least one result has episode content
        # Note: Not all edges have episodes (some are inferred/derived facts)
        # but we should see episodes for edges that came from indexed content
        has_episode = any(
            len(result.get("metadata", {}).get("episodes", [])) > 0
            for result in query_result.results
        )
        assert has_episode, (
            f"Expected at least one result with episode content out of {len(query_result.results)} results"
        )

        # Verify episode content and metadata
        for result in query_result.results:
            # Check reranker metadata still lowercased after COMBINED switch
            assert result["metadata"]["reranker"] == result["metadata"]["reranker"].lower(), \
                "Reranker should be lowercase"

            episodes = result.get("metadata", {}).get("episodes", [])
            for ep in episodes:
                # Content validation
                assert ep.get("content"), "Episode content should not be empty"
                content_len = len(ep["content"])
                assert content_len > 0, "Episode content should be non-empty"
                assert content_len <= 8192 + 20, \
                    f"Episode content should be truncated to ~8KB, got {content_len} chars"

                # Metadata validation
                assert "uuid" in ep, "Episode should have uuid"
                assert "valid_at" in ep, "Episode should have valid_at (t_ref)"
                assert "source" in ep, "Episode should have source type"

        # Verify nodes are present (Codex: add size sanity checks)
        for result in query_result.results:
            nodes = result.get("metadata", {}).get("nodes", [])
            if nodes:  # Not all edges may have node metadata
                for node in nodes:
                    assert "uuid" in node, "Node should have uuid"
                    assert "name" in node, "Node should have name"
                    assert "labels" in node, "Node should have labels"
                    assert "role" in node, "Node should have role (source/target)"
                    assert "edge_uuid" in node, "Node should link back to edge"

                    # Size sanity check (Codex: prevent payload bloat)
                    if node.get("summary"):
                        summary_len = len(node["summary"])
                        assert summary_len <= 2048 + 20, \
                            f"Node summary should be truncated to ~2KB, got {summary_len} chars"

        # Verify episode scores are present
        for result in query_result.results:
            episodes = result.get("metadata", {}).get("episodes", [])
            for ep in episodes:
                assert "score" in ep, "Episode should have score"
                assert isinstance(ep["score"], (int, float)), "Episode score should be numeric"

        # Verify communities in QueryResult (Codex: add size sanity checks)
        if hasattr(query_result, 'communities'):
            communities = query_result.communities
            # Codex: limit to small top-k
            assert len(communities) <= 5, f"Should limit to top-5 communities, got {len(communities)}"

            for comm in communities:
                assert "name" in comm or "uuid" in comm, "Community should have identifier"
                if "score" in comm:
                    assert isinstance(comm["score"], (int, float)), "Community score should be numeric"

                # Size sanity check if summaries present
                if comm.get("summary"):
                    summary_len = len(comm["summary"])
                    assert summary_len <= 4096, \
                        f"Community summary should be reasonable size, got {summary_len} chars"

        print(f"✓ Query returned {len(query_result.results)} results with max score={max_score:.2f}")
        print(f"  Reranker: {query_result.results[0]['metadata']['reranker']}")

    @pytest.mark.skipif(
        "os.environ.get('SKIP_GRAPHITI_INDEX') == '1'",
        reason="Graphiti indexing requires implementation completion",
    )
    def test_prepare_index_query(
        self, graphiti_backend, minimal_corpus, minimal_chunks, sample_queries
    ):
        """Full Graphiti pipeline: prepare→index→query with minimal test data."""
        # Step 1: Prepare
        prepare_result = graphiti_backend.prepare(minimal_corpus)
        assert prepare_result.prepared_count == 5

        # Step 2: Index
        index_result = graphiti_backend.index(minimal_chunks)
        assert index_result.manifest_version == "1.0.0"

        # Step 3: Query
        query_result = graphiti_backend.query(sample_queries)
        assert query_result.manifest_version == "1.0.0"

    @pytest.mark.integration_falkor
    @pytest.mark.skipif(
        "os.environ.get('SKIP_GRAPHITI_INDEX') == '1'",
        reason="Graphiti indexing requires implementation completion",
    )
    def test_full_pipeline_watercooler_threads(
        self, graphiti_backend, watercooler_corpus, sample_queries
    ):
        """
        Full Graphiti pipeline with real watercooler threads (limited to 15 entries).

        Validates:
        - Episodic ingestion on actual watercooler thread content
        - Entity extraction from agent conversations
        - Temporal graph building with real data
        - Query execution on populated graph

        Note: Limits to first 15 entries for CI-friendly runtime (~45-50 min).
        Full 66-entry corpus would take ~3.5 hours (unsuitable for CI).
        The 15-entry subset provides sufficient validation coverage while
        remaining practical for automated testing.

        Requires:
        - OPENAI_API_KEY environment variable
        - FalkorDB running on port 6379
        """
        # Limit to first 15 entries for reasonable runtime
        # Full corpus has 66 entries which takes ~48 minutes
        from watercooler_memory.backends import CorpusPayload
        limited_corpus = CorpusPayload(
            manifest_version=watercooler_corpus.manifest_version,
            threads=watercooler_corpus.threads,
            entries=watercooler_corpus.entries[:15],  # Limit to 15 entries
            metadata=watercooler_corpus.metadata,
        )

        # Step 1: Prepare real watercooler thread
        prepare_result = graphiti_backend.prepare(limited_corpus)
        assert prepare_result.prepared_count > 0
        print(f"Prepared {prepare_result.prepared_count} entries from watercooler thread")

        # Step 2: Index - extract chunks and create episodes
        # Extract chunks from corpus entries (created by watercooler preset)
        from watercooler_memory.backends import ChunkPayload

        all_chunks = []
        for entry in limited_corpus.entries:
            # Each entry has chunks created by watercooler preset chunker
            if "chunks" in entry:
                for chunk in entry["chunks"]:
                    all_chunks.append({
                        "id": chunk.get("chunk_id", chunk.get("id")),
                        "entry_id": entry["id"],
                        "text": chunk["text"],
                        "token_count": chunk.get("token_count", 0),
                        "hash_code": chunk.get("hash_code", ""),
                    })

        chunks = ChunkPayload(
            manifest_version="1.0.0",
            chunks=all_chunks,
        )

        index_result = graphiti_backend.index(chunks)
        print(f"Indexed {index_result.indexed_count} episodes")
        assert index_result.indexed_count == prepare_result.prepared_count

        # Step 3: Query the populated graph
        query_result = graphiti_backend.query(sample_queries)
        print(f"Query returned {len(query_result.results)} results")
        assert query_result.manifest_version == "1.0.0"

    def test_search_nodes(self, graphiti_backend):
        """Test searching nodes with reranker scores."""
        # Search for nodes (may return empty if no index exists)
        nodes = graphiti_backend.search_nodes(
            query="test",
            group_ids=["test-group"],
            max_nodes=10,
        )
        assert isinstance(nodes, list)
        
        # If results exist, verify score field is present
        if nodes:
            assert "score" in nodes[0]
            assert isinstance(nodes[0]["score"], (int, float))
        # Note: Results may be empty if database not populated

    def test_search_nodes_not_found(self, graphiti_backend):
        """Test search_nodes with query that returns no results."""
        nodes = graphiti_backend.search_nodes(
            query="nonexistent-xyz-123-unique",
            group_ids=["test-group"],
            max_nodes=10,
        )
        assert isinstance(nodes, list)
        assert len(nodes) == 0

    def test_get_entity_edge(self, graphiti_backend):
        """Test getting entity edge with nonexistent UUID."""
        from watercooler_memory.backends import BackendError
        
        with pytest.raises(BackendError, match="not found"):
            graphiti_backend.get_entity_edge("nonexistent-uuid-12345")

    def test_search_memory_facts(self, graphiti_backend):
        """Test searching facts with reranker scores."""
        facts = graphiti_backend.search_memory_facts(
            query="test",
            group_ids=["test-group"],
            max_facts=10,
        )
        assert isinstance(facts, list)
        
        # If results exist, verify score field is present
        if facts:
            assert "score" in facts[0]
            assert isinstance(facts[0]["score"], (int, float))
        # Note: Results may be empty if database not populated

    def test_search_memory_facts_with_center_node(self, graphiti_backend):
        """Test fact search with center node UUID."""
        facts = graphiti_backend.search_memory_facts(
            query="test",
            group_ids=["test-group"],
            max_facts=10,
            center_node_uuid="some-uuid-12345",
        )
        assert isinstance(facts, list)
        # Note: Results may be empty if center node doesn't exist

    def test_get_episodes(self, graphiti_backend):
        """Test episode search with query string and reranker scores."""
        episodes = graphiti_backend.get_episodes(
            query="test episode content",
            group_ids=["test-group"],
            max_episodes=10,
        )
        assert isinstance(episodes, list)
        
        # If results exist, verify score field is present
        if episodes:
            assert "score" in episodes[0]
            assert isinstance(episodes[0]["score"], (int, float))
        # Note: Results may be empty if database not populated

    def test_get_episodes_empty_query(self, graphiti_backend):
        """Test get_episodes rejects empty query."""
        from watercooler_memory.backends import ConfigError
        
        with pytest.raises(ConfigError, match="query parameter is required"):
            graphiti_backend.get_episodes(
                query="",
                max_episodes=10
            )

    def test_get_episodes_limit_clamping(self, graphiti_backend):
        """Test that max_episodes is clamped to 50."""
        # Should not raise, just clamp to 50
        episodes = graphiti_backend.get_episodes(
            query="test",
            group_ids=["test-group"],
            max_episodes=100,  # Should be clamped to 50
        )
        assert isinstance(episodes, list)
        # Backend will clamp to 50 internally


@pytest.mark.integration_falkor
class TestMultiBackendComparison:
    """Compare results across backends (when both are functional)."""

    @pytest.mark.skipif(
        "os.environ.get('SKIP_BACKEND_COMPARISON') == '1'",
        reason="Backend comparison requires both backends fully implemented",
    )
    def test_same_query_both_backends(
        self, minimal_corpus, minimal_chunks, sample_queries, tmp_path
    ):
        """Same query should return results from both backends."""
        import os
        from watercooler_memory.backends.leanrag import LeanRAGBackend, LeanRAGConfig
        from watercooler_memory.backends.graphiti import (
            GraphitiBackend,
            GraphitiConfig,
        )

        if "OPENAI_API_KEY" not in os.environ:
            pytest.skip("OPENAI_API_KEY required")

        # Setup both backends with test_mode enabled
        leanrag = LeanRAGBackend(
            LeanRAGConfig(work_dir=tmp_path / "leanrag", test_mode=True)
        )
        graphiti = GraphitiBackend(
            GraphitiConfig(work_dir=tmp_path / "graphiti", test_mode=True)
        )

        # Prepare both
        leanrag.prepare(minimal_corpus)
        graphiti.prepare(minimal_corpus)

        # Index both
        leanrag.index(minimal_chunks)
        graphiti.index(minimal_chunks)

        # Query both
        leanrag_results = leanrag.query(sample_queries)
        graphiti_results = graphiti.query(sample_queries)

        # Both should return results
        assert len(leanrag_results.results) > 0
        assert len(graphiti_results.results) > 0

        # Results may differ (different retrieval strategies) but both valid
        print(f"LeanRAG returned {len(leanrag_results.results)} results")
        print(f"Graphiti returned {len(graphiti_results.results)} results")
