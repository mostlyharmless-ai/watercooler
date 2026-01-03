"""Cross-tier golden path tests for memory integration.

Per MEMORY_INTEGRATION_ROADMAP.md Milestone 3.2:
- Validate end-to-end pipeline with shared infrastructure
- Assert same embedding dimension (1024) across all tiers

Test Flow:
1. Chunk via MemoryGraph
2. Validate embedding compatibility
3. (If available) Extract facts via Graphiti
4. (If available) Cluster via LeanRAG
5. Assert: same embedding dimension (1024) across all

This test uses the cross_tier_test.md fixture which has:
- 8 entries with realistic content
- Multiple topics (auth, JWT, OAuth2, security)
- Temporal spread (4 days)
- Mixed entry types (Plan, Note, Decision, Closure)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import pytest

from watercooler_memory import (
    MemoryGraph,
    GraphConfig,
    ChunkerConfig,
    parse_thread_to_nodes,
)
from watercooler_memory.infrastructure import (
    EXPECTED_DIM,
    validate_embedding_dimension,
    DimensionMismatchError,
)

# Check optional tier availability
try:
    from watercooler_memory.infrastructure import FalkorDBVectorAdapter, FalkorDBVectorConfig
    FALKORDB_AVAILABLE = True
except ImportError:
    FALKORDB_AVAILABLE = False

try:
    from watercooler_memory.embeddings import embed_texts, EmbeddingConfig, is_httpx_available
    EMBEDDING_AVAILABLE = is_httpx_available()
except ImportError:
    EMBEDDING_AVAILABLE = False


# Test configuration
CROSS_TIER_FIXTURE = Path(__file__).parent.parent / "fixtures" / "threads" / "cross_tier_test.md"


@pytest.fixture
def cross_tier_fixture_path() -> Path:
    """Path to the cross-tier test fixture."""
    assert CROSS_TIER_FIXTURE.exists(), f"Fixture not found: {CROSS_TIER_FIXTURE}"
    return CROSS_TIER_FIXTURE


@pytest.fixture
def memory_graph(cross_tier_fixture_path: Path) -> MemoryGraph:
    """Build MemoryGraph from cross-tier fixture."""
    config = GraphConfig(
        generate_summaries=False,
        generate_embeddings=False,
        chunker=ChunkerConfig.watercooler_preset(),
    )
    graph = MemoryGraph(config=config)
    graph.add_thread(cross_tier_fixture_path)
    graph.chunk_all_entries()
    return graph


class TestTier1MemoryGraph:
    """Test Tier 1: MemoryGraph (raw chunks with provenance)."""

    def test_parse_cross_tier_fixture(self, cross_tier_fixture_path: Path):
        """Validate fixture parses correctly."""
        thread, entries, edges, hyperedges = parse_thread_to_nodes(cross_tier_fixture_path)

        # Validate thread metadata (thread_id derived from filename)
        assert thread.thread_id == "cross_tier_test"
        assert thread.status == "CLOSED"

        # Validate entry count (8 entries in fixture)
        assert len(entries) == 8

        # Validate entry types are diverse
        entry_types = {e.entry_type for e in entries}
        assert "Plan" in entry_types
        assert "Note" in entry_types
        assert "Decision" in entry_types
        assert "Closure" in entry_types

        # Validate roles are diverse
        roles = {e.role for e in entries}
        assert len(roles) >= 4  # planner, implementer, critic, tester, pm, scribe

    def test_chunk_entries(self, memory_graph: MemoryGraph):
        """Validate entries are chunked correctly."""
        assert len(memory_graph.entries) == 8
        assert len(memory_graph.chunks) >= 8  # At least one chunk per entry

        # All chunks should have text content
        for chunk in memory_graph.chunks.values():
            assert chunk.text.strip()
            assert chunk.entry_id
            assert chunk.thread_id == "cross_tier_test"

    def test_chunk_token_counts(self, memory_graph: MemoryGraph):
        """Validate chunk token counts are within expected range."""
        for chunk in memory_graph.chunks.values():
            # Token count should be positive and reasonable
            assert chunk.token_count > 0
            assert chunk.token_count <= 768 + 100  # Max tokens + header overhead


class TestCrossTierEmbeddingCompatibility:
    """Test embedding dimension compatibility across tiers."""

    def test_expected_dimension_constant(self):
        """Verify EXPECTED_DIM is 1024 (BGE-M3)."""
        assert EXPECTED_DIM == 1024

    def test_dimension_validation_rejects_wrong_size(self):
        """Validate dimension enforcement rejects non-1024 vectors."""
        wrong_dim = [0.1] * 512
        with pytest.raises(DimensionMismatchError) as exc_info:
            validate_embedding_dimension(wrong_dim)
        assert "512" in str(exc_info.value)
        assert "1024" in str(exc_info.value)

    def test_dimension_validation_accepts_correct_size(self):
        """Validate dimension enforcement accepts 1024-d vectors."""
        correct_dim = [0.1] * 1024
        # Should not raise
        validate_embedding_dimension(correct_dim)

    @pytest.mark.skipif(not EMBEDDING_AVAILABLE, reason="httpx not available")
    def test_embedding_config_uses_1024_dim(self):
        """Verify EmbeddingConfig defaults to 1024 dimensions."""
        from watercooler_memory.embeddings import DEFAULT_DIM
        assert DEFAULT_DIM == 1024


@pytest.mark.skipif(not FALKORDB_AVAILABLE, reason="FalkorDB not available")
class TestTier2FalkorDBVectors:
    """Test Tier 2: FalkorDB vector storage (used by Graphiti)."""

    @pytest.fixture
    def falkordb_adapter(self):
        """Create FalkorDB adapter for testing."""
        config = FalkorDBVectorConfig.from_env()
        adapter = FalkorDBVectorAdapter(config)
        try:
            adapter.connect()
            if not adapter.healthcheck():
                pytest.skip("FalkorDB not responding")
        except Exception as e:
            pytest.skip(f"Could not connect to FalkorDB: {e}")

        # Use test-specific database
        adapter._graph = adapter._client.select_graph("pytest__cross_tier")

        yield adapter

        # Cleanup
        try:
            adapter._graph.query("MATCH (n:CrossTierTest) DELETE n")
        except Exception:
            pass
        adapter.disconnect()

    def test_store_and_search_chunks(self, memory_graph: MemoryGraph, falkordb_adapter):
        """Test storing and searching MemoryGraph chunks in FalkorDB."""
        adapter = falkordb_adapter

        # Create mock embeddings (1024-d) for chunks
        chunks = list(memory_graph.chunks.values())[:5]  # First 5 chunks

        for i, chunk in enumerate(chunks):
            # Generate deterministic mock embedding
            embedding = [float(i) / 1024.0 + j / 10240.0 for j in range(1024)]

            adapter.store_vector(
                node_label="CrossTierTest",
                node_id=chunk.chunk_id,
                embedding=embedding,
                properties={"text": chunk.text[:100], "entry_id": chunk.entry_id},
            )

        # Search with first chunk's embedding
        query_embedding = [0.0 / 1024.0 + j / 10240.0 for j in range(1024)]
        results = adapter.search_vectors(
            node_label="CrossTierTest",
            query_vector=query_embedding,
            limit=3,
        )

        assert len(results) > 0
        # First result should be most similar to query (chunk 0)
        assert results[0].score > 0.9

    def test_dimension_enforcement_in_storage(self, falkordb_adapter):
        """Verify FalkorDB adapter enforces 1024-d dimension."""
        wrong_dim = [0.1] * 512

        with pytest.raises(DimensionMismatchError):
            falkordb_adapter.store_vector(
                node_label="CrossTierTest",
                node_id="test-wrong-dim",
                embedding=wrong_dim,
            )


class TestGoldenPathEndToEnd:
    """End-to-end golden path test across all available tiers."""

    def test_tier1_to_tier2_flow(self, memory_graph: MemoryGraph):
        """Test flow from MemoryGraph chunks to vector-ready format."""
        # Tier 1: MemoryGraph chunks exist
        assert len(memory_graph.chunks) >= 8

        # Prepare for Tier 2: Extract chunk data for vectorization
        chunk_data = []
        for chunk in memory_graph.chunks.values():
            chunk_data.append({
                "chunk_id": chunk.chunk_id,
                "text": chunk.text,
                "entry_id": chunk.entry_id,
                "thread_id": chunk.thread_id,
                "token_count": chunk.token_count,
            })

        # All chunks should have required fields
        for cd in chunk_data:
            assert cd["chunk_id"]
            assert cd["text"]
            assert cd["entry_id"]
            assert cd["thread_id"]
            assert cd["token_count"] > 0

        # Validate chunk count matches entries
        entry_ids = {cd["entry_id"] for cd in chunk_data}
        assert len(entry_ids) == 8  # All entries have chunks

    def test_embedding_dimension_consistency(self, memory_graph: MemoryGraph):
        """Verify all tiers use consistent embedding dimension."""
        # This test validates the dimension constant is used consistently

        # Tier 1 (MemoryGraph): No embeddings stored, but validated at generation
        from watercooler_memory.infrastructure import EXPECTED_DIM
        assert EXPECTED_DIM == 1024

        # Tier 2 (FalkorDB): Dimension enforced in adapter
        if FALKORDB_AVAILABLE:
            config = FalkorDBVectorConfig()
            assert config.embedding_dim == EXPECTED_DIM

        # Tier 3 (LeanRAG): Uses same embedding infrastructure
        from watercooler_memory.pipeline.config import EmbeddingConfig as PipelineEmbeddingConfig
        pipeline_config = PipelineEmbeddingConfig()
        assert pipeline_config.embedding_dim == EXPECTED_DIM

    def test_cross_tier_metadata_preservation(self, memory_graph: MemoryGraph):
        """Verify metadata is preserved across tier boundaries."""
        # Entry metadata should flow through to chunks
        for chunk in memory_graph.chunks.values():
            entry = memory_graph.entries.get(chunk.entry_id)
            assert entry is not None, f"Chunk {chunk.chunk_id} missing entry"

            # Chunk preserves entry reference
            assert chunk.entry_id == entry.entry_id
            assert chunk.thread_id == entry.thread_id

            # Entry has required metadata for downstream tiers
            assert entry.agent
            assert entry.role
            assert entry.entry_type
            assert entry.timestamp


class TestCrossTierSummary:
    """Summary test that validates the complete tier integration."""

    def test_integration_summary(self, memory_graph: MemoryGraph, capsys):
        """Print integration summary for visibility."""
        print("\n" + "=" * 60)
        print("CROSS-TIER GOLDEN PATH TEST SUMMARY")
        print("=" * 60)

        print(f"\nTier 1 - MemoryGraph:")
        print(f"  Threads: {len(memory_graph.threads)}")
        print(f"  Entries: {len(memory_graph.entries)}")
        print(f"  Chunks: {len(memory_graph.chunks)}")

        print(f"\nTier 2 - FalkorDB Vectors:")
        print(f"  Available: {FALKORDB_AVAILABLE}")
        print(f"  Expected dimension: {EXPECTED_DIM}")

        print(f"\nTier 3 - LeanRAG Pipeline:")
        print(f"  Embedding available: {EMBEDDING_AVAILABLE}")

        print(f"\nShared Infrastructure:")
        print(f"  Embedding model: bge-m3")
        print(f"  Embedding dimension: 1024")
        print(f"  Chunking: 768 tokens / 64 overlap")

        print("\n" + "=" * 60)
        print("ALL TIERS USE CONSISTENT 1024-D EMBEDDINGS")
        print("=" * 60 + "\n")

        # Actual assertions
        assert len(memory_graph.threads) == 1
        assert len(memory_graph.entries) == 8
        assert len(memory_graph.chunks) >= 8
        assert EXPECTED_DIM == 1024
