"""Configuration schema for Watercooler.

Defines all configuration options with types, defaults, and validation.
Uses Pydantic for schema enforcement and clear error messages.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CommonConfig(BaseModel):
    """Shared settings for both MCP and Dashboard."""

    # Threads repo naming pattern
    # Placeholders: {org}, {repo}, {namespace}
    # HTTPS is the default - works with credential helpers and tokens without SSH agent
    threads_pattern: str = Field(
        default="https://github.com/{org}/{repo}-threads.git",
        description="URL pattern for threads repos. Placeholders: {org}, {repo}, {namespace}",
    )
    threads_suffix: str = Field(
        default="-threads",
        description="Suffix appended to code repo name for threads repo. "
                    "Override with WATERCOOLER_THREADS_SUFFIX env var.",
    )
    templates_dir: str = Field(
        default="",
        description="Path to templates directory (empty = use bundled)",
    )

    @field_validator("templates_dir")
    @classmethod
    def validate_templates_dir(cls, v: str) -> str:
        """Warn if templates directory doesn't exist."""
        if v:
            path = Path(v).expanduser()
            if not path.exists():
                warnings.warn(
                    f"Templates directory does not exist: {v}",
                    UserWarning,
                )
            elif not path.is_dir():
                warnings.warn(
                    f"Templates path is not a directory: {v}",
                    UserWarning,
                )
        return v


class AgentConfig(BaseModel):
    """Configuration for a specific agent platform."""

    name: str = Field(description="Display name for this agent")
    default_spec: str = Field(
        default="general-purpose",
        description="Default specialization for this agent",
    )


class GitConfig(BaseModel):
    """Git-related MCP settings."""

    author: str = Field(
        default="",
        description="Git commit author (empty = use agent name)",
    )
    email: str = Field(
        default="mcp@watercooler.dev",
        description="Git commit email",
    )
    ssh_key: str = Field(
        default="",
        description="Path to SSH private key (empty = use default)",
    )

    @field_validator("ssh_key")
    @classmethod
    def validate_ssh_key(cls, v: str) -> str:
        """Warn if SSH key path doesn't exist."""
        if v:
            path = Path(v).expanduser()
            if not path.exists():
                warnings.warn(
                    f"SSH key path does not exist: {v}",
                    UserWarning,
                )
            elif not path.is_file():
                warnings.warn(
                    f"SSH key path is not a file: {v}",
                    UserWarning,
                )
        return v


class SyncConfig(BaseModel):
    """Git sync behavior settings."""

    async_sync: bool = Field(
        default=True,
        alias="async",
        description="Enable async git operations",
    )
    batch_window: float = Field(
        default=5.0,
        ge=0,
        description="Seconds to batch commits before push",
    )
    max_delay: float = Field(
        default=30.0,
        ge=0,
        description="Maximum delay before forcing push",
    )
    max_batch_size: int = Field(
        default=50,
        ge=1,
        description="Maximum entries per batch commit",
    )
    max_retries: int = Field(
        default=5,
        ge=0,
        description="Maximum retry attempts for failed operations",
    )
    max_backoff: float = Field(
        default=300.0,
        ge=0,
        description="Maximum backoff delay in seconds",
    )
    interval: float = Field(
        default=30.0,
        ge=1,
        description="Background sync interval in seconds",
    )
    stale_threshold: float = Field(
        default=60.0,
        ge=0,
        description="Seconds before considering sync stale",
    )

    class Config:
        populate_by_name = True


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="Log level",
    )
    dir: str = Field(
        default="",
        description="Log directory (empty = ~/.watercooler/logs)",
    )
    max_bytes: int = Field(
        default=10485760,  # 10MB
        ge=0,
        description="Maximum log file size in bytes",
    )
    backup_count: int = Field(
        default=5,
        ge=0,
        description="Number of backup log files to keep",
    )
    disable_file: bool = Field(
        default=False,
        description="Disable file logging (stderr only)",
    )

    @field_validator("dir")
    @classmethod
    def validate_log_dir(cls, v: str) -> str:
        """Warn if log directory doesn't exist (will be created on use)."""
        if v:
            path = Path(v).expanduser()
            if path.exists() and not path.is_dir():
                warnings.warn(
                    f"Log path exists but is not a directory: {v}",
                    UserWarning,
                )
        return v


class SlackConfig(BaseModel):
    """Slack integration configuration for notifications and bidirectional sync."""

    # Webhook for simple notifications (no token required)
    webhook_url: str = Field(
        default="",
        description="Slack Incoming Webhook URL for notifications",
    )

    # Bot token for full API access (Phase 2+)
    bot_token: str = Field(
        default="",
        description="Slack Bot Token (xoxb-...) for full API access",
    )

    # App token for Socket Mode (dev only)
    app_token: str = Field(
        default="",
        description="Slack App Token (xapp-...) for Socket Mode",
    )

    # Channel configuration (Phase 2+)
    channel_prefix: str = Field(
        default="wc-",
        description="Prefix for auto-created channels (e.g., 'wc-' -> #wc-watercooler-cloud)",
    )
    auto_create_channels: bool = Field(
        default=True,
        description="Auto-create Slack channels for repos on first sync",
    )

    # Default channel for activity feed
    default_channel: str = Field(
        default="",
        description="Default Slack channel for activity notifications (e.g., #watercooler-activity)",
    )

    # Notification toggles
    notify_on_say: bool = Field(
        default=True,
        description="Send notification when new entry is added",
    )
    notify_on_ball_flip: bool = Field(
        default=True,
        description="Send notification when ball is passed to another agent",
    )
    notify_on_status_change: bool = Field(
        default=True,
        description="Send notification when thread status changes",
    )
    notify_on_handoff: bool = Field(
        default=True,
        description="Send notification on explicit handoff",
    )

    # Rate limiting
    min_notification_interval: float = Field(
        default=1.0,
        ge=0,
        description="Minimum seconds between notifications (rate limit)",
    )

    @property
    def is_enabled(self) -> bool:
        """Check if Slack is enabled (webhook or bot token configured)."""
        return bool(self.webhook_url) or bool(self.bot_token)

    @property
    def is_webhook_only(self) -> bool:
        """Check if using webhook-only mode (Phase 1)."""
        return bool(self.webhook_url) and not bool(self.bot_token)

    @property
    def is_bot_enabled(self) -> bool:
        """Check if bot API mode is enabled (Phase 2+)."""
        return bool(self.bot_token)


class GraphConfig(BaseModel):
    """Baseline graph configuration for summaries and embeddings.

    LLM/embedding settings resolve via priority chain:
    1. Environment variables (LLM_API_BASE, EMBEDDING_API_BASE, etc.)
    2. TOML config values (if non-empty)
    3. Built-in defaults from memory_config module

    Empty string values signal "resolve from unified config at runtime".
    """

    # Summary generation
    generate_summaries: bool = Field(
        default=False,
        description="Generate LLM summaries for entries on write (requires LLM service)",
    )
    summarizer_api_base: str = Field(
        default="",
        description="Summarizer API base URL (empty = resolve from unified config)",
    )
    summarizer_model: str = Field(
        default="",
        description="Model for summarization (empty = resolve from unified config)",
    )

    # Embedding generation
    generate_embeddings: bool = Field(
        default=False,
        description="Generate embedding vectors for entries on write (requires embedding service)",
    )
    embedding_api_base: str = Field(
        default="",
        description="Embedding API base URL (empty = resolve from unified config)",
    )
    embedding_model: str = Field(
        default="",
        description="Model for embeddings (empty = resolve from unified config)",
    )

    # Behavior
    prefer_extractive: bool = Field(
        default=False,
        description="Use extractive summaries (no LLM) when True",
    )
    auto_detect_services: bool = Field(
        default=True,
        description="Check service availability before generation; skip gracefully if unavailable",
    )
    auto_start_services: bool = Field(
        default=False,
        description="Auto-start LLM/embedding services if unavailable (requires ServerManager)",
    )

    # Arc change detection for thread summaries
    embedding_divergence_threshold: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="Cosine similarity threshold for thread summary regeneration. "
                    "When a new entry's embedding similarity to the previous entry "
                    "falls below this threshold, it indicates a significant topic "
                    "shift ('arc change') and triggers automatic thread summary "
                    "regeneration. Lower values (0.4-0.5) reduce summary churn, "
                    "higher values (0.7-0.8) trigger more responsive updates. "
                    "Override with WATERCOOLER_EMBEDDING_DIVERGENCE_THRESHOLD env var.",
    )


class ServiceProvisionConfig(BaseModel):
    """Auto-provisioning configuration for external services.

    Controls whether watercooler automatically downloads binaries and models
    when they are needed but not found locally.

    Security note: Downloading executables (llama_server=true) fetches binaries
    from GitHub releases. Set to false and install manually if this is a concern.
    """

    models: bool = Field(
        default=True,
        description="Auto-download GGUF models from HuggingFace when needed",
    )
    llama_server: bool = Field(
        default=True,
        description="Auto-download llama-server binary from GitHub releases when needed",
    )


class HttpConfig(BaseModel):
    """HTTP transport configuration (only used when transport = "http")."""

    # CORS settings
    cors_origins: str = Field(
        default="",
        description="Comma-separated list of allowed CORS origins (empty = allow all)",
    )

    # Request limits
    max_request_size: int = Field(
        default=1024 * 1024,  # 1MB
        ge=1024,
        description="Maximum request body size in bytes",
    )
    request_timeout: int = Field(
        default=30,
        ge=1,
        le=300,
        description="Request timeout in seconds",
    )


class CacheConfig(BaseModel):
    """Cache configuration for MCP server."""

    # Backend selection
    backend: Literal["memory", "database"] = Field(
        default="memory",
        description="Cache backend: memory (local) or database (hosted)",
    )

    # TTL settings
    default_ttl: float = Field(
        default=300.0,
        ge=0,
        description="Default cache TTL in seconds",
    )

    # Memory cache limits
    max_entries: int = Field(
        default=10000,
        ge=100,
        description="Maximum entries in memory cache before LRU eviction",
    )

    # Database cache settings (only used when backend = "database")
    api_url: str = Field(
        default="",
        description="Base URL for database cache API (hosted mode)",
    )


class HostedConfig(BaseModel):
    """Hosted service configuration (watercooler.dev integration)."""

    # API endpoints
    api_url: str = Field(
        default="",
        description="Watercooler hosted API URL",
    )

    # Note: API keys and secrets should remain env-only for security


class McpConfig(BaseModel):
    """MCP server configuration."""

    # Transport
    transport: Literal["stdio", "http"] = Field(
        default="stdio",
        description="MCP transport mode",
    )
    host: str = Field(
        default="127.0.0.1",
        description="HTTP server host (http transport only)",
    )
    port: int = Field(
        default=3000,
        ge=1,
        le=65535,
        description="HTTP server port (http transport only)",
    )

    # Agent identity
    default_agent: str = Field(
        default="Agent",
        description="Default agent name when not detected",
    )
    agent_tag: str = Field(
        default="",
        description="User tag appended to agent name",
    )

    # Behavior
    auto_branch: bool = Field(
        default=True,
        description="Auto-create matching threads branches",
    )
    auto_provision: bool = Field(
        default=True,
        description="Auto-create threads repos if missing",
    )

    # Paths
    threads_dir: str = Field(
        default="",
        description="Explicit threads directory (empty = auto-discover)",
    )
    threads_base: str = Field(
        default="",
        description="Base directory for threads repos",
    )

    # Nested configs
    git: GitConfig = Field(default_factory=GitConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    graph: GraphConfig = Field(default_factory=GraphConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)
    service_provision: ServiceProvisionConfig = Field(
        default_factory=ServiceProvisionConfig,
        description="Auto-provisioning settings for external services (llama-server, models)",
    )
    http: HttpConfig = Field(
        default_factory=HttpConfig,
        description="HTTP transport settings (only used when transport = 'http')",
    )
    cache: CacheConfig = Field(
        default_factory=CacheConfig,
        description="Cache backend settings",
    )
    hosted: HostedConfig = Field(
        default_factory=HostedConfig,
        description="Hosted service (watercooler.dev) settings",
    )

    # Agent-specific overrides (keyed by platform slug)
    agents: Dict[str, AgentConfig] = Field(
        default_factory=lambda: {
            "claude-code": AgentConfig(name="Claude Code", default_spec="implementer-code"),
            "cursor": AgentConfig(name="Cursor", default_spec="implementer-code"),
            "codex": AgentConfig(name="Codex", default_spec="planner-architecture"),
            "gemini": AgentConfig(name="Gemini", default_spec="general-purpose"),
        },
        description="Agent-specific configuration overrides",
    )


class DashboardConfig(BaseModel):
    """Dashboard (watercooler-site) configuration."""

    default_repo: str = Field(
        default="",
        description="Pre-select this repo on dashboard load",
    )
    default_branch: str = Field(
        default="main",
        description="Default branch for new selections",
    )
    poll_interval_active: int = Field(
        default=15,
        ge=5,
        description="Polling interval when tab is active (seconds)",
    )
    poll_interval_moderate: int = Field(
        default=30,
        ge=10,
        description="Polling interval when tab is visible but inactive",
    )
    poll_interval_idle: int = Field(
        default=60,
        ge=15,
        description="Polling interval when tab is hidden",
    )
    expand_threads_by_default: bool = Field(
        default=False,
        description="Expand all threads on load",
    )
    show_closed_threads: bool = Field(
        default=False,
        description="Show closed threads by default",
    )


class EntryValidationConfig(BaseModel):
    """Entry format validation rules."""

    require_metadata: bool = Field(
        default=True,
        description="Require agent/role/type metadata in entries",
    )
    allowed_roles: List[str] = Field(
        default=["planner", "critic", "implementer", "tester", "pm", "scribe"],
        description="Valid entry roles",
    )
    allowed_types: List[str] = Field(
        default=["Note", "Plan", "Decision", "PR", "Closure"],
        description="Valid entry types",
    )
    require_spec_field: bool = Field(
        default=True,
        description="Require Spec: field in entry body",
    )


class CommitValidationConfig(BaseModel):
    """Commit footer validation rules."""

    require_footers: bool = Field(
        default=True,
        description="Require commit footers in threads commits",
    )
    required_footer_fields: List[str] = Field(
        default=[
            "Code-Repo",
            "Code-Branch",
            "Code-Commit",
            "Watercooler-Entry-ID",
        ],
        description="Required footer fields",
    )


class ValidationConfig(BaseModel):
    """Protocol validation configuration."""

    on_write: bool = Field(
        default=True,
        description="Validate on write operations",
    )
    on_commit: bool = Field(
        default=True,
        description="Validate on commit",
    )
    fail_on_violation: bool = Field(
        default=False,
        description="Fail on violation (vs warn)",
    )
    check_branch_pairing: bool = Field(
        default=True,
        description="Validate branch pairing",
    )
    check_commit_footers: bool = Field(
        default=True,
        description="Validate commit footers",
    )
    check_entry_format: bool = Field(
        default=True,
        description="Validate entry format",
    )
    check_status_values: bool = Field(
        default=True,
        description="Validate status values",
    )

    entry: EntryValidationConfig = Field(default_factory=EntryValidationConfig)
    commit: CommitValidationConfig = Field(default_factory=CommitValidationConfig)


# =============================================================================
# Memory Backend Configuration
# =============================================================================


class LLMServiceConfig(BaseModel):
    """LLM service configuration for memory backends.

    Env overrides: LLM_API_KEY, LLM_API_BASE, LLM_MODEL, LLM_TIMEOUT, LLM_MAX_TOKENS,
                   LLM_CONTEXT_SIZE

    Note: API keys should be stored in credentials.toml, not config.toml.
    Use [openai].api_key, [anthropic].api_key, etc. in ~/.watercooler/credentials.toml
    """

    api_base: str = Field(
        default="",
        description="LLM API base URL. Empty means use context-specific default (localhost for baseline graph).",
    )
    model: str = Field(
        default="",
        description="LLM model name. Empty means use context-specific default.",
    )
    timeout: float = Field(
        default=60.0,
        ge=1.0,
        description="Request timeout in seconds",
    )
    max_tokens: int = Field(
        default=512,
        ge=1,
        description="Maximum tokens for LLM response",
    )
    context_size: int = Field(
        default=8192,
        ge=512,
        description="Context window size for local llama-server auto-start (ignored for external APIs). Env: LLM_CONTEXT_SIZE",
    )
    # Prompt configuration for summarization
    system_prompt: str = Field(
        default="",
        description="System prompt for chat-style LLMs. Empty means auto-detect based on model.",
    )
    prompt_prefix: str = Field(
        default="",
        description="Prefix added to user prompt (e.g., '/no_think' for Qwen3). Empty means auto-detect.",
    )
    summary_prompt: str = Field(
        default="Summarize this thread entry in 1-2 sentences. Be concise and factual.",
        description="Prompt template for entry summarization. Use {context} and {content} placeholders.",
    )
    thread_summary_prompt: str = Field(
        default="Summarize this development thread in 2-3 sentences. Include the main topic, key decisions, and outcome if any.",
        description="Prompt template for thread summarization. Use {title} and {entries} placeholders.",
    )
    # Few-shot example for summarization (improves format compliance)
    summary_example_input: str = Field(
        default="Implemented OAuth2 authentication with JWT tokens. Added refresh token rotation and secure cookie storage.",
        description="Example input for few-shot summarization prompt.",
    )
    summary_example_output: str = Field(
        default="OAuth2 authentication implemented with JWT tokens, refresh rotation, and secure cookie storage.\ntags: #authentication #OAuth2 #JWT #security",
        description="Example output for few-shot summarization prompt.",
    )


class EmbeddingServiceConfig(BaseModel):
    """Embedding service configuration for memory backends.

    Env overrides: EMBEDDING_API_KEY, EMBEDDING_API_BASE, EMBEDDING_MODEL, EMBEDDING_DIM,
                   EMBEDDING_TIMEOUT, EMBEDDING_BATCH_SIZE, EMBEDDING_CONTEXT_SIZE

    Note: API keys should be stored in credentials.toml, not config.toml.
    Use [openai].api_key, [voyage].api_key, etc. in ~/.watercooler/credentials.toml
    """

    api_base: str = Field(
        default="http://localhost:8080/v1",
        description="Embedding API base URL (llama.cpp default)",
    )
    model: str = Field(
        default="bge-m3",
        description="Embedding model name",
    )
    dim: int = Field(
        default=1024,
        ge=1,
        description="Embedding dimension",
    )
    context_size: int = Field(
        default=8192,
        ge=128,
        description="Context window size for embedding server (tokens). Env: EMBEDDING_CONTEXT_SIZE",
    )
    timeout: float = Field(
        default=60.0,
        ge=1.0,
        description="Request timeout in seconds",
    )
    batch_size: int = Field(
        default=32,
        ge=1,
        description="Batch size for embedding requests",
    )


class MemoryDatabaseConfig(BaseModel):
    """Database (FalkorDB) configuration for memory backends.

    Env overrides: FALKORDB_HOST, FALKORDB_PORT, FALKORDB_PASSWORD
    """

    host: str = Field(
        default="localhost",
        description="Database host",
    )
    port: int = Field(
        default=6379,
        ge=1,
        le=65535,
        description="Database port",
    )
    username: str = Field(
        default="",
        description="Database username (optional)",
    )
    password: str = Field(
        default="",
        description="Database password (optional)",
    )


class GraphitiBackendConfig(BaseModel):
    """Graphiti-specific configuration overrides.

    These override shared [memory.llm] and [memory.embedding] settings.

    Note: API keys should be stored in credentials.toml, not config.toml.
    Use [openai].api_key, etc. in ~/.watercooler/credentials.toml
    """

    # LLM overrides (empty = use shared)
    llm_model: str = Field(
        default="",
        description="Override LLM model for Graphiti",
    )
    llm_api_base: str = Field(
        default="",
        description="Override LLM API base for Graphiti",
    )

    # Embedding overrides (empty = use shared)
    embedding_model: str = Field(
        default="",
        description="Override embedding model for Graphiti",
    )
    embedding_api_base: str = Field(
        default="",
        description="Override embedding API base for Graphiti",
    )

    # Graphiti-specific settings
    reranker: str = Field(
        default="rrf",
        description="Reranker algorithm: rrf, mmr, cross_encoder, node_distance, episode_mentions",
    )
    track_entry_episodes: bool = Field(
        default=True,
        description="Track entry-episode mappings in index",
    )

    # Chunking settings for entry sync
    chunk_on_sync: bool = Field(
        default=True,
        description="Enable chunking when syncing entries to Graphiti",
    )
    chunk_max_tokens: int = Field(
        default=768,
        ge=100,
        le=4096,
        description="Maximum tokens per chunk (768 balances comprehensiveness vs 'lost in the middle')",
    )
    chunk_overlap: int = Field(
        default=64,
        ge=0,
        le=256,
        description="Token overlap between chunks for context continuity",
    )
    use_summary: bool = Field(
        default=False,
        description=(
            "Send enriched summary to Graphiti instead of raw body. "
            "Requires enrichment with generate_summaries=true. "
            "Falls back to raw body when summary is empty."
        ),
    )


class LeanRAGBackendConfig(BaseModel):
    """LeanRAG-specific configuration overrides.

    These override shared [memory.llm] and [memory.embedding] settings.

    Note: API keys should be stored in credentials.toml, not config.toml.
    Use [openai].api_key, etc. in ~/.watercooler/credentials.toml
    """

    # Path to LeanRAG installation
    path: str = Field(
        default="",
        description="Path to LeanRAG installation directory. Env override: LEANRAG_PATH",
    )

    # LLM overrides (empty = use shared)
    llm_model: str = Field(
        default="",
        description="Override LLM model for LeanRAG",
    )
    llm_api_base: str = Field(
        default="",
        description="Override LLM API base for LeanRAG",
    )

    # Embedding overrides (empty = use shared)
    embedding_model: str = Field(
        default="",
        description="Override embedding model for LeanRAG",
    )
    embedding_api_base: str = Field(
        default="",
        description="Override embedding API base for LeanRAG",
    )

    # LeanRAG-specific settings
    max_workers: int = Field(
        default=8,
        ge=1,
        description="Max parallel workers for graph building",
    )


class TierOrchestrationConfig(BaseModel):
    """Multi-tier memory query orchestration configuration.

    Controls which memory tiers are enabled and escalation behavior.
    Environment variables override TOML settings.

    Env overrides:
        WATERCOOLER_TIER_T1_ENABLED, WATERCOOLER_TIER_T2_ENABLED,
        WATERCOOLER_TIER_T3_ENABLED, WATERCOOLER_TIER_MAX_TIERS,
        WATERCOOLER_TIER_MIN_RESULTS
    """

    t1_enabled: bool = Field(
        default=True,
        description="Enable T1 (Baseline) tier - JSONL graph with keyword/semantic search",
    )
    t2_enabled: bool = Field(
        default=True,
        description="Enable T2 (Graphiti) tier - FalkorDB temporal graph. Auto-enabled when memory.backend='graphiti'",
    )
    t3_enabled: bool = Field(
        default=False,
        description="Enable T3 (LeanRAG) tier - Hierarchical clustering with multi-hop reasoning. Expensive, opt-in.",
    )
    max_tiers: int = Field(
        default=2,
        ge=1,
        le=3,
        description="Maximum number of tiers to query before stopping (budget control)",
    )
    min_results: int = Field(
        default=3,
        ge=1,
        description="Minimum results required before considering a tier sufficient",
    )
    min_confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Minimum average confidence score for sufficiency",
    )
    t1_limit: int = Field(
        default=10,
        ge=1,
        description="Maximum results to fetch from T1",
    )
    t2_limit: int = Field(
        default=10,
        ge=1,
        description="Maximum results to fetch from T2",
    )
    t3_limit: int = Field(
        default=5,
        ge=1,
        description="Maximum results to fetch from T3",
    )


class MemoryConfig(BaseModel):
    """Memory backend configuration.

    Single source of truth for LLM and embedding settings across all memory backends.
    Environment variables override TOML settings.
    Backend-specific sections override shared settings.
    """

    enabled: bool = Field(
        default=True,
        description="Enable memory backends globally",
    )
    backend: Literal["graphiti", "leanrag", "null"] = Field(
        default="graphiti",
        description="Default memory backend",
    )
    queue_enabled: bool = Field(
        default=False,
        description="Enable persistent memory task queue with retry and dead-letter semantics",
    )

    # Shared service configs
    llm: LLMServiceConfig = Field(default_factory=LLMServiceConfig)
    embedding: EmbeddingServiceConfig = Field(default_factory=EmbeddingServiceConfig)
    database: MemoryDatabaseConfig = Field(default_factory=MemoryDatabaseConfig)

    # Tier orchestration
    tiers: TierOrchestrationConfig = Field(default_factory=TierOrchestrationConfig)

    # Backend-specific overrides
    graphiti: GraphitiBackendConfig = Field(default_factory=GraphitiBackendConfig)
    leanrag: LeanRAGBackendConfig = Field(default_factory=LeanRAGBackendConfig)


class FederationScoringConfig(BaseModel):
    """Scoring parameters for federated search.

    Uses ConfigDict(frozen=True) — intentional Pydantic v2 pattern upgrade.
    Existing config models use legacy `class Config:` pattern.
    """

    model_config = ConfigDict(frozen=True)

    local_weight: float = Field(
        default=1.0, ge=0.0, le=10.0,
        description="NW for primary namespace (max 10.0; 0.0 disables the namespace; values > ~1.43 produce ranking_score > 1.0)",
    )
    wide_weight: float = Field(
        default=0.55, ge=0.0, le=10.0,
        description="NW for wide-scope namespaces (max 10.0; 0.0 disables the namespace; values > ~1.43 produce ranking_score > 1.0)",
    )
    recency_floor: float = Field(default=0.7, ge=0.0, le=1.0)
    recency_half_life_days: float = Field(default=60.0, gt=0.0)


class FederationNamespaceConfig(BaseModel):
    """Configuration for a single federated namespace."""

    model_config = ConfigDict(frozen=True)

    code_path: str = Field(description="Absolute path to the namespace's code repo root")
    deny_topics: List[str] = Field(default_factory=list)

    @field_validator("code_path")
    @classmethod
    def validate_code_path(cls, v: str) -> str:
        """Reject null bytes, require absolute path, resolve traversals."""
        if "\x00" in v:
            raise ValueError("code_path contains null bytes")
        if not os.path.isabs(v):
            raise ValueError(f"code_path must be absolute, got: {v}")
        return str(Path(v).resolve())


class FederationAccessConfig(BaseModel):
    """Per-primary-namespace access allowlists."""

    model_config = ConfigDict(frozen=True)

    allowlists: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Map of primary namespace -> list of allowed secondary namespaces",
    )


class FederationConfig(BaseModel):
    """Top-level federation configuration.

    Lives at `[federation]` in TOML config, peer of [memory], [common], etc.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = Field(default=False, description="Enable federation features")
    namespaces: Dict[str, FederationNamespaceConfig] = Field(default_factory=dict)
    access: FederationAccessConfig = Field(default_factory=FederationAccessConfig)
    scoring: FederationScoringConfig = Field(default_factory=FederationScoringConfig)
    namespace_timeout: float = Field(
        default=0.4, gt=0.0, le=30.0,
        description="Per-namespace search timeout in seconds (max 30). Note: cancelling "
                    "a timed-out asyncio.to_thread task stops the coroutine wrapper "
                    "but the underlying search_graph thread runs to completion. "
                    "Tune conservatively to avoid thread accumulation under load.",
    )
    max_namespaces: int = Field(
        default=5, ge=1, le=20,
        description="Maximum number of secondary namespaces to query "
                    "(primary is always included and does not count toward this limit)",
    )
    max_total_timeout: float = Field(
        default=2.0, gt=0.0, le=60.0,
        description="Total wall-clock budget for all namespace searches combined (max 60s)",
    )

    @model_validator(mode="after")
    def check_timeout_ordering(self) -> "FederationConfig":
        """Ensure per-namespace timeout does not exceed total timeout budget."""
        if self.namespace_timeout > self.max_total_timeout:
            raise ValueError(
                f"namespace_timeout ({self.namespace_timeout}s) must be <= "
                f"max_total_timeout ({self.max_total_timeout}s)"
            )
        return self

    @model_validator(mode="after")
    def check_no_basename_collisions(self) -> "FederationConfig":
        """Reject configs where two namespaces map to the same worktree basename."""
        basenames: Dict[str, str] = {}
        for ns_id, ns_config in self.namespaces.items():
            basename = Path(ns_config.code_path).name
            if basename in basenames:
                raise ValueError(
                    f"Namespace basename collision: '{ns_id}' and '{basenames[basename]}' "
                    f"both resolve to worktree basename '{basename}'"
                )
            basenames[basename] = ns_id
        return self


class WatercoolerConfig(BaseModel):
    """Root configuration model."""

    version: int = Field(
        default=1,
        ge=1,
        description="Config schema version",
    )

    common: CommonConfig = Field(default_factory=CommonConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    federation: FederationConfig = Field(default_factory=FederationConfig)

    @classmethod
    def default(cls) -> "WatercoolerConfig":
        """Create config with all defaults."""
        return cls()

    def get_agent_config(self, platform_slug: str) -> Optional[AgentConfig]:
        """Get agent-specific config by platform slug.

        Args:
            platform_slug: Platform identifier (e.g., "claude-code", "cursor")

        Returns:
            AgentConfig if found, None otherwise
        """
        # Normalize slug
        slug = platform_slug.lower().replace(" ", "-").replace("_", "-")
        return self.mcp.agents.get(slug)

    def resolve_agent_name(
        self,
        agent_func: Optional[str] = None,
        env_agent: Optional[str] = None,
        platform_slug: Optional[str] = None,
    ) -> str:
        """Resolve agent name using priority order.

        Priority (highest first):
        1. agent_func parameter (e.g., "Claude Code:sonnet-4:implementer")
        2. Environment variable (WATERCOOLER_AGENT)
        3. Platform-specific config
        4. Default agent

        Args:
            agent_func: Per-call agent function string
            env_agent: WATERCOOLER_AGENT environment value
            platform_slug: Detected platform identifier

        Returns:
            Resolved agent name
        """
        # 1. agent_func takes priority
        if agent_func:
            parts = agent_func.split(":")
            if parts:
                return parts[0]

        # 2. Environment variable
        if env_agent:
            return env_agent

        # 3. Platform-specific config
        if platform_slug:
            agent_config = self.get_agent_config(platform_slug)
            if agent_config:
                return agent_config.name

        # 4. Default
        return self.mcp.default_agent
