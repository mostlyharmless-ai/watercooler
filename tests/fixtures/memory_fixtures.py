"""Shared test fixtures for watercooler memory system testing.

This module provides reusable fixtures for testing the three-tier memory system:
- T1 (Baseline Graph): JSONL-based keyword/semantic search
- T2 (Graphiti): FalkorDB temporal knowledge graph
- T3 (LeanRAG): Hierarchical clustering with multi-hop reasoning

Usage:
    from tests.fixtures.memory_fixtures import (
        create_baseline_graph,
        create_mock_graphiti_backend,
        sample_tier_evidence,
    )
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock


# ============================================================================
# Data Classes for Test Data
# ============================================================================


@dataclass
class SampleEntry:
    """Sample thread entry for testing."""

    entry_id: str
    thread_topic: str
    title: str
    body: str
    agent: str = "Claude (dev)"
    role: str = "implementer"
    entry_type: str = "Note"
    timestamp: str = ""
    summary: str = ""
    index: int = 0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()
        if not self.summary:
            self.summary = self.body[:100]

    def to_node_dict(self) -> Dict[str, Any]:
        """Convert to graph node format."""
        return {
            "type": "entry",
            "entry_id": self.entry_id,
            "thread_topic": self.thread_topic,
            "index": self.index,
            "agent": self.agent,
            "role": self.role,
            "entry_type": self.entry_type,
            "title": self.title,
            "timestamp": self.timestamp,
            "summary": self.summary,
            "body": self.body,
        }


@dataclass
class SampleThread:
    """Sample thread for testing."""

    topic: str
    title: str
    status: str = "OPEN"
    ball: str = "Claude"
    summary: str = ""
    entries: List[SampleEntry] = field(default_factory=list)

    def __post_init__(self):
        if not self.summary:
            self.summary = f"Thread about {self.title.lower()}"

    def to_node_dict(self) -> Dict[str, Any]:
        """Convert to graph node format."""
        return {
            "type": "thread",
            "topic": self.topic,
            "title": self.title,
            "status": self.status,
            "ball": self.ball,
            "summary": self.summary,
            "entry_count": len(self.entries),
            "last_updated": self.entries[-1].timestamp if self.entries else "",
        }


# ============================================================================
# Baseline Graph Fixtures
# ============================================================================


def create_baseline_graph(
    threads_dir: Path,
    threads: Optional[List[SampleThread]] = None,
) -> Path:
    """Create a baseline graph directory structure with test data.

    Args:
        threads_dir: Path to threads directory
        threads: List of SampleThread objects. Uses default test data if None.

    Returns:
        Path to the created graph directory
    """
    if threads is None:
        threads = get_default_test_threads()

    # Create graph directory structure (per-thread format)
    graph_dir = threads_dir / "graph" / "baseline"
    graph_dir.mkdir(parents=True, exist_ok=True)

    # Create per-thread directories
    for thread in threads:
        thread_dir = graph_dir / "threads" / thread.topic
        thread_dir.mkdir(parents=True, exist_ok=True)

        # Write meta.json
        meta_file = thread_dir / "meta.json"
        meta_file.write_text(json.dumps(thread.to_node_dict()))

        # Write entries.jsonl
        if thread.entries:
            entries_file = thread_dir / "entries.jsonl"
            with open(entries_file, "w") as f:
                for entry in thread.entries:
                    f.write(json.dumps(entry.to_node_dict()) + "\n")

    return graph_dir


def get_default_test_threads() -> List[SampleThread]:
    """Get default test threads for baseline graph testing."""
    auth_thread = SampleThread(
        topic="auth-implementation",
        title="Authentication Implementation",
        status="OPEN",
        ball="Claude",
        entries=[
            SampleEntry(
                entry_id="01AUTH001",
                thread_topic="auth-implementation",
                title="Authentication Plan",
                body="We will implement JWT tokens with RS256 signing for secure authentication.",
                role="planner",
                entry_type="Plan",
                index=0,
                timestamp="2025-01-15T09:00:00Z",
            ),
            SampleEntry(
                entry_id="01AUTH002",
                thread_topic="auth-implementation",
                title="OAuth2 Integration Started",
                body="Began integrating OAuth2 provider. Using passport.js for the middleware layer.",
                role="implementer",
                entry_type="Note",
                index=1,
                timestamp="2025-01-15T10:00:00Z",
            ),
            SampleEntry(
                entry_id="01AUTH003",
                thread_topic="auth-implementation",
                title="Security Review",
                body="Reviewed the auth implementation. Found potential CSRF vulnerability in callback handler.",
                role="critic",
                entry_type="Note",
                index=2,
                timestamp="2025-01-15T14:00:00Z",
            ),
        ],
    )

    error_thread = SampleThread(
        topic="error-handling",
        title="Error Handling Patterns",
        status="CLOSED",
        ball="User",
        entries=[
            SampleEntry(
                entry_id="01ERR001",
                thread_topic="error-handling",
                title="Error Handling Strategy",
                body="Decided to use try-catch patterns with custom error classes for structured handling.",
                role="planner",
                entry_type="Decision",
                index=0,
                timestamp="2025-01-10T08:00:00Z",
            ),
            SampleEntry(
                entry_id="01ERR002",
                thread_topic="error-handling",
                title="Implementation Complete",
                body="Error handling middleware implemented. All API endpoints now return structured error responses.",
                role="implementer",
                entry_type="Closure",
                index=1,
                timestamp="2025-01-12T16:00:00Z",
            ),
        ],
    )

    return [auth_thread, error_thread]


# ============================================================================
# Mock Backend Factories
# ============================================================================


def create_mock_graphiti_backend(
    search_results: Optional[List[Dict]] = None,
    episodes: Optional[List[Dict]] = None,
    nodes: Optional[List[Dict]] = None,
    facts: Optional[List[Dict]] = None,
) -> MagicMock:
    """Create a mock Graphiti backend for testing.

    Args:
        search_results: Results to return from search operations
        episodes: Episodes to return from get_episodes
        nodes: Nodes to return from search_nodes
        facts: Facts to return from search_memory_facts

    Returns:
        MagicMock configured as a Graphiti backend
    """
    mock = MagicMock()

    # Default responses
    if search_results is None:
        search_results = []
    if episodes is None:
        episodes = []
    if nodes is None:
        nodes = []
    if facts is None:
        facts = []

    # Configure search operations
    mock.search_entries.return_value = search_results
    mock.get_episodes.return_value = {"episodes": episodes, "count": len(episodes)}
    mock.search_nodes.return_value = nodes
    mock.search_memory_facts.return_value = facts

    # Configure write operations
    mock.add_episode_direct = AsyncMock(
        return_value={
            "episode_uuid": "ep-test-uuid-12345",
            "entities_extracted": ["Entity1", "Entity2"],
            "facts_extracted": 2,
        }
    )
    mock.index_entry_as_episode = MagicMock()
    mock.clear_group_episodes = MagicMock(
        return_value={"removed": 5, "group_id": "test-group", "message": "Cleared"}
    )

    # Configure entity/edge operations
    mock.get_entity_edge = MagicMock(
        return_value={
            "uuid": "edge-uuid-123",
            "fact": "Test relationship",
            "source_node_uuid": "node-1",
            "target_node_uuid": "node-2",
            "valid_at": "2025-01-15T10:00:00Z",
            "created_at": "2025-01-15T10:00:00Z",
            "group_id": "test-group",
        }
    )

    return mock


def create_mock_leanrag_backend(
    search_results: Optional[List[Dict]] = None,
    cluster_count: int = 5,
) -> MagicMock:
    """Create a mock LeanRAG backend for testing.

    Args:
        search_results: Results to return from search operations
        cluster_count: Number of clusters to simulate

    Returns:
        MagicMock configured as a LeanRAG backend
    """
    mock = MagicMock()

    if search_results is None:
        search_results = []

    # Configure search operations
    mock.search.return_value = search_results

    # Configure index operations
    mock_index_result = MagicMock()
    mock_index_result.indexed_count = cluster_count
    mock_index_result.message = f"Indexed {cluster_count} chunks"
    mock.index = MagicMock(return_value=mock_index_result)

    return mock


def create_mock_tier_orchestrator(
    available_tiers: Optional[List[str]] = None,
    query_result: Optional[Dict] = None,
) -> MagicMock:
    """Create a mock TierOrchestrator for testing.

    Args:
        available_tiers: List of available tier names ("T1", "T2", "T3")
        query_result: Result to return from query() method

    Returns:
        MagicMock configured as a TierOrchestrator
    """
    from watercooler_memory.tier_strategy import Tier, TierResult, TierEvidence

    mock = MagicMock()

    if available_tiers is None:
        available_tiers = ["T1"]

    # Convert string tier names to Tier enum
    mock.available_tiers = [Tier(t) for t in available_tiers]

    # Default query result
    if query_result is None:
        mock_result = TierResult(
            query="test query",
            tiers_queried=[Tier.T1],
            primary_tier=Tier.T1,
            sufficient=True,
            evidence=[
                TierEvidence(
                    tier=Tier.T1,
                    id="entry-1",
                    content="Test content",
                    score=0.85,
                    name="Test Entry",
                )
            ],
            message="Found 1 result",
        )
        mock.query = MagicMock(return_value=mock_result)
    else:
        mock_result = TierResult(**query_result)
        mock.query = MagicMock(return_value=mock_result)

    return mock


# ============================================================================
# Sample Evidence Fixtures
# ============================================================================


def sample_tier_evidence(tier: str = "T1", count: int = 3) -> List[Dict]:
    """Generate sample TierEvidence data for testing.

    Args:
        tier: Tier name ("T1", "T2", or "T3")
        count: Number of evidence items to generate

    Returns:
        List of evidence dictionaries
    """
    evidence = []
    for i in range(count):
        evidence.append({
            "tier": tier,
            "id": f"evidence-{tier.lower()}-{i}",
            "content": f"Sample evidence content {i} from {tier}",
            "score": 0.9 - (i * 0.1),  # Descending scores
            "name": f"Evidence {i}",
            "provenance": {
                "thread_topic": "test-thread",
                "entry_id": f"01TEST{i:03d}",
            },
        })
    return evidence


def sample_search_results(count: int = 5) -> List[Dict]:
    """Generate sample search results for testing.

    Args:
        count: Number of results to generate

    Returns:
        List of search result dictionaries
    """
    results = []
    topics = ["auth", "error-handling", "api-design", "testing", "deployment"]

    for i in range(count):
        results.append({
            "entry_id": f"01RES{i:03d}",
            "thread_topic": topics[i % len(topics)],
            "title": f"Search Result {i}",
            "body": f"Content for search result {i} about {topics[i % len(topics)]}",
            "score": 0.95 - (i * 0.05),
            "matched_fields": ["title", "body"],
        })
    return results


# ============================================================================
# Thread Markdown Fixtures
# ============================================================================


def create_thread_markdown(
    threads_dir: Path,
    topic: str,
    title: str,
    entries: List[Dict],
    status: str = "OPEN",
    ball: str = "Claude (dev)",
) -> Path:
    """Create a thread markdown file for testing.

    Args:
        threads_dir: Directory to create the thread in
        topic: Thread topic (used as filename)
        title: Thread title
        entries: List of entry dictionaries with keys:
            agent, timestamp, role, entry_type, title, entry_id, body
        status: Thread status
        ball: Who has the ball

    Returns:
        Path to created thread file
    """
    threads_dir.mkdir(parents=True, exist_ok=True)

    content_parts = [
        f"# {topic} — Thread",
        "",
        f"Status: {status}",
        f"Ball: {ball}",
        "",
        "---",
    ]

    for entry in entries:
        content_parts.extend([
            "",
            f"Entry: {entry['agent']} {entry['timestamp']}",
            f"Role: {entry['role']}",
            f"Type: {entry['entry_type']}",
            f"Title: {entry['title']}",
            f"<!-- Entry-ID: {entry['entry_id']} -->",
            "",
            entry['body'],
            "",
            "---",
        ])

    content = "\n".join(content_parts)
    thread_path = threads_dir / f"{topic}.md"
    thread_path.write_text(content)

    return thread_path


# ============================================================================
# MCP Context Fixtures
# ============================================================================


def create_mock_mcp_context() -> MagicMock:
    """Create a mock MCP context for tool testing.

    Returns:
        MagicMock configured as an MCP Context
    """
    mock = MagicMock()
    mock.request_context = MagicMock()
    return mock


# ============================================================================
# Configuration Fixtures
# ============================================================================


def create_tier_config(
    threads_dir: Optional[Path] = None,
    code_path: Optional[Path] = None,
    t1_enabled: bool = True,
    t2_enabled: bool = False,
    t3_enabled: bool = False,
    min_results: int = 3,
    min_confidence: float = 0.5,
    max_tiers: int = 2,
) -> Dict[str, Any]:
    """Create a tier configuration dictionary for testing.

    Args:
        threads_dir: Path to threads directory
        code_path: Path to code repository
        t1_enabled: Enable T1 (Baseline)
        t2_enabled: Enable T2 (Graphiti)
        t3_enabled: Enable T3 (LeanRAG)
        min_results: Minimum results for sufficiency
        min_confidence: Minimum confidence for sufficiency
        max_tiers: Maximum tiers to query

    Returns:
        Configuration dictionary
    """
    return {
        "threads_dir": threads_dir,
        "code_path": code_path,
        "t1_enabled": t1_enabled,
        "t2_enabled": t2_enabled,
        "t3_enabled": t3_enabled,
        "min_results": min_results,
        "min_confidence": min_confidence,
        "max_tiers": max_tiers,
    }


def create_graphiti_config(
    enabled: bool = True,
    llm_api_key: str = "stub-llm-key",
    embedding_api_key: str = "stub-embed-key",
    llm_model: str = "gpt-4o-mini",
    embedding_model: str = "bge-m3",
    database: str = "test_db",
) -> MagicMock:
    """Create a mock Graphiti configuration for testing.

    Args:
        enabled: Whether Graphiti is enabled
        llm_api_key: LLM API key
        embedding_api_key: Embedding API key
        llm_model: LLM model name
        embedding_model: Embedding model name
        database: Database name

    Returns:
        MagicMock configured as GraphitiConfig
    """
    if not enabled:
        return None

    mock = MagicMock()
    mock.llm_api_key = llm_api_key
    mock.embedding_api_key = embedding_api_key
    mock.llm_model = llm_model
    mock.embedding_model = embedding_model
    mock.database = database
    mock.openai_api_key = None  # Deprecated field
    mock.llm_api_base = "http://localhost:8000/v1"

    return mock
