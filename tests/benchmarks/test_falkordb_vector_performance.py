"""FalkorDB vector performance benchmarks.

Per MEMORY_INTEGRATION_ROADMAP.md Milestone 2.2:
- Insert 5k/10k vectors
- Search latency (target: <100ms avg, <500ms p99)
- Recall parity vs baseline (target: ≥90%)

Scaling Note (from GRAPHITI_MCP_ARCHITECTURE.md):
- Current FalkorDB uses inline `vec.cosineDistance` without an index
- For >~10k vectors, benchmark FalkorDB's vector index support
- Document latency degradation curve

Usage:
    # Run benchmarks (requires FalkorDB running on localhost:6379)
    pytest tests/benchmarks/test_falkordb_vector_performance.py -v -s

    # Skip if FalkorDB not available
    pytest tests/benchmarks/ -v -s --ignore-glob="*_performance.py"

Environment:
    FALKORDB_HOST=localhost
    FALKORDB_PORT=6379
    BENCHMARK_VECTOR_COUNT=1000  # Override vector count
"""

import os
import time
import random
import statistics
from dataclasses import dataclass, field
from typing import Optional

import pytest

# Check if FalkorDB is available for integration testing
try:
    from falkordb import FalkorDB
    FALKORDB_AVAILABLE = True
except ImportError:
    FALKORDB_AVAILABLE = False

from watercooler_memory.infrastructure import (
    EXPECTED_DIM,
    FalkorDBVectorAdapter,
    FalkorDBVectorConfig,
)


# Benchmark configuration
VECTOR_DIM = EXPECTED_DIM  # 1024
SMALL_BATCH = int(os.environ.get("BENCHMARK_SMALL_BATCH", "1000"))
LARGE_BATCH = int(os.environ.get("BENCHMARK_LARGE_BATCH", "5000"))
SEARCH_ITERATIONS = int(os.environ.get("BENCHMARK_SEARCH_ITERATIONS", "100"))

# Performance targets
TARGET_INSERT_TPS = 100  # Vectors per second minimum
TARGET_SEARCH_AVG_MS = 100  # Average search latency
TARGET_SEARCH_P99_MS = 500  # P99 search latency
TARGET_RECALL = 0.90  # 90% recall


@dataclass
class BenchmarkResult:
    """Result from a benchmark run."""

    name: str
    vector_count: int
    total_time_ms: float
    avg_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    throughput: float  # Operations per second
    passed: bool
    notes: str = ""


@dataclass
class RecallResult:
    """Result from recall evaluation."""

    total_queries: int
    correct_matches: int
    recall: float
    passed: bool


def generate_random_vector(dim: int = VECTOR_DIM, seed: Optional[int] = None) -> list[float]:
    """Generate a random normalized vector."""
    if seed is not None:
        random.seed(seed)
    vec = [random.gauss(0, 1) for _ in range(dim)]
    # Normalize
    norm = sum(x * x for x in vec) ** 0.5
    return [x / norm for x in vec]


def generate_similar_vector(
    base: list[float],
    noise_level: float = 0.1,
) -> list[float]:
    """Generate a vector similar to base with added noise."""
    noisy = [x + random.gauss(0, noise_level) for x in base]
    norm = sum(x * x for x in noisy) ** 0.5
    return [x / norm for x in noisy]


@pytest.fixture(scope="module")
def falkordb_adapter():
    """Create and connect FalkorDB adapter for benchmarks."""
    if not FALKORDB_AVAILABLE:
        pytest.skip("FalkorDB not installed")

    config = FalkorDBVectorConfig.from_env()
    adapter = FalkorDBVectorAdapter(config)

    try:
        adapter.connect()
        if not adapter.healthcheck():
            pytest.skip("FalkorDB not responding")
    except Exception as e:
        pytest.skip(f"Could not connect to FalkorDB: {e}")

    # Use a dedicated benchmark database
    adapter._graph = adapter._client.select_graph("benchmark_test")

    yield adapter

    # Cleanup
    try:
        adapter._graph.query("MATCH (n:BenchmarkVector) DELETE n")
    except Exception:
        pass
    adapter.disconnect()


def _measure_latencies(operation, iterations: int) -> list[float]:
    """Measure latencies for an operation over multiple iterations."""
    latencies = []
    for _ in range(iterations):
        start = time.perf_counter()
        operation()
        end = time.perf_counter()
        latencies.append((end - start) * 1000)  # Convert to ms
    return latencies


@pytest.mark.benchmark
@pytest.mark.skipif(not FALKORDB_AVAILABLE, reason="FalkorDB not installed")
class TestFalkorDBInsertPerformance:
    """Benchmark vector insertion performance."""

    def test_insert_small_batch(self, falkordb_adapter):
        """Benchmark inserting SMALL_BATCH vectors."""
        adapter = falkordb_adapter
        count = SMALL_BATCH

        # Generate vectors
        vectors = [
            (f"bench:{i}", generate_random_vector(seed=i), {"index": i})
            for i in range(count)
        ]

        # Measure insertion time
        start = time.perf_counter()
        stored = adapter.batch_store_vectors(
            node_label="BenchmarkVector",
            vectors=vectors,
        )
        end = time.perf_counter()

        total_ms = (end - start) * 1000
        tps = count / (end - start)

        result = BenchmarkResult(
            name=f"insert_{count}_vectors",
            vector_count=count,
            total_time_ms=total_ms,
            avg_latency_ms=total_ms / count,
            p50_latency_ms=total_ms / count,  # Batch operation
            p95_latency_ms=total_ms / count,
            p99_latency_ms=total_ms / count,
            throughput=tps,
            passed=tps >= TARGET_INSERT_TPS,
            notes=f"Target: ≥{TARGET_INSERT_TPS} TPS",
        )

        print(f"\n=== Insert Benchmark ({count} vectors) ===")
        print(f"Total time: {result.total_time_ms:.2f}ms")
        print(f"Throughput: {result.throughput:.2f} vectors/sec")
        print(f"Avg latency: {result.avg_latency_ms:.2f}ms per vector")
        print(f"Target: ≥{TARGET_INSERT_TPS} TPS - {'PASS' if result.passed else 'FAIL'}")

        assert stored == count
        # Soft assertion - don't fail test, just report
        if not result.passed:
            pytest.xfail(f"Throughput {tps:.2f} below target {TARGET_INSERT_TPS}")

    def test_insert_large_batch(self, falkordb_adapter):
        """Benchmark inserting LARGE_BATCH vectors (scaling test)."""
        adapter = falkordb_adapter
        count = LARGE_BATCH

        # Clear previous data
        try:
            adapter._graph.query("MATCH (n:BenchmarkVectorLarge) DELETE n")
        except Exception:
            pass

        # Generate vectors
        vectors = [
            (f"benchlg:{i}", generate_random_vector(seed=i + 10000), {"index": i})
            for i in range(count)
        ]

        # Measure insertion time
        start = time.perf_counter()
        stored = adapter.batch_store_vectors(
            node_label="BenchmarkVectorLarge",
            vectors=vectors,
        )
        end = time.perf_counter()

        total_ms = (end - start) * 1000
        tps = count / (end - start)

        print(f"\n=== Large Insert Benchmark ({count} vectors) ===")
        print(f"Total time: {total_ms:.2f}ms ({total_ms/1000:.2f}s)")
        print(f"Throughput: {tps:.2f} vectors/sec")
        print(f"Avg latency: {total_ms/count:.2f}ms per vector")

        assert stored == count


@pytest.mark.benchmark
@pytest.mark.skipif(not FALKORDB_AVAILABLE, reason="FalkorDB not installed")
class TestFalkorDBSearchPerformance:
    """Benchmark vector search performance."""

    @pytest.fixture(autouse=True)
    def setup_search_data(self, falkordb_adapter):
        """Ensure test data exists for search benchmarks."""
        adapter = falkordb_adapter

        # Check if data already exists
        try:
            result = adapter._graph.query(
                "MATCH (n:BenchmarkVector) RETURN count(n) AS count"
            )
            if result.result_set and result.result_set[0][0] >= SMALL_BATCH:
                return  # Data already exists
        except Exception:
            pass

        # Insert test data
        vectors = [
            (f"bench:{i}", generate_random_vector(seed=i), {"index": i})
            for i in range(SMALL_BATCH)
        ]
        adapter.batch_store_vectors(
            node_label="BenchmarkVector",
            vectors=vectors,
        )

    def test_search_latency(self, falkordb_adapter):
        """Benchmark search latency over multiple queries."""
        adapter = falkordb_adapter
        iterations = SEARCH_ITERATIONS

        # Generate random query vectors
        query_vectors = [generate_random_vector(seed=i + 50000) for i in range(iterations)]

        # Measure search latencies
        latencies = []
        for qv in query_vectors:
            start = time.perf_counter()
            results = adapter.search_vectors(
                node_label="BenchmarkVector",
                query_vector=qv,
                limit=10,
            )
            end = time.perf_counter()
            latencies.append((end - start) * 1000)

        # Calculate statistics
        avg_ms = statistics.mean(latencies)
        p50_ms = statistics.median(latencies)
        p95_ms = sorted(latencies)[int(len(latencies) * 0.95)]
        p99_ms = sorted(latencies)[int(len(latencies) * 0.99)]

        result = BenchmarkResult(
            name=f"search_{SMALL_BATCH}_vectors",
            vector_count=SMALL_BATCH,
            total_time_ms=sum(latencies),
            avg_latency_ms=avg_ms,
            p50_latency_ms=p50_ms,
            p95_latency_ms=p95_ms,
            p99_latency_ms=p99_ms,
            throughput=1000 / avg_ms,  # Queries per second
            passed=(avg_ms <= TARGET_SEARCH_AVG_MS and p99_ms <= TARGET_SEARCH_P99_MS),
            notes=f"Target: avg≤{TARGET_SEARCH_AVG_MS}ms, p99≤{TARGET_SEARCH_P99_MS}ms",
        )

        print(f"\n=== Search Latency Benchmark ({SMALL_BATCH} vectors, {iterations} queries) ===")
        print(f"Avg latency: {result.avg_latency_ms:.2f}ms")
        print(f"P50 latency: {result.p50_latency_ms:.2f}ms")
        print(f"P95 latency: {result.p95_latency_ms:.2f}ms")
        print(f"P99 latency: {result.p99_latency_ms:.2f}ms")
        print(f"Throughput: {result.throughput:.2f} queries/sec")
        print(f"Targets: avg≤{TARGET_SEARCH_AVG_MS}ms, p99≤{TARGET_SEARCH_P99_MS}ms")
        print(f"Result: {'PASS' if result.passed else 'FAIL'}")

        # Soft assertion
        if not result.passed:
            pytest.xfail(
                f"Latency exceeded targets: avg={avg_ms:.2f}ms, p99={p99_ms:.2f}ms"
            )


@pytest.mark.benchmark
@pytest.mark.skipif(not FALKORDB_AVAILABLE, reason="FalkorDB not installed")
class TestFalkorDBRecall:
    """Benchmark recall accuracy."""

    def test_recall_known_vectors(self, falkordb_adapter):
        """Test recall by searching for known similar vectors."""
        adapter = falkordb_adapter

        # Clear and insert fresh data
        try:
            adapter._graph.query("MATCH (n:RecallTest) DELETE n")
        except Exception:
            pass

        # Generate base vectors and their noisy variants
        num_bases = 100
        base_vectors = [generate_random_vector(seed=i) for i in range(num_bases)]

        # Store base vectors
        for i, vec in enumerate(base_vectors):
            adapter.store_vector(
                node_label="RecallTest",
                node_id=f"recall:{i}",
                embedding=vec,
                properties={"base_index": i},
            )

        # Search for similar vectors and check if base is in top-k
        correct = 0
        k = 5  # Top-k to check

        for i, base_vec in enumerate(base_vectors):
            # Add small noise to base vector
            query_vec = generate_similar_vector(base_vec, noise_level=0.05)

            results = adapter.search_vectors(
                node_label="RecallTest",
                query_vector=query_vec,
                limit=k,
            )

            # Check if original is in results
            result_ids = [r.node_id for r in results]
            if f"recall:{i}" in result_ids:
                correct += 1

        recall = correct / num_bases

        result = RecallResult(
            total_queries=num_bases,
            correct_matches=correct,
            recall=recall,
            passed=recall >= TARGET_RECALL,
        )

        print(f"\n=== Recall Benchmark (top-{k}) ===")
        print(f"Total queries: {result.total_queries}")
        print(f"Correct matches: {result.correct_matches}")
        print(f"Recall: {result.recall:.2%}")
        print(f"Target: ≥{TARGET_RECALL:.0%}")
        print(f"Result: {'PASS' if result.passed else 'FAIL'}")

        # Cleanup
        adapter._graph.query("MATCH (n:RecallTest) DELETE n")

        if not result.passed:
            pytest.xfail(f"Recall {recall:.2%} below target {TARGET_RECALL:.0%}")


@pytest.mark.benchmark
@pytest.mark.skipif(not FALKORDB_AVAILABLE, reason="FalkorDB not installed")
class TestFalkorDBScaling:
    """Benchmark performance scaling with vector count."""

    def test_scaling_curve(self, falkordb_adapter):
        """Measure search latency at different vector counts."""
        adapter = falkordb_adapter

        # Test at different scales
        scales = [100, 500, 1000, 2000, 5000]
        results = []

        for scale in scales:
            # Clear and insert data
            try:
                adapter._graph.query("MATCH (n:ScaleTest) DELETE n")
            except Exception:
                pass

            vectors = [
                (f"scale:{i}", generate_random_vector(seed=i), {})
                for i in range(scale)
            ]
            adapter.batch_store_vectors(node_label="ScaleTest", vectors=vectors)

            # Measure search latency
            query_vec = generate_random_vector(seed=99999)
            latencies = []
            for _ in range(20):  # 20 queries per scale
                start = time.perf_counter()
                adapter.search_vectors(
                    node_label="ScaleTest",
                    query_vector=query_vec,
                    limit=10,
                )
                end = time.perf_counter()
                latencies.append((end - start) * 1000)

            avg_ms = statistics.mean(latencies)
            results.append((scale, avg_ms))

        print("\n=== Scaling Curve ===")
        print("Vectors | Avg Latency (ms)")
        print("-" * 30)
        for scale, avg_ms in results:
            bar = "█" * int(avg_ms / 5)  # Visual bar
            print(f"{scale:>7} | {avg_ms:>6.2f} {bar}")

        # Cleanup
        adapter._graph.query("MATCH (n:ScaleTest) DELETE n")

        # Check for linear or sub-linear scaling
        # Latency should not grow faster than O(n)
        if len(results) >= 2:
            first_scale, first_latency = results[0]
            last_scale, last_latency = results[-1]
            scale_factor = last_scale / first_scale
            latency_factor = last_latency / first_latency

            print(f"\nScale factor: {scale_factor:.1f}x")
            print(f"Latency factor: {latency_factor:.1f}x")
            print(f"Scaling efficiency: {scale_factor / latency_factor:.2f}")

            if latency_factor > scale_factor * 2:
                pytest.xfail(
                    f"Latency scaling worse than O(n²): {latency_factor:.1f}x for {scale_factor:.1f}x data"
                )
