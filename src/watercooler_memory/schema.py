"""Schema definitions for memory graph nodes and edges.

This module defines the graph-specific node types that wrap/compose with
the existing ThreadEntry from watercooler.thread_entries. These schemas
are internal to the memory graph (not shared with watercooler-site).

Design principles:
- Compose with existing ThreadEntry, don't duplicate
- Add graph-specific metadata (embeddings, summaries, temporal tracking)
- Support bi-temporal model (event_time from source, ingestion_time when processed)
- Enable projection to LeanRAG and future Graphiti formats
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import uuid

from ._utils import _utc_now_iso


def _generate_id() -> str:
    """Generate a UUID for node/edge identification."""
    return str(uuid.uuid4())


class EdgeType(str, Enum):
    """Types of edges in the memory graph."""

    # Containment hierarchy
    CONTAINS = "contains"  # Thread→Entry, Entry→Chunk

    # Temporal sequencing
    FOLLOWS = "follows"  # Entry→Entry (sequential in thread)
    PRECEDES = "precedes"  # Inverse of follows

    # References
    REFERENCES = "references"  # Entry→Commit, Entry→Entity
    MENTIONS = "mentions"  # Chunk→Entity

    # Entity relationships
    RELATES_TO = "relates_to"  # Entity→Entity (semantic)
    SUPERSEDES = "supersedes"  # Entity→Entity (temporal evolution)

    # Git DAG (future)
    PARENT_OF = "parent_of"  # Commit→Commit


class HyperedgeType(str, Enum):
    """Types of hyperedges in the memory graph."""

    THREAD_MEMBERSHIP = "thread_membership"  # All entries in a thread
    TOPIC_CLUSTER = "topic_cluster"  # LeanRAG hierarchical cluster
    TAG = "tag"  # User-defined grouping
    BRANCH_CONTEXT = "branch_context"  # All entries on a git branch


@dataclass
class ThreadNode:
    """Graph node representing a watercooler thread.

    This wraps thread-level metadata with graph-specific attributes.
    The original thread data comes from parsing the .md file header.

    Attributes:
        thread_id: Topic slug (e.g., "feature-auth")
        title: Thread title from header
        status: Thread status (OPEN, IN_REVIEW, CLOSED, etc.)
        ball: Current ball holder
        created_at: First entry timestamp (event_time)
        updated_at: Last entry timestamp
        summary: Generated summary of thread content
        embedding: Vector embedding of summary (None until computed)
        entry_ids: Ordered list of entry IDs in this thread
        branch_context: Git branch name (if known)
        initial_commit: First associated commit SHA (if known)
        ingestion_time: When this node was created in the graph
    """

    thread_id: str
    title: str
    status: str
    ball: str
    created_at: str
    updated_at: str
    entry_ids: list[str] = field(default_factory=list)
    summary: str = ""
    embedding: Optional[list[float]] = None
    branch_context: Optional[str] = None
    initial_commit: Optional[str] = None
    ingestion_time: str = field(default_factory=_utc_now_iso)

    @property
    def event_time(self) -> str:
        """Event time is when the thread was created."""
        return self.created_at

    @property
    def node_id(self) -> str:
        """Unique identifier for this node in the graph."""
        return f"thread:{self.thread_id}"


@dataclass
class EntryNode:
    """Graph node representing a thread entry.

    This wraps the existing ThreadEntry with graph-specific attributes.
    Core entry data comes from thread_entries.parse_thread_entries().

    Attributes:
        entry_id: ULID from entry header (unique identifier)
        thread_id: Parent thread topic slug
        index: Zero-based position in thread
        agent: Agent name (e.g., "Claude Code")
        role: Agent role (planner, implementer, etc.)
        entry_type: Entry type (Note, Plan, Decision, etc.)
        title: Entry title
        timestamp: Entry timestamp (event_time)
        body: Full entry body text
        summary: Generated summary of entry content
        embedding: Vector embedding of summary (None until computed)
        chunk_ids: Child chunk IDs
        sequence_index: Global sequence within thread
        preceding_entry_id: Previous entry in sequence
        following_entry_id: Next entry in sequence
        ingestion_time: When this node was created in the graph
    """

    entry_id: str
    thread_id: str
    index: int
    agent: Optional[str]
    role: Optional[str]
    entry_type: Optional[str]
    title: Optional[str]
    timestamp: Optional[str]
    body: str
    chunk_ids: list[str] = field(default_factory=list)
    summary: str = ""
    embedding: Optional[list[float]] = None
    sequence_index: int = 0
    preceding_entry_id: Optional[str] = None
    following_entry_id: Optional[str] = None
    ingestion_time: str = field(default_factory=_utc_now_iso)

    @property
    def event_time(self) -> Optional[str]:
        """Event time is the entry timestamp."""
        return self.timestamp

    @property
    def node_id(self) -> str:
        """Unique identifier for this node in the graph."""
        return f"entry:{self.entry_id}"


@dataclass
class ChunkNode:
    """Graph node representing a text chunk from an entry.

    Chunks are created by splitting entry bodies for embedding.
    They inherit temporal attributes from their parent entry.

    Attributes:
        chunk_id: Hash-based identifier
        entry_id: Parent entry ULID
        thread_id: Grandparent thread topic slug
        index: Position within entry
        text: Chunk text content
        token_count: Number of tokens in chunk
        embedding: Vector embedding (None until computed)
        event_time: Inherited from parent entry
        ingestion_time: When this chunk was created
    """

    chunk_id: str
    entry_id: str
    thread_id: str
    index: int
    text: str
    token_count: int
    embedding: Optional[list[float]] = None
    event_time: Optional[str] = None
    ingestion_time: str = field(default_factory=_utc_now_iso)

    @property
    def node_id(self) -> str:
        """Unique identifier for this node in the graph."""
        return f"chunk:{self.chunk_id}"


@dataclass
class EntityNode:
    """Graph node representing an extracted entity.

    Entities are concepts, code symbols, decisions, etc. extracted
    from entry/chunk text via NER or pattern matching.

    Attributes:
        entity_id: UUID identifier
        name: Canonical entity name
        entity_type: Type classification (CONCEPT, CODE_SYMBOL, etc.)
        description: Entity description
        aliases: Alternative names for this entity
        source_chunks: Chunk IDs where this entity was extracted
        embedding: Vector embedding (None until computed)
        first_seen: First mention event_time
        last_seen: Most recent mention event_time
        ingestion_time: When this entity was created
    """

    entity_id: str = field(default_factory=_generate_id)
    name: str = ""
    entity_type: str = ""
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    source_chunks: list[str] = field(default_factory=list)
    embedding: Optional[list[float]] = None
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    ingestion_time: str = field(default_factory=_utc_now_iso)

    @property
    def node_id(self) -> str:
        """Unique identifier for this node in the graph."""
        return f"entity:{self.entity_id}"


@dataclass
class DocumentNode:
    """Graph node representing an ingested reference document.

    Documents are standalone files (white papers, reference docs) that
    are ingested separately from watercooler threads.

    Attributes:
        doc_id: Hash of file path + content.
        file_path: Source file path.
        title: Document title.
        doc_type: Type classification ("whitepaper", "reference", "generic").
        metadata: Additional metadata (authors, year, source).
        chunk_ids: Child chunk IDs.
        summary: Generated summary (None until computed).
        embedding: Vector embedding (None until computed).
        ingestion_time: When this document was ingested.
    """

    doc_id: str
    file_path: str
    title: str
    doc_type: str = "generic"
    metadata: dict = field(default_factory=dict)
    chunk_ids: list[str] = field(default_factory=list)
    summary: str = ""
    embedding: Optional[list[float]] = None
    ingestion_time: str = field(default_factory=_utc_now_iso)

    @property
    def node_id(self) -> str:
        """Unique identifier for this node in the graph."""
        return f"document:{self.doc_id}"


@dataclass
class DocumentChunkNode:
    """Graph node representing a chunk from a document.

    Similar to ChunkNode but for standalone documents rather than entries.

    Attributes:
        chunk_id: Hash-based identifier.
        doc_id: Parent document ID.
        index: Position within document.
        text: Chunk text content.
        token_count: Number of tokens.
        section_path: Section breadcrumb if available.
        embedding: Vector embedding (None until computed).
        ingestion_time: When this chunk was created.
    """

    chunk_id: str
    doc_id: str
    index: int
    text: str
    token_count: int
    section_path: Optional[str] = None
    embedding: Optional[list[float]] = None
    ingestion_time: str = field(default_factory=_utc_now_iso)

    @property
    def node_id(self) -> str:
        """Unique identifier for this node in the graph."""
        return f"doc_chunk:{self.chunk_id}"


@dataclass
class Edge:
    """Directed edge connecting two nodes in the graph.

    Edges support temporal validity windows for tracking when
    relationships were established and whether they're still valid.

    Attributes:
        edge_id: UUID identifier
        source_id: Source node ID
        target_id: Target node ID
        edge_type: Type of relationship
        weight: Relationship strength (0.0-1.0)
        description: Optional description of the relationship
        event_time: When the relationship was established
        valid_from: Start of validity window
        valid_to: End of validity (None = still valid)
    """

    source_id: str
    target_id: str
    edge_type: EdgeType
    edge_id: str = field(default_factory=_generate_id)
    weight: float = 1.0
    description: str = ""
    event_time: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None

    @classmethod
    def contains(cls, parent_id: str, child_id: str, event_time: Optional[str] = None) -> Edge:
        """Create a CONTAINS edge (parent→child relationship)."""
        return cls(
            source_id=parent_id,
            target_id=child_id,
            edge_type=EdgeType.CONTAINS,
            event_time=event_time,
            valid_from=event_time,
        )

    @classmethod
    def follows(cls, preceding_id: str, following_id: str, event_time: Optional[str] = None) -> Edge:
        """Create a FOLLOWS edge (temporal sequence)."""
        return cls(
            source_id=preceding_id,
            target_id=following_id,
            edge_type=EdgeType.FOLLOWS,
            event_time=event_time,
            valid_from=event_time,
        )


@dataclass
class Hyperedge:
    """Hyperedge connecting multiple nodes.

    Hyperedges represent n-ary relationships like thread membership
    or topic clusters that involve more than two nodes.

    Attributes:
        hyperedge_id: UUID identifier
        hyperedge_type: Type of grouping
        member_ids: All connected node IDs
        properties: Type-specific metadata
        event_time: When the hyperedge was formed
        valid_from: Start of validity window
        valid_to: End of validity (None = still valid)
    """

    hyperedge_type: HyperedgeType
    member_ids: list[str] = field(default_factory=list)
    hyperedge_id: str = field(default_factory=_generate_id)
    properties: dict = field(default_factory=dict)
    event_time: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None

    @classmethod
    def thread_membership(cls, thread_id: str, entry_ids: list[str], event_time: Optional[str] = None) -> Hyperedge:
        """Create a THREAD_MEMBERSHIP hyperedge."""
        return cls(
            hyperedge_type=HyperedgeType.THREAD_MEMBERSHIP,
            member_ids=[f"thread:{thread_id}"] + [f"entry:{eid}" for eid in entry_ids],
            properties={"thread_id": thread_id},
            event_time=event_time,
            valid_from=event_time,
        )
