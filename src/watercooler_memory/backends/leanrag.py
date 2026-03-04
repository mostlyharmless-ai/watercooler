"""LeanRAG backend adapter for entity extraction and hierarchical graph RAG."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import os
# subprocess removed - now using native build_hierarchical_graph()
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator, Literal, Sequence

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

logger = logging.getLogger(__name__)

# Thread-safe lock for os.chdir() operations
# os.chdir() changes the process-wide current directory, which is not thread-safe.
# This lock ensures that only one thread can change directories at a time.
_chdir_lock = threading.RLock()

# Track LeanRAG paths that have been added to sys.path
# NOTE: sys.path entries are intentionally NOT removed after use because:
#   1. Python caches imported modules; removing the path would break reimports
#   2. Duplicate entries are prevented by the `in sys.path` check
#   3. In long-running processes, this is a bounded set (one path per LeanRAG location)
_leanrag_paths_added: set[str] = set()


@contextmanager
def _leanrag_import_context(leanrag_path: Path) -> Generator[None, None, None]:
    """Context manager for LeanRAG imports requiring chdir and sys.path setup.

    Handles thread-safe directory changes and sys.path modifications needed
    for importing LeanRAG modules (which load config.yaml at import time).

    Note: sys.path entries are added once and persist for the process lifetime.
    This is intentional - Python caches imported modules, so removing the path
    after import would break module reloading. The check `if path not in sys.path`
    prevents duplicate entries.

    Args:
        leanrag_path: Absolute path to LeanRAG installation directory.

    Yields:
        Control to the caller with proper context set up.
    """
    leanrag_path_str = str(leanrag_path)

    with _chdir_lock:
        # Add LeanRAG to sys.path if not already there (persists for process lifetime)
        if leanrag_path_str not in sys.path:
            sys.path.insert(0, leanrag_path_str)
            _leanrag_paths_added.add(leanrag_path_str)
            logger.debug(f"Added LeanRAG to sys.path: {leanrag_path_str}")

        # Temporarily change to LeanRAG directory for imports
        original_cwd = os.getcwd()
        try:
            os.chdir(str(leanrag_path))
            yield
        finally:
            os.chdir(original_cwd)


# Resolve default submodule path from package location
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DEFAULT_LEANRAG_PATH = _PACKAGE_ROOT / "external" / "LeanRAG"

# Track whether leanrag is available as installed package vs submodule
_LEANRAG_INSTALLED_AS_PACKAGE: bool | None = None


def _is_leanrag_installed() -> bool:
    """Check if leanrag is installed as a Python package.

    Uses importlib.util.find_spec to check without triggering module execution.
    This avoids the FileNotFoundError from leanrag's import-time config loading.

    Returns:
        True if leanrag can be imported directly (installed via pip/uv),
        False if it needs to be loaded from submodule path.
    """
    global _LEANRAG_INSTALLED_AS_PACKAGE
    if _LEANRAG_INSTALLED_AS_PACKAGE is not None:
        return _LEANRAG_INSTALLED_AS_PACKAGE

    # Use find_spec to check availability without executing module code.
    # Direct import would trigger load_config() at module level, which fails
    # if config.yaml isn't in the current working directory.
    spec = importlib.util.find_spec("leanrag")
    if spec is not None and spec.origin is not None:
        # Verify it's a real installed package (not from our submodule path manipulation)
        origin = Path(spec.origin)
        if "site-packages" in str(origin) or "dist-packages" in str(origin):
            _LEANRAG_INSTALLED_AS_PACKAGE = True
            logger.debug("leanrag found as installed package at %s", origin)
            return True

    _LEANRAG_INSTALLED_AS_PACKAGE = False
    logger.debug("leanrag not installed as package, will use submodule")
    return False


def _get_leanrag_path() -> Path | None:
    """Get leanrag submodule path (only needed if not installed as package).

    Environment Variables:
        LEANRAG_PATH: Override the default leanrag path

    Returns:
        Path to the leanrag submodule directory, or None if installed as package
    """
    # If installed as package, no path needed
    if _is_leanrag_installed():
        return None

    env_path = os.environ.get("LEANRAG_PATH")
    if env_path:
        return Path(env_path)
    return _DEFAULT_LEANRAG_PATH


def _ensure_leanrag_available() -> None:
    """Ensure leanrag is importable, either as package or via submodule.

    For installed packages (uvx --from "watercooler-cloud[memory]"):
        leanrag is already in site-packages, nothing to do.

    For development (git submodule):
        Add the submodule path to sys.path and temporarily change to that
        directory so leanrag's import-time config.yaml loading succeeds.

    Raises:
        ConfigError: If leanrag cannot be made available.
    """
    # Already installed as package? Nothing to do.
    if _is_leanrag_installed():
        return

    # Get submodule path
    leanrag_path = _get_leanrag_path()
    if leanrag_path is None:
        raise ConfigError("leanrag not installed and no submodule path available")

    if not leanrag_path.exists():
        raise ConfigError(
            f"LeanRAG not found at {leanrag_path}. "
            "Either install with: pip install 'watercooler-cloud[memory]' "
            "or run: git submodule update --init external/LeanRAG"
        )

    leanrag_module = leanrag_path / "leanrag"
    if not leanrag_module.exists():
        raise ConfigError(
            f"LeanRAG module not found at {leanrag_module}. "
            "Ensure LeanRAG submodule is properly initialized."
        )

    # Check for config.yaml in the submodule directory
    config_yaml = leanrag_path / "config.yaml"
    if not config_yaml.exists():
        raise ConfigError(
            f"LeanRAG config.yaml not found at {config_yaml}. "
            "Ensure the LeanRAG submodule has a config.yaml file."
        )

    # Add to sys.path if not already there
    leanrag_path_str = str(leanrag_path)
    if leanrag_path_str not in sys.path:
        sys.path.insert(0, leanrag_path_str)
        logger.debug(f"Added leanrag submodule to sys.path: {leanrag_path_str}")

    # Import leanrag with CWD temporarily changed to the submodule directory.
    # LeanRAG's llm.py loads config.yaml at import time from CWD, so we must
    # be in the directory containing config.yaml during import.
    original_cwd = os.getcwd()
    with _chdir_lock:
        try:
            os.chdir(leanrag_path)
            logger.debug(f"Changed to leanrag directory for import: {leanrag_path}")
            import leanrag  # noqa: F401
        except (ImportError, FileNotFoundError) as e:
            raise ConfigError(
                f"Failed to import leanrag after adding to path: {e}"
            ) from e
        finally:
            os.chdir(original_cwd)
            logger.debug(f"Restored original directory: {original_cwd}")


@dataclass
class LeanRAGConfig:
    """Configuration for LeanRAG backend."""

    # LeanRAG submodule location (None if installed as package, Path if using submodule)
    # For installed packages: leanrag is in site-packages, no path needed
    # For development: points to external/LeanRAG submodule
    leanrag_path: Path | None = None  # Resolved by _get_leanrag_path() if needed

    # Database configuration
    falkordb_host: str = "localhost"
    falkordb_port: int = 6379

    # LLM configuration (maps to DEEPSEEK_* env vars)
    llm_api_key: str | None = None
    llm_api_base: str | None = None
    llm_model: str | None = None

    # Embedding configuration (maps to GLM_* env vars)
    # Note: LeanRAG's embedding endpoint uses raw HTTP without auth (no API key)
    embedding_api_base: str | None = None
    embedding_model: str | None = None

    # Database password (maps to FALKORDB_PASSWORD)
    falkordb_password: str | None = None

    # Working directory for exports
    work_dir: Path | None = None

    # Test mode: Add pytest__ prefix to database names for isolation
    test_mode: bool = False

    # Max workers for graph building
    max_workers: int = 8

    # Max concurrent LLM calls during triple extraction.
    # Controls asyncio.Semaphore for generate_text_async calls.
    # Default of 20 is well within DeepSeek's rate limits (~500 RPM) while
    # providing ~5x throughput improvement over the previous default of 4.
    # Lower values (2-4) can be used if hitting rate limits on restricted plans.
    max_concurrent_llm_calls: int = 20

    @classmethod
    def from_unified(cls) -> "LeanRAGConfig":
        """Create LeanRAGConfig from unified watercooler configuration.

        Uses the unified config system with proper priority chain:
        1. Environment variables (highest)
        2. Backend-specific TOML overrides ([memory.leanrag])
        3. Shared TOML settings ([memory.llm], [memory.embedding], [memory.database])
        4. Built-in defaults (lowest)

        Returns:
            LeanRAGConfig instance with all settings resolved

        Example:
            >>> config = LeanRAGConfig.from_unified()
            >>> backend = LeanRAGBackend(config)
        """
        from watercooler.memory_config import (
            resolve_llm_config,
            resolve_embedding_config,
            resolve_database_config,
            get_leanrag_max_workers,
        )

        llm = resolve_llm_config("leanrag")
        embedding = resolve_embedding_config("leanrag")
        db = resolve_database_config()

        return cls(
            llm_api_key=llm.api_key,
            llm_api_base=llm.api_base if llm.api_base != "https://api.openai.com/v1" else None,
            llm_model=llm.model,
            embedding_api_base=embedding.api_base if embedding.api_base != "https://api.openai.com/v1" else None,
            embedding_model=embedding.model,
            falkordb_host=db.host,
            falkordb_port=db.port,
            falkordb_password=db.password if db.password else None,
            max_workers=get_leanrag_max_workers(),
        )


class LeanRAGBackend(MemoryBackend):
    """
    LeanRAG adapter implementing MemoryBackend contract.

    LeanRAG provides:
    - Entity and relation extraction
    - Hierarchical semantic clustering (GMM + UMAP)
    - Multi-layer knowledge graph construction
    - Reduced redundancy (~46% vs flat baselines)

    This adapter uses native LeanRAG Python APIs and maps to/from canonical payloads.

    Performance Considerations:
    - Current implementation creates new FalkorDB connections per request
    - Future Enhancement: Connection pooling for high-throughput scenarios
      - Recommended for production deployments handling >100 QPS
      - Consider using a connection pool (e.g., redis-py connection pool)
      - Pool size should be tuned based on concurrent request volume
      - Trade-off: Memory overhead vs connection setup latency
    """

    def __init__(self, config: LeanRAGConfig | None = None) -> None:
        self.config = config or LeanRAGConfig()
        self._apply_config_to_env()  # Bridge config to LeanRAG's expected env vars
        self._validate_config()

    def _apply_config_to_env(self) -> None:
        """Set environment variables from resolved config.

        LeanRAG's config.yaml uses ${VAR} substitution to read environment variables.
        This method bridges the unified watercooler config to LeanRAG's expected env vars.

        Equivalence mapping (Watercooler standard → LeanRAG):
            LLM_API_KEY      → DEEPSEEK_API_KEY
            LLM_MODEL        → DEEPSEEK_MODEL
            LLM_API_BASE     → DEEPSEEK_BASE_URL
            EMBEDDING_MODEL  → GLM_MODEL
            EMBEDDING_API_BASE → GLM_BASE_URL

        Database vars use the same names in both systems:
            FALKORDB_HOST, FALKORDB_PORT, FALKORDB_PASSWORD

        Priority: If the LeanRAG var is already set, we don't override it.
        If only the watercooler standard var is set, we bridge it to LeanRAG's name.
        """
        # Bridge standard watercooler env vars to LeanRAG equivalents
        # This allows users to use either naming convention
        standard_to_leanrag = [
            ("LLM_API_KEY", "DEEPSEEK_API_KEY"),
            ("LLM_MODEL", "DEEPSEEK_MODEL"),
            ("LLM_API_BASE", "DEEPSEEK_BASE_URL"),
            ("LLM_TIMEOUT", "DEEPSEEK_TIMEOUT"),
            ("LLM_MAX_RETRIES", "DEEPSEEK_MAX_ATTEMPTS"),
            ("EMBEDDING_MODEL", "GLM_MODEL"),
            # LeanRAG config.yaml may use a distinct var name for embeddings.
            ("EMBEDDING_MODEL", "GLM_EMBEDDING_MODEL"),
            ("EMBEDDING_API_BASE", "GLM_BASE_URL"),
        ]
        for standard_var, leanrag_var in standard_to_leanrag:
            if leanrag_var not in os.environ and standard_var in os.environ:
                os.environ[leanrag_var] = os.environ[standard_var]
                logger.debug(f"Bridged {standard_var} → {leanrag_var}")

        # Resolve LLM timeout/retry from unified config for LeanRAG backend.
        # Only set DEEPSEEK_TIMEOUT if TOML has an explicit override;
        # otherwise let config.yaml's ${DEEPSEEK_TIMEOUT:-120} default apply.
        # Lazy import: resolve_llm_config imports config_schema which imports
        # pydantic — keep it out of module-level to avoid circular imports
        # when watercooler_memory is imported before watercooler is fully loaded.
        from watercooler.config_schema import LLM_TIMEOUT_DEFAULT
        from watercooler.memory_config import resolve_llm_config
        llm = resolve_llm_config("leanrag")
        deepseek_timeout = None
        # Note: float equality is stable here (literal constant comparison).
        # If someone explicitly sets timeout=60.0 in TOML, it reads as "no
        # override" and config.yaml's ${DEEPSEEK_TIMEOUT:-120} default applies.
        if llm.timeout != LLM_TIMEOUT_DEFAULT:
            deepseek_timeout = str(llm.timeout)

        # Set from resolved config (only if not already set)
        env_mappings = [
            # LeanRAG path (needed by _ensure_leanrag_available() / _get_leanrag_path())
            ("LEANRAG_PATH", str(self.config.leanrag_path) if self.config.leanrag_path else None),
            # LLM config (used by generate_text via OpenAI client)
            ("DEEPSEEK_API_KEY", self.config.llm_api_key),
            ("DEEPSEEK_MODEL", self.config.llm_model),
            ("DEEPSEEK_BASE_URL", self.config.llm_api_base),
            ("DEEPSEEK_TIMEOUT", deepseek_timeout),
            ("DEEPSEEK_MAX_ATTEMPTS", "3"),  # config.yaml reads DEEPSEEK_MAX_ATTEMPTS (not MAX_RETRIES)
            # Embedding config (used by embedding() via raw HTTP POST - no auth)
            ("GLM_BASE_URL", self.config.embedding_api_base),
            ("GLM_MODEL", self.config.embedding_model),
            ("GLM_EMBEDDING_MODEL", self.config.embedding_model),
            # Database config
            ("FALKORDB_HOST", self.config.falkordb_host),
            ("FALKORDB_PORT", str(self.config.falkordb_port) if self.config.falkordb_port else None),
            ("FALKORDB_PASSWORD", self.config.falkordb_password),
        ]
        for env_name, value in env_mappings:
            if value and env_name not in os.environ:
                os.environ[env_name] = value
                logger.debug(f"Set {env_name} from unified config")

        # LeanRAG's config.yaml env substitution requires the variable to be
        # present even when unset (e.g., no password for local FalkorDB).
        os.environ.setdefault("FALKORDB_PASSWORD", "")

        # LeanRAG config.yaml references MYSQL_* even in FalkorDB-only deployments.
        # These are never used for actual connections; they prevent config-load
        # failures from unresolved env var substitutions.
        os.environ.setdefault("MYSQL_HOST", "localhost")
        os.environ.setdefault("MYSQL_PORT", "3306")
        os.environ.setdefault("MYSQL_USER", "root")
        os.environ.setdefault("MYSQL_PASSWORD", "")

    def _validate_config(self) -> None:
        """Validate configuration and LeanRAG availability."""
        # Ensure leanrag is importable (installed package or submodule)
        _ensure_leanrag_available()

        # Log provenance to help debug "patched submodule but running different package"
        # FileNotFoundError: installed leanrag's config.yaml lookup fails if CWD != LeanRAG dir.
        # This is expected when calling from the project root; the real import (with correct CWD)
        # happens inside index() / incremental_index() under the _chdir_lock.
        try:
            import leanrag as _lr
            logger.debug("LeanRAG loaded from: %s", getattr(_lr, '__file__', 'unknown'))
        except (ImportError, FileNotFoundError):
            logger.debug("LeanRAG not importable for provenance check (config.yaml not in CWD)")

    def _normalize_entity_name(self, name: str | None) -> str | None:
        """Normalize entity name by stripping quotes and whitespace.

        LeanRAG stores entity names in different ways:
        - Milvus: Raw names with quotes (e.g., '"OAUTH2"')
        - FalkorDB: Stripped names without quotes (e.g., 'OAUTH2')

        This normalization ensures consistency across backends by matching
        FalkorDB's normalization pattern (see falkordb.py:161).

        Args:
            name: Entity name to normalize (may be None for optional fields)

        Returns:
            Normalized entity name, or None if input was None

        Example:
            >>> backend._normalize_entity_name('"OAUTH2"  ')
            'OAUTH2'
        """
        if name is None:
            return None
        return name.strip().strip('"').strip()

    def _apply_test_prefix(self, work_dir: Path) -> Path:
        """Apply pytest__ prefix to work_dir basename if test_mode is enabled.

        LeanRAG uses os.path.basename(work_dir) as the FalkorDB database name.
        When test_mode=True, we prepend 'pytest__' to the directory name to
        ensure test databases are isolated and can be cleaned up separately.

        Args:
            work_dir: Original working directory path

        Returns:
            Path with pytest__ prefix applied to basename if test_mode=True,
            otherwise returns original path unchanged
        """
        if not self.config.test_mode:
            return work_dir

        # Get parent and basename
        parent = work_dir.parent
        basename = work_dir.name

        # Prepend pytest__ if not already present
        if not basename.startswith("pytest__"):
            basename = f"pytest__{basename}"

        return parent / basename

    def prepare(self, corpus: CorpusPayload) -> PrepareResult:
        """
        Prepare corpus for LeanRAG ingestion.

        Maps canonical payload to LeanRAG's expected JSON format:
        - documents.json: Entries with content and metadata
        - threads.json: Thread metadata
        - threads_chunk.json: Chunks generated from provided entries
        - manifest.json: Export metadata
        """
        if self.config.work_dir:
            work_dir = self.config.work_dir
        else:
            work_dir = Path(tempfile.mkdtemp(prefix="leanrag-prepare-"))

        # Convert to absolute path for reliability
        work_dir = work_dir.resolve()

        # Apply pytest__ prefix if in test mode
        work_dir = self._apply_test_prefix(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            documents = [
                {
                    "id": entry.get("id"),
                    "thread_id": entry.get("thread_id"),
                    "title": entry.get("title"),
                    "content": entry.get("body", entry.get("content", "")),
                    "agent": entry.get("agent"),
                    "role": entry.get("role"),
                    "type": entry.get("type"),
                    "timestamp": entry.get("timestamp"),
                }
                for entry in corpus.entries
            ]

            threads = [
                {
                    "id": thread.get("id"),
                    "topic": thread.get("topic"),
                    "status": thread.get("status"),
                    "ball": thread.get("ball"),
                    "entry_count": thread.get("entry_count"),
                    "title": thread.get("title"),
                }
                for thread in corpus.threads
            ]

            chunks = self._extract_chunks_from_entries(corpus)

            (work_dir / "documents.json").write_text(json.dumps(documents, indent=2))
            (work_dir / "threads.json").write_text(json.dumps(threads, indent=2))
            (work_dir / "threads_chunk.json").write_text(json.dumps(chunks, indent=2))

            manifest = {
                "format": "leanrag-corpus",
                "version": "1.0",
                "source": "watercooler-cloud",
                "memory_payload_version": corpus.manifest_version,
                "chunker": {
                    "name": corpus.chunker_name,
                    "params": corpus.chunker_params,
                },
                "statistics": {
                    "threads": len(corpus.threads),
                    "entries": len(corpus.entries),
                    "chunks": len(chunks),
                },
                "files": {
                    "documents": "documents.json",
                    "threads": "threads.json",
                    "chunks": "threads_chunk.json",
                },
            }
            (work_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

            return PrepareResult(
                manifest_version=corpus.manifest_version,
                prepared_count=len(documents),
                message=f"Prepared corpus at {work_dir}",
            )
        except Exception as exc:
            raise BackendError(f"Failed to prepare corpus: {exc}") from exc

    def _extract_chunks_from_entries(
        self, corpus: CorpusPayload
    ) -> list[dict[str, Any]]:
        """
        Extract chunks from all entries for LeanRAG query pipeline.

        Creates threads_chunk.json with format:
        [{"hash_code": "...", "text": "..."}, ...]
        """
        import hashlib

        chunks: list[dict[str, Any]] = []
        for entry in corpus.entries:
            entry_chunks = entry.get("chunks", [])

            if not entry_chunks:
                full_text = f"# {entry.get('title', '')}\n\n{entry.get('body', '')}"
                hash_code = hashlib.md5(full_text.encode()).hexdigest()
                chunks.append({"hash_code": hash_code, "text": full_text})
                continue

            for chunk in entry_chunks:
                if isinstance(chunk, str):
                    chunk_text = chunk
                elif isinstance(chunk, dict):
                    chunk_text = chunk.get("text", chunk.get("content", str(chunk)))
                else:
                    chunk_text = str(chunk)

                hash_code = hashlib.md5(chunk_text.encode()).hexdigest()
                chunks.append({"hash_code": hash_code, "text": chunk_text})

        return chunks

    def _ensure_chunk_file(self, work_dir: Path, chunks: ChunkPayload) -> Path:
        """Write chunks to threads_chunk.json in the working directory.

        Always overwrites any existing file to ensure fresh data is used.
        """
        chunk_file = work_dir / "threads_chunk.json"

        serialized = []
        for item in chunks.chunks:
            text = item.get("text", item.get("content", ""))
            hash_code = item.get("hash_code") or item.get("id") or item.get("chunk_id")
            if not hash_code:
                import hashlib

                hash_code = hashlib.md5(text.encode()).hexdigest()
            serialized.append({"hash_code": hash_code, "text": text})

        chunk_file.write_text(json.dumps(serialized, indent=2))
        return chunk_file

    def index(
        self,
        chunks: ChunkPayload,
        progress_callback: Any | None = None,
    ) -> IndexResult:
        """
        Run LeanRAG entity extraction and graph building.

        Executes LeanRAG pipeline:
        1. triple_extraction (bypasses LeanRAG chunking)
        2. build_hierarchical_graph() to construct hierarchical graph (native)

        Args:
            chunks: ChunkPayload containing chunks to index
            progress_callback: Optional callback for progress updates.
                Signature: (stage: str, step: str, current: int, total: int) -> None
        """
        import logging
        import time

        logger = logging.getLogger(__name__)

        def _report(stage: str, step: str, current: int = 0, total: int = 0) -> None:
            """Report progress via callback if provided."""
            if progress_callback:
                progress_callback(stage, step, current, total)

        work_dir = self.config.work_dir or Path(tempfile.mkdtemp(prefix="leanrag-index-"))

        # Convert to absolute path so it works when we change directories
        work_dir = work_dir.resolve()

        # Apply pytest__ prefix if in test mode
        work_dir = self._apply_test_prefix(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            chunk_file = self._ensure_chunk_file(work_dir, chunks)

            leanrag_abspath = self.config.leanrag_path.resolve()

            with open(chunk_file, "r") as fh:
                corpus = json.load(fh)
            chunks_dict = {item["hash_code"]: item["text"] for item in corpus}

            # Stage 1: Triple extraction (entity/relation extraction via LLM)
            num_chunks = len(chunks_dict)
            _report("triple_extraction", "starting", 0, num_chunks)
            extraction_start = time.perf_counter()

            # Thread-safe directory change AND sys.path modification
            with _chdir_lock:
                # Add LeanRAG to path inside lock to prevent race conditions
                if str(leanrag_abspath) not in sys.path:
                    sys.path.insert(0, str(leanrag_abspath))

                original_cwd = os.getcwd()
                try:
                    os.chdir(str(self.config.leanrag_path))

                    from leanrag.extraction.chunk import triple_extraction
                    from leanrag.core.llm import generate_text_async

                    # Wrap LLM function to track progress
                    # Each chunk requires ~4-6 LLM calls (entity, relation, gleaning)
                    llm_call_count = [0]
                    last_report_time = [time.perf_counter()]
                    report_interval = 10.0  # Report every 10 seconds

                    _llm_sem = asyncio.Semaphore(self.config.max_concurrent_llm_calls)

                    async def _tracked_llm(prompt: str, **kwargs) -> str:
                        async with _llm_sem:
                            result = await generate_text_async(prompt, **kwargs)
                        llm_call_count[0] += 1
                        # Report progress periodically
                        now = time.perf_counter()
                        if now - last_report_time[0] >= report_interval:
                            elapsed = now - extraction_start
                            # Estimate: ~5 LLM calls per chunk
                            estimated_chunks = llm_call_count[0] // 5
                            _report("triple_extraction", "processing", estimated_chunks, num_chunks)
                            last_report_time[0] = now
                        return result

                    asyncio.run(
                        triple_extraction(
                            chunks_dict, _tracked_llm, str(work_dir), save_filtered=False
                        )
                    )
                finally:
                    os.chdir(original_cwd)

            extraction_elapsed = time.perf_counter() - extraction_start
            _report("triple_extraction", "complete", num_chunks, num_chunks)
            logger.info(f"Triple extraction complete: {num_chunks} chunks, {llm_call_count[0]} LLM calls in {extraction_elapsed:.1f}s")

            # Stage 2: Hierarchical graph building
            _report("graph_building", "starting", 0, 5)
            build_start = time.perf_counter()
            logger.info(f"Running LeanRAG graph building (native) at {work_dir}")

            # Progress callback wrapper for build_hierarchical_graph
            def _build_progress(step_name: str, current: int, total: int) -> None:
                _report("graph_building", step_name, current, total)

            # Import inside lock context to ensure sys.path is set
            with _chdir_lock:
                if str(leanrag_abspath) not in sys.path:
                    sys.path.insert(0, str(leanrag_abspath))

                original_cwd = os.getcwd()
                try:
                    os.chdir(str(self.config.leanrag_path))

                    from leanrag.pipelines.build_native import build_hierarchical_graph

                    build_result = build_hierarchical_graph(
                        working_dir=str(work_dir),
                        max_workers=self.config.max_workers,
                        fresh_start=False,  # Allow checkpoint resume
                        progress_callback=_build_progress,
                    )
                finally:
                    os.chdir(original_cwd)

            build_elapsed = time.perf_counter() - build_start
            _report("graph_building", "complete", 5, 5)

            if build_result.errors:
                logger.warning(f"Graph building completed with errors: {build_result.errors}")

            logger.info(
                f"Graph building complete: {build_result.entries_processed} entities, "
                f"{build_result.clusters_created} clusters, "
                f"{build_result.duration_seconds:.1f}s"
            )

            return IndexResult(
                manifest_version=chunks.manifest_version,
                indexed_count=len(chunks.chunks),
                message=(
                    f"Indexed {len(chunks.chunks)} chunks via LeanRAG at {work_dir}. "
                    f"Built {build_result.clusters_created} clusters in {build_result.duration_seconds:.1f}s"
                ),
            )
        except FileNotFoundError as exc:
            raise ConfigError(
                f"Required LeanRAG files not found: {exc}. "
                "Ensure LeanRAG submodule is initialized and prepared has been run."
            ) from exc
        except ValueError as exc:
            # Checkpoint corruption or other value errors from build_native
            raise BackendError(f"LeanRAG build error: {exc}") from exc
        except Exception as exc:
            raise BackendError(f"Unexpected error during index: {exc}") from exc

    def has_incremental_state(self) -> bool:
        """Check if saved cluster state exists for incremental updates.

        Returns True if a prior full build saved cluster centroids + UMAP
        models that the incremental path can reuse.
        """
        work_dir = self.config.work_dir
        if work_dir is None:
            return False

        work_dir = work_dir.resolve()
        work_dir = self._apply_test_prefix(work_dir)
        state_dir = work_dir / ".cluster_state"

        if not state_dir.exists():
            return False

        try:
            leanrag_abspath = self.config.leanrag_path.resolve()

            with _chdir_lock:
                if str(leanrag_abspath) not in sys.path:
                    sys.path.insert(0, str(leanrag_abspath))

                original_cwd = os.getcwd()
                try:
                    os.chdir(str(self.config.leanrag_path))
                    from leanrag.clustering.state_manager import StateManager

                    with StateManager(state_dir) as sm:
                        return sm.has_cluster_state(layer=0)
                finally:
                    os.chdir(original_cwd)
        except Exception as exc:
            logger.debug("has_incremental_state check failed: %s", exc)
            return False

    def incremental_index(
        self,
        chunks: ChunkPayload,
        progress_callback: Any | None = None,
    ) -> IndexResult:
        """Run incremental LeanRAG entity extraction and cluster assignment.

        Uses saved cluster state from a prior full build to assign new entities
        to existing clusters without rebuilding the entire hierarchy.

        Falls back to full ``index()`` if no saved cluster state exists.

        Args:
            chunks: ChunkPayload containing new chunks to index.
            progress_callback: Optional callback for progress updates.
        """
        import time

        if not self.has_incremental_state():
            # Guard against degenerate UMAP with very few chunks.
            # UMAP requires n_neighbors > 0, which fails for N < ~5 samples.
            # Skip the full build and return a descriptive result instead.
            MIN_CHUNKS_FOR_INITIAL_BUILD = 5
            if len(chunks.chunks) < MIN_CHUNKS_FOR_INITIAL_BUILD:
                logger.info(
                    "Too few chunks (%d) for initial build — skipping (need >= %d)",
                    len(chunks.chunks), MIN_CHUNKS_FOR_INITIAL_BUILD,
                )
                return IndexResult(
                    manifest_version=chunks.manifest_version,
                    indexed_count=0,
                    message=(
                        f"Skipped: {len(chunks.chunks)} chunks insufficient for "
                        f"initial build (need >= {MIN_CHUNKS_FOR_INITIAL_BUILD})"
                    ),
                )
            logger.info("No incremental state found — falling back to full index()")
            return self.index(chunks, progress_callback=progress_callback)

        def _report(stage: str, step: str, current: int = 0, total: int = 0) -> None:
            if progress_callback:
                progress_callback(stage, step, current, total)

        work_dir = self.config.work_dir or Path(tempfile.mkdtemp(prefix="leanrag-incr-"))
        work_dir = work_dir.resolve()
        work_dir = self._apply_test_prefix(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            chunk_file = self._ensure_chunk_file(work_dir, chunks)
            leanrag_abspath = self.config.leanrag_path.resolve()

            with open(chunk_file, "r") as fh:
                corpus = json.load(fh)
            chunks_dict = {item["hash_code"]: item["text"] for item in corpus}

            # Stage 1: Triple extraction (same as full index)
            num_chunks = len(chunks_dict)
            _report("triple_extraction", "starting", 0, num_chunks)
            extraction_start = time.perf_counter()

            with _chdir_lock:
                if str(leanrag_abspath) not in sys.path:
                    sys.path.insert(0, str(leanrag_abspath))

                original_cwd = os.getcwd()
                try:
                    os.chdir(str(self.config.leanrag_path))

                    from leanrag.extraction.chunk import triple_extraction
                    from leanrag.core.llm import generate_text_async, embedding

                    _llm_sem = asyncio.Semaphore(self.config.max_concurrent_llm_calls)

                    async def _tracked_llm(prompt: str, **kwargs) -> str:
                        async with _llm_sem:
                            return await generate_text_async(prompt, **kwargs)

                    asyncio.run(
                        triple_extraction(
                            chunks_dict, _tracked_llm, str(work_dir), save_filtered=False
                        )
                    )
                finally:
                    os.chdir(original_cwd)

            _report("triple_extraction", "complete", num_chunks, num_chunks)

            # Stage 2: Load extracted entities and generate embeddings
            _report("incremental_update", "embedding", 0, 3)

            with _chdir_lock:
                if str(leanrag_abspath) not in sys.path:
                    sys.path.insert(0, str(leanrag_abspath))

                original_cwd = os.getcwd()
                try:
                    os.chdir(str(self.config.leanrag_path))

                    from leanrag.core.llm import embedding as embed_fn
                    from leanrag.core.llm import generate_text
                    try:
                        from leanrag.pipelines.incremental import incremental_update
                    except ImportError:
                        logger.warning(
                            "leanrag.pipelines.incremental not available; "
                            "falling back to full rebuild"
                        )
                        incremental_update = None  # type: ignore[assignment]

                    # Load extracted entities
                    entity_path = work_dir / "entity.jsonl"
                    if not entity_path.exists():
                        logger.warning("No entities extracted — nothing to index incrementally")
                        return IndexResult(
                            manifest_version=chunks.manifest_version,
                            indexed_count=0,
                            message="No entities extracted from chunks",
                        )

                    import json as _json
                    entities_meta = []
                    entity_descriptions = []
                    with open(entity_path) as f:
                        for line in f:
                            ent = _json.loads(line)
                            name = str(ent.get("entity_name", ""))
                            entities_meta.append({
                                "entity_id": int(hashlib.sha256(name.encode()).hexdigest(), 16) % (2**31),
                                "entity_name": name,
                            })
                            entity_descriptions.append(
                                ent.get("description", name)[:4096]
                            )

                    if not entities_meta:
                        return IndexResult(
                            manifest_version=chunks.manifest_version,
                            indexed_count=0,
                            message="No entities extracted",
                        )

                    # Generate embeddings
                    import numpy as np

                    batch_size = 64
                    all_embeddings = []
                    for i in range(0, len(entity_descriptions), batch_size):
                        batch = entity_descriptions[i:i + batch_size]
                        emb = embed_fn(batch)
                        all_embeddings.append(emb)

                    embeddings = np.vstack(all_embeddings)

                    if incremental_update is None:
                        # Fallback: full rebuild when incremental module unavailable
                        logger.info(
                            "Incremental pipeline unavailable; delegating to full index()"
                        )
                        os.chdir(original_cwd)  # restore before calling index()
                        return self.index(chunks, progress_callback=progress_callback)

                    # Stage 3: Run incremental update pipeline
                    _report("incremental_update", "assigning", 1, 3)
                    state_dir = str(work_dir / ".cluster_state")

                    # Wire up llm_func so dirty communities get re-summarized.
                    # generate_text is the sync LLM wrapper used by the full
                    # build pipeline (same signature: str -> str).
                    inc_result = incremental_update(
                        working_dir=str(work_dir),
                        new_entity_embeddings=embeddings,
                        new_entity_metadata=entities_meta,
                        state_dir=state_dir,
                        llm_func=generate_text,
                    )
                finally:
                    os.chdir(original_cwd)

            _report("incremental_update", "complete", 3, 3)

            return IndexResult(
                manifest_version=chunks.manifest_version,
                indexed_count=len(chunks.chunks),
                message=(
                    f"Incremental index: {inc_result.entities_assigned} entities assigned, "
                    f"{inc_result.entities_orphaned} orphaned, "
                    f"{inc_result.communities_resummarized} communities re-summarized "
                    f"in {inc_result.duration_seconds:.1f}s"
                ),
            )
        except FileNotFoundError as exc:
            raise ConfigError(f"Required LeanRAG files not found: {exc}") from exc
        except Exception as exc:
            raise BackendError(f"Incremental index error: {exc}") from exc

    def query(self, query: QueryPayload) -> QueryResult:
        """
        Execute queries against LeanRAG graph by calling query_graph directly.
        """
        work_dir = self.config.work_dir or Path(tempfile.mkdtemp(prefix="leanrag-query-"))
        # Convert to absolute path for reliability
        work_dir = work_dir.resolve()
        # Apply pytest__ prefix if in test mode (consistent with prepare/index)
        work_dir = self._apply_test_prefix(work_dir)

        if not (work_dir / "threads_chunk.json").exists():
            raise ConfigError(
                f"threads_chunk.json not found in {work_dir}. "
                "Run prepare() and index() before querying."
            )

        try:
            leanrag_abspath = self.config.leanrag_path.resolve()

            # Thread-safe directory change AND sys.path modification
            with _chdir_lock:
                # Add LeanRAG to path inside lock to prevent race conditions
                if str(leanrag_abspath) not in sys.path:
                    sys.path.insert(0, str(leanrag_abspath))

                original_cwd = os.getcwd()
                try:
                    os.chdir(str(self.config.leanrag_path))

                    from leanrag.pipelines.query import query_graph
                    from leanrag.core.llm import embedding, generate_text

                    results: list[dict[str, Any]] = []
                    for q in query.queries:
                        query_text = q.get("query", q.get("text", ""))
                        topk = q.get("limit", q.get("topk", 5))
                        if not query_text:
                            continue

                        global_config = {
                            "working_dir": str(work_dir),
                            "chunks_file": str(work_dir / "threads_chunk.json"),
                            "embeddings_func": embedding,
                            "use_llm_func": generate_text,
                            "topk": topk,
                            "level_mode": 1,
                        }

                        context, answer = query_graph(global_config, None, query_text)
                        results.append(
                            {
                                "query": query_text,
                                "answer": answer,
                                "context": context,
                                "topk": topk,
                            }
                        )

                        if answer:
                            print(f"Query answer: {answer[:200]}...")
                finally:
                    os.chdir(original_cwd)

            return QueryResult(
                manifest_version=query.manifest_version,
                results=results,
                message=f"Executed {len(results)} queries via LeanRAG",
            )
        except Exception as exc:
            raise BackendError(f"Unexpected error during query: {exc}") from exc

    def healthcheck(self) -> HealthStatus:
        """
        Check LeanRAG and database health.
        """
        try:
            self._validate_config()

            try:
                import redis

                redis.Redis(
                    host=self.config.falkordb_host,
                    port=self.config.falkordb_port,
                    socket_connect_timeout=2,
                ).ping()
                db_status = "FalkorDB: connected"
            except ImportError:
                db_status = "FalkorDB: redis-py not installed"
            except (redis.ConnectionError, redis.TimeoutError) as exc:
                db_status = f"FalkorDB: unreachable ({exc})"

            return HealthStatus(
                ok=True,
                details=f"LeanRAG available at {self.config.leanrag_path}, {db_status}",
            )
        except ConfigError as exc:
            return HealthStatus(ok=False, details=str(exc))
        except Exception as exc:
            return HealthStatus(ok=False, details=f"Health check failed: {exc}")

    def search_nodes(
        self,
        query: str,
        group_ids: Sequence[str] | None = None,
        max_results: int = 10,
        entity_types: list[str] | None = None,
        level_mode: Literal[0, 1, 2] = 2,
    ) -> list[dict[str, Any]]:
        """Search for entity nodes using vector similarity search.

        Args:
            query: Search query string
            group_ids: Optional list of group IDs to filter by (ignored - LeanRAG uses separate databases)
            max_results: Maximum number of results to return
            entity_types: Optional list of entity types to filter by (not implemented in LeanRAG)
            level_mode: LeanRAG hierarchy level mode (maps to search_vector_search):
                - 0: Base entities only (precise, individual nodes)
                - 1: Clusters only (hierarchical summaries)
                - 2: All levels (base + clusters, default)

        Returns:
            List of normalized CoreResult dictionaries with node data

        Raises:
            ConfigError: If work_dir not set or database not indexed
            BackendError: If search fails
            TransientError: If database connection fails
        """
        work_dir = self.config.work_dir
        if not work_dir:
            raise ConfigError("work_dir must be set before searching")

        work_dir = work_dir.resolve()
        if not (work_dir / "threads_chunk.json").exists():
            raise ConfigError(
                f"Database not indexed. threads_chunk.json not found in {work_dir}"
            )

        try:
            # Add LeanRAG to path
            leanrag_abspath = self.config.leanrag_path.resolve()

            # Thread-safe directory change AND sys.path modification
            with _chdir_lock:
                # Add LeanRAG to path inside lock to prevent race conditions
                if str(leanrag_abspath) not in sys.path:
                    sys.path.insert(0, str(leanrag_abspath))

                original_cwd = os.getcwd()
                try:
                    os.chdir(str(self.config.leanrag_path))

                    # Import LeanRAG functions
                    from leanrag.core.llm import embedding
                    from leanrag.database.vector import search_vector_search

                    # Convert text query to embedding vector
                    query_embedding = embedding(query)

                    # Execute vector search with specified level_mode.
                    # On DB unavailable (redis ConnectionError etc.) or zero entities,
                    # fall back to chunk-level similarity so benchmarks produce T3 evidence.
                    results: list[Any] = []
                    try:
                        logger.debug(f"LeanRAG search_nodes: level_mode={level_mode}")
                        results = search_vector_search(
                            str(work_dir),
                            query_embedding,
                            topk=max_results,
                            level_mode=level_mode
                        )
                    except (ConnectionError, OSError, Exception) as vec_err:
                        logger.debug(
                            "LeanRAG vector search failed (DB may be down or empty): %s",
                            vec_err,
                        )
                        results = []

                    # Fallback: if the LeanRAG extraction/build produced zero EntityVector
                    # nodes (common for tiny corpora + weak local LLM extraction), or
                    # FalkorDB/Milvus is unavailable, use chunk-level similarity. For
                    # benchmarks we still want deterministic "T3 evidence" with a
                    # provenance.source that maps back to the chunk id (episode UUID).
                    if not results:
                        try:
                            import math

                            def _to_flat_floats(vec: Any) -> list[float]:
                                """Flatten embedding to list of floats.

                                LeanRAG embedding() returns np.ndarray shape (1, dim) for a single
                                string. This is a type/shape handling requirement, not a model
                                mismatch — query and chunks use the same embedding model.
                                """
                                if hasattr(vec, "tolist"):
                                    flat = vec.tolist()
                                elif isinstance(vec, list):
                                    flat = vec
                                else:
                                    flat = list(vec)
                                # Unwrap batch dimension: [[x,y,...]] -> [x,y,...]
                                if flat and isinstance(flat[0], (list, tuple)):
                                    flat = list(flat[0])
                                return [float(x) for x in flat]

                            chunk_items = json.loads((work_dir / "threads_chunk.json").read_text(encoding="utf-8"))
                            if not isinstance(chunk_items, list):
                                chunk_items = []

                            def _cosine(a: list[float], b: list[float]) -> float:
                                # Pure-Python cosine similarity to avoid numpy dependency.
                                dot = 0.0
                                na = 0.0
                                nb = 0.0
                                for x, y in zip(a, b):
                                    dot += x * y
                                    na += x * x
                                    nb += y * y
                                denom = math.sqrt(na) * math.sqrt(nb)
                                return dot / denom if denom else 0.0

                            q_flat = _to_flat_floats(query_embedding)
                            scored: list[tuple[float, dict[str, Any]]] = []
                            for item in chunk_items[:200]:
                                if not isinstance(item, dict):
                                    continue
                                source_id = str(item.get("hash_code") or "")
                                text = str(item.get("text") or "")
                                if not source_id or not text.strip():
                                    continue
                                v = embedding(text)
                                v_flat = _to_flat_floats(v)
                                if len(q_flat) != len(v_flat):
                                    logger.debug(
                                        "Embedding dimension mismatch (q=%d v=%d), skipping chunk",
                                        len(q_flat),
                                        len(v_flat),
                                    )
                                    continue
                                sim = _cosine(q_flat, v_flat)
                                scored.append((sim, {"source_id": source_id, "text": text}))

                            scored.sort(key=lambda t: t[0], reverse=True)
                            normalized_results = []
                            for sim, item in scored[: max_results or 10]:
                                source_id = str(item.get("source_id") or "")
                                text = str(item.get("text") or "")
                                normalized_results.append(
                                    {
                                        "id": f"chunk::{source_id}",
                                        "name": f"chunk::{source_id}",
                                        "summary": text[:400],
                                        "score": float(sim),
                                        "backend": "leanrag",
                                        "content": text,
                                        "source": source_id,
                                        "metadata": {"parent": None},
                                        "extra": {"corpus": str(work_dir), "fallback": "chunk_similarity"},
                                    }
                                )
                            return normalized_results
                        except Exception as e:
                            logger.warning(f"LeanRAG search_nodes fallback failed: {e}")
                            return []

                    # Normalize to CoreResult format
                    normalized_results = []
                    for entity_name, parent, description, source_id in results:
                        # Normalize entity names to match FalkorDB pattern
                        normalized_name = self._normalize_entity_name(entity_name)
                        normalized_parent = self._normalize_entity_name(parent)

                        normalized_results.append({
                            "id": normalized_name,  # Required by CoreResult
                            "name": normalized_name,
                            "summary": description,
                            "score": 0.0,  # Milvus doesn't return scores in current API
                            "backend": "leanrag",  # Required by CoreResult
                            "content": None,  # Entities don't have content
                            "source": source_id,  # Chunk hash where entity was found
                            "metadata": {
                                "parent": normalized_parent,  # Hierarchical parent (for clusters)
                            },
                            "extra": {
                                "corpus": str(work_dir),
                            },
                        })

                    return normalized_results

                finally:
                    os.chdir(original_cwd)

        except ImportError as e:
            raise TransientError(f"Failed to import LeanRAG modules: {e}") from e
        except Exception as e:
            raise BackendError(f"Entity search failed: {e}") from e

    def search_facts(
        self,
        query: str,
        group_ids: Sequence[str] | None = None,
        max_results: int = 10,
        center_node_id: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Search for facts/relationships via entity search + hierarchical edge traversal.

        This implements LeanRAG's reasoning chain pattern from query.py:
        1. Find relevant entities via vector search
        2. Get hierarchical paths for each entity (entity → parent → grandparent → root)
        3. Search for relationships between all entities in those hierarchical paths

        Args:
            query: Search query string
            group_ids: Optional list of group IDs to filter by (ignored - LeanRAG uses separate databases)
            max_results: Maximum number of results to return
            center_node_id: Optional entity name to center search around (not yet implemented)
            **kwargs: Absorbs Graphiti-specific params (active_only, start_time, end_time)
                that have no effect on LeanRAG — LeanRAG has no bi-temporal supersession.

        Returns:
            List of normalized CoreResult dictionaries with fact/edge data

        Raises:
            ConfigError: If work_dir not set or database not indexed
            BackendError: If search fails
            TransientError: If database connection fails
        """
        work_dir = self.config.work_dir
        if not work_dir:
            raise ConfigError("work_dir must be set before searching")

        work_dir = work_dir.resolve()
        if not (work_dir / "threads_chunk.json").exists():
            raise ConfigError(
                f"Database not indexed. threads_chunk.json not found in {work_dir}"
            )

        try:
            # Add LeanRAG to path
            leanrag_abspath = self.config.leanrag_path.resolve()

            # Thread-safe directory change AND sys.path modification
            with _chdir_lock:
                # Add LeanRAG to path inside lock to prevent race conditions
                if str(leanrag_abspath) not in sys.path:
                    sys.path.insert(0, str(leanrag_abspath))

                original_cwd = os.getcwd()
                try:
                    os.chdir(str(self.config.leanrag_path))

                    # Import LeanRAG functions
                    from leanrag.database.adapter import search_nodes_link, find_tree_root
                    from itertools import combinations
                    import logging

                    logger = logging.getLogger(__name__)

                    # Strategy: Find relevant entities via vector search, then traverse
                    # hierarchical relationships (matches LeanRAG query.py get_reasoning_chain)

                    # 1. Find relevant entities (more than requested to increase relationship discovery)
                    entities = self.search_nodes(query, max_results=max_results * 2)

                    # 2. Get hierarchical paths for each entity
                    # find_tree_root returns [entity, parent, grandparent, ..., root]
                    db_name = work_dir.name
                    entity_paths = []
                    for entity in entities:
                        entity_name = entity["id"]
                        path = find_tree_root(db_name, entity_name)
                        if path:
                            entity_paths.append(path)

                    # Performance cap: Limit entity paths to prevent combinatorial explosion
                    # For max_results=10, limit to top 10 paths to keep combinations manageable
                    MAX_ENTITY_PATHS = max(10, max_results)
                    if len(entity_paths) > MAX_ENTITY_PATHS:
                        logger.debug(
                            f"Performance cap hit: Limiting entity paths from {len(entity_paths)} to {MAX_ENTITY_PATHS} "
                            f"(max_results={max_results}). Combinatorics capped to avoid explosion."
                        )
                    entity_paths = entity_paths[:MAX_ENTITY_PATHS]

                    # 3. For each pair of entity paths, search for relationships
                    # between all entities in those paths
                    facts = []
                    seen_edges = set()  # Deduplicate edges (bidirectional)

                    # Performance cap: Limit entities per path pair
                    MAX_ENTITIES_PER_PAIR = 20

                    for path1, path2 in combinations(entity_paths, 2):
                        # Get all unique entities from both paths
                        all_entities = list(set(path1 + path2))

                        # Cap entities per pair to prevent explosion
                        if len(all_entities) > MAX_ENTITIES_PER_PAIR:
                            logger.debug(
                                f"Performance cap hit: Limiting entities per path pair from {len(all_entities)} to {MAX_ENTITIES_PER_PAIR}. "
                                f"Prevents combinatorial explosion in relationship search."
                            )
                        all_entities = all_entities[:MAX_ENTITIES_PER_PAIR]

                        # Search for relationships between all pairs of entities
                        for e1, e2 in combinations(all_entities, 2):
                            if e1 == e2:
                                continue

                            # Early exit if we have enough results
                            # Collect extra to allow for scoring/ranking
                            if len(facts) >= max_results * 3:
                                break

                            # Deduplicate using bidirectional key (frozenset treats {A,B} == {B,A})
                            edge_key = frozenset([e1, e2])
                            if edge_key in seen_edges:
                                continue

                            try:
                                # search_nodes_link returns (src, tgt, description, weight, level)
                                link = search_nodes_link(e1, e2, str(work_dir), level=None)

                                if link:
                                    seen_edges.add(edge_key)
                                    # Preserve original directionality from search_nodes_link
                                    src, tgt, description, weight, level = link
                                    facts.append({
                                        "id": f"{src}||{tgt}",  # Synthetic ID with original direction
                                        "source_node_id": src,  # Original source
                                        "target_node_id": tgt,  # Original target
                                        "summary": description,  # Relationship description
                                        "score": float(weight) if weight else 0.0,
                                        "backend": "leanrag",
                                        "content": None,  # Facts don't have content
                                        "source": None,  # Not applicable to edges
                                        "metadata": {
                                            "level": level,  # Hierarchy level for downstream ranking
                                        },
                                        "extra": {
                                            "corpus": str(work_dir),
                                        },
                                    })
                            except Exception as e:
                                # Log failed link lookups for debugging (may be expected if no relationship exists)
                                logger.debug(f"Link lookup failed for ({e1}, {e2}): {e}")
                                continue

                        # Early exit at path pair level too
                        if len(facts) >= max_results * 3:
                            break

                    # Return top-scored results (sort before truncate ensures best quality)
                    # This is critical: sorting before slicing ensures we return the highest-scored
                    # relationships, not just the first N found (which would be order-dependent)
                    facts.sort(key=lambda x: x["score"], reverse=True)
                    return facts[:max_results]

                finally:
                    os.chdir(original_cwd)

        except ImportError as e:
            raise TransientError(f"Failed to import LeanRAG modules: {e}") from e
        except Exception as e:
            raise BackendError(f"Fact search failed: {e}") from e

    def search_episodes(
        self,
        query: str,
        group_ids: Sequence[str] | None = None,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """Episodes are not supported by LeanRAG.

        LeanRAG chunks are static document segments without provenance information
        (who created/modified content, when changes occurred). Episodes require this
        temporal and actor context which LeanRAG doesn't provide.

        Args:
            query: Search query string (unused)
            group_ids: Optional group IDs (unused)
            max_results: Maximum results (unused)

        Returns:
            Never returns - always raises UnsupportedOperationError

        Raises:
            UnsupportedOperationError: Always raised - LeanRAG doesn't support episodes
        """
        from . import UnsupportedOperationError
        raise UnsupportedOperationError(
            "LeanRAG backend does not support episode search. "
            "Episodes require provenance (who/when) which LeanRAG chunks lack. "
            "LeanRAG chunks are static document segments without actor/time context."
        )

    def get_node(
        self,
        node_id: str,
        group_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get entity node by name (LeanRAG uses names, not UUIDs).

        Args:
            node_id: Entity name to retrieve (e.g., "OAUTH2", "JWT_TOKENS")
            group_id: Optional group ID (ignored - LeanRAG uses separate databases)

        Returns:
            Normalized CoreResult dictionary with node data, or None if not found

        Raises:
            IdNotSupportedError: If node_id is UUID-style (LeanRAG uses entity names)
            ConfigError: If work_dir not set or database not indexed
            BackendError: If retrieval fails
            TransientError: If database connection fails
        """
        # Validate ID format - LeanRAG uses entity names, not UUIDs
        if self._looks_like_uuid(node_id):
            from . import IdNotSupportedError
            raise IdNotSupportedError(
                f"LeanRAG get_node() requires entity names, not UUIDs. "
                f"Received: {node_id[:20]}..."
            )

        work_dir = self.config.work_dir
        if not work_dir:
            raise ConfigError("work_dir must be set before retrieving nodes")

        work_dir = work_dir.resolve()
        if not (work_dir / "threads_chunk.json").exists():
            raise ConfigError(
                f"Database not indexed. threads_chunk.json not found in {work_dir}"
            )

        try:
            # Add LeanRAG to path
            leanrag_abspath = self.config.leanrag_path.resolve()

            # Thread-safe directory change AND sys.path modification
            with _chdir_lock:
                # Add LeanRAG to path inside lock to prevent race conditions
                if str(leanrag_abspath) not in sys.path:
                    sys.path.insert(0, str(leanrag_abspath))

                original_cwd = os.getcwd()
                try:
                    os.chdir(str(self.config.leanrag_path))

                    # Import FalkorDB connection function
                    from leanrag.database.falkordb import get_falkordb_connection

                    # Query entity at ANY level (not just level=0)
                    graph_name = work_dir.name
                    db, graph = get_falkordb_connection(graph_name)

                    try:
                        # Query for entity at any level
                        query = """
                        MATCH (n:Entity {entity_name: $entity_name})
                        RETURN n.entity_name, n.description, n.source_id, n.degree, n.parent, n.level
                        LIMIT 1
                        """

                        result = graph.query(query, params={'entity_name': node_id})

                        if not result.result_set:
                            return None

                        row = result.result_set[0]
                        entity_name, description, source_id, degree, parent, level = row

                        return {
                            "id": entity_name,  # Required by CoreResult
                            "name": entity_name,
                            "summary": description,
                            "score": None,  # Not applicable for direct retrieval
                            "backend": "leanrag",
                            "content": None,  # Entities don't have content
                            "source": source_id,  # Chunk hash where entity was found
                            "metadata": {
                                "parent": parent,  # Hierarchical parent
                                "degree": degree,  # Graph connectivity
                                "level": level,  # Hierarchy level
                            },
                            "extra": {
                                "corpus": str(work_dir),
                            },
                        }
                    finally:
                        db.close()  # Always close database connection

                finally:
                    os.chdir(original_cwd)

        except ImportError as e:
            raise TransientError(f"Failed to import LeanRAG modules: {e}") from e
        except Exception as e:
            raise BackendError(f"Node retrieval failed: {e}") from e

    def _looks_like_uuid(self, value: str) -> bool:
        """Check if a string looks like a UUID or ULID.
        
        Args:
            value: String to check
            
        Returns:
            True if value resembles a UUID/ULID format
        """
        if not value:
            return False
        
        # UUID: 8-4-4-4-12 hex digits with hyphens
        # ULID: 26 alphanumeric characters (base32)
        # Check length and character patterns
        if len(value) == 36 and value.count('-') == 4:
            # Looks like UUID
            return True
        elif len(value) == 26 and value.isalnum() and value.isupper():
            # Looks like ULID
            return True
        
        return False

    def get_edge(
        self,
        edge_id: str,
        group_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get edge/relationship by synthetic ID (SOURCE||TARGET format).

        LeanRAG doesn't have native edge IDs. This method expects edge_id
        in the format "SOURCE||TARGET" where SOURCE and TARGET are entity names.

        Args:
            edge_id: Synthetic edge ID in format "SOURCE||TARGET"
            group_id: Optional group ID (ignored - LeanRAG uses separate databases)

        Returns:
            Normalized CoreResult dictionary with edge data, or None if not found

        Raises:
            IdNotSupportedError: If edge_id format is invalid (must be SOURCE||TARGET)
            ConfigError: If work_dir not set or database not indexed
            BackendError: If retrieval fails
            TransientError: If database connection fails
        """
        # Validate edge_id format
        if "||" not in edge_id or not edge_id.strip():
            from . import IdNotSupportedError
            raise IdNotSupportedError(
                f"LeanRAG get_edge() requires synthetic edge IDs in format SOURCE||TARGET. "
                f"Received: {edge_id[:50]}"
            )

        # Validate that split produces non-empty entity names
        parts = edge_id.split("||", 1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            from . import IdNotSupportedError
            raise IdNotSupportedError(
                f"LeanRAG get_edge() requires synthetic edge IDs in format SOURCE||TARGET. "
                f"Received: {edge_id[:50]}"
            )

        work_dir = self.config.work_dir
        if not work_dir:
            raise ConfigError("work_dir must be set before retrieving edges")

        work_dir = work_dir.resolve()
        if not (work_dir / "threads_chunk.json").exists():
            raise ConfigError(
                f"Database not indexed. threads_chunk.json not found in {work_dir}"
            )

        try:
            # Add LeanRAG to path
            leanrag_abspath = self.config.leanrag_path.resolve()

            # Thread-safe directory change AND sys.path modification
            with _chdir_lock:
                # Add LeanRAG to path inside lock to prevent race conditions
                if str(leanrag_abspath) not in sys.path:
                    sys.path.insert(0, str(leanrag_abspath))

                original_cwd = os.getcwd()
                try:
                    os.chdir(str(self.config.leanrag_path))

                    # Import LeanRAG function
                    from leanrag.database.adapter import search_nodes_link

                    # Parse synthetic ID (already validated above)
                    source, target = parts

                    # Retrieve relationship
                    # search_nodes_link returns (src, tgt, description, weight, level)
                    result = search_nodes_link(source, target, str(work_dir), level=None)

                    if not result:
                        return None

                    src, tgt, description, weight, level = result

                    return {
                        "id": edge_id,  # Required by CoreResult
                        "source_node_id": src,
                        "target_node_id": tgt,
                        "summary": description,
                        "score": float(weight) if weight else 0.0,
                        "backend": "leanrag",
                        "content": None,  # Edges don't have content
                        "source": None,  # Not applicable to edges
                        "metadata": {
                            "level": level,
                        },
                        "extra": {
                            "corpus": str(work_dir),
                        },
                    }

                finally:
                    os.chdir(original_cwd)

        except ImportError as e:
            raise TransientError(f"Failed to import LeanRAG modules: {e}") from e
        except Exception as e:
            raise BackendError(f"Edge retrieval failed: {e}") from e

    def get_capabilities(self) -> Capabilities:
        """Return LeanRAG capabilities with Phase 1 protocol extensions."""
        return Capabilities(
            # Existing capabilities
            embeddings=bool(self.config.embedding_api_base),
            entity_extraction=True,
            graph_query=True,
            rerank=False,
            schema_versions=["1.0.0"],
            supports_falkor=True,
            supports_milvus=bool(self.config.embedding_api_base),
            supports_neo4j=False,
            max_tokens=768,
            
            # Phase 1 protocol extensions
            supports_nodes=True,       # ✅ Via Milvus vector search on entity embeddings
            supports_facts=True,       # ✅ Via entity search + relationship traversal
            supports_episodes=False,   # ❌ No provenance (chunks are static segments)
            supports_chunks=False,     # ❌ Not yet implemented (will be added in future phase)
            supports_edges=True,       # ✅ Via synthetic SOURCE||TARGET ID format
            
            # ID modality (how LeanRAG identifies entities/edges)
            node_id_type="name",       # Entity names (e.g., "OAUTH2", "JWT_TOKENS")
            edge_id_type="synthetic",  # SOURCE||TARGET format (no native edge IDs)
        )
