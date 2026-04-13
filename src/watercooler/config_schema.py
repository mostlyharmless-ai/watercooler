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
        le=10_737_418_240,  # 10GB cap
        description="Maximum log file size in bytes",
    )
    backup_count: int = Field(
        default=5,
        ge=0,
        le=100,
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


class ThreadAuditorConfig(BaseModel):
    """Configuration for the thread auditor daemon."""

    enabled: bool = Field(
        default=False,
        description="Enable the thread auditor daemon (opt-in, requires global daemons.enabled=True)",
    )
    interval: float = Field(
        default=300.0,
        ge=10.0,
        description="Seconds between audit scans",
    )
    check_missing_status: bool = Field(
        default=True,
        description="Flag threads with no Status: header",
    )
    check_missing_ball: bool = Field(
        default=True,
        description="Flag threads with no Ball: header",
    )
    check_missing_entry_ids: bool = Field(
        default=True,
        description="Flag entries with no Entry-ID comment",
    )
    check_missing_summaries: bool = Field(
        default=True,
        description="Flag entries/threads missing graph summaries",
    )
    check_stale_threads: bool = Field(
        default=True,
        description="Flag threads with no recent activity",
    )
    stale_days: int = Field(
        default=14,
        ge=1,
        description="Days of inactivity before a thread is considered stale",
    )
    check_classification: bool = Field(
        default=True,
        description="Suggest directory reclassification for misplaced threads",
    )
    max_findings_per_run: int = Field(
        default=200,
        ge=1,
        description="Cap findings per tick to prevent runaway",
    )


class ContentScoutConfig(BaseModel):
    """Configuration for the content scout daemon."""

    enabled: bool = Field(
        default=False,
        description="Enable the content scout daemon (opt-in, requires global daemons.enabled=True)",
    )
    interval: float = Field(
        default=300.0,
        ge=10.0,
        description="Seconds between content scans",
    )
    check_blog_opportunities: bool = Field(
        default=True,
        description="Flag threads with enough depth for blog posts",
    )
    check_social_opportunities: bool = Field(
        default=True,
        description="Flag threads with sharp insights for social media",
    )
    check_novel_features: bool = Field(
        default=True,
        description="Flag shipped features worth announcing",
    )
    check_process_stories: bool = Field(
        default=True,
        description="Flag notable human/agent collaboration patterns",
    )
    max_findings_per_run: int = Field(
        default=200,
        ge=1,
        description="Cap findings per tick to prevent runaway",
    )


class DaemonLLMConfig(BaseModel):
    """LLM configuration for daemon use.

    Can be specified at the shared daemon level ([mcp.daemons.llm])
    or per-daemon (e.g. [mcp.daemons.content_refiner.llm]).
    Per-daemon settings override shared settings.
    """

    api_base: str = Field(
        default="",
        description="LLM API base URL (empty = fall through to resolve_baseline_graph_llm_config)",
    )
    model: str = Field(
        default="",
        description="LLM model name (empty = fall through to baseline config)",
    )
    api_key: str = Field(
        default="",
        exclude=True,
        description="API key (prefer credentials.toml or env vars over this field). "
        "Excluded from model_dump()/serialization to prevent leakage.",
    )
    timeout: Optional[float] = Field(
        default=None,
        ge=1.0,
        le=600.0,
        description="Request timeout in seconds (None = fall through to shared/baseline)",
    )
    max_tokens: Optional[int] = Field(
        default=None,
        ge=1,
        le=32768,
        description="Maximum tokens in LLM response (None = fall through to shared/baseline)",
    )

    def __repr__(self) -> str:
        """Redact api_key in repr to prevent log leakage."""
        key_display = "***" if self.api_key else ""
        return (
            f"DaemonLLMConfig(api_base={self.api_base!r}, model={self.model!r}, "
            f"api_key={key_display!r}, timeout={self.timeout}, max_tokens={self.max_tokens})"
        )


class ContentRefinerConfig(BaseModel):
    """Configuration for the content refiner daemon (Layer 2 LLM scoring)."""

    enabled: bool = Field(
        default=False,
        description="Enable the content refiner daemon (opt-in, requires global daemons.enabled=True)",
    )
    interval: float = Field(
        default=600.0,
        ge=10.0,
        description="Seconds between refiner ticks",
    )
    max_candidates_per_tick: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Maximum Layer 1 findings to process per tick",
    )
    min_layer1_confidence: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="Minimum Layer 1 confidence to consider for refinement",
    )
    max_context_chars: int = Field(
        default=3000,
        ge=500,
        le=16000,
        description="Character budget for deep thread context sent to LLM",
    )
    cursor_gc_interval: int = Field(
        default=24,
        ge=1,
        description="Ticks between cursor garbage collection (prune stale processed IDs)",
    )
    score_weights: Dict[str, float] = Field(
        default_factory=lambda: {
            "publishability": 0.30,
            "evidence_quality": 0.25,
            "audience_appeal": 0.20,
            "novelty": 0.15,
            "actionability": 0.10,
        },
        description="Weights for multi-dimensional composite score calculation",
    )
    llm: Optional[DaemonLLMConfig] = Field(
        default=None,
        description="Per-daemon LLM overrides (falls through to shared daemon LLM config if unset)",
    )

    @field_validator("score_weights")
    @classmethod
    def validate_score_weights(cls, v: Dict[str, float]) -> Dict[str, float]:
        """Validate score weights: all 5 dimensions required, sum to ~1.0, positive values."""
        _VALID_DIMENSIONS = {
            "publishability",
            "evidence_quality",
            "audience_appeal",
            "novelty",
            "actionability",
        }
        if not v:
            raise ValueError("score_weights must not be empty")
        missing = _VALID_DIMENSIONS - set(v.keys())
        if missing:
            raise ValueError(
                f"score_weights must include all 5 dimensions. "
                f"Missing: {sorted(missing)}"
            )
        for key, weight in v.items():
            if key not in _VALID_DIMENSIONS:
                raise ValueError(
                    f"score_weights[{key!r}] is not a valid dimension. "
                    f"Valid: {sorted(_VALID_DIMENSIONS)}"
                )
            if weight < 0:
                raise ValueError(f"score_weights[{key!r}] must be >= 0, got {weight}")
        total = sum(v.values())
        if not (0.95 <= total <= 1.05):
            raise ValueError(f"score_weights must sum to ~1.0, got {total:.3f}")
        return v


class CompoundConfig(BaseModel):
    """Per-project opt-in for compound artifact generation.

    When ``enabled = true``, compound artifact generation is activated.
    The ``generate_compound_artifacts()`` function in
    ``watercooler_mcp.daemons.compound`` serves as the callable hook; callers
    are responsible for dispatching it at the appropriate point (e.g. thread
    closure). Full dispatch wiring is tracked in issue #214.

    Compound artifacts are visible workflow artifacts that imply a process
    step was completed. They require explicit opt-in per project.
    """

    enabled: bool = Field(
        default=False,
        description="Enable compound artifact generation (must be explicitly opted in)",
    )
    auto_report_on_closure: bool = Field(
        default=True,
        description="Generate report when thread closes (if enabled)",
    )
    auto_learnings: bool = Field(
        default=True,
        description="Extract learnings from threads (if enabled)",
    )
    auto_suggestions: bool = Field(
        default=True,
        description="Generate suggestions from threads (if enabled)",
    )


class DecisionDetectorConfig(BaseModel):
    """Configuration for the decision detector daemon."""

    enabled: bool = Field(
        default=False,
        description="Enable the decision detector daemon (opt-in, requires global daemons.enabled=True)",
    )
    interval: float = Field(
        default=300.0,
        ge=10.0,
        description="Seconds between detection scans",
    )
    min_score: int = Field(
        default=2,
        ge=1,
        le=10,
        description="Minimum score to report as finding (2=Medium+High, 4=High only)",
    )
    max_findings_per_run: int = Field(
        default=200,
        ge=1,
        description="Cap findings per tick to prevent runaway on first scan",
    )
    fuzzy_threshold: int = Field(
        default=85,
        ge=0,
        le=100,
        description="rapidfuzz threshold (0=disabled). Requires rapidfuzz package.",
    )
    scan_closed_threads: bool = Field(
        default=True,
        description="Include closed threads in scanning (decisions may exist in closed threads)",
    )
    exclude_agents: list[str] = Field(
        default_factory=lambda: ["ExtractDecisionsDaemon"],
        description="Agent name prefixes to exclude from scoring (prevents feedback loops)",
    )


class DecisionExtractorConfig(BaseModel):
    """Configuration for the decision extractor daemon."""

    enabled: bool = Field(
        default=False,
        description="Enable the decision extractor daemon (opt-in)",
    )
    interval: float = Field(
        default=1800.0,
        ge=60.0,
        description="Seconds between extraction cycles (default 30 min)",
    )
    min_extraction_score: int = Field(
        default=4,
        ge=1,
        le=10,
        description="Minimum detect score to attempt extraction (4=High tier only)",
    )
    max_candidates_per_tick: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Max candidates to process per tick (LLM cost control)",
    )
    max_extractions_per_day: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Daily extraction cap (date-based, resets at midnight UTC)",
    )
    max_body_chars: int = Field(
        default=4000,
        ge=500,
        le=32000,
        description="Max entry body chars sent to LLM",
    )
    min_confidence: int = Field(
        default=3,
        ge=1,
        le=5,
        description="Minimum LLM confidence score to emit Decision entry (3=floor)",
    )
    max_tick_duration: float = Field(
        default=300.0,
        ge=30.0,
        description="Hard timeout per tick in seconds (prevents runaway LLM calls)",
    )
    max_extraction_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description=(
            "Per-entry cap on LLM-caused extraction failures "
            "(llm_unavailable, empty_decision_body). After this many attempts "
            "the entry is permanently skipped via extraction_cap_reached."
        ),
    )
    max_write_failure_attempts: int = Field(
        default=5,
        ge=1,
        le=20,
        description=(
            "Per-entry cap on infrastructure write failures. Higher than "
            "max_extraction_attempts because write failures are typically "
            "transient (disk, git push)."
        ),
    )
    llm: Optional[DaemonLLMConfig] = Field(
        default=None,
        description="Daemon-specific LLM config (falls through to [mcp.daemons.llm])",
    )


class PulseSnapshotConfig(BaseModel):
    """Configuration for the pulse snapshot daemon."""

    enabled: bool = Field(
        default=False,
        description="Enable the pulse snapshot daemon (opt-in)",
    )
    interval: float = Field(
        default=600.0,
        ge=30.0,
        description="Seconds between snapshot scans (default 10 min)",
    )
    session_window_days: int = Field(
        default=7,
        ge=1,
        le=90,
        description="Look-back window for session themes",
    )
    stale_thread_days: int = Field(
        default=14,
        ge=1,
        le=365,
        description="Days of inactivity before a thread is flagged stale",
    )
    analysis_freshness_days: int = Field(
        default=7,
        ge=1,
        le=90,
        description="Days before analysis report is considered stale",
    )
    long_window_days: int = Field(
        default=30,
        ge=7,
        le=365,
        description=(
            "Long-window lookback for baseline score computation. "
            "Must be greater than session_window_days."
        ),
    )
    llm: Optional[DaemonLLMConfig] = Field(
        default=None,
        description=(
            "LLM config for enrichment pass. None (default) = deterministic-only. "
            "Enrichment is opt-in: setting this field enables it; the shared "
            "[mcp.daemons.llm] fallback is intentionally NOT consulted."
        ),
    )
    enrich_every_n_ticks: int = Field(
        default=6,
        ge=1,
        description=(
            "Run LLM enrichment every Nth tick "
            "(default 6 = hourly at the default 600s interval)"
        ),
    )
    max_enrichments_per_day: int = Field(
        default=48,
        ge=1,
        description="Safety cap on daily LLM enrichment runs (per repo_key)",
    )
    enrich_mode: Literal["always", "pre_report"] = Field(
        default="always",
        description=(
            "Enrichment scheduling mode. "
            '"always": fire every enrich_every_n_ticks ticks, subject to daily cap '
            "(current behaviour). "
            '"pre_report": fire at most once per UTC day, only before the daily report '
            "has run. Recommended production setting once PulseReportDaemon (P1.1) is "
            "deployed. Note: max_enrichments_per_day is not consulted in pre_report "
            "mode — the once-per-day gate is enforced by the enrichment_daily_count "
            "field directly."
        ),
    )
    # max_session_threads and max_findings_per_run are internal constants
    # in the daemon (50 and 200 respectively), not user-configurable.

    @model_validator(mode="after")
    def _validate_window_order(self) -> "PulseSnapshotConfig":
        if self.long_window_days <= self.session_window_days:
            if "long_window_days" in self.model_fields_set:
                # User explicitly provided an invalid value — report it clearly.
                raise ValueError(
                    f"long_window_days ({self.long_window_days}) must be greater than "
                    f"session_window_days ({self.session_window_days})"
                )
            # long_window_days was not explicitly set (took the default of 30);
            # auto-adjust so configs that only set session_window_days don't break
            # at startup when the default would collide (e.g. session_window_days=30).
            adjusted = self.session_window_days + 14
            object.__setattr__(self, "long_window_days", adjusted)
        return self

    # frozen=True: intentional divergence from older daemon configs;
    # follow-up issue to retrofit frozen to all daemon configs.
    model_config = ConfigDict(frozen=True)


class AnalysisSnapshotConfig(BaseModel):
    """Configuration for the analysis snapshot daemon."""

    enabled: bool = Field(
        default=False,
        description="Enable analysis snapshot daemon (opt-in)",
    )
    interval: float = Field(
        default=3600.0,
        ge=60.0,
        description="Seconds between analysis runs (default 1h)",
    )
    window_days: int = Field(
        default=7,
        ge=1,
        le=90,
        description="Look-back window for analysis",
    )
    include_closed: bool = Field(
        default=False,
        description="Include closed threads in analysis",
    )
    code_branch: str = Field(
        default="*",
        description="Code branch filter; '*' = all branches",
    )

    model_config = ConfigDict(frozen=True)


class TrendSnapshotConfig(BaseModel):
    """Configuration for the trend snapshot daemon."""

    enabled: bool = Field(
        default=False,
        description="Enable trend snapshot daemon (opt-in)",
    )
    interval: float = Field(
        default=3600.0,
        ge=60.0,
        description="Seconds between trend analysis runs (default 1h)",
    )
    query: str = Field(
        default="decided committed architecture approach design",
        description="Search query for fact retrieval from Graphiti",
    )
    max_facts: int = Field(
        default=50,
        ge=10,
        le=50,
        description="Max facts per tick (GraphitiBackend.MAX_SEARCH_RESULTS=50 hard cap)",
    )

    model_config = ConfigDict(frozen=True)


class PulseReportConfig(BaseModel):
    """Configuration for the pulse report daemon."""

    enabled: bool = Field(
        default=False,
        description="Enable automated pulse report generation (opt-in)",
    )
    interval: float = Field(
        default=86400.0,
        ge=600.0,
        description="Seconds between report generation runs (default 24h)",
    )
    report_thread: str = Field(
        default="project-pulse-report",
        description="Thread topic for posting generated reports",
    )
    report_branch: str = Field(
        default="main",
        description="code_branch for report thread entries (scheduled reports write to main)",
    )
    report_output_dir: str = Field(
        default="dev_docs/reports/pulse",
        description="Directory for .md report files, relative to repo root (must be relative)",
    )
    window_days: int = Field(
        default=7,
        ge=1,
        le=90,
        description="Look-back window passed to report inputs (fallback if not in snapshot)",
    )
    snapshot_max_age_hours: float = Field(
        default=4.0,
        ge=0.5,
        description="Max snapshot age before skipping report generation (hours)",
    )
    analysis_freshness_days: int = Field(
        default=7,
        ge=1,
        le=90,
        description="Days before analysis data is considered stale",
    )
    trend_snapshot_max_age_hours: float = Field(
        default=4.0,
        ge=0.5,
        description=(
            "Hours before a trend snapshot is considered stale and skipped. "
            "Must exceed trend_snapshot.interval / 3600 to avoid self-breaking."
        ),
    )
    llm: Optional[DaemonLLMConfig] = Field(
        default=None,
        description=(
            "LLM config for generating executive summary in daemon reports. "
            "Must be set explicitly — shared [mcp.daemons.llm] and baseline LLM "
            "config are intentionally not consulted. "
            "Omit (or leave None) to use deterministic summary fallback."
        ),
    )

    @field_validator("report_output_dir")
    @classmethod
    def _must_be_relative(cls, v: str) -> str:
        p = Path(v)
        if p.is_absolute():
            raise ValueError(
                f"report_output_dir must be a relative path, got absolute: {v!r}"
            )
        if ".." in p.parts:
            raise ValueError(
                f"report_output_dir must not contain '..' traversal components, got: {v!r}"
            )
        return v

    model_config = ConfigDict(frozen=True)


class ProjectCoordinatorConfig(BaseModel):
    """Configuration for the project coordinator daemon."""

    enabled: bool = Field(
        default=False,
        description="Enable the project coordinator daemon (opt-in)",
    )
    interval: float = Field(
        default=600.0,
        ge=60.0,
        description="Seconds between coordination scans (default 10 min)",
    )
    max_findings_per_run: int = Field(
        default=200,
        ge=1,
        description="Cap findings per tick to prevent runaway on first scan",
    )
    suppression_tags: list[str] = Field(
        default_factory=lambda: ["parked", "wontfix", "deferred"],
        description="Thread annotation tags that soft-suppress stalled_* findings",
    )
    stance_enabled: bool = Field(
        default=True,
        description="Enable stance advisory emission (v1B)",
    )
    stance_snapshot_max_age_hours: float = Field(
        default=4.0,
        ge=0.5,
        description="Max age of pulse snapshot before degraded mode",
    )

    model_config = ConfigDict(frozen=True)


class SyncGuardConfig(BaseModel):
    """Configuration for the sync guard daemon."""

    enabled: bool = Field(
        default=True,
        description="Enable sync guard daemon (proactive worktree parity checks)",
    )
    interval: float = Field(
        default=180.0,
        ge=30.0,
        description="Seconds between parity checks",
    )


class DaemonsConfig(BaseModel):
    """Daemon management configuration."""

    enabled: bool = Field(
        default=False,
        description="Enable daemon management system globally (opt-in per project)",
    )
    llm: Optional[DaemonLLMConfig] = Field(
        default=None,
        description="Shared LLM config for all daemons ([mcp.daemons.llm])",
    )
    compound: CompoundConfig = Field(
        default_factory=CompoundConfig,
        description="Compound artifact generation settings (off by default)",
    )
    thread_auditor: ThreadAuditorConfig = Field(
        default_factory=ThreadAuditorConfig,
        description="Thread auditor daemon settings",
    )
    content_scout: ContentScoutConfig = Field(
        default_factory=ContentScoutConfig,
        description="Content scout daemon settings",
    )
    content_refiner: ContentRefinerConfig = Field(
        default_factory=ContentRefinerConfig,
        description="Content refiner daemon settings (Layer 2 LLM scoring)",
    )
    decision_detector: DecisionDetectorConfig = Field(
        default_factory=DecisionDetectorConfig,
        description="Decision detector daemon settings",
    )
    decision_extractor: DecisionExtractorConfig = Field(
        default_factory=DecisionExtractorConfig,
        description="Decision extractor daemon settings (LLM-powered extraction)",
    )
    pulse_snapshot: PulseSnapshotConfig = Field(
        default_factory=PulseSnapshotConfig,
        description="Pulse snapshot daemon settings",
    )
    pulse_report: PulseReportConfig = Field(
        default_factory=PulseReportConfig,
        description="Pulse report daemon settings (automated scheduled report generation)",
    )
    analysis_snapshot: AnalysisSnapshotConfig = Field(
        default_factory=AnalysisSnapshotConfig,
        description="Analysis snapshot daemon settings",
    )
    trend_snapshot: TrendSnapshotConfig = Field(
        default_factory=TrendSnapshotConfig,
        description="Trend snapshot daemon settings (Tier 3 trend metrics from Graphiti)",
    )
    project_coordinator: ProjectCoordinatorConfig = Field(
        default_factory=ProjectCoordinatorConfig,
        description="Project coordinator daemon settings (coordination intelligence)",
    )
    sync_guard: SyncGuardConfig = Field(
        default_factory=SyncGuardConfig,
        description="Sync guard daemon settings (proactive worktree parity checks)",
    )


class McpConfig(BaseModel):
    """MCP server configuration."""

    # Transport
    transport: Literal["stdio", "http", "proxy", "hybrid"] = Field(
        default="stdio",
        description="MCP transport mode: stdio (default), http, proxy (forward to remote), "
        "or hybrid (local threads + remote premium capabilities)",
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
    url: str = Field(
        default="",
        description="Remote MCP endpoint URL (proxy or hybrid remote endpoint)",
    )
    capability_routes: Dict[str, str] = Field(
        default_factory=dict,
        description="Per-capability route overrides for hybrid transport. "
        "Keys are capability ids, values are 'auto', 'local', 'remote', or 'disabled'.",
    )
    proxy_repo: str = Field(
        default="",
        description="Repository name for proxy headers (e.g. 'org/repo'). "
        "Falls back to local git discovery.",
    )
    proxy_branch: str = Field(
        default="",
        description="Branch name for proxy headers. Falls back to local git discovery.",
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
    daemons: DaemonsConfig = Field(
        default_factory=DaemonsConfig,
        description="Daemon management system settings",
    )

    # Agent-specific overrides (keyed by platform slug)
    agents: Dict[str, AgentConfig] = Field(
        default_factory=lambda: {
            "claude-code": AgentConfig(
                name="Claude Code", default_spec="implementer-code"
            ),
            "cursor": AgentConfig(name="Cursor", default_spec="implementer-code"),
            "codex": AgentConfig(name="Codex", default_spec="planner-architecture"),
            "gemini": AgentConfig(name="Gemini", default_spec="general-purpose"),
        },
        description="Agent-specific configuration overrides",
    )

    @field_validator("capability_routes")
    @classmethod
    def validate_capability_routes(cls, v: Dict[str, str]) -> Dict[str, str]:
        """Validate capability route overrides through the capability registry."""
        if not v:
            return v
        from watercooler_mcp.capabilities import validate_capability_routes

        validate_capability_routes(v)
        return v


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


# Exported so consumers can detect "no explicit TOML override" without magic numbers.
LLM_TIMEOUT_DEFAULT = 60.0


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
        default=LLM_TIMEOUT_DEFAULT,
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

    Env overrides: FALKORDB_HOST, FALKORDB_PORT, FALKORDB_USERNAME, FALKORDB_PASSWORD
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
    socket_timeout: int = Field(
        default=600,
        ge=10,
        le=3600,
        description=(
            "Per-operation FalkorDB socket timeout in seconds. "
            "Should be >= queue_task_timeout to avoid cutting off slow entity dedup queries."
        ),
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

    # Path to Graphiti installation (for development submodule setups)
    path: str = Field(
        default="",
        description="Path to Graphiti installation directory. Env override: WATERCOOLER_GRAPHITI_PATH",
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
        default="null",
        description="Default memory backend",
    )
    queue_enabled: bool = Field(
        default=False,
        description="Enable persistent memory task queue with retry and dead-letter semantics",
    )
    queue_max_workers: int = Field(
        default=1,
        ge=1,
        le=32,
        description=(
            "Concurrent worker threads for the memory task queue. "
            "Values > 1 enable parallel T2 Graphiti indexing (recommended: 3-5). "
            "Override with WATERCOOLER_MEMORY_QUEUE_MAX_WORKERS env var."
        ),
    )
    queue_task_timeout: float = Field(
        default=300.0,
        ge=10.0,
        le=3600.0,
        description=(
            "Base timeout (seconds) for a single memory queue task. "
            "Tasks that exceed this timeout are failed and the thread's cached "
            "backend is evicted so the next retry starts with a fresh connection. "
            "The effective timeout escalates on retry: base * 2^(attempt-1), "
            "capped at stale_timeout. Graphiti entity extraction typically takes "
            "30–300s; 300s base with escalation covers p99. "
            "Override with WATERCOOLER_MEMORY_QUEUE_TASK_TIMEOUT env var."
        ),
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
        default=1.0,
        ge=0.0,
        le=10.0,
        description="NW for primary namespace (max 10.0; 0.0 disables the namespace; values > ~1.43 produce ranking_score > 1.0)",
    )
    wide_weight: float = Field(
        default=0.55,
        ge=0.0,
        le=10.0,
        description="NW for wide-scope namespaces (max 10.0; 0.0 disables the namespace; values > ~1.43 produce ranking_score > 1.0)",
    )
    recency_floor: float = Field(default=0.7, ge=0.0, le=1.0)
    recency_half_life_days: float = Field(default=60.0, gt=0.0)


class FederationNamespaceConfig(BaseModel):
    """Configuration for a single federated namespace."""

    model_config = ConfigDict(frozen=True)

    code_path: str = Field(
        description="Absolute path to the namespace's code repo root"
    )
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
        default=0.4,
        gt=0.0,
        le=30.0,
        description="Per-namespace search timeout in seconds (max 30). Note: cancelling "
        "a timed-out asyncio.to_thread task stops the coroutine wrapper "
        "but the underlying search_graph thread runs to completion. "
        "Tune conservatively to avoid thread accumulation under load.",
    )
    max_namespaces: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum number of secondary namespaces to query "
        "(primary is always included and does not count toward this limit)",
    )
    max_total_timeout: float = Field(
        default=2.0,
        gt=0.0,
        le=60.0,
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

    # Product mode: controls open-core vs hosted feature boundary
    # "local" (default): stdio MCP, local git operations, T1 memory only
    # "hosted": HTTP MCP, token service auth, full T1/T2/T3 memory
    # Override with WATERCOOLER_MODE env var
    mode: Literal["local", "hosted"] = Field(
        default="local",
        description="Product mode: 'local' for open-core (T1 only) or 'hosted' for full control plane (T1/T2/T3)",
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
