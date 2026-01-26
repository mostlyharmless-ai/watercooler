"""Configuration schema for Watercooler.

Defines all configuration options with types, defaults, and validation.
Uses Pydantic for schema enforcement and clear error messages.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


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
        description="Suffix appended to code repo name for threads repo",
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

    Env overrides: LLM_API_KEY, LLM_API_BASE, LLM_MODEL, LLM_TIMEOUT, LLM_MAX_TOKENS
    """

    api_key: str = Field(
        default="",
        description="LLM API key (set via env for security)",
    )
    api_base: str = Field(
        default="https://api.openai.com/v1",
        description="LLM API base URL",
    )
    model: str = Field(
        default="gpt-4o-mini",
        description="LLM model name",
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
    summary_prompt: str = Field(
        default="Summarize this thread entry in 1-2 sentences. Be concise and factual.",
        description="Prompt template for entry summarization. Use {context} and {content} placeholders.",
    )


class EmbeddingServiceConfig(BaseModel):
    """Embedding service configuration for memory backends.

    Env overrides: EMBEDDING_API_KEY, EMBEDDING_API_BASE, EMBEDDING_MODEL, EMBEDDING_DIM,
                   EMBEDDING_TIMEOUT, EMBEDDING_BATCH_SIZE, EMBEDDING_CONTEXT_SIZE
    """

    api_key: str = Field(
        default="",
        description="Embedding API key (often not needed for local servers)",
    )
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
    """

    # LLM overrides (empty = use shared)
    llm_api_key: str = Field(
        default="",
        description="Override LLM API key for Graphiti (e.g., OpenAI key when shared uses local)",
    )
    llm_model: str = Field(
        default="",
        description="Override LLM model for Graphiti",
    )
    llm_api_base: str = Field(
        default="",
        description="Override LLM API base for Graphiti",
    )

    # Embedding overrides (empty = use shared)
    embedding_api_key: str = Field(
        default="",
        description="Override embedding API key for Graphiti",
    )
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


class LeanRAGBackendConfig(BaseModel):
    """LeanRAG-specific configuration overrides.

    These override shared [memory.llm] and [memory.embedding] settings.
    """

    # LLM overrides (empty = use shared)
    llm_api_key: str = Field(
        default="",
        description="Override LLM API key for LeanRAG (e.g., OpenAI key when shared uses local)",
    )
    llm_model: str = Field(
        default="",
        description="Override LLM model for LeanRAG",
    )
    llm_api_base: str = Field(
        default="",
        description="Override LLM API base for LeanRAG",
    )

    # Embedding overrides (empty = use shared)
    embedding_api_key: str = Field(
        default="",
        description="Override embedding API key for LeanRAG",
    )
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

    # Shared service configs
    llm: LLMServiceConfig = Field(default_factory=LLMServiceConfig)
    embedding: EmbeddingServiceConfig = Field(default_factory=EmbeddingServiceConfig)
    database: MemoryDatabaseConfig = Field(default_factory=MemoryDatabaseConfig)

    # Backend-specific overrides
    graphiti: GraphitiBackendConfig = Field(default_factory=GraphitiBackendConfig)
    leanrag: LeanRAGBackendConfig = Field(default_factory=LeanRAGBackendConfig)


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
