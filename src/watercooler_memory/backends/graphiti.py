"""Graphiti backend adapter for episodic memory and temporal graph RAG."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Final, Sequence

logger = logging.getLogger(__name__)

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

from watercooler.memory_config import is_anthropic_url
from watercooler.path_resolver import derive_group_id

# Providers whose OpenAI-compatible APIs do NOT support json_schema
# structured outputs (response_format type). These need json_object fallback.
_NO_STRUCTURED_OUTPUTS_DOMAINS = (
    "deepseek.com",
    # Local OpenAI-compatible servers (llama.cpp, etc.) generally do not support
    # json_schema structured outputs; force json_object mode.
    "localhost",
    "127.0.0.1",
)
# DeepSeek max output tokens is 8192; graphiti_core defaults to 16384
_DEEPSEEK_MAX_TOKENS = 8192


def _needs_json_object_only(api_base: str | None) -> bool:
    """Check if provider requires json_object mode (no structured outputs)."""
    if not api_base:
        return False
    try:
        from urllib.parse import urlparse
        hostname = (urlparse(api_base).hostname or "").lower()
        return any(hostname.endswith(d) for d in _NO_STRUCTURED_OUTPUTS_DOMAINS)
    except Exception:
        return False

# Resolve package root from this file's location
# graphiti.py is at: src/watercooler_memory/backends/graphiti.py
# Package root is 4 levels up: watercooler-cloud/
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DEFAULT_GRAPHITI_PATH = _PACKAGE_ROOT / "external" / "graphiti"

# Track whether graphiti is available as installed package vs submodule
_GRAPHITI_INSTALLED_AS_PACKAGE: bool | None = None


def _is_graphiti_installed() -> bool:
    """Check if graphiti_core is installed as a Python package.

    Returns:
        True if graphiti_core can be imported directly (installed via pip/uv),
        False if it needs to be loaded from submodule path.
    """
    global _GRAPHITI_INSTALLED_AS_PACKAGE
    if _GRAPHITI_INSTALLED_AS_PACKAGE is not None:
        logger.debug(f"_is_graphiti_installed: cached result = {_GRAPHITI_INSTALLED_AS_PACKAGE}")
        return _GRAPHITI_INSTALLED_AS_PACKAGE

    try:
        logger.debug("_is_graphiti_installed: attempting import graphiti_core")
        import graphiti_core  # noqa: F401
        _GRAPHITI_INSTALLED_AS_PACKAGE = True
        logger.debug("_is_graphiti_installed: graphiti_core found as installed package")
        return True
    except ImportError:
        _GRAPHITI_INSTALLED_AS_PACKAGE = False
        logger.debug("_is_graphiti_installed: graphiti_core not installed as package, will use submodule")
        return False


def _get_graphiti_path() -> Path | None:
    """Get graphiti submodule path (only needed if not installed as package).

    This resolves the graphiti submodule path for development setups where
    graphiti is checked out as a git submodule rather than installed as a package.

    Environment Variables:
        WATERCOOLER_GRAPHITI_PATH: Override the default graphiti path

    Returns:
        Path to the graphiti submodule directory, or None if installed as package
    """
    # If installed as package, no path needed
    if _is_graphiti_installed():
        return None

    env_path = os.environ.get("WATERCOOLER_GRAPHITI_PATH")
    if env_path:
        return Path(env_path)
    return _DEFAULT_GRAPHITI_PATH


def _ensure_graphiti_available() -> None:
    """Ensure graphiti_core is importable, either as package or via submodule.

    For installed packages (uvx --from "watercooler-cloud[memory]"):
        graphiti_core is already in site-packages, nothing to do.

    For development (git submodule):
        Add the submodule path to sys.path so imports work.

    Raises:
        ConfigError: If graphiti cannot be made available.
    """
    import sys

    # Already installed as package? Nothing to do.
    if _is_graphiti_installed():
        logger.debug("_ensure_graphiti_available: graphiti_core already installed as package")
        return

    # Get submodule path
    graphiti_path = _get_graphiti_path()
    if graphiti_path is None:
        # Shouldn't happen, but handle gracefully
        raise ConfigError("graphiti_core not installed and no submodule path available")

    if not graphiti_path.exists():
        raise ConfigError(
            f"Graphiti not found at {graphiti_path}. "
            "Either install with: pip install 'watercooler-cloud[memory]' "
            "or run: git submodule update --init external/graphiti"
        )

    graphiti_core_dir = graphiti_path / "graphiti_core"
    if not graphiti_core_dir.exists():
        raise ConfigError(
            f"Graphiti core not found at {graphiti_core_dir}. "
            "Ensure Graphiti submodule is properly initialized."
        )

    # Add to sys.path if not already there
    graphiti_path_str = str(graphiti_path)
    if graphiti_path_str not in sys.path:
        sys.path.insert(0, graphiti_path_str)
        logger.debug(f"Added graphiti submodule to sys.path: {graphiti_path_str}")

    # Verify import works now
    try:
        logger.debug("_ensure_graphiti_available: importing graphiti_core from submodule")
        import graphiti_core  # noqa: F401
        logger.debug("_ensure_graphiti_available: graphiti_core import successful")
    except ImportError as e:
        raise ConfigError(
            f"Failed to import graphiti_core after adding to path: {e}"
        ) from e


def _derive_database_name(code_path: Path | str | None) -> str:
    """Derive database name from project directory.

    Uses unified derive_group_id() from path_resolver for consistent
    sanitization across all backends.

    Converts project directory name to a valid FalkorDB database name:
    - Replaces hyphens with underscores (FalkorDB doesn't like hyphens)
    - Converts to lowercase
    - Falls back to 'watercooler' if no code_path provided

    Note: Does NOT remove dots or other special chars to preserve
    compatibility with existing migrated FalkorDB data.

    Args:
        code_path: Path to the project directory

    Returns:
        Sanitized database name (e.g., 'watercooler_cloud')
    """
    return derive_group_id(code_path=Path(code_path) if code_path else None)


def _best_extra_match(missing_field: str, extras: set[str]) -> str | None:
    """Pick the extra field most likely to correspond to *missing_field*.

    When DeepSeek (and similar providers) use ``json_object`` mode instead of
    ``json_schema``, the LLM is free to choose its own field names.  Commonly
    it prefixes every field with the parent concept (``entity_name`` instead of
    ``name``, ``entity_nodes`` instead of ``extracted_entities``).

    The heuristic ranks candidates in decreasing confidence:

    1. **Suffix match** – ``entity_name`` ends with ``_name`` → strong signal
       that the semantic meaning is "name".
    2. **Substring match** – the target name appears *somewhere* inside the
       candidate (weaker, but still useful for ``xnamex``-style variants).
    3. **Alphabetical fallback** – deterministic tie-breaker when no textual
       similarity exists.  This keeps behaviour stable across runs but is
       essentially a guess; in practice the first two tiers catch real cases.
    """
    suffix: list[str] = []
    contains: list[str] = []
    for e in sorted(extras):
        if e.endswith(f"_{missing_field}") or e == missing_field:
            suffix.append(e)
        elif missing_field in e:
            contains.append(e)
    if suffix:
        return suffix[0]
    if contains:
        return contains[0]
    return sorted(extras)[0] if extras else None


# Maximum recursion depth for _normalize_json_response.
# Graphiti responses are 2-3 levels deep; this is a defensive ceiling.
MAX_NORMALIZE_DEPTH = 10

# Sentinel for detecting truly-unset Pydantic fields (vs fields with a default).
try:
    from pydantic_core import PydanticUndefined as _PYDANTIC_UNDEFINED
except Exception:  # pragma: no cover
    _PYDANTIC_UNDEFINED = object()  # type: ignore[assignment]


def _default_for_annotation(annotation: Any) -> Any:
    """Return a safe empty default for a Pydantic field annotation."""
    origin = getattr(annotation, "__origin__", None)
    if origin is list:
        return []
    if origin is dict:
        return {}
    if annotation in (list, tuple, set):
        return []
    if annotation is dict:
        return {}
    if annotation is str:
        return ""
    if annotation is bool:
        return False
    if annotation in (int, float):
        return 0
    return []


def _normalize_json_response(
    data: dict[str, Any], response_model: type,
    *, _depth: int = 0,
) -> dict[str, Any]:
    """Remap response field names to match a Pydantic model schema.

    Without ``json_schema`` enforcement, LLMs may use synonymous field
    names (e.g. ``entity_nodes`` instead of ``extracted_entities``,
    ``entity_name`` instead of ``name`` inside nested objects).
    This detects required fields missing from *data* and remaps extra
    keys that aren't in the model schema, recursing into nested
    Pydantic models within list fields.

    The recursion is depth-limited to :data:`MAX_NORMALIZE_DEPTH` as a
    defensive guard against pathological payloads; real Graphiti
    responses never exceed 3 levels.
    """
    if _depth >= MAX_NORMALIZE_DEPTH:
        logger.warning(
            "json_object normalize: hit depth limit (%d) for %s, returning as-is",
            MAX_NORMALIZE_DEPTH, getattr(response_model, "__name__", response_model),
        )
        return data
    from pydantic import BaseModel

    if not (isinstance(response_model, type) and issubclass(response_model, BaseModel)):
        return data

    # --- top-level field remap ---
    required = {
        name for name, info in response_model.model_fields.items()
        if info.is_required()
    }
    present = set(data.keys())
    missing = required - present
    extra = present - set(response_model.model_fields.keys())

    remapped = dict(data)

    if missing and extra:
        extra_remaining = set(extra)
        for m_field in sorted(missing):
            if not extra_remaining:
                break
            e_field = _best_extra_match(m_field, extra_remaining)
            if e_field is None:
                break
            extra_remaining.discard(e_field)
            logger.info(
                "json_object field remap: '%s' -> '%s' for %s",
                e_field, m_field, response_model.__name__,
            )
            remapped[m_field] = remapped.pop(e_field)

    # --- fill required fields with safe defaults ---
    # Some OpenAI-compatible servers return `{}` or omit required keys even when
    # the prompt asks for structured JSON. Prefer an empty-but-valid payload
    # over hard failure so the episode can still be ingested.
    for field_name, field_info in response_model.model_fields.items():
        if field_name in remapped:
            continue
        if not field_info.is_required():
            continue
        if getattr(field_info, "default", _PYDANTIC_UNDEFINED) is not _PYDANTIC_UNDEFINED:
            remapped[field_name] = field_info.default
            continue
        default_factory = getattr(field_info, "default_factory", None)
        if default_factory is not None:
            try:
                remapped[field_name] = default_factory()
                continue
            except Exception:
                pass
        remapped[field_name] = _default_for_annotation(field_info.annotation)

    # --- recurse into nested Pydantic models ---
    for field_name, field_info in response_model.model_fields.items():
        value = remapped.get(field_name)
        if value is None:
            continue
        # --- scalar coercions for weak local models ---
        # Graphiti schemas often use integer indices (e.g. edge endpoints) but
        # smaller models sometimes emit ULID-like strings. Coerce to 0 to avoid
        # Pydantic hard failures; downstream graphiti_core validation will
        # drop invalid indices.
        if field_info.annotation is int:
            v = value
            if isinstance(v, list):
                v = v[0] if v else 0
            if isinstance(v, bool):  # must check before int (bool is subclass)
                remapped[field_name] = int(v)
            elif isinstance(v, int):
                remapped[field_name] = v
            elif isinstance(v, str):
                try:
                    remapped[field_name] = int(v)
                except Exception:
                    remapped[field_name] = 0
            else:
                remapped[field_name] = 0
            value = remapped[field_name]
        elif field_info.annotation is str:
            v = value
            if isinstance(v, list):
                v = v[0] if v else ""
            remapped[field_name] = v if isinstance(v, str) else ""
            value = remapped[field_name]
        inner_model = _get_list_item_model(field_info.annotation)
        if inner_model is not None and not isinstance(value, (list, dict)):
            # Some local models return a scalar where a list-of-objects is expected.
            # Prefer dropping the field to an empty list rather than failing validation.
            logger.warning(
                "json_object list coercion: expected list[%s] for '%s' in %s, got %s; coercing to []",
                inner_model.__name__, field_name, response_model.__name__, type(value).__name__,
            )
            remapped[field_name] = []
            continue
        if inner_model is not None and isinstance(value, list):
            remapped[field_name] = [
                _normalize_json_response(item, inner_model, _depth=_depth + 1)
                if isinstance(item, dict) else item
                for item in value
            ]
        elif inner_model is not None and isinstance(value, dict):
            # DeepSeek sometimes returns a single dict where list is expected.
            # Validate the dict looks plausible before wrapping: it must have
            # at least one key that either matches a model field directly OR
            # could be remapped to one (e.g. entity_name → name).
            model_fields = set(inner_model.model_fields.keys())
            value_keys = set(value.keys())
            has_direct = bool(value_keys & model_fields)
            # Only count as remappable if there's a suffix or substring
            # match — the alphabetical fallback in _best_extra_match is
            # too loose for deciding whether to coerce dict→list.
            has_remappable = False
            if not has_direct:
                for mf in model_fields:
                    if mf in value_keys:
                        continue
                    for vk in value_keys:
                        if vk.endswith(f"_{mf}") or vk == mf or mf in vk:
                            has_remappable = True
                            break
                    if has_remappable:
                        break
            if not (has_direct or has_remappable) or not value:
                logger.warning(
                    "json_object list coercion: dict has no keys matching %s "
                    "fields (%s), skipping coercion for '%s' in %s",
                    inner_model.__name__, model_fields, field_name,
                    response_model.__name__,
                )
                remapped[field_name] = []
            else:
                logger.info(
                    "json_object list coercion: wrapping single dict in list "
                    "for field '%s' in %s",
                    field_name, response_model.__name__,
                )
                remapped[field_name] = [
                    _normalize_json_response(value, inner_model, _depth=_depth + 1),
                ]
        elif (
            isinstance(value, dict)
            and isinstance(field_info.annotation, type)
            and issubclass(field_info.annotation, BaseModel)
        ):
            remapped[field_name] = _normalize_json_response(
                value, field_info.annotation, _depth=_depth + 1,
            )

    return remapped


def _get_list_item_model(annotation: Any) -> type | None:
    """Extract the Pydantic model from a ``list[Model]`` annotation."""
    from pydantic import BaseModel

    origin = getattr(annotation, "__origin__", None)
    if origin is not list:
        return None
    args = getattr(annotation, "__args__", ())
    if args and isinstance(args[0], type) and issubclass(args[0], BaseModel):
        return args[0]
    return None


class _JsonObjectOnlyClient:
    """OpenAI-compatible LLM client that forces json_object response format.

    Some providers (DeepSeek, etc.) reject OpenAI's json_schema structured
    outputs with HTTP 400.  This thin wrapper delegates to
    ``OpenAIGenericClient`` but clears ``response_model`` so the parent
    always uses ``{"type": "json_object"}`` instead of ``json_schema``.

    Graphiti prompts already include JSON format instructions, so the
    model output is correct even without strict schema enforcement.
    The response is then normalised via :func:`_normalize_json_response`
    to remap any variant field names back to the expected Pydantic schema.
    """

    def __new__(cls, **kwargs: Any) -> Any:  # type: ignore[override]
        from graphiti_core.llm_client.openai_generic_client import (
            OpenAIGenericClient,
        )

        class _Inner(OpenAIGenericClient):
            async def _generate_response(
                self, messages: Any, response_model: Any = None, **kw: Any,
            ) -> dict[str, Any]:
                # Force json_object by dropping response_model
                raw = await super()._generate_response(
                    messages, response_model=None, **kw,
                )
                # Without json_schema enforcement, LLMs may use variant
                # field names.  Remap to match the expected Pydantic model.
                if response_model is not None:
                    raw = _normalize_json_response(raw, response_model)
                return raw

        return _Inner(**kwargs)


@functools.lru_cache(maxsize=1)
def _build_noop_cross_encoder() -> Any:
    """Build a type-compatible no-op cross encoder (cached singleton).

    Graphiti initializes a default OpenAI reranker when ``cross_encoder`` is
    omitted. That implicit default requires ``OPENAI_API_KEY`` even for
    non-cross-encoder reranker modes, which breaks provider-agnostic setups.

    Graphiti validates ``cross_encoder`` as a ``CrossEncoderClient`` instance,
    so this helper creates a small subclass at runtime to satisfy that contract.
    The subclass is defined inside the function (rather than at module level)
    because ``CrossEncoderClient`` is a lazy import; the cache freezes it to
    whichever import was live at first call, which is correct for a long-running
    process where ``graphiti_core`` is never reloaded.
    """
    from graphiti_core.cross_encoder.client import CrossEncoderClient

    class _NoopCrossEncoderClient(CrossEncoderClient):
        async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
            del query  # Unused in the no-op path
            return [(passage, 0.0) for passage in passages]

    return _NoopCrossEncoderClient()


@dataclass
class GraphitiConfig:
    """Configuration for Graphiti backend."""

    # Graphiti submodule location (None if installed as package, Path if using submodule)
    # For installed packages: graphiti_core is in site-packages, no path needed
    # For development: points to external/graphiti submodule
    graphiti_path: Path | None = field(default_factory=_get_graphiti_path)

    # Database configuration (FalkorDB)
    falkordb_host: str = "localhost"
    falkordb_port: int = 6379
    falkordb_username: str | None = None  # FalkorDB doesn't require auth
    falkordb_password: str | None = None

    # Unified database name (one database per project)
    # All threads share this database, partitioned by group_id
    database: str | None = None  # Derived from code_path if not set

    # LLM configuration (flexible provider - supports OpenAI, local, DeepSeek, etc.)
    llm_api_base: str | None = None      # e.g., "http://localhost:8000/v1" for local
    llm_api_key: str | None = None       # Required for all providers
    llm_model: str = "gpt-4o-mini"       # Default model

    # Embedding configuration (flexible provider - supports OpenAI, local llama.cpp, etc.)
    embedding_api_base: str | None = None  # e.g., "http://localhost:8080/v1" for local
    embedding_api_key: str | None = None   # Required for all providers
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1024  # Vector dimension (must match your embedding model)

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
        """Set default index path if not provided and set default values."""
        if self.entry_episode_index_path is None and self.track_entry_episodes:
            self.entry_episode_index_path = (
                Path.home() / ".watercooler" / "graphiti" / "entry_episode_index.json"
            )

    @classmethod
    def from_unified(cls) -> "GraphitiConfig":
        """Create GraphitiConfig from unified watercooler configuration.

        Uses the unified config system with proper priority chain:
        1. Environment variables (highest)
        2. Backend-specific TOML overrides ([memory.graphiti])
        3. Shared TOML settings ([memory.llm], [memory.embedding], [memory.database])
        4. Built-in defaults (lowest)

        Returns:
            GraphitiConfig instance with all settings resolved

        Example:
            >>> config = GraphitiConfig.from_unified()
            >>> backend = GraphitiBackend(config)
        """
        from watercooler.memory_config import (
            resolve_llm_config,
            resolve_embedding_config,
            resolve_database_config,
            get_graphiti_reranker,
            get_graphiti_track_entry_episodes,
        )

        llm = resolve_llm_config("graphiti")
        embedding = resolve_embedding_config("graphiti")
        db = resolve_database_config()

        return cls(
            llm_api_key=llm.api_key,
            # Pass through any resolved URL; converts empty string to None so
            # the graphiti_core client applies its own default (api.openai.com).
            llm_api_base=llm.api_base or None,
            llm_model=llm.model,
            embedding_api_key=embedding.api_key,
            embedding_api_base=embedding.api_base or None,
            embedding_model=embedding.model,
            embedding_dim=embedding.dim,
            falkordb_host=db.host,
            falkordb_port=db.port,
            falkordb_username=db.username if db.username else None,
            falkordb_password=db.password if db.password else None,
            reranker=get_graphiti_reranker(),
            track_entry_episodes=get_graphiti_track_entry_episodes(),
        )


def _filter_by_time_range(
    results: list[dict[str, Any]],
    start_time: str,
    end_time: str,
    time_key: str = "created_at",
) -> list[dict[str, Any]]:
    """Post-filter results by timestamp range.

    Compares each result's ``time_key`` field against the given ISO 8601
    bounds.  Gracefully handles empty filter strings (no-op), missing or
    unparseable timestamp values on individual results (excluded when
    filters are active), and timezone-naive datetimes (treated as UTC).

    Args:
        results: List of result dicts from Graphiti search.
        start_time: ISO 8601 lower bound (inclusive). Empty string = no lower bound.
        end_time: ISO 8601 upper bound (inclusive). Empty string = no upper bound.
        time_key: Dict key to read the timestamp from (default ``"created_at"``).

    Returns:
        Filtered list (may be shorter than input, never longer).
    """
    if not start_time and not end_time:
        return results

    def _parse_dt(value: str) -> datetime | None:
        try:
            # Normalize trailing Z → +00:00 (Python 3.10 fromisoformat doesn't accept Z)
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            dt = datetime.fromisoformat(value)
            # Ensure timezone-aware (assume UTC for naive datetimes)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return None

    lower = _parse_dt(start_time) if start_time else None
    upper = _parse_dt(end_time) if end_time else None

    # If both bounds failed to parse, skip filtering entirely
    if lower is None and upper is None:
        return results

    filtered: list[dict[str, Any]] = []
    for r in results:
        raw = r.get(time_key)
        if raw is None:
            continue  # Exclude results with missing timestamp when filters are active
        ts = _parse_dt(raw.isoformat() if isinstance(raw, datetime) else str(raw))
        if ts is None:
            continue  # Unparseable timestamp — exclude
        if lower and ts < lower:
            continue
        if upper and ts > upper:
            continue
        filtered.append(r)

    return filtered


def _filter_active_only(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter results to only include currently-valid (non-superseded) facts.

    Removes entries where ``invalid_at`` is set to a non-None value, keeping
    only facts that have not been invalidated by a later contradicting episode.

    Args:
        results: List of result dicts, each optionally containing ``invalid_at``.

    Returns:
        Filtered list containing only entries where ``invalid_at`` is None.
    """
    # Graphiti serializes invalid_at as edge.invalid_at.isoformat() or None —
    # never an empty string — so `is None` is the correct strict check here.
    return [r for r in results if r.get("invalid_at") is None]


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
    MIN_SEARCH_RESULTS = 1   # Minimum valid max_results parameter
    MAX_SEARCH_RESULTS = 50  # Maximum valid max_results parameter
    # Over-fetch multiplier for post-filter queries (active_only, time-range).
    # Assumes at most ~66% of returned edges are filtered out; callers may receive
    # fewer than `limit` results if the actual supersession rate exceeds this.
    OVERFETCH_MULTIPLIER = 3
    
    # Community limits (top-5 to prevent payload bloat per Codex feedback)
    MAX_COMMUNITIES_RETURNED = 5

    def __init__(self, config: GraphitiConfig | None = None) -> None:
        logger.debug("GraphitiBackend.__init__ starting")
        self.config = config or GraphitiConfig()
        logger.debug("GraphitiBackend.__init__ calling _validate_config")
        self._validate_config()
        logger.debug("GraphitiBackend.__init__ calling _init_entry_episode_index")
        self._init_entry_episode_index()
        # Cache for graphiti client to avoid creating new connections per call
        # This is critical for MCP migration which makes many sequential calls
        # Client lifecycle: created on first use, reused until close() or __del__
        self._cached_graphiti_client: Any = None
        self._indices_built: bool = False
        logger.debug("GraphitiBackend.__init__ complete")

    def close(self) -> None:
        """Close the backend and release resources.

        Closes the cached FalkorDB connection if present. Safe to call multiple
        times. After close(), the backend can still be used - a new connection
        will be created on next operation.

        Example:
            backend = GraphitiBackend(config)
            try:
                # ... use backend ...
            finally:
                backend.close()

            # Or use as context manager:
            with GraphitiBackend(config) as backend:
                # ... use backend ...
        """
        if self._cached_graphiti_client is not None:
            try:
                # FalkorDB client has a close() method on the driver
                driver = getattr(self._cached_graphiti_client, "driver", None)
                if driver is not None and hasattr(driver, "close"):
                    driver.close()
            except Exception:
                pass  # Ignore cleanup errors
            finally:
                self._cached_graphiti_client = None
                self._indices_built = False

    def __enter__(self) -> "GraphitiBackend":
        """Enter context manager - returns self."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit context manager - closes connection."""
        self.close()

    def __del__(self) -> None:
        """Destructor - attempts to close connection on garbage collection.

        Note: __del__ is not guaranteed to run in all cases (e.g., circular
        references, interpreter shutdown). For reliable cleanup, use close()
        explicitly or use the backend as a context manager.
        """
        try:
            self.close()
        except Exception:
            pass  # Ignore errors during garbage collection

    def _validate_config(self) -> None:
        """Validate configuration and Graphiti availability."""
        # Ensure graphiti_core is importable (installed package or submodule)
        logger.debug("_validate_config: calling _ensure_graphiti_available")
        _ensure_graphiti_available()
        logger.debug("_validate_config: graphiti_core available")

        # Validate LLM API key is set (required for Graphiti)
        if not self.config.llm_api_key:
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

    def _init_entry_episode_index(self) -> None:
        """Initialize the entry-episode index if enabled."""
        if not self.config.track_entry_episodes:
            logger.debug("_init_entry_episode_index: tracking disabled, skipping")
            self.entry_episode_index = None
            return

        logger.debug(f"_init_entry_episode_index: loading from {self.config.entry_episode_index_path}")
        index_config = IndexConfig(
            backend="graphiti",
            index_path=self.config.entry_episode_index_path,
        )
        self.entry_episode_index = EntryEpisodeIndex(index_config, auto_load=True)
        logger.debug(f"_init_entry_episode_index: loaded {len(self.entry_episode_index)} entries")

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
        previous_episode_uuids: list[str] | None = None,
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
            previous_episode_uuids: Optional list of episode UUIDs that this episode
                follows. Used to establish explicit temporal ordering when multiple
                episodes share the same reference_time (e.g., chunks of the same entry).
                When None, Graphiti uses default context retrieval.

        Returns:
            Dict with episode_uuid, entities_extracted, and facts_extracted

        Raises:
            BackendError: If Graphiti client creation or episode addition fails
            TransientError: If database connection fails
        """
        import time
        logger.debug(f"add_episode_direct called, group_id={group_id}")
        logger.debug(f"add_episode_direct LLM config: api_base={self.config.llm_api_base}, model={self.config.llm_model}, key_set={bool(self.config.llm_api_key)}")
        try:
            # Use cached client with indices already built to avoid per-call overhead
            graphiti = await self._get_graphiti_client_with_indices()
        except (ConnectionError, TimeoutError, OSError) as e:
            raise TransientError(f"Database connection failed: {e}") from e
        except ConfigError:
            raise  # Re-raise config errors as-is
        except Exception as e:
            # Unexpected error during client creation
            raise BackendError(f"Failed to create Graphiti client: {e}") from e

        try:
            # Sanitize episode_body to prevent RediSearch operator errors during
            # entity deduplication (same as _map_entries_to_episodes does for index())
            sanitized_body = self._sanitize_redisearch_operators(episode_body)

            logger.debug(f"add_episode_direct calling graphiti.add_episode (LLM entity extraction starts)")
            start_time = time.time()
            result = await graphiti.add_episode(
                name=name,
                episode_body=sanitized_body,
                source_description=source_description,
                reference_time=reference_time,
                group_id=group_id,
                previous_episode_uuids=previous_episode_uuids,
            )
            elapsed = time.time() - start_time
            logger.debug(f"add_episode_direct graphiti.add_episode completed in {elapsed:.2f}s")

            # Extract episode UUID from result - fail if missing
            # AddEpisodeResults has an 'episode' field containing the EpisodicNode
            episode = getattr(result, "episode", None)
            episode_uuid = getattr(episode, "uuid", None) if episode else None
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

            logger.debug(f"add_episode_direct extracted {len(entities)} entities, {facts_count} facts")
            if entities:
                logger.debug(f"add_episode_direct entities: {entities[:5]}{'...' if len(entities) > 5 else ''}")

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

    def _ensure_embedding_service_available(self) -> None:
        """Ensure embedding service is available, auto-starting if configured.

        Checks if the embedding service is reachable and attempts to auto-start
        it if mcp.graph.auto_start_services is enabled in config.

        This uses the same auto-start logic as the baseline graph module,
        ensuring consistent behavior across both systems.
        """
        logger.debug(f"Checking embedding service availability at {self.config.embedding_api_base}")
        try:
            from watercooler.baseline_graph.sync import (
                is_embedding_available,
                EmbeddingConfig,
                _should_auto_start_services,
                _try_auto_start_service,
            )

            # Create config matching our embedder settings
            embed_config = EmbeddingConfig(
                api_base=self.config.embedding_api_base,
                model=self.config.embedding_model,
            )

            # Check if already available
            if is_embedding_available(embed_config):
                logger.debug("Embedding service is available")
                return

            # Try auto-start if enabled
            if _should_auto_start_services():
                logger.debug(
                    f"Embedding service not available at {self.config.embedding_api_base}, "
                    "attempting auto-start..."
                )
                if _try_auto_start_service("embedding", self.config.embedding_api_base):
                    # Verify it's now available
                    if is_embedding_available(embed_config):
                        logger.debug("Embedding service auto-started successfully")
                        return
                    else:
                        logger.warning("Embedding service started but not responding")
                else:
                    logger.warning("Failed to auto-start embedding service")
            else:
                logger.debug(
                    f"Embedding service not available at {self.config.embedding_api_base} "
                    "and auto_start_services is disabled"
                )

        except ImportError as e:
            logger.debug(f"Could not import auto-start utilities: {e}")
        except Exception as e:
            logger.debug(f"Error checking embedding service: {e}")

    def _create_graphiti_client(self, use_cache: bool = False) -> Any:
        """Create and configure Graphiti client with FalkorDB, LLM, and embedder.

        Args:
            use_cache: If True, return cached client if available. Default is False
                because caching is incompatible with asyncio.run() creating new
                event loops - the cached client's asyncio.Lock objects become bound
                to a stale event loop, causing "Lock is bound to a different event
                loop" errors on subsequent calls.

        Returns:
            Configured Graphiti instance ready for operations.

        Raises:
            ConfigError: If required dependencies are not installed.
        """
        # Return cached client if available and caching enabled
        # WARNING: Caching is disabled by default because each method uses
        # asyncio.run() which creates a new event loop. The graphiti client
        # has internal asyncio.Lock objects that become bound to the event loop
        # from the first call, causing errors on subsequent calls.
        if use_cache and self._cached_graphiti_client is not None:
            logger.debug("Returning cached Graphiti client")
            return self._cached_graphiti_client

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

        # Create FalkorDB driver with unified database name
        # All threads share one database, partitioned by group_id property
        database_name = self.config.database or "watercooler"

        # Monkey patch to prevent FalkorDriver.__init__ from scheduling a background
        # task via loop.create_task(). This background task causes race conditions
        # with MCP stdio transport. We explicitly await build_indices_and_constraints()
        # in operations that need it (add_episode_direct, find_episode_by_chunk_id_async).
        original_get_running_loop = asyncio.get_running_loop
        asyncio.get_running_loop = lambda: (_ for _ in ()).throw(
            RuntimeError("patched to prevent background task")
        )
        try:
            falkor_driver = FalkorDriver(
                host=self.config.falkordb_host,
                port=self.config.falkordb_port,
                username=self.config.falkordb_username,
                password=self.config.falkordb_password,
                database=database_name,
            )
        finally:
            asyncio.get_running_loop = original_get_running_loop

        # Configure LLM client (supports OpenAI, Anthropic, local servers, DeepSeek, etc.)
        llm_api_base = self.config.llm_api_base or ""
        is_anthropic = is_anthropic_url(llm_api_base)

        # Hoisted above the is_anthropic branch so the cross_encoder setup
        # below can reference it regardless of which LLM client path is taken.
        llm_config = LLMConfig(
            api_key=self.config.llm_api_key,
            model=self.config.llm_model,
            base_url=self.config.llm_api_base,
        )

        if is_anthropic:
            # Use native Anthropic client for Anthropic API
            from graphiti_core.llm_client.anthropic_client import AnthropicClient
            llm_client = AnthropicClient(
                api_key=self.config.llm_api_key,
                model=self.config.llm_model,
            )
        else:
            # Use OpenAI-compatible client for OpenAI, DeepSeek, Groq, local servers
            if _needs_json_object_only(llm_api_base):
                # DeepSeek (and similar) don't support json_schema structured
                # outputs. Use a thin wrapper that forces json_object mode.
                # Also cap max_tokens to 8192 (DeepSeek's output limit).
                llm_client = _JsonObjectOnlyClient(
                    config=llm_config, max_tokens=_DEEPSEEK_MAX_TOKENS,
                )
                logger.warning(
                    "Using json_object-only LLM client for %s "
                    "(provider does not support json_schema structured outputs)",
                    llm_api_base,
                )
            else:
                llm_client = OpenAIGenericClient(config=llm_config)

        # Always provide an explicit cross_encoder to avoid Graphiti's default
        # OpenAIRerankerClient() initialization (which assumes OPENAI_API_KEY).
        reranker = self.config.reranker.lower()
        if reranker == "cross_encoder":
            if is_anthropic:
                raise ConfigError(
                    "Graphiti reranker 'cross_encoder' requires an OpenAI-compatible "
                    "LLM endpoint. Anthropic URLs are not supported for reranking."
                )
            from graphiti_core.cross_encoder.openai_reranker_client import (
                OpenAIRerankerClient,
            )
            # Both OpenAIGenericClient and _JsonObjectOnlyClient (DeepSeek)
            # expose a .client attribute (AsyncOpenAI).  The getattr fallback
            # to None is a safety net; OpenAIRerankerClient handles None by
            # constructing its own AsyncOpenAI from config, so either way is safe.
            cross_encoder = OpenAIRerankerClient(
                config=llm_config,
                client=getattr(llm_client, "client", None),
            )
        else:
            cross_encoder = _build_noop_cross_encoder()

        # Configure embedder (supports OpenAI, local llama.cpp, etc.)
        # Check if embedding service needs auto-start
        self._ensure_embedding_service_available()

        embedder_config = OpenAIEmbedderConfig(
            embedding_model=self.config.embedding_model,
            api_key=self.config.embedding_api_key,
            base_url=self.config.embedding_api_base,
            embedding_dim=self.config.embedding_dim,
        )
        embedder = OpenAIEmbedder(config=embedder_config)

        client = Graphiti(
            graph_driver=falkor_driver,
            llm_client=llm_client,
            embedder=embedder,
            cross_encoder=cross_encoder,
        )

        # Cache the client for reuse
        if use_cache:
            self._cached_graphiti_client = client
            self._indices_built = False  # Reset indices flag for new client

        return client

    async def _get_graphiti_client_with_indices(self) -> Any:
        """Get cached graphiti client and ensure indices are built.

        This is the preferred method for getting a graphiti client in async
        contexts. It reuses the cached client and only builds indices once.

        Returns:
            Graphiti client with indices built.

        Raises:
            TransientError: If database connection fails.
            ConfigError: If dependencies are not installed.
        """
        graphiti = self._create_graphiti_client(use_cache=True)

        # Build indices once per client instance
        if not self._indices_built:
            logger.debug("Building Graphiti indices and constraints...")
            await graphiti.build_indices_and_constraints()
            self._indices_built = True
            logger.debug("Graphiti indices built successfully")

        return graphiti

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
                        # AddEpisodeResults has 'episode' field containing EpisodicNode with uuid
                        result_episode = getattr(result, "episode", None)
                        result_uuid = getattr(result_episode, "uuid", None) if result_episode else None
                        if entry_id and result_uuid:
                            indexed_mappings.append((entry_id, result_uuid, thread_id))

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
                    # With unified database, group_ids filter within single DB (no separate databases)
                    topic = query_item.get("topic")
                    if topic:
                        # Sanitize topic to group_id format
                        group_ids = [self._sanitize_thread_id(topic)]
                    else:
                        # No topic specified - search across all threads in unified database
                        # Pass None to search all group_ids within the single project database
                        group_ids = None

                    try:
                        # Get search config with configured reranker
                        search_config = self._get_search_config()

                        # Unified database: no driver cloning needed
                        # group_ids filters by property within the single project database
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

        Note: For single group_id queries, the driver is cloned to point at
        the specific database. This is required because Graphiti's
        @handle_multiple_group_ids decorator only activates for >1 group_ids.
        This pattern matches the query() method implementation.

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
            raise ConfigError("query cannot be empty")

        # Validate max_results to prevent resource exhaustion
        if max_results < self.MIN_SEARCH_RESULTS or max_results > self.MAX_SEARCH_RESULTS:
            raise ConfigError(
                f"max_results must be between {self.MIN_SEARCH_RESULTS} and {self.MAX_SEARCH_RESULTS}, got {max_results}"
            )

        try:
            graphiti = self._create_graphiti_client()
        except Exception as e:
            raise TransientError(f"Database connection failed: {e}") from e

        async def search_nodes_async():
            # Sanitize group_ids for filtering within unified database
            sanitized_group_ids = None
            if group_ids:
                sanitized_group_ids = [self._sanitize_thread_id(gid) for gid in group_ids]

            # Use NODE_HYBRID_SEARCH_RRF (official Graphiti MCP server approach)
            from graphiti_core.search.search_config_recipes import NODE_HYBRID_SEARCH_RRF
            from graphiti_core.search.search_filters import SearchFilters

            # Always create SearchFilters (match official implementation)
            search_filters = SearchFilters(node_labels=entity_types)

            # Unified database: no driver cloning needed
            # group_ids filters by property within the single project database
            search_results = await graphiti.search_(
                query=query,
                config=NODE_HYBRID_SEARCH_RRF,
                group_ids=sanitized_group_ids,
                search_filter=search_filters,
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

            # Unified database: no driver cloning needed
            # Node lookup by UUID works across all group_ids in the single project database
            node = await EntityNode.get_by_uuid(graphiti.driver, node_id)
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
        start_time: str = "",
        end_time: str = "",
        active_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Search for facts (edges) using semantic search.

        Protocol-compliant wrapper for search_memory_facts().

        Args:
            query: Search query string
            group_ids: Optional list of group IDs to filter by
            max_results: Maximum facts to return (default: 10, max: 50)
            center_node_id: Optional node UUID to center search around
            start_time: ISO 8601 lower bound for created_at (inclusive). Empty = no bound.
            end_time: ISO 8601 upper bound for created_at (inclusive). Empty = no bound.
            active_only: If True, exclude superseded facts (those with invalid_at set).

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
            start_time=start_time,
            end_time=end_time,
            active_only=active_only,
        )
        
        # Add CoreResult-compliant fields to each result
        for result in results:
            result.setdefault("id", result.get("uuid"))  # Required by CoreResult
            result.setdefault("backend", "graphiti")  # Required by CoreResult
            result.setdefault("content", result.get("fact"))  # Map fact text to content
            result.setdefault("name", result.get("fact", "")[:100] if result.get("fact") else None)
            result.setdefault("source", None)  # Source tracking not applicable to edges
            result.setdefault("metadata", {})  # Additional metadata
            result.setdefault("extra", {})  # Backend-specific fields
        
        return results

    def search_episodes(
        self,
        query: str,
        group_ids: Sequence[str] | None = None,
        max_results: int = DEFAULT_MAX_EPISODES,
        start_time: str = "",
        end_time: str = "",
    ) -> list[dict[str, Any]]:
        """Search for episodes (provenance-bearing content) using semantic search.

        Protocol-compliant wrapper for get_episodes().

        Args:
            query: Search query string
            group_ids: Optional list of group IDs to filter by
            max_results: Maximum episodes to return (default: 10, max: 50)
            start_time: ISO 8601 lower bound for created_at (inclusive). Empty = no bound.
            end_time: ISO 8601 upper bound for created_at (inclusive). Empty = no bound.

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
            start_time=start_time,
            end_time=end_time,
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

            # Unified database: no driver cloning needed
            # Edge lookup by UUID works across all group_ids in the single project database
            edge = await EntityEdge.get_by_uuid(graphiti.driver, uuid)
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


    def get_edges_by_group_ids(
        self,
        group_ids: list[str],
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Enumerate entity edges for the given group IDs.

        Args:
            group_ids: Raw group IDs (will be sanitized internally).
            limit: Maximum edges to return.

        Returns:
            List of edge dicts with uuid, fact, valid_at, invalid_at, etc.

        Raises:
            BackendError: If edge enumeration fails.
        """
        try:
            from graphiti_core.edges import EntityEdge  # type: ignore[import-not-found]
        except ImportError as e:
            raise BackendError(f"Graphiti edge import failed: {e}") from e

        sanitized = [self._sanitize_thread_id(gid) for gid in group_ids]
        graphiti = self._create_graphiti_client()

        async def _fetch() -> list[Any]:
            return await EntityEdge.get_by_group_ids(
                graphiti.driver, sanitized, limit=limit
            )

        try:
            edges = asyncio.run(_fetch())
        except Exception as e:
            raise BackendError(f"Graphiti edge enumeration failed: {e}") from e

        results: list[dict[str, Any]] = []
        for e in edges:
            results.append({
                "uuid": getattr(e, "uuid", ""),
                "fact": getattr(e, "fact", ""),
                "content": getattr(e, "fact", ""),
                "valid_at": getattr(e, "valid_at", None).isoformat()
                    if getattr(e, "valid_at", None) else None,
                "invalid_at": getattr(e, "invalid_at", None).isoformat()
                    if getattr(e, "invalid_at", None) else None,
                "created_at": getattr(e, "created_at", None).isoformat()
                    if getattr(e, "created_at", None) else None,
                "group_id": getattr(e, "group_id", None),
                "score": 0.0,
            })
        return results

    def search_memory_facts(
        self,
        query: str,
        group_ids: list[str] | None = None,
        max_facts: int = DEFAULT_MAX_FACTS,
        center_node_uuid: str | None = None,
        start_time: str = "",
        end_time: str = "",
        active_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Search for facts (edges) with optional center-node traversal.

        Note: For single group_id queries, the driver is cloned to point at
        the specific database. This is required because Graphiti's
        @handle_multiple_group_ids decorator only activates for >1 group_ids.
        This pattern matches the query() method implementation.

        Args:
            query: Search query string
            group_ids: Optional list of group IDs to filter by
            max_facts: Maximum facts to return (default: 10, max: 50)
            center_node_uuid: Optional node UUID to center search around
            start_time: ISO 8601 lower bound for created_at (inclusive). Empty = no bound.
            end_time: ISO 8601 upper bound for created_at (inclusive). Empty = no bound.
            active_only: If True, exclude superseded facts (those with invalid_at set).
                Over-fetches by 3x before filtering to preserve result count.

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
            # Sanitize group_ids for filtering within unified database
            sanitized_group_ids = None
            if group_ids:
                sanitized_group_ids = [self._sanitize_thread_id(gid) for gid in group_ids]

            # Use search_() API for facts with reranker scores (match query_memory pattern)
            # Over-fetch when post-filters are active to preserve result count after filtering.
            # The multiplied limit is injected into a copy of the SearchConfig so Graphiti
            # actually returns more edges before Python-side post-filters reduce the count.
            # Note: if the combined supersession/time-filter rate exceeds
            # (1 - 1/OVERFETCH_MULTIPLIER) ≈ 66%, the caller will receive fewer
            # than `max_facts` results with no explicit truncation signal.
            base_limit = min(max_facts, self.MAX_SEARCH_RESULTS)
            has_post_filters = bool(start_time or end_time or active_only)
            limit = min(base_limit * self.OVERFETCH_MULTIPLIER, self.MAX_SEARCH_RESULTS) if has_post_filters else base_limit
            # model_copy(update=...) is the Pydantic v2 idiom for a modified shallow copy.
            # SearchConfig is unfrozen and we only update the scalar `limit` field, so
            # nested sub-configs (EdgeSearchConfig etc.) are safely shared by reference.
            search_config = self._get_search_config().model_copy(update={"limit": limit})

            # Unified database: no driver cloning needed
            # group_ids filters by property within the single project database
            search_results = await graphiti.search_(
                query=query,
                config=search_config,
                group_ids=sanitized_group_ids,
                center_node_uuid=center_node_uuid,
            )

            # Extract edges with scores (already limited by search_config.limit above).
            # When no post-filters are active, cap at base_limit before the formatting
            # loop so we don't do unnecessary dict construction for discarded edges.
            edges = search_results.edges if search_results.edges else []
            edges_to_format = edges if has_post_filters else edges[:base_limit]

            # Format results with reranker scores
            results = []
            for idx, edge in enumerate(edges_to_format):
                # Extract score with defensive indexing (match query_memory pattern)
                score = 0.0
                if idx < len(search_results.edge_reranker_scores):
                    score = search_results.edge_reranker_scores[idx]

                results.append({
                    "uuid": edge.uuid,
                    "name": edge.name,  # Relation name (e.g. "IMPLEMENTS")
                    "fact": edge.fact,
                    "content": edge.fact,  # CoreResult-compliant: map fact text to content
                    "source_node_uuid": edge.source_node_uuid,
                    "target_node_uuid": edge.target_node_uuid,
                    "valid_at": edge.valid_at.isoformat() if edge.valid_at else None,
                    "invalid_at": edge.invalid_at.isoformat() if edge.invalid_at else None,
                    "created_at": edge.created_at.isoformat() if edge.created_at else None,
                    "group_id": edge.group_id if hasattr(edge, 'group_id') else None,
                    "score": score,  # Hybrid search reranker score
                })

            # Post-filter by time range (Graphiti search_ ignores time filters).
            # Uses time_key="created_at" (default) — filters by when the fact was
            # first recorded, NOT by when it was superseded. Combining end_time with
            # active_only is therefore valid: "facts created before T that are still
            # active". (time_key="invalid_at" exists but is not wired here; see #258.)
            results = _filter_by_time_range(results, start_time, end_time)
            # Post-filter to active (non-superseded) facts only
            if active_only:
                results = _filter_active_only(results)
            return results[:base_limit]

        try:
            return asyncio.run(search_facts_async())
        except Exception as e:
            raise BackendError(f"Fact search failed for '{query}': {e}") from e

    def get_episodes(
        self,
        query: str,
        group_ids: list[str] | None = None,
        max_episodes: int = DEFAULT_MAX_EPISODES,
        start_time: str = "",
        end_time: str = "",
    ) -> list[dict[str, Any]]:
        """Search for episodes from Graphiti memory using semantic search.

        Note: Graphiti doesn't support enumerating all episodes. This tool
        performs semantic episode search using the query string.

        Note: For single group_id queries, the driver is cloned to point at
        the specific database. This is required because Graphiti's
        @handle_multiple_group_ids decorator only activates for >1 group_ids.
        This pattern matches the query() method implementation.

        Args:
            query: Search query string (required, must be non-empty)
            group_ids: Optional list of group IDs to filter by
            max_episodes: Maximum episodes to return (default: 10, max: 50)
            start_time: ISO 8601 lower bound for created_at (inclusive). Empty = no bound.
            end_time: ISO 8601 upper bound for created_at (inclusive). Empty = no bound.

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
            # Sanitize group_ids for filtering within unified database
            sanitized_group_ids = None
            if group_ids:
                sanitized_group_ids = [self._sanitize_thread_id(gid) for gid in group_ids]

            # Search episodes via COMBINED config (retrieves episodes + edges + nodes)
            limit = min(max_episodes, self.MAX_SEARCH_RESULTS)
            search_config = self._get_search_config()

            # Unified database: no driver cloning needed
            # group_ids filters by property within the single project database
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

            # Post-filter by time range (Graphiti search_ ignores time filters)
            return _filter_by_time_range(results, start_time, end_time)

        try:
            return asyncio.run(search_episodes_async())
        except Exception as e:
            raise BackendError(f"Episode search failed for '{query}': {e}") from e

    # Safety limit for full-corpus enumeration (get_group_episodes).
    # Prevents unbounded result sets from consuming all memory.
    EPISODE_SAFETY_LIMIT: Final[int] = 10_000

    def get_group_episodes(
        self,
        group_id: str,
        start_time: str = "",
        end_time: str = "",
    ) -> list["EpisodeRecord"]:
        """Get ALL episodes for a group via Cypher (not semantic search).

        Unlike get_episodes() which performs semantic search and requires a
        query string, this method enumerates the complete episode set for a
        group_id. Used by LeanRAG pipeline which needs the full corpus for
        UMAP/clustering.

        This is NOT a MemoryBackend protocol method — it is only meaningful
        for Graphiti (FalkorDB-backed storage). LeanRAG does not store episodes
        directly; it consumes them from Graphiti.

        Args:
            group_id: Project group ID (e.g., "watercooler_cloud")
            start_time: ISO 8601 lower bound for created_at (inclusive)
            end_time: ISO 8601 upper bound for created_at (inclusive)

        Returns:
            List of EpisodeRecord dataclasses for the group.
            Results are truncated at ``EPISODE_SAFETY_LIMIT`` (10,000)
            with a WARNING log.  When a time filter is also active, the
            safety limit is applied *before* the Python-side time filter,
            so the returned list may be smaller than the true matching
            set — a second WARNING is emitted when this happens.  Callers
            should check logs if completeness is critical.

        Raises:
            ConfigError: If group_id is empty
            TransientError: If database connection fails
            BackendError: If query execution fails

        Warning:
            This is a **synchronous** method that internally calls
            ``asyncio.run()``. It must NOT be called directly from
            an async context — use ``await asyncio.to_thread(...)``
            instead, which runs in a thread pool with its own event
            loop.
        """
        from . import EpisodeRecord

        if not group_id or not group_id.strip():
            raise ConfigError("group_id is required for get_group_episodes")

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # No running loop — safe to call asyncio.run()
        else:
            raise RuntimeError(
                "get_group_episodes() called from async context. "
                "Use `await asyncio.to_thread(backend.get_group_episodes, ...)` instead."
            )

        try:
            graphiti = self._create_graphiti_client()
        except Exception as e:
            raise TransientError(f"Database connection failed: {e}") from e

        sanitized_group_id = self._sanitize_thread_id(group_id)

        async def _fetch_all() -> list[EpisodeRecord]:
            driver = graphiti.clients.driver
            # NOTE: Time-range filtering (start_time / end_time) is intentionally
            # kept in Python (_filter_by_time_range) rather than pushed into this
            # Cypher WHERE clause.  FalkorDB stores created_at as an opaque string,
            # so lexicographic comparison in Cypher is fragile across ISO-8601
            # offset variants.  Deferring to Phase 2 once datetime semantics are
            # validated end-to-end.  ORDER BY is safe here because FalkorDB sorts
            # strings lexicographically and our created_at values are ISO-8601 UTC
            # (constant-width, zero-offset), so chronological order is preserved.
            cypher = """
            MATCH (e:Episodic)
            WHERE e.group_id = $group_id
            RETURN e.uuid as uuid, e.name as name, e.content as content,
                   e.source_description as source_description,
                   e.group_id as group_id, e.created_at as created_at
            ORDER BY e.created_at
            LIMIT $safety_limit
            """
            result, _, _ = await driver.execute_query(
                cypher,
                group_id=sanitized_group_id,
                safety_limit=self.EPISODE_SAFETY_LIMIT,
            )

            raw_dicts = [dict(record) for record in result]

            pre_filter_count = len(raw_dicts)
            if pre_filter_count >= self.EPISODE_SAFETY_LIMIT:
                logger.warning(
                    "get_group_episodes: hit safety limit %d for group %s"
                    " — results may be incomplete",
                    self.EPISODE_SAFETY_LIMIT,
                    group_id,
                )

            # Filter by time range on raw dicts (before dataclass construction)
            filtered = _filter_by_time_range(raw_dicts, start_time, end_time)

            if pre_filter_count >= self.EPISODE_SAFETY_LIMIT and len(filtered) < pre_filter_count:
                logger.warning(
                    "get_group_episodes: time filter reduced %d → %d for group %s"
                    " (safety limit may have excluded matching episodes)",
                    pre_filter_count,
                    len(filtered),
                    group_id,
                )

            # Construct typed records with null-safe defaults
            records = []
            for r in filtered:
                safe = {k: v if v is not None else "" for k, v in r.items()}
                if not safe.get("uuid"):
                    logger.warning(
                        "get_group_episodes: episode with empty uuid in group %s"
                        " (source_description=%s) — downstream will use md5 fallback",
                        group_id,
                        safe.get("source_description", "")[:80],
                    )
                records.append(EpisodeRecord(**safe))
            return records

        try:
            return asyncio.run(_fetch_all())
        except Exception as e:
            raise BackendError(
                f"get_group_episodes failed for group '{group_id}': {e}"
            ) from e

    async def find_episode_by_chunk_id_async(
        self,
        chunk_id: str,
        group_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Find an existing episode by chunk_id in source_description (async version).

        Used for migration deduplication - checks if a chunk has already been
        migrated to the graph by searching for episodes with matching chunk_id
        in their source_description field.

        This is the async version that should be used when calling from async contexts
        (e.g., MCP tools, migration code running in event loop).

        Args:
            chunk_id: The chunk ID to search for (first 12 chars used in source_description)
            group_id: Optional group ID to filter by

        Returns:
            Episode dict with uuid, name, etc. if found, None otherwise

        Raises:
            TransientError: If database connection fails
        """
        if not chunk_id or not chunk_id.strip():
            return None

        # Match the pattern used in migration: "chunk:{chunk_id[:12]}"
        search_pattern = f"chunk:{chunk_id[:12]}"

        try:
            # Use cached client with indices already built to avoid per-call overhead
            graphiti = await self._get_graphiti_client_with_indices()
        except Exception as e:
            raise TransientError(f"Database connection failed: {e}") from e

        try:
            # Query FalkorDB directly for episodes with matching source_description
            driver = graphiti.clients.driver

            # Sanitize group_id if provided
            sanitized_group_id = None
            if group_id:
                sanitized_group_id = self._sanitize_thread_id(group_id)

            # Build query with optional group_id filter
            if sanitized_group_id:
                query = """
                MATCH (e:Episodic)
                WHERE e.source_description CONTAINS $pattern
                AND e.group_id = $group_id
                RETURN e.uuid as uuid, e.name as name, e.source_description as source_description,
                       e.content as content, e.group_id as group_id, e.created_at as created_at
                LIMIT 1
                """
                result, _, _ = await driver.execute_query(
                    query,
                    pattern=search_pattern,
                    group_id=sanitized_group_id,
                )
            else:
                query = """
                MATCH (e:Episodic)
                WHERE e.source_description CONTAINS $pattern
                RETURN e.uuid as uuid, e.name as name, e.source_description as source_description,
                       e.content as content, e.group_id as group_id, e.created_at as created_at
                LIMIT 1
                """
                result, _, _ = await driver.execute_query(
                    query,
                    pattern=search_pattern,
                )

            if result and len(result) > 0:
                record = result[0]
                return {
                    "uuid": record["uuid"],
                    "name": record["name"],
                    "source_description": record["source_description"],
                    "content": record.get("content", ""),
                    "group_id": record.get("group_id", ""),
                    "created_at": record.get("created_at", ""),
                }
            return None
        except Exception as e:
            # Log but don't fail - deduplication is best-effort
            logger.warning(f"Episode lookup by chunk_id failed: {e}")
            return None

    def find_episode_by_chunk_id(
        self,
        chunk_id: str,
        group_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Find an existing episode by chunk_id in source_description (sync version).

        Used for migration deduplication - checks if a chunk has already been
        migrated to the graph by searching for episodes with matching chunk_id
        in their source_description field.

        WARNING: This sync version uses asyncio.run() internally and will fail
        if called from within an async context. Use find_episode_by_chunk_id_async()
        when calling from async code (e.g., MCP tools).

        Args:
            chunk_id: The chunk ID to search for (first 12 chars used in source_description)
            group_id: Optional group ID to filter by

        Returns:
            Episode dict with uuid, name, etc. if found, None otherwise

        Raises:
            TransientError: If database connection fails
        """
        try:
            return asyncio.run(self.find_episode_by_chunk_id_async(chunk_id, group_id))
        except RuntimeError as e:
            if "cannot be called from a running event loop" in str(e):
                raise RuntimeError(
                    "find_episode_by_chunk_id() called from async context. "
                    "Use find_episode_by_chunk_id_async() instead."
                ) from e
            raise

    def clear_group_episodes(
        self,
        group_id: str,
    ) -> dict[str, Any]:
        """Clear all episodes for a specific group_id.

        Used for cleanup/testing - removes all episodes belonging to a thread/group
        from the graph. This is a destructive operation.

        Note: This only removes Episodic nodes. Entity nodes and edges that were
        created from these episodes may still remain (Graphiti doesn't automatically
        cascade delete).

        Args:
            group_id: The group ID (thread topic) to clear episodes for

        Returns:
            Dict with removed count and any errors

        Raises:
            ConfigError: If group_id is empty
            TransientError: If database connection fails
            BackendError: If deletion fails
        """
        if not group_id or not group_id.strip():
            raise ConfigError("group_id parameter is required and must be non-empty")

        sanitized_group_id = self._sanitize_thread_id(group_id)

        try:
            graphiti = self._create_graphiti_client()
        except Exception as e:
            raise TransientError(f"Database connection failed: {e}") from e

        async def clear_episodes_async():
            driver = graphiti.clients.driver

            # First, get count of episodes to delete
            count_query = """
            MATCH (e:Episodic {group_id: $group_id})
            RETURN count(e) as count
            """
            count_result, _, _ = await driver.execute_query(
                count_query,
                group_id=sanitized_group_id,
            )
            episode_count = count_result[0]["count"] if count_result else 0

            if episode_count == 0:
                return {"removed": 0, "group_id": sanitized_group_id, "message": "No episodes found"}

            # Delete all episodes for this group_id
            # Also delete relationships to/from these episodes
            delete_query = """
            MATCH (e:Episodic {group_id: $group_id})
            DETACH DELETE e
            RETURN count(e) as deleted
            """
            delete_result, _, _ = await driver.execute_query(
                delete_query,
                group_id=sanitized_group_id,
            )

            deleted_count = delete_result[0]["deleted"] if delete_result else 0

            logger.info(f"Cleared {deleted_count} episodes for group_id '{sanitized_group_id}'")

            return {
                "removed": deleted_count,
                "group_id": sanitized_group_id,
                "message": f"Removed {deleted_count} episodes",
            }

        try:
            return asyncio.run(clear_episodes_async())
        except Exception as e:
            raise BackendError(f"Failed to clear episodes for group '{group_id}': {e}") from e

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
