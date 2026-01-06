"""Graphiti backend adapter for episodic memory and temporal graph RAG."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
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
from ..entry_episode_index import EntryEpisodeIndex, IndexConfig


@dataclass
class GraphitiConfig:
    """Configuration for Graphiti backend."""

    # Graphiti submodule location
    graphiti_path: Path = Path("external/graphiti")

    # Database configuration (FalkorDB)
    falkordb_host: str = "localhost"
    falkordb_port: int = 6379
    falkordb_username: str | None = None  # FalkorDB doesn't require auth
    falkordb_password: str | None = None

    # LLM configuration (flexible provider - supports OpenAI, local, DeepSeek, etc.)
    llm_api_base: str | None = None      # e.g., "http://localhost:8000/v1" for local
    llm_api_key: str | None = None       # Required for all providers
    llm_model: str = "gpt-4o-mini"       # Default model

    # Embedding configuration (flexible provider - supports OpenAI, local llama.cpp, etc.)
    embedding_api_base: str | None = None  # e.g., "http://localhost:8080/v1" for local
    embedding_api_key: str | None = None   # Required for all providers
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536              # 1536 for OpenAI, 1024 for bge-m3

    # Legacy fields (deprecated, use llm_* and embedding_* instead)
    openai_api_key: str | None = None      # DEPRECATED: use llm_api_key
    openai_api_base: str | None = None     # DEPRECATED: use llm_api_base
    openai_model: str | None = None        # DEPRECATED: use llm_model

    # Search reranker algorithm (rrf, mmr, cross_encoder, node_distance, episode_mentions)
    # RRF (Reciprocal Rank Fusion) is fast and provides good results for most cases
    reranker: str = "rrf"

    # Working directory for exports
    work_dir: Path | None = None

    # Test mode: Add pytest__ prefix to database names for isolation
    test_mode: bool = False

    # Entry-Episode Index configuration
    # Enables bidirectional mapping between watercooler entry IDs and Graphiti episode UUIDs
    track_entry_episodes: bool = True

    # Path to entry-episode index file
    # Default: ~/.watercooler/graphiti/entry_episode_index.json
    entry_episode_index_path: Path | None = None

    # Auto-save index after each mapping update
    auto_save_index: bool = True

    def __post_init__(self):
        """Set default index path if not provided."""
        if self.entry_episode_index_path is None and self.track_entry_episodes:
            self.entry_episode_index_path = (
                Path.home() / ".watercooler" / "graphiti" / "entry_episode_index.json"
            )


class GraphitiBackend(MemoryBackend):
    """
    Graphiti adapter implementing MemoryBackend contract.

    Graphiti provides:
    - Episodic ingestion (one episode per entry)
    - Temporal entity tracking with time-aware edges
    - Automatic fact extraction and deduplication
    - Hybrid search (semantic + graph traversal)
    - Chronological reasoning

    This adapter wraps Graphiti API calls and maps to/from canonical payloads.
    """

    # Maximum length for body snippet fallback in episode names
    # (50 chars provides enough context without bloating database keys or UI displays)
    _MAX_FALLBACK_NAME_LENGTH = 50

    # Maximum database name length (Redis/FalkorDB key size limit of 512 bytes,
    # we use 64 chars conservatively to allow for UTF-8 multi-byte characters)
    _MAX_DB_NAME_LENGTH = 64

    # Length of test database prefix (pytest__) for test isolation
    _TEST_PREFIX_LENGTH = len("pytest__")  # 8 characters

    # Maximum episode content length to include in query results (8KB)
    # Episodes can be large (multi-KB watercooler entries), so we truncate
    # to keep response sizes manageable while providing sufficient context for RAG
    _MAX_EPISODE_CONTENT_LENGTH = 8192

    # Maximum node summary length (2KB - shorter than episodes)
    # Node summaries are regional consolidations that can be verbose,
    # truncate to prevent payload bloat per Codex feedback
    _MAX_NODE_SUMMARY_LENGTH = 2048

    # Query result default and hard limits
    DEFAULT_MAX_NODES = 10
    DEFAULT_MAX_FACTS = 10
    DEFAULT_MAX_EPISODES = 10
    # Search result validation limits
    MIN_SEARCH_RESULTS = 1  # Minimum valid max_results parameter
    MAX_SEARCH_RESULTS = 50  # Maximum valid max_results parameter
    
    # Community limits (top-5 to prevent payload bloat per Codex feedback)
    MAX_COMMUNITIES_RETURNED = 5

    def __init__(self, config: GraphitiConfig | None = None) -> None:
        self.config = config or GraphitiConfig()
        self._validate_config()
        self._init_entry_episode_index()

    def _validate_config(self) -> None:
        """Validate configuration and Graphiti availability."""
        if not self.config.graphiti_path.exists():
            raise ConfigError(
                f"Graphiti not found at {self.config.graphiti_path}. "
                "Run: git submodule update --init external/graphiti"
            )

        # Check for required Graphiti module
        graphiti_core = self.config.graphiti_path / "graphiti_core"
        if not graphiti_core.exists():
            raise ConfigError(
                f"Graphiti core not found at {graphiti_core}. "
                "Ensure Graphiti submodule is properly initialized."
            )

        # Validate LLM API key is set (required for Graphiti)
        # Support legacy openai_api_key field for backwards compatibility
        if not self.config.llm_api_key:
            if self.config.openai_api_key:
                # Legacy fallback: use openai_api_key if llm_api_key not set
                self.config.llm_api_key = self.config.openai_api_key
            else:
                raise ConfigError(
                    "LLM_API_KEY is required for Graphiti. "
                    "Set via environment variable or config."
                )

        # Validate embedding API key is set (required for Graphiti)
        if not self.config.embedding_api_key:
            raise ConfigError(
                "EMBEDDING_API_KEY is required for Graphiti. "
                "Set via environment variable or config."
            )

        # Support legacy openai_api_base field
        if not self.config.llm_api_base and self.config.openai_api_base:
            self.config.llm_api_base = self.config.openai_api_base

        # Support legacy openai_model field
        if self.config.openai_model and self.config.llm_model == "gpt-4o-mini":
            self.config.llm_model = self.config.openai_model

    def _init_entry_episode_index(self) -> None:
        """Initialize the entry-episode index if enabled."""
        if not self.config.track_entry_episodes:
            self.entry_episode_index = None
            return

        index_config = IndexConfig(
            backend="graphiti",
            index_path=self.config.entry_episode_index_path,
        )
        self.entry_episode_index = EntryEpisodeIndex(index_config, auto_load=True)

    def index_entry_as_episode(
        self,
        entry_id: str,
        episode_uuid: str,
        thread_id: str,
    ) -> None:
        """Add an entry-episode mapping to the index.

        Args:
            entry_id: The watercooler entry ID (ULID format)
            episode_uuid: The Graphiti episode UUID
            thread_id: The thread this entry belongs to
        """
        if self.entry_episode_index is None:
            return

        self.entry_episode_index.add(entry_id, episode_uuid, thread_id)

        if self.config.auto_save_index:
            self.entry_episode_index.save()

    def get_episode_for_entry(self, entry_id: str) -> str | None:
        """Get the Graphiti episode UUID for a watercooler entry ID.

        Args:
            entry_id: The watercooler entry ID

        Returns:
            The episode UUID, or None if not found or index disabled
        """
        if self.entry_episode_index is None:
            return None
        return self.entry_episode_index.get_episode(entry_id)

    def get_entry_for_episode(self, episode_uuid: str) -> str | None:
        """Get the watercooler entry ID for a Graphiti episode UUID.

        Args:
            episode_uuid: The Graphiti episode UUID

        Returns:
            The entry ID, or None if not found or index disabled
        """
        if self.entry_episode_index is None:
            return None
        return self.entry_episode_index.get_entry(episode_uuid)

    async def add_episode_direct(
        self,
        name: str,
        episode_body: str,
        source_description: str,
        reference_time: datetime,
        group_id: str,
    ) -> dict[str, Any]:
        """Add an episode directly to Graphiti without the prepare/index workflow.

        This method provides direct access to Graphiti's add_episode API for use cases
        like migration tools and real-time sync where the full prepare/index pipeline
        is not appropriate.

        Args:
            name: Episode title/name
            episode_body: The episode content
            source_description: Description of the source
            reference_time: Timestamp for the episode (datetime object)
            group_id: Group/thread identifier for partitioning

        Returns:
            Dict with episode_uuid, entities_extracted, and facts_extracted

        Raises:
            BackendError: If Graphiti client creation or episode addition fails
            TransientError: If database connection fails
        """
        try:
            graphiti = self._create_graphiti_client()
        except (ConnectionError, TimeoutError, OSError) as e:
            raise TransientError(f"Database connection failed: {e}") from e
        except ConfigError:
            raise  # Re-raise config errors as-is
        except Exception as e:
            # Unexpected error during client creation
            raise BackendError(f"Failed to create Graphiti client: {e}") from e

        try:
            result = await graphiti.add_episode(
                name=name,
                episode_body=episode_body,
                source_description=source_description,
                reference_time=reference_time,
                group_id=group_id,
            )

            # Extract episode UUID from result - fail if missing
            episode_uuid = getattr(result, "uuid", None)
            if not episode_uuid:
                raise BackendError(
                    "Graphiti returned success but no episode UUID - "
                    "episode may not have been created properly"
                )

            # Count extracted entities and facts if available
            entities = []
            facts_count = 0
            if hasattr(result, "nodes"):
                entities = [getattr(n, "name", str(n)) for n in result.nodes]
            if hasattr(result, "edges"):
                facts_count = len(result.edges)

            return {
                "episode_uuid": episode_uuid,
                "entities_extracted": entities,
                "facts_extracted": facts_count,
            }

        except (ConnectionError, TimeoutError, OSError) as e:
            raise TransientError(f"Database operation failed: {e}") from e
        except BackendError:
            raise  # Re-raise our own errors as-is
        except Exception as e:
            raise BackendError(f"Failed to add episode: {e}") from e

    def prepare(self, corpus: CorpusPayload) -> PrepareResult:
        """
        Prepare corpus for Graphiti ingestion.

        Maps canonical payload to Graphiti's episodic format:
        - Each entry becomes an episode
        - Episodes include: name, content, source, timestamp

        Args:
            corpus: Canonical corpus with threads, entries, edges

        Returns:
            PrepareResult with prepared count and export location
        """
        # Create working directory
        if self.config.work_dir:
            work_dir = self.config.work_dir
            work_dir.mkdir(parents=True, exist_ok=True)
        else:
            work_dir = Path(tempfile.mkdtemp(prefix="graphiti-prepare-"))

        try:
            # Map corpus to Graphiti episodes format
            episodes = self._map_entries_to_episodes(corpus)

            # Write episodes file
            episodes_path = work_dir / "episodes.json"
            episodes_path.write_text(json.dumps(episodes, indent=2))

            # Write manifest
            manifest_path = work_dir / "manifest.json"
            manifest = {
                "format": "graphiti-episodes",
                "version": "1.0",
                "source": "watercooler-cloud",
                "memory_payload_version": corpus.manifest_version,
                "chunker": {
                    "name": corpus.chunker_name,
                    "params": corpus.chunker_params,
                },
                "statistics": {
                    "threads": len(corpus.threads),
                    "episodes": len(episodes),
                },
                "files": {
                    "episodes": "episodes.json",
                },
            }
            manifest_path.write_text(json.dumps(manifest, indent=2))

            return PrepareResult(
                manifest_version=corpus.manifest_version,
                prepared_count=len(episodes),
                message=f"Prepared {len(episodes)} episodes at {work_dir}",
            )

        except Exception as e:
            raise BackendError(f"Failed to prepare corpus: {e}") from e

    def _sanitize_redisearch_operators(self, text: str) -> str:
        """Sanitize RediSearch operator characters in text.

        Replaces special characters that RediSearch interprets as query operators
        with safe alternatives to prevent syntax errors during entity extraction.

        This is a workaround for Graphiti's fulltext search not properly escaping
        entity names containing RediSearch operators (/, |, (), etc.) during
        entity deduplication searches.

        Args:
            text: Input text that may contain RediSearch operators

        Returns:
            Sanitized text with operators replaced
        """
        if not text:
            return text

        # Map RediSearch operators to safe replacements (single-pass translation)
        # Based on lucene_sanitize() in graphiti_core/helpers.py:62-96
        # Using str.translate() for O(n) performance instead of O(n*m) with sequential replace()
        #
        # TODO: This is a workaround for Graphiti's fulltext search bypassing lucene_sanitize()
        # in entity deduplication. Track upstream fix at: https://github.com/getzep/graphiti
        translation_table = str.maketrans({
            '/': '-',      # Forward slash → dash
            '|': '-',      # Pipe → dash
            '(': ' ',      # Parentheses → space
            ')': ' ',
            '+': ' ',      # Plus → space
            '&': ' and ',  # Ampersand → word
            '!': ' ',      # Exclamation → space
            '{': ' ',      # Braces → space
            '}': ' ',
            '[': ' ',      # Brackets → space
            ']': ' ',
            '^': ' ',      # Caret → space
            '~': ' ',      # Tilde → space
            '*': ' ',      # Asterisk → space
            '?': ' ',      # Question mark → space
            ':': ' ',      # Colon → space
            '\\': ' ',     # Backslash → space
            '@': ' ',      # At sign → space
            '<': ' ',      # Angle brackets → space
            '>': ' ',
            '=': ' ',      # Equals → space
            '`': ' ',      # Backtick → space
        })

        # Apply translation and collapse multiple spaces
        result = text.translate(translation_table)
        result = re.sub(r'\s+', ' ', result)

        return result.strip()

    def _create_graphiti_client(self) -> Any:
        """Create and configure Graphiti client with FalkorDB, LLM, and embedder.

        Returns:
            Configured Graphiti instance ready for operations.

        Raises:
            ConfigError: If required dependencies are not installed.
        """
        try:
            from graphiti_core import Graphiti
            from graphiti_core.driver.falkordb_driver import FalkorDriver
            from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
            from graphiti_core.llm_client.config import LLMConfig
            from graphiti_core.embedder import OpenAIEmbedder
            from graphiti_core.embedder.openai import OpenAIEmbedderConfig
        except ImportError as e:
            raise ConfigError(
                f"Graphiti dependencies not installed: {e}. "
                "Run: pip install -e 'external/graphiti[falkordb]'"
            ) from e

        # Create FalkorDB driver
        falkor_driver = FalkorDriver(
            host=self.config.falkordb_host,
            port=self.config.falkordb_port,
            username=self.config.falkordb_username,
            password=self.config.falkordb_password,
        )

        # Configure LLM client (supports OpenAI, local servers, DeepSeek, etc.)
        llm_config = LLMConfig(
            api_key=self.config.llm_api_key,
            model=self.config.llm_model,
            base_url=self.config.llm_api_base,
        )
        llm_client = OpenAIGenericClient(config=llm_config)

        # Configure embedder (supports OpenAI, local llama.cpp, etc.)
        embedder_config = OpenAIEmbedderConfig(
            embedding_model=self.config.embedding_model,
            api_key=self.config.embedding_api_key,
            base_url=self.config.embedding_api_base,
        )
        embedder = OpenAIEmbedder(config=embedder_config)

        return Graphiti(
            graph_driver=falkor_driver,
            llm_client=llm_client,
            embedder=embedder,
        )

    def _get_search_config(self) -> Any:
        """Get SearchConfig based on configured reranker algorithm.

        Returns:
            SearchConfig instance for multi-layer hybrid search.
            COMBINED configs populate edges, episodes, nodes, and communities
            for comprehensive RAG with full episode content.

        Raises:
            ConfigError: If reranker name is invalid
        """
        try:
            from graphiti_core.search.search_config_recipes import (
                COMBINED_HYBRID_SEARCH_RRF,
                COMBINED_HYBRID_SEARCH_MMR,
                COMBINED_HYBRID_SEARCH_CROSS_ENCODER,
                EDGE_HYBRID_SEARCH_NODE_DISTANCE,
                EDGE_HYBRID_SEARCH_EPISODE_MENTIONS,
            )
        except ImportError as e:
            raise ConfigError(
                f"Graphiti search config not available: {e}. "
                "Ensure graphiti_core is properly installed."
            ) from e

        # Map reranker names to SearchConfig objects
        # Use COMBINED configs for rrf/mmr/cross_encoder to get episodes + nodes + communities
        # node_distance and episode_mentions remain edge-focused (no COMBINED variants)
        reranker_configs = {
            "rrf": COMBINED_HYBRID_SEARCH_RRF,
            "mmr": COMBINED_HYBRID_SEARCH_MMR,
            "cross_encoder": COMBINED_HYBRID_SEARCH_CROSS_ENCODER,
            "node_distance": EDGE_HYBRID_SEARCH_NODE_DISTANCE,
            "episode_mentions": EDGE_HYBRID_SEARCH_EPISODE_MENTIONS,
        }

        reranker = self.config.reranker.lower()
        if reranker not in reranker_configs:
            valid_options = ", ".join(reranker_configs.keys())
            raise ConfigError(
                f"Invalid reranker '{reranker}'. "
                f"Valid options: {valid_options}"
            )

        return reranker_configs[reranker]

    # Class-level cache for graph list (avoid repeated GRAPH.LIST calls)
    _graph_list_cache: dict[str, tuple[list[str], float]] = {}
    _GRAPH_LIST_CACHE_TTL = 60  # seconds
    _MAX_GRAPHS_LIMIT = 100  # Resource limit for GRAPH.LIST fallback

    def _get_effective_group_ids(self, group_ids: list[str] | None) -> list[str] | None:
        """Get effective group IDs for search, with GRAPH.LIST fallback.

        When group_ids is None, attempts to list all available graphs from FalkorDB
        and use all non-default databases. Falls back to None if listing fails.

        This mirrors the logic from query() method to ensure search operations
        target actual thread databases instead of only searching default_db.

        Resource limits:
        - Caches graph list for 60 seconds to avoid repeated queries
        - Limits to first 100 graphs to prevent memory exhaustion
        - Logs warning when fallback is used or limit is hit

        Args:
            group_ids: Optional list of group IDs

        Returns:
            List of group IDs to search, or None (searches default_db)
        """
        if group_ids is not None:
            # Already have group_ids, use as-is (will be sanitized later)
            return group_ids

        # Check cache first
        import time
        cache_key = f"{self.config.falkordb_host}:{self.config.falkordb_port}"
        if cache_key in self._graph_list_cache:
            cached_graphs, cached_time = self._graph_list_cache[cache_key]
            if time.time() - cached_time < self._GRAPH_LIST_CACHE_TTL:
                return cached_graphs if cached_graphs else None

        # No group_ids specified - try to list all available graphs
        try:
            import redis
            
            r = redis.Redis(
                host=self.config.falkordb_host,
                port=self.config.falkordb_port,
                password=self.config.falkordb_password,
            )
            graph_list = r.execute_command('GRAPH.LIST')
            
            # Decode bytes to strings (do this once, not twice)
            decoded_graphs = [g.decode() if isinstance(g, bytes) else g for g in graph_list]
            
            # Filter out default_db
            effective_group_ids = [g for g in decoded_graphs if g != 'default_db']
            
            # Apply resource limit
            if len(effective_group_ids) > self._MAX_GRAPHS_LIMIT:
                # Limit to prevent memory exhaustion
                effective_group_ids = effective_group_ids[:self._MAX_GRAPHS_LIMIT]
            
            # Cache the results
            self._graph_list_cache[cache_key] = (effective_group_ids, time.time())
            
            return effective_group_ids if effective_group_ids else None
        except Exception:
            # If we can't list graphs, fall back to None (searches default_db)
            return None

    def _sanitize_thread_id(self, thread_id: str) -> str:
        """Sanitize thread ID for use as Graphiti group_id.

        With RediSearch escaping now handled in FalkorDB driver (external/graphiti),
        we only need minimal sanitization to remove truly problematic characters.
        
        Preserves hyphens and underscores for readability and provenance.

        Sanitization rules:
        - Remove control characters and null bytes
        - Strip leading/trailing whitespace
        - Ensure non-empty (defaults to "unknown")
        - Ensure starts with letter (prepends "t_" if needed, following FalkorDB best practice)
        - Enforce maximum length (64 chars, minus pytest__ prefix space if needed)
        - Add pytest__ prefix if in test mode

        Args:
            thread_id: Original thread identifier (e.g., "memory-backend.md")

        Returns:
            Sanitized group_id with pytest__ prefix if test_mode=True
            
        Examples:
            "memory-backend.md" → "memory-backend.md"
            "123-test" → "t_123-test"
            "  spaces  " → "spaces"
        """
        # Remove control characters and null bytes (keep printable chars including hyphens/underscores)
        sanitized = ''.join(c for c in thread_id if c.isprintable() and c != '\x00')
        
        # Strip whitespace from edges
        sanitized = sanitized.strip()
        
        # Ensure non-empty
        if not sanitized:
            sanitized = "unknown"
            
        # Ensure starts with letter (FalkorDB best practice)
        if not sanitized[0].isalpha():
            sanitized = "t_" + sanitized
        
        # Apply length limit (reserve space for pytest__ prefix if in test mode)
        max_len = self._MAX_DB_NAME_LENGTH - (self._TEST_PREFIX_LENGTH if self.config.test_mode else 0)
        if len(sanitized) > max_len:
            sanitized = sanitized[:max_len]
        
        # Add pytest__ prefix for test database identification (if in test mode)
        if self.config.test_mode:
            # Don't duplicate prefix if already present
            if not sanitized.startswith("pytest__"):
                sanitized = "pytest__" + sanitized

        return sanitized

    def _truncate_episode_content(
        self, content: str, max_length: int | None = None
    ) -> str:
        """Truncate episode content to maximum length with ellipsis marker.

        Args:
            content: Episode content to truncate
            max_length: Maximum character length (default: _MAX_EPISODE_CONTENT_LENGTH)

        Returns:
            Truncated content with "...[truncated]" marker if needed

        Example:
            >>> backend._truncate_episode_content("Short text")
            'Short text'
            >>> backend._truncate_episode_content("A" * 10000)
            'AAA...[truncated]'  # Truncated to 8192 chars
        """
        if max_length is None:
            max_length = self._MAX_EPISODE_CONTENT_LENGTH

        if len(content) <= max_length:
            return content

        return content[:max_length] + "\n...[truncated]"

    def _truncate_node_summary(self, summary: str | None) -> str | None:
        """Truncate node summary to prevent payload bloat.

        Args:
            summary: Node summary to truncate (may be None)

        Returns:
            Truncated summary with marker if needed, or None if input was None

        Example:
            >>> backend._truncate_node_summary(None)
            None
            >>> backend._truncate_node_summary("Short summary")
            'Short summary'
            >>> backend._truncate_node_summary("A" * 3000)
            'AAA...[truncated]'  # Truncated to 2048 chars
        """
        if not summary:
            return None

        if len(summary) <= self._MAX_NODE_SUMMARY_LENGTH:
            return summary

        return summary[:self._MAX_NODE_SUMMARY_LENGTH] + "...[truncated]"

    def _map_entries_to_episodes(
        self, corpus: CorpusPayload
    ) -> list[dict[str, Any]]:
        """Map canonical entries to Graphiti episode format with strict validation.

        Raises:
            BackendError: If entry missing required timestamp or both title and body
        """
        episodes = []

        for idx, entry in enumerate(corpus.entries):
            # Graphiti episode format:
            # - name: Episode identifier/title (required, cannot be None)
            # - episode_body: Content
            # - source_description: Metadata about source
            # - reference_time: datetime object (required, cannot be None)
            # - uuid: Entry ID for stable mapping
            # - group_id: Thread ID for per-thread partitioning

            # Get entry_id with fallback chain
            entry_id = entry.get("id") or entry.get("entry_id") or f"entry-{idx}"

            # STRICT: Fail fast on missing timestamp (temporal graph requirement)
            timestamp = entry.get("timestamp")
            if not timestamp:
                raise BackendError(
                    f"Entry '{entry_id}' missing required 'timestamp' field. "
                    "Temporal graph requires valid timestamps for all entries."
                )

            # Get name with body snippet fallback
            name = entry.get("title")
            body_text = entry.get("body", entry.get("content", ""))

            if not name:
                # Fallback to first N chars of body
                if body_text:
                    max_len = self._MAX_FALLBACK_NAME_LENGTH
                    name = (body_text[:max_len] + "...") if len(body_text) > max_len else body_text
                else:
                    raise BackendError(
                        f"Entry '{entry_id}' has neither 'title' nor 'body' content. "
                        "Cannot create episode with no name or content."
                    )

            # Get thread_id for group_id (sanitized for DB name)
            thread_id = entry.get("thread_id", "unknown")
            sanitized_thread = self._sanitize_thread_id(thread_id)

            # Embed entry_id in episode name for provenance
            # Format: "{entry_id}: {title/snippet}"
            episode_name = f"{entry_id}: {name}"

            episode = {
                "name": episode_name,
                "episode_body": self._sanitize_redisearch_operators(body_text),
                "source_description": self._format_source_description(entry),
                "reference_time": timestamp,
                "group_id": sanitized_thread,  # Per-thread partitioning
                "metadata": {
                    "entry_id": entry_id,
                    "thread_id": thread_id,
                    "agent": entry.get("agent"),
                    "role": entry.get("role"),
                    "type": entry.get("type"),
                },
            }
            episodes.append(episode)

        return episodes

    def _format_source_description(self, entry: dict[str, Any]) -> str:
        """Format entry metadata as source description."""
        agent = entry.get("agent", "Unknown")
        role = entry.get("role", "unknown")
        thread = entry.get("thread_id", "unknown")
        entry_type = entry.get("type", "Note")

        return f"Watercooler thread '{thread}' - {entry_type} by {agent} ({role})"

    def index(self, chunks: ChunkPayload) -> IndexResult:
        """
        Ingest episodes into Graphiti temporal graph.

        Uses Graphiti Python API to:
        1. Initialize Graphiti client
        2. Add episodes sequentially
        3. Extract entities and facts
        4. Build temporal graph in database

        Args:
            chunks: Chunk payload (contains episodes from prepare)

        Returns:
            IndexResult with indexed count

        Raises:
            BackendError: If ingestion fails
            TransientError: If database connection fails
        """
        if self.config.work_dir:
            work_dir = self.config.work_dir
        else:
            work_dir = Path(tempfile.mkdtemp(prefix="graphiti-index-"))

        try:
            # Load episodes from prepare() output
            episodes_path = work_dir / "episodes.json"
            if not episodes_path.exists():
                raise BackendError(
                    f"Episodes file not found at {episodes_path}. "
                    "Run prepare() first."
                )

            episodes = json.loads(episodes_path.read_text())

            # Create Graphiti client with FalkorDB connection
            try:
                graphiti = self._create_graphiti_client()
            except Exception as e:
                raise TransientError(f"Database connection failed: {e}") from e

            # Ingest episodes sequentially (async operation wrapped in sync)
            # Track entry-episode mappings for cross-tier retrieval
            indexed_mappings: list[tuple[str, str, str]] = []  # (entry_id, episode_uuid, thread_id)

            async def ingest_episodes():
                from datetime import datetime, timezone

                count = 0
                for episode in episodes:
                    try:
                        # Convert reference_time from ISO string to datetime object
                        ref_time_str = episode["reference_time"]
                        if isinstance(ref_time_str, str):
                            ref_time = datetime.fromisoformat(ref_time_str.replace('Z', '+00:00'))
                        else:
                            ref_time = ref_time_str  # Already a datetime

                        result = await graphiti.add_episode(
                            name=episode["name"],
                            episode_body=episode["episode_body"],
                            source_description=episode["source_description"],
                            reference_time=ref_time,
                            group_id=episode.get("group_id"),  # Per-thread partitioning
                        )

                        # Track entry-episode mapping for cross-tier retrieval
                        metadata = episode.get("metadata", {})
                        entry_id = metadata.get("entry_id")
                        thread_id = metadata.get("thread_id", episode.get("group_id", "unknown"))
                        if entry_id and result and hasattr(result, "uuid"):
                            indexed_mappings.append((entry_id, result.uuid, thread_id))

                        count += 1
                    except Exception as e:
                        raise BackendError(
                            f"Failed to add episode '{episode.get('name')}': {e}"
                        ) from e
                return count

            indexed_count = asyncio.run(ingest_episodes())

            # Persist entry-episode mappings to index
            for entry_id, episode_uuid, thread_id in indexed_mappings:
                self.index_entry_as_episode(entry_id, episode_uuid, thread_id)

            return IndexResult(
                manifest_version=chunks.manifest_version,
                indexed_count=indexed_count,
                message=f"Indexed {indexed_count} episodes via Graphiti at {work_dir}",
            )

        except ConfigError:
            raise
        except TransientError:
            raise
        except BackendError:
            raise
        except Exception as e:
            raise BackendError(f"Unexpected error during indexing: {e}") from e

    def query(self, query: QueryPayload) -> QueryResult:
        """
        Query Graphiti temporal graph with comprehensive multi-layer retrieval.

        Executes hybrid search across all four Graphiti subgraphs:
        - Edges: Extracted facts with bi-temporal tracking
        - Episodes: Full original content (non-lossy source data)
        - Nodes: Entity-centric summaries
        - Communities: Domain-level context clusters

        Args:
            query: Query payload with search queries

        Returns:
            QueryResult with:
            - results: List of edge-centric results, each containing:
                - content: Brief extracted fact (edge.fact)
                - score: Edge reranker relevance score (0.0-10.0+)
                - metadata:
                    - Edge identifiers (uuid, source_node_uuid, target_node_uuid)
                    - Temporal tracking (valid_at, invalid_at)
                    - Backend provenance (source_backend, reranker)
                    - episodes: List of source episodes with:
                        - uuid, content (truncated to 8KB), score (episode relevance)
                        - source, source_description, valid_at, created_at, name
                        - NOTE: episode.score represents episode relevance, may differ
                          from edge.score (fact relevance). For fact-based ranking, use
                          edge scores. For content-based ranking, use episode scores.
                    - nodes: List of connected entities with:
                        - uuid, name, labels, summary (truncated to 2KB), created_at
                        - role (source/target), edge_uuid (for context stitching)
            - communities: Top 5 domain-level clusters (optional) with:
                - uuid, name, summary, score

        Scoring Precedence (Codex feedback):
            - Edge scores: Rank extracted facts by relevance to query
            - Episode scores: Rank source content by relevance to query
            - Use edge scores for fact-based RAG (precise facts)
            - Use episode scores for content-based RAG (rich context)
            - Both are valid strategies depending on use case

        Example Multi-Strategy RAG:
            1. Fact-based: Rank by edge scores, extract episodes for context
            2. Entity-based: Filter by node labels, use node summaries
            3. Topic-based: Group by communities, aggregate related content
            4. Content-based: Rank by episode scores, use episodes as primary source

        Raises:
            BackendError: If query fails
            TransientError: If database connection fails
            ConfigError: If configuration is invalid
        """
        try:
            # Create Graphiti client with FalkorDB connection
            try:
                graphiti = self._create_graphiti_client()
            except Exception as e:
                raise TransientError(f"Database connection failed: {e}") from e

            # Execute queries asynchronously
            async def execute_queries():
                results = []
                all_communities = []  # Collect communities across all queries
                for query_item in query.queries:
                    query_text = query_item.get("query", "")
                    limit = query_item.get("limit", 10)

                    # Extract optional topic for group_id filtering
                    topic = query_item.get("topic")
                    if topic:
                        # Sanitize topic to group_id format
                        group_ids = [self._sanitize_thread_id(topic)]
                    else:
                        # No topic specified - search across all available graphs
                        # List all graphs from FalkorDB
                        try:
                            import redis
                            r = redis.Redis(
                                host=self.config.falkordb_host,
                                port=self.config.falkordb_port,
                                password=self.config.falkordb_password,
                            )
                            graph_list = r.execute_command('GRAPH.LIST')
                            # Filter out default_db and decode bytes to strings
                            group_ids = [
                                g.decode() if isinstance(g, bytes) else g
                                for g in graph_list
                                if (g.decode() if isinstance(g, bytes) else g) != 'default_db'
                            ]
                        except Exception as e:
                            # If we can't list graphs, fall back to None (searches default_db)
                            group_ids = None

                    try:
                        # Get search config with configured reranker
                        search_config = self._get_search_config()
                        
                        # Handle single group_id case: @handle_multiple_group_ids decorator
                        # only activates for len(group_ids) > 1, so we need to manually
                        # clone the driver for single group_id queries
                        if group_ids and len(group_ids) == 1:
                            # Clone driver to point at the specific database
                            driver = graphiti.clients.driver.clone(database=group_ids[0])
                            search_results = await graphiti.search_(
                                query=query_text,
                                config=search_config,
                                group_ids=group_ids,
                                driver=driver,
                            )
                        else:
                            # Multiple group_ids or None - let decorator handle it
                            search_results = await graphiti.search_(
                                query=query_text,
                                config=search_config,
                                group_ids=group_ids,
                            )

                        # Map Graphiti SearchResults to canonical format
                        # search_results.edges contains EntityEdge models
                        # search_results.edge_reranker_scores contains scores (positionally aligned)
                        # search_results.episodes contains EpisodicNode models with full content

                        # Build episode index for efficient edge→episode lookup
                        # Episodes link to edges via episode.entity_edges (list of edge UUIDs)
                        # Note: Not all edges have linked episodes (some are inferred/derived facts)
                        episode_index: dict[str, list[Any]] = {}
                        for ep in search_results.episodes:
                            # entity_edges is a list of edge UUIDs that reference this episode
                            for edge_uuid in ep.entity_edges:
                                if edge_uuid not in episode_index:
                                    episode_index[edge_uuid] = []
                                episode_index[edge_uuid].append(ep)

                        # Build node index for efficient edge→node lookup
                        # Nodes are connected entities (source/target) for each edge
                        node_index: dict[str, Any] = {}
                        for node in search_results.nodes:
                            node_index[node.uuid] = node

                        # Build episode score index with defensive length checks
                        # Codex: defend against array length mismatches
                        episode_score_index: dict[str, float] = {}
                        num_episode_scores = len(search_results.episode_reranker_scores)
                        for idx, ep in enumerate(search_results.episodes):
                            # Use positional alignment but defend against array length mismatch
                            if idx < num_episode_scores:
                                episode_score_index[ep.uuid] = search_results.episode_reranker_scores[idx]
                            else:
                                # Default to 0.0 if scores missing (shouldn't happen but defend anyway)
                                episode_score_index[ep.uuid] = 0.0

                        # Return only top N results after reranking
                        # Graphiti already sorted edges by reranker score
                        for idx, edge in enumerate(search_results.edges[:limit]):
                            # Extract score (defaults to 0.0 if not available)
                            score = 0.0
                            if idx < len(search_results.edge_reranker_scores):
                                score = search_results.edge_reranker_scores[idx]

                            # Find episodes that contain this edge
                            source_episodes = episode_index.get(edge.uuid, [])

                            # Find connected nodes (entities) for this edge
                            # Codex: include edge UUID for context stitching
                            source_node = node_index.get(edge.source_node_uuid)
                            target_node = node_index.get(edge.target_node_uuid)

                            # Build nodes list with role and edge linkage
                            edge_nodes = []
                            if source_node:
                                edge_nodes.append({
                                    "uuid": source_node.uuid,
                                    "name": source_node.name,
                                    "labels": source_node.labels,
                                    "summary": self._truncate_node_summary(
                                        source_node.summary if hasattr(source_node, 'summary') else None
                                    ),
                                    "created_at": source_node.created_at.isoformat() if source_node.created_at else None,
                                    "role": "source",
                                    "edge_uuid": edge.uuid,
                                })
                            if target_node and target_node.uuid != (source_node.uuid if source_node else None):
                                # Avoid duplicates when source and target are the same node
                                edge_nodes.append({
                                    "uuid": target_node.uuid,
                                    "name": target_node.name,
                                    "labels": target_node.labels,
                                    "summary": self._truncate_node_summary(
                                        target_node.summary if hasattr(target_node, 'summary') else None
                                    ),
                                    "created_at": target_node.created_at.isoformat() if target_node.created_at else None,
                                    "role": "target",
                                    "edge_uuid": edge.uuid,
                                })

                            results.append({
                                "query": query_text,
                                "content": edge.fact,  # The fact/relationship text
                                "score": score,  # Actual reranker score
                                "metadata": {
                                    # Graphiti-specific IDs
                                    "uuid": edge.uuid,
                                    "source_node_uuid": edge.source_node_uuid,
                                    "target_node_uuid": edge.target_node_uuid,
                                    "valid_at": edge.valid_at.isoformat() if edge.valid_at else None,
                                    "invalid_at": edge.invalid_at.isoformat() if edge.invalid_at else None,
                                    "group_id": edge.group_id,
                                    # Backend provenance for cross-backend reranking
                                    "source_backend": "graphiti",
                                    "reranker": self.config.reranker.lower(),
                                    # Episode content (non-lossy source data)
                                    "episodes": [
                                        {
                                            "uuid": ep.uuid,
                                            "content": self._truncate_episode_content(ep.content),
                                            "score": episode_score_index.get(ep.uuid, 0.0),  # Episode relevance score
                                            "source": ep.source.value if hasattr(ep.source, 'value') else str(ep.source),
                                            "source_description": ep.source_description,
                                            "valid_at": ep.valid_at.isoformat() if ep.valid_at else None,
                                            "created_at": ep.created_at.isoformat() if hasattr(ep, 'created_at') and ep.created_at else None,
                                            "name": ep.name,
                                        }
                                        for ep in source_episodes
                                    ],
                                    # Connected nodes (entities) with summaries
                                    # Codex: include for entity-centric queries and context stitching
                                    "nodes": edge_nodes,
                                },
                            })

                        # Extract top 5 communities (domain-level clusters)
                        # Codex: limit to small top-k to prevent payload bloat
                        for idx, comm in enumerate(search_results.communities[:self.MAX_COMMUNITIES_RETURNED]):
                            comm_dict = {
                                "uuid": comm.uuid if hasattr(comm, 'uuid') else None,
                                "name": comm.name if hasattr(comm, 'name') else f"Community {idx}",
                                "summary": comm.summary if hasattr(comm, 'summary') else None,
                            }
                            # Add score if available (positional alignment)
                            if idx < len(search_results.community_reranker_scores):
                                comm_dict["score"] = search_results.community_reranker_scores[idx]
                            else:
                                comm_dict["score"] = 0.0

                            # Add if not already present (avoid duplicates across queries)
                            if comm_dict not in all_communities:
                                all_communities.append(comm_dict)

                    except Exception as e:
                        raise BackendError(f"Query '{query_text}' failed: {e}") from e

                return results, all_communities

            # Run async query execution
            results, communities = asyncio.run(execute_queries())

            return QueryResult(
                manifest_version=query.manifest_version,
                results=results,
                communities=communities,
            )

        except ConfigError:
            raise
        except TransientError:
            raise
        except BackendError:
            raise
        except Exception as e:
            raise BackendError(f"Query execution failed: {e}") from e

    def _validate_uuid(self, value: str, param_name: str) -> None:
        """Validate that a string is a valid UUID format.
        
        Args:
            value: String to validate as UUID
            param_name: Parameter name for error messages
            
        Raises:
            ConfigError: If value is not a valid UUID
        """
        try:
            import uuid
            uuid.UUID(value)
        except ValueError:
            from . import ConfigError
            raise ConfigError(
                f"{param_name} must be a valid UUID, got: {repr(value)}"
            )

    def search_nodes(
        self,
        query: str,
        group_ids: Sequence[str] | None = None,
        max_results: int = DEFAULT_MAX_NODES,
        entity_types: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search for nodes (entities) using hybrid semantic search.

        Protocol-compliant direct implementation (not a wrapper).
        Unlike search_facts() and search_episodes(), this method doesn't delegate
        to another method - it's the primary implementation. Added in Phase 1 as
        a new protocol method with full Graphiti integration.

        Note: This method uses a sync facade pattern (approved by Codex).
        It's synchronous but internally uses asyncio.run() to call async
        Graphiti operations. The MCP layer calls this via asyncio.to_thread()
        to avoid blocking. This pattern matches the existing query() method
        and avoids nested event loop issues.

        Args:
            query: Search query string
            group_ids: Optional list of group IDs to filter by
            max_results: Maximum nodes to return (default: 10, max: 50)
            entity_types: Optional list of entity type names to filter

        Returns:
            List of node dicts with uuid, name, labels, summary, etc.

        Raises:
            ConfigError: If max_results is out of valid range
            BackendError: If search fails
            TransientError: If database connection fails
        """
        # Validate query is not empty
        if not query or not query.strip():
            from . import ConfigError
            raise ConfigError("query cannot be empty")
        
        # Validate max_results to prevent resource exhaustion
        if max_results < self.MIN_SEARCH_RESULTS or max_results > self.MAX_SEARCH_RESULTS:
            from . import ConfigError
            raise ConfigError(
                f"max_results must be between {self.MIN_SEARCH_RESULTS} and {self.MAX_SEARCH_RESULTS}, got {max_results}"
            )
        
        try:
            graphiti = self._create_graphiti_client()
        except Exception as e:
            raise TransientError(f"Database connection failed: {e}") from e

        async def search_nodes_async():
            # Get effective group_ids with GRAPH.LIST fallback
            effective_group_ids = self._get_effective_group_ids(group_ids)
            
            # Sanitize group_ids
            sanitized_group_ids = None
            if effective_group_ids:
                sanitized_group_ids = [self._sanitize_thread_id(gid) for gid in effective_group_ids]

            # Use NODE_HYBRID_SEARCH_RRF (official Graphiti MCP server approach)
            from graphiti_core.search.search_config_recipes import NODE_HYBRID_SEARCH_RRF
            from graphiti_core.search.search_filters import SearchFilters
            
            # Always create SearchFilters (match official implementation)
            search_filters = SearchFilters(node_labels=entity_types)

            # Execute search with node-specific config
            search_results = await graphiti.search_(
                query=query,
                config=NODE_HYBRID_SEARCH_RRF,
                group_ids=sanitized_group_ids,
                search_filter=search_filters,  # Note: singular 'filter' not 'filters'
            )
            
            # Extract nodes from results (official approach)
            limit = min(max_results, self.MAX_SEARCH_RESULTS)
            nodes = search_results.nodes[:limit] if search_results.nodes else []
            
            # Format results with reranker scores
            results = []
            for idx, node in enumerate(nodes):
                # Extract score with defensive indexing (match query_memory pattern)
                score = 0.0
                if idx < len(search_results.node_reranker_scores):
                    score = search_results.node_reranker_scores[idx]
                
                results.append({
                    "id": node.uuid,  # Required by CoreResult protocol
                    "uuid": node.uuid,  # Preserved for backwards compatibility
                    "name": node.name,
                    "labels": node.labels if node.labels else [],
                    "summary": self._truncate_node_summary(
                        node.summary if hasattr(node, 'summary') else None
                    ),
                    "created_at": node.created_at.isoformat() if node.created_at else None,
                    "group_id": node.group_id if hasattr(node, 'group_id') else None,
                    "score": score,  # Hybrid search reranker score
                    "backend": "graphiti",  # Required by CoreResult protocol
                    # Optional CoreResult fields
                    "content": None,  # Nodes don't have content (episodes do)
                    "source": None,  # Source tracking not applicable to entities
                    "metadata": {},  # Additional metadata can be added here
                    "extra": {},  # Backend-specific fields can be added here
                })
            
            return results

        try:
            return asyncio.run(search_nodes_async())
        except Exception as e:
            raise BackendError(f"Node search failed for '{query}': {e}") from e


    def get_node(
        self,
        node_id: str,
        group_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get a node by UUID.

        Args:
            node_id: Node UUID to retrieve
            group_id: Group ID (database name) where the node is stored.
                     Required for multi-database setups. If None, queries default_db.

        Returns:
            Node dict with uuid, name, labels, summary, etc. or None if not found

        Raises:
            ConfigError: If node_id is empty or invalid
            BackendError: If retrieval fails
            TransientError: If database connection fails
        """
        # Validate node_id is not empty
        if not node_id or not node_id.strip():
            from . import ConfigError
            raise ConfigError("node_id cannot be empty")
        
        # Validate node_id is a valid UUID format
        self._validate_uuid(node_id, "node_id")
        
        try:
            graphiti = self._create_graphiti_client()
        except Exception as e:
            raise TransientError(f"Database connection failed: {e}") from e

        async def get_node_async():
            from graphiti_core.nodes import EntityNode

            # Clone driver to use specific database if group_id provided
            driver = graphiti.driver
            if group_id:
                sanitized_group_id = self._sanitize_thread_id(group_id)
                driver = graphiti.driver.clone(database=sanitized_group_id)

            # Get node by UUID
            node = await EntityNode.get_by_uuid(driver, node_id)
            if not node:
                return None

            # Format result
            return {
                "id": node.uuid,  # Required by CoreResult protocol
                "uuid": node.uuid,  # Preserved for backwards compatibility
                "name": node.name,
                "labels": node.labels if node.labels else [],
                "summary": self._truncate_node_summary(
                    node.summary if hasattr(node, 'summary') else None
                ),
                "created_at": node.created_at.isoformat() if node.created_at else None,
                "group_id": node.group_id if hasattr(node, 'group_id') else None,
                "backend": "graphiti",  # Required by CoreResult protocol
            }

        try:
            return asyncio.run(get_node_async())
        except Exception as e:
            raise BackendError(f"Failed to get node '{node_id}': {e}") from e


    def search_facts(
        self,
        query: str,
        group_ids: Sequence[str] | None = None,
        max_results: int = DEFAULT_MAX_FACTS,
        center_node_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search for facts (edges) using semantic search.

        Protocol-compliant wrapper for search_memory_facts().

        Args:
            query: Search query string
            group_ids: Optional list of group IDs to filter by
            max_results: Maximum facts to return (default: 10, max: 50)
            center_node_id: Optional node UUID to center search around

        Returns:
            List of fact dicts with edge data

        Raises:
            ConfigError: If query is empty or max_results is out of valid range
            BackendError: If search fails
            TransientError: If database connection fails
        """
        # Validate query is not empty
        if not query or not query.strip():
            from . import ConfigError
            raise ConfigError("query cannot be empty")
        
        # Validate max_results to prevent resource exhaustion
        if max_results < self.MIN_SEARCH_RESULTS or max_results > self.MAX_SEARCH_RESULTS:
            from . import ConfigError
            raise ConfigError(
                f"max_results must be between {self.MIN_SEARCH_RESULTS} and {self.MAX_SEARCH_RESULTS}, got {max_results}"
            )
        
        # Get results from underlying method
        results = self.search_memory_facts(
            query=query,
            group_ids=group_ids,
            max_facts=max_results,
            center_node_uuid=center_node_id,
        )
        
        # Add CoreResult-compliant fields to each result
        for result in results:
            result.setdefault("id", result.get("uuid"))  # Required by CoreResult
            result.setdefault("backend", "graphiti")  # Required by CoreResult
            result.setdefault("content", None)  # Facts don't have content
            result.setdefault("source", None)  # Source tracking not applicable to edges
            result.setdefault("metadata", {})  # Additional metadata
            result.setdefault("extra", {})  # Backend-specific fields
        
        return results

    def search_episodes(
        self,
        query: str,
        group_ids: Sequence[str] | None = None,
        max_results: int = DEFAULT_MAX_EPISODES,
    ) -> list[dict[str, Any]]:
        """Search for episodes (provenance-bearing content) using semantic search.

        Protocol-compliant wrapper for get_episodes().

        Args:
            query: Search query string
            group_ids: Optional list of group IDs to filter by
            max_results: Maximum episodes to return (default: 10, max: 50)

        Returns:
            List of episode dicts with uuid, name, content, timestamps

        Raises:
            ConfigError: If query is empty or max_results is out of valid range
            BackendError: If search fails
            TransientError: If database connection fails
        """
        # Validate query is not empty
        if not query or not query.strip():
            from . import ConfigError
            raise ConfigError("query cannot be empty")
        
        # Validate max_results to prevent resource exhaustion
        if max_results < self.MIN_SEARCH_RESULTS or max_results > self.MAX_SEARCH_RESULTS:
            from . import ConfigError
            raise ConfigError(
                f"max_results must be between {self.MIN_SEARCH_RESULTS} and {self.MAX_SEARCH_RESULTS}, got {max_results}"
            )
        
        # Get results from underlying method
        results = self.get_episodes(
            query=query,
            group_ids=group_ids,
            max_episodes=max_results,
        )
        
        # Add CoreResult-compliant fields to each result
        for result in results:
            result.setdefault("id", result.get("uuid"))  # Required by CoreResult
            result.setdefault("backend", "graphiti")  # Required by CoreResult
            result.setdefault("metadata", {})  # Additional metadata
            result.setdefault("extra", {})  # Backend-specific fields
        
        return results

    def get_edge(
        self,
        edge_id: str,
        group_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get an edge/fact by UUID.

        Protocol-compliant wrapper for get_entity_edge().

        Args:
            edge_id: Edge UUID to retrieve
            group_id: Group ID (database name) where the edge is stored

        Returns:
            Edge dict or None if not found

        Raises:
            ConfigError: If edge_id is empty or invalid
            BackendError: If retrieval fails
            TransientError: If database connection fails
        """
        # Validate edge_id is not empty
        if not edge_id or not edge_id.strip():
            from . import ConfigError
            raise ConfigError("edge_id cannot be empty")
        
        # Validate edge_id is a valid UUID format
        self._validate_uuid(edge_id, "edge_id"
            )
        
        # get_entity_edge() now returns None for not-found cases
        return self.get_entity_edge(uuid=edge_id, group_id=group_id)

    def get_entity_edge(self, uuid: str, group_id: str | None = None) -> dict[str, Any] | None:
        """Get an entity edge by UUID.

        .. warning::
            BEHAVIOR CHANGE: This method now returns None when an edge is not found,
            instead of raising an exception. This is a potentially breaking change
            if existing code expects exceptions for missing edges. Update error
            handling accordingly.

        Args:
            uuid: Edge UUID to retrieve
            group_id: Group ID (database name) where the edge is stored.
                     Required for multi-database setups. If None, queries default_db.

        Returns:
            Edge dict with uuid, fact, source/target nodes, timestamps, or None if not found

        Raises:
            ConfigError: If uuid is empty or invalid
            BackendError: If retrieval fails (not for "not found" cases)
            TransientError: If database connection fails
        """
        # Validate uuid is not empty
        if not uuid or not uuid.strip():
            from . import ConfigError
            raise ConfigError("uuid cannot be empty")
        
        # Validate uuid is a valid UUID format
        self._validate_uuid(uuid, "uuid")
        
        try:
            graphiti = self._create_graphiti_client()
        except Exception as e:
            raise TransientError(f"Database connection failed: {e}") from e

        async def get_edge_async():
            from graphiti_core.edges import EntityEdge

            # Clone driver to use specific database if group_id provided
            driver = graphiti.driver
            if group_id:
                sanitized_group_id = self._sanitize_thread_id(group_id)
                driver = graphiti.driver.clone(database=sanitized_group_id)

            # Get edge by UUID
            edge = await EntityEdge.get_by_uuid(driver, uuid)
            if not edge:
                # Return None for not-found cases (protocol compliant)
                return None

            # Format result
            return {
                "uuid": edge.uuid,
                "fact": edge.fact,
                "source_node_uuid": edge.source_node_uuid,
                "target_node_uuid": edge.target_node_uuid,
                "valid_at": edge.valid_at.isoformat() if edge.valid_at else None,
                "invalid_at": edge.invalid_at.isoformat() if edge.invalid_at else None,
                "created_at": edge.created_at.isoformat() if edge.created_at else None,
                "group_id": edge.group_id if hasattr(edge, 'group_id') else None,
            }

        try:
            return asyncio.run(get_edge_async())
        except BackendError:
            raise
        except Exception as e:
            raise BackendError(f"Failed to get entity edge '{uuid}': {e}") from e

    def search_memory_facts(
        self,
        query: str,
        group_ids: list[str] | None = None,
        max_facts: int = DEFAULT_MAX_FACTS,
        center_node_uuid: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search for facts (edges) with optional center-node traversal.

        Args:
            query: Search query string
            group_ids: Optional list of group IDs to filter by
            max_facts: Maximum facts to return (default: 10, max: 50)
            center_node_uuid: Optional node UUID to center search around

        Returns:
            List of fact dicts with edge data

        Raises:
            BackendError: If search fails
            TransientError: If database connection fails
        """
        try:
            graphiti = self._create_graphiti_client()
        except Exception as e:
            raise TransientError(f"Database connection failed: {e}") from e

        async def search_facts_async():
            # Get effective group_ids with GRAPH.LIST fallback
            effective_group_ids = self._get_effective_group_ids(group_ids)
            
            # Sanitize group_ids
            sanitized_group_ids = None
            if effective_group_ids:
                sanitized_group_ids = [self._sanitize_thread_id(gid) for gid in effective_group_ids]

            # Use search_() API for facts with reranker scores (match query_memory pattern)
            limit = min(max_facts, self.MAX_SEARCH_RESULTS)
            search_config = self._get_search_config()
            
            search_results = await graphiti.search_(
                query=query,
                config=search_config,
                group_ids=sanitized_group_ids,
                center_node_uuid=center_node_uuid,
            )
            
            # Extract edges with scores
            edges = search_results.edges[:limit] if search_results.edges else []
            
            # Format results with reranker scores
            results = []
            for idx, edge in enumerate(edges):
                # Extract score with defensive indexing (match query_memory pattern)
                score = 0.0
                if idx < len(search_results.edge_reranker_scores):
                    score = search_results.edge_reranker_scores[idx]
                
                results.append({
                    "uuid": edge.uuid,
                    "fact": edge.fact,
                    "source_node_uuid": edge.source_node_uuid,
                    "target_node_uuid": edge.target_node_uuid,
                    "valid_at": edge.valid_at.isoformat() if edge.valid_at else None,
                    "invalid_at": edge.invalid_at.isoformat() if edge.invalid_at else None,
                    "created_at": edge.created_at.isoformat() if edge.created_at else None,
                    "group_id": edge.group_id if hasattr(edge, 'group_id') else None,
                    "score": score,  # Hybrid search reranker score
                })

            return results

        try:
            return asyncio.run(search_facts_async())
        except Exception as e:
            raise BackendError(f"Fact search failed for '{query}': {e}") from e

    def get_episodes(
        self,
        query: str,
        group_ids: list[str] | None = None,
        max_episodes: int = DEFAULT_MAX_EPISODES,
    ) -> list[dict[str, Any]]:
        """Search for episodes from Graphiti memory using semantic search.

        Note: Graphiti doesn't support enumerating all episodes. This tool
        performs semantic episode search using the query string.

        Args:
            query: Search query string (required, must be non-empty)
            group_ids: Optional list of group IDs to filter by
            max_episodes: Maximum episodes to return (default: 10, max: 50)

        Returns:
            List of episode dicts with uuid, name, content, timestamps

        Raises:
            ConfigError: If query is empty
            BackendError: If search fails
            TransientError: If database connection fails
        """
        # Validate query
        if not query or not query.strip():
            raise ConfigError("query parameter is required and must be non-empty")

        try:
            graphiti = self._create_graphiti_client()
        except Exception as e:
            raise TransientError(f"Database connection failed: {e}") from e

        async def search_episodes_async():
            # Get effective group_ids with GRAPH.LIST fallback
            effective_group_ids = self._get_effective_group_ids(group_ids)
            
            # Sanitize group_ids
            sanitized_group_ids = None
            if effective_group_ids:
                sanitized_group_ids = [self._sanitize_thread_id(gid) for gid in effective_group_ids]

            # Search episodes via COMBINED config (retrieves episodes + edges + nodes)
            limit = min(max_episodes, self.MAX_SEARCH_RESULTS)
            search_config = self._get_search_config()
            
            search_results = await graphiti.search_(
                query=query,
                config=search_config,
                group_ids=sanitized_group_ids,
            )
            
            # Extract episodes from search results
            episodes = search_results.episodes[:limit] if search_results.episodes else []

            # Format results with reranker scores
            results = []
            for idx, ep in enumerate(episodes):
                # Extract score with defensive indexing (match query_memory pattern)
                score = 0.0
                if idx < len(search_results.episode_reranker_scores):
                    score = search_results.episode_reranker_scores[idx]
                
                results.append({
                    "uuid": ep.uuid,
                    "name": ep.name,
                    "content": self._truncate_episode_content(ep.content),
                    "created_at": ep.created_at.isoformat() if ep.created_at else None,
                    "source": ep.source.value if hasattr(ep.source, 'value') else str(ep.source),
                    "source_description": ep.source_description,
                    "group_id": ep.group_id,
                    "valid_at": ep.valid_at.isoformat() if hasattr(ep, 'valid_at') and ep.valid_at else None,
                    "score": score,  # Hybrid search reranker score
                })

            return results

        try:
            return asyncio.run(search_episodes_async())
        except Exception as e:
            raise BackendError(f"Episode search failed for '{query}': {e}") from e

    def healthcheck(self) -> HealthStatus:
        """
        Check Graphiti and database health.

        Verifies:
        - Graphiti module is accessible
        - Neo4j/FalkorDB is reachable

        Returns:
            HealthStatus with availability and details
        """
        try:
            # Check Graphiti availability
            self._validate_config()

            # Check FalkorDB connectivity (via Redis protocol)
            try:
                import redis

                r = redis.Redis(
                    host=self.config.falkordb_host,
                    port=self.config.falkordb_port,
                    username=self.config.falkordb_username,
                    password=self.config.falkordb_password,
                    socket_connect_timeout=2,
                )
                r.ping()
                db_status = "FalkorDB: connected"
            except ImportError:
                db_status = "FalkorDB: redis-py not installed"
            except (redis.ConnectionError, redis.TimeoutError) as e:
                db_status = f"FalkorDB: unreachable ({e})"

            return HealthStatus(
                ok=True,
                details=f"Graphiti available at {self.config.graphiti_path}, {db_status}",
            )

        except ConfigError as e:
            return HealthStatus(ok=False, details=str(e))
        except Exception as e:
            return HealthStatus(ok=False, details=f"Health check failed: {e}")

    def get_capabilities(self) -> Capabilities:
        """
        Return Graphiti capabilities.

        Graphiti provides:
        - Embeddings: Yes (via OpenAI)
        - Entity extraction: Yes (automatic)
        - Graph query: Yes (temporal graph)
        - Rerank: No (hybrid search instead)
        - New operation support flags (Phase 1)
        """
        return Capabilities(
            # Legacy capabilities
            embeddings=True,  # Always via OpenAI or compatible
            entity_extraction=True,  # Automatic fact extraction
            graph_query=True,  # Temporal graph queries
            rerank=False,  # Hybrid search, not explicit reranking
            schema_versions=["1.0.0"],
            supports_falkor=True,  # Primary target via FalkorDriver
            supports_milvus=False,  # Not used
            supports_neo4j=True,  # Graphiti also supports Neo4j
            max_tokens=None,  # No fixed limit
            # New operation support flags (Phase 1)
            supports_nodes=True,  # ✅ Via search_nodes()
            supports_facts=True,  # ✅ Via search_memory_facts()
            supports_episodes=True,  # ✅ Via get_episodes()
            supports_chunks=False,  # ❌ Episodes are not chunks
            supports_edges=True,  # ✅ Via get_entity_edge()
            # ID modality
            node_id_type="uuid",  # Graphiti uses UUIDs for nodes
            edge_id_type="uuid",  # Graphiti uses UUIDs for edges
        )
