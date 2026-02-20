"""Pipeline stage implementations."""

import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from watercooler.baseline_graph import storage as graph_storage
from watercooler.baseline_graph.storage import get_graph_dir

from ..graph import MemoryGraph, GraphConfig
from ..leanrag_export import export_to_leanrag
from .config import PipelineConfig
from .state import Stage, PipelineState
from .logging import PipelineLogger


class StageError(Exception):
    """Error during stage execution."""

    pass


# Patterns for redacting sensitive data in logs
# Ordered from most specific to least specific
_SENSITIVE_PATTERNS = [
    # Environment variable assignments with sensitive names
    (re.compile(r"(DEEPSEEK_API_KEY|API_KEY|SECRET|PASSWORD|TOKEN|CREDENTIAL)=\S+", re.I), r"\1=[REDACTED]"),
    # Common API key prefixes (sk- for OpenAI/Anthropic, api-, key-)
    (re.compile(r"(sk-|api-|key-)[a-zA-Z0-9]{20,}"), "[REDACTED_KEY]"),
    # JWT tokens (three base64 sections separated by dots, starts with eyJ)
    (re.compile(r"eyJ[a-zA-Z0-9_-]*\.eyJ[a-zA-Z0-9_-]*\.[a-zA-Z0-9_-]*"), "[REDACTED_JWT]"),
    # Bearer tokens in headers
    (re.compile(r"(Bearer\s+)[a-zA-Z0-9_-]{20,}", re.I), r"\1[REDACTED_TOKEN]"),
    # X-API-Key and similar headers
    (re.compile(r"(X-API-Key[:\s]+)[a-zA-Z0-9_-]{16,}", re.I), r"\1[REDACTED_KEY]"),
    # AWS-style keys (AKIA prefix)
    (re.compile(r"AKIA[A-Z0-9]{16,}"), "[REDACTED_AWS_KEY]"),
    # URL credentials (https://user:password@host)
    (re.compile(r"(https?://[^:]+:)[^@]+(@)"), r"\1[REDACTED]\2"),
    # Basic auth header (base64 encoded)
    (re.compile(r"(Basic\s+)[A-Za-z0-9+/=]{20,}", re.I), r"\1[REDACTED_BASE64]"),
]


def _redact_sensitive(text: str) -> str:
    """Redact sensitive information from text (API keys, passwords, etc.)."""
    for pattern, replacement in _SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _run_subprocess_with_timeout(
    cmd: list[str],
    cwd: str,
    env: dict[str, str],
    timeout: int,
    operation_name: str,
) -> subprocess.CompletedProcess:
    """Run subprocess with proper timeout handling that kills orphaned processes.

    Uses process groups on Unix to ensure all child processes are killed on timeout.

    Args:
        cmd: Command and arguments to run
        cwd: Working directory for the subprocess
        env: Environment variables
        timeout: Timeout in seconds
        operation_name: Name of operation for error messages

    Returns:
        CompletedProcess with stdout/stderr

    Raises:
        StageError: If timeout expires (after killing the process tree)
    """
    # On Unix, use process groups to kill entire process tree on timeout
    # On Windows, start_new_session is not supported the same way
    use_process_group = sys.platform != "win32"

    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=use_process_group,
    )

    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    except subprocess.TimeoutExpired:
        # Kill the entire process group (or just the process on Windows)
        if use_process_group:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # Process already terminated
        else:
            process.kill()
        # Wait for process to fully terminate and collect output
        stdout, stderr = process.communicate()
        timeout_mins = timeout // 60
        raise StageError(
            f"{operation_name} timed out after {timeout_mins} minutes. "
            "Process tree was terminated."
        )


def _build_leanrag_env(config: PipelineConfig) -> dict[str, str]:
    """Build environment dict with LeanRAG API credentials from pipeline config.

    Sets LLM and embedding environment variables required by LeanRAG's
    config_loader at import time.

    Args:
        config: Pipeline configuration with LLM and embedding settings.

    Returns:
        Copy of os.environ with LeanRAG variables set.
    """
    env = os.environ.copy()
    env["DEEPSEEK_API_KEY"] = config.llm.api_key
    env["DEEPSEEK_BASE_URL"] = config.llm.base_url
    env["LLM_API_BASE"] = config.llm.base_url
    env["DEEPSEEK_MODEL"] = config.llm.model
    env["GLM_MODEL"] = config.embedding.model
    env["GLM_EMBEDDING_MODEL"] = config.embedding.model
    env["GLM_BASE_URL"] = config.embedding.base_url
    env["EMBEDDING_API_BASE"] = config.embedding.base_url
    env["EMBEDDING_BATCH_SIZE"] = str(config.embedding.batch_size)
    return env


class StageRunner:
    """Base class for stage runners."""

    stage: Stage

    def __init__(self, config: PipelineConfig, state: PipelineState, logger: PipelineLogger):
        self.config = config
        self.state = state
        self.logger = logger

    def run(self) -> dict[str, Any]:
        """Run the stage. Returns outputs dict."""
        raise NotImplementedError

    def validate_inputs(self) -> list[str]:
        """Validate inputs are available. Returns list of errors."""
        return []


class ExportStageRunner(StageRunner):
    """Export stage: threads → LeanRAG format."""

    stage = Stage.EXPORT

    def validate_inputs(self) -> list[str]:
        errors = []
        threads_dir = self.config.threads_dir

        # Check for .watercooler subdirectory
        if (threads_dir / ".watercooler").is_dir():
            threads_dir = threads_dir / ".watercooler"

        if not threads_dir.exists():
            errors.append(f"Threads directory not found: {threads_dir}")
        elif not graph_storage.list_thread_topics(get_graph_dir(threads_dir)):
            errors.append(f"No threads found in graph for {threads_dir}")

        return errors

    def run(self) -> dict[str, Any]:
        """Run export stage."""
        threads_dir = self.config.threads_dir

        # Check for .watercooler subdirectory
        if (threads_dir / ".watercooler").is_dir():
            threads_dir = threads_dir / ".watercooler"

        self.logger.info(f"Threads directory: {threads_dir}")

        # Log thread filtering if active
        if self.config.thread_filter:
            self.logger.info(f"Thread filter active: processing {len(self.config.thread_filter)} threads")
            for thread_file in self.config.thread_filter:
                self.logger.debug(f"  - {thread_file}")

        # Build memory graph (skip embeddings - not needed for export)
        graph_config = GraphConfig(generate_embeddings=False)
        graph = MemoryGraph(graph_config)

        def progress_cb(current: int, total: int, message: str) -> None:
            self.logger.progress(current, total, message)

        with self.logger.timed("graph_build"):
            graph.build(
                threads_dir,
                progress_callback=progress_cb,
                thread_filter=self.config.thread_filter,
            )

        stats = graph.stats()
        self.logger.info(f"Graph built: {stats['threads']} threads, {stats['entries']} entries, {stats['chunks']} chunks")

        # Record stats
        self.logger.record_stat("threads_processed", stats["threads"])
        self.logger.record_stat("entries_processed", stats["entries"])
        self.logger.record_stat("chunks_created", stats["chunks"])

        # Apply test limit if enabled
        if self.config.test_mode:
            limit = self.config.test_limit
            self.logger.warning(f"Test mode: limiting to {limit} threads")
            # Limit by threads, keeping all entries from selected threads
            if len(graph.threads) > limit:
                limited_thread_ids = set(list(graph.threads.keys())[:limit])
                graph.threads = {k: v for k, v in graph.threads.items() if k in limited_thread_ids}
                # Keep only entries belonging to selected threads
                graph.entries = {k: v for k, v in graph.entries.items() if v.thread_id in limited_thread_ids}
                # Rebuild chunks for limited entries
                graph.chunks = {}
                graph.chunk_all_entries()
                self.logger.info(f"Limited to {len(graph.threads)} threads, {len(graph.entries)} entries, {len(graph.chunks)} chunks")

        # Export to LeanRAG format
        export_dir = self.config.work_dir / "export"
        export_dir.mkdir(parents=True, exist_ok=True)

        with self.logger.timed("leanrag_export"):
            manifest = export_to_leanrag(graph, export_dir, include_embeddings=False)

        self.logger.info(f"Exported: {manifest['statistics']['documents']} documents, {manifest['statistics']['chunks']} chunks")
        self.logger.record_stat("documents_exported", manifest["statistics"]["documents"])

        return {
            "export_dir": str(export_dir),
            "documents_file": str(export_dir / "documents.json"),
            "threads_file": str(export_dir / "threads.json"),
            "manifest_file": str(export_dir / "manifest.json"),
            "statistics": manifest["statistics"],
        }


class ExtractStageRunner(StageRunner):
    """Extract stage: entity/relation extraction using LeanRAG."""

    stage = Stage.EXTRACT

    def validate_inputs(self) -> list[str]:
        errors = []

        # Check export outputs exist
        export_state = self.state.get_stage(Stage.EXPORT)
        if not export_state.outputs.get("documents_file"):
            errors.append("Export stage outputs not found")
        elif not Path(export_state.outputs["documents_file"]).exists():
            errors.append(f"Documents file not found: {export_state.outputs['documents_file']}")

        # Check LeanRAG is available
        if not self.config.leanrag_dir:
            errors.append("LEANRAG_DIR not configured")
        elif not self.config.leanrag_dir.exists():
            errors.append(f"LeanRAG directory not found: {self.config.leanrag_dir}")
        elif not (self.config.leanrag_dir / "leanrag/pipelines/process.py").exists():
            errors.append("LeanRAG pipeline script not found at leanrag/pipelines/process.py")

        # Check LLM config
        errors.extend(self.config.llm.validate())

        return errors

    def run(self) -> dict[str, Any]:
        """Run extract stage using LeanRAG pipeline."""
        export_state = self.state.get_stage(Stage.EXPORT)
        export_dir = Path(export_state.outputs["export_dir"])

        # Output directories
        extract_dir = self.config.work_dir / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)

        working_dir = extract_dir / "kg_working"
        working_dir.mkdir(parents=True, exist_ok=True)

        # We need to convert our documents.json to markdown files for LeanRAG
        # or modify LeanRAG to accept our format directly
        # For now, let's create a markdown directory from documents.json
        md_dir = extract_dir / "markdown"
        md_dir.mkdir(parents=True, exist_ok=True)

        # Convert documents.json to markdown files
        docs_file = export_dir / "documents.json"
        with self.logger.timed("file_read", file=str(docs_file)):
            with open(docs_file) as f:
                documents = json.load(f)

        self.logger.info(f"Converting {len(documents)} documents to markdown")

        with self.logger.timed("markdown_conversion", doc_count=len(documents)):
            # Track used filenames to avoid collisions
            used_names: set[str] = set()

            for doc in documents:
                doc_id = doc["doc_id"]
                title = doc.get("title", "Untitled")
                content = doc.get("content", "")
                metadata = doc.get("metadata", {})

                # Create markdown with metadata header
                md_content = f"# {title}\n\n"
                md_content += f"**Thread:** {metadata.get('thread_id', 'unknown')}\n"
                md_content += f"**Agent:** {metadata.get('agent', 'unknown')}\n"
                md_content += f"**Role:** {metadata.get('role', 'unknown')}\n"
                md_content += f"**Type:** {metadata.get('entry_type', 'Note')}\n"
                md_content += f"**Timestamp:** {metadata.get('timestamp', '')}\n\n"
                md_content += "---\n\n"
                md_content += content

                # Write to file (sanitize doc_id for safe filename)
                # Only allow alphanumeric, dash, underscore
                # Strip leading underscores to prevent hidden files
                # Limit length to avoid filesystem issues (max 200 chars before .md extension)
                safe_id = re.sub(r"[^\w-]", "_", doc_id).lstrip("_")[:200]
                if not safe_id:
                    safe_id = "unnamed"

                # Handle filename collisions with counter suffix
                base_id = safe_id
                counter = 0
                while safe_id in used_names:
                    counter += 1
                    safe_id = f"{base_id}_{counter}"
                used_names.add(safe_id)

                md_path = md_dir / f"{safe_id}.md"
                md_path.write_text(md_content)

        self.logger.info(f"Created {len(documents)} markdown files in {md_dir}")

        # Run LeanRAG pipeline
        leanrag_dir = self.config.leanrag_dir
        pipeline_script = leanrag_dir / "leanrag/pipelines/process.py"

        cmd = [
            sys.executable,
            str(pipeline_script),
            "--input-dir",
            str(md_dir),
            "--output-dir",
            str(extract_dir),
            "--working-dir",
            str(working_dir),
            "--max-tokens",
            str(self.config.max_tokens),
            "--overlap-tokens",
            str(self.config.overlap_tokens),
        ]

        self.logger.info(f"Running LeanRAG pipeline: {' '.join(cmd)}")

        env = _build_leanrag_env(self.config)

        with self.logger.timed("llm_call", operation="leanrag_extraction"):
            result = _run_subprocess_with_timeout(
                cmd=cmd,
                cwd=str(leanrag_dir),
                env=env,
                timeout=3600,  # 1 hour timeout
                operation_name="LeanRAG pipeline",
            )

            # Log output
            if result.stdout:
                for line in result.stdout.split("\n"):
                    if line.strip():
                        self.logger.debug(line)

            if result.returncode != 0:
                self.logger.error(f"LeanRAG pipeline failed: {_redact_sensitive(result.stderr)}")
                raise StageError(f"LeanRAG pipeline failed with code {result.returncode}")

        # Verify outputs
        entity_file = working_dir / "entity.jsonl"
        relation_file = working_dir / "relation.jsonl"

        if not entity_file.exists():
            raise StageError(f"Entity file not created: {entity_file}")

        # Count entities and relations
        with open(entity_file) as f:
            entity_count = sum(1 for _ in f)
        if relation_file.exists():
            with open(relation_file) as f:
                relation_count = sum(1 for _ in f)
        else:
            relation_count = 0

        self.logger.info(f"Extracted: {entity_count} entities, {relation_count} relations")

        # Record stats
        self.logger.record_stat("entities_extracted", entity_count)
        self.logger.record_stat("relations_extracted", relation_count)

        return {
            "working_dir": str(working_dir),
            "entity_file": str(entity_file),
            "relation_file": str(relation_file),
            "entity_count": entity_count,
            "relation_count": relation_count,
        }


class DedupeStageRunner(StageRunner):
    """Dedupe stage: deduplicate and summarize entities."""

    stage = Stage.DEDUPE

    def validate_inputs(self) -> list[str]:
        errors = []

        # Check extract outputs exist
        extract_state = self.state.get_stage(Stage.EXTRACT)
        if not extract_state.outputs.get("entity_file"):
            errors.append("Extract stage outputs not found")
        elif not Path(extract_state.outputs["entity_file"]).exists():
            errors.append(f"Entity file not found: {extract_state.outputs['entity_file']}")

        # Check LeanRAG is available
        if not self.config.leanrag_dir:
            errors.append("LEANRAG_DIR not configured")
        elif not (self.config.leanrag_dir / "GraphExtraction" / "deal_triple.py").exists():
            errors.append("LeanRAG deal_triple.py not found")

        return errors

    def run(self) -> dict[str, Any]:
        """Run dedupe stage using LeanRAG."""
        extract_state = self.state.get_stage(Stage.EXTRACT)
        working_dir = Path(extract_state.outputs["working_dir"])

        # Output directory
        processed_dir = self.config.work_dir / "graph" / "processed"
        processed_dir.mkdir(parents=True, exist_ok=True)

        # Run LeanRAG deal_triple.py
        leanrag_dir = self.config.leanrag_dir
        deal_triple_script = leanrag_dir / "GraphExtraction" / "deal_triple.py"

        cmd = [
            sys.executable,
            str(deal_triple_script),
            "--working-dir",
            str(working_dir),
            "--output-path",
            str(processed_dir),
        ]

        self.logger.info(f"Running entity deduplication: {' '.join(cmd)}")

        env = _build_leanrag_env(self.config)

        with self.logger.timed("llm_call", operation="entity_deduplication"):
            result = _run_subprocess_with_timeout(
                cmd=cmd,
                cwd=str(leanrag_dir),
                env=env,
                timeout=1800,  # 30 minute timeout
                operation_name="Entity deduplication",
            )

            if result.stdout:
                for line in result.stdout.split("\n"):
                    if line.strip():
                        self.logger.debug(line)

            if result.returncode != 0:
                self.logger.error(f"Deduplication failed: {_redact_sensitive(result.stderr)}")
                raise StageError(f"Deduplication failed with code {result.returncode}")

        # Verify outputs
        entity_file = processed_dir / "entity.jsonl"
        if not entity_file.exists():
            raise StageError(f"Processed entity file not created: {entity_file}")

        # Count
        with open(entity_file) as f:
            entity_count = sum(1 for _ in f)
        relation_file = processed_dir / "relation.jsonl"
        if relation_file.exists():
            with open(relation_file) as f:
                relation_count = sum(1 for _ in f)
        else:
            relation_count = 0

        self.logger.info(f"Deduplicated: {entity_count} entities, {relation_count} relations")

        # Record stats
        self.logger.record_stat("entities_deduplicated", entity_count)

        return {
            "processed_dir": str(processed_dir),
            "entity_file": str(entity_file),
            "relation_file": str(relation_file),
            "entity_count": entity_count,
            "relation_count": relation_count,
        }


class BuildStageRunner(StageRunner):
    """Build stage: build knowledge graph with embeddings and clustering."""

    stage = Stage.BUILD

    def validate_inputs(self) -> list[str]:
        errors = []

        # Check dedupe outputs exist
        dedupe_state = self.state.get_stage(Stage.DEDUPE)
        if not dedupe_state.outputs.get("processed_dir"):
            errors.append("Dedupe stage outputs not found")
        elif not Path(dedupe_state.outputs["processed_dir"]).exists():
            errors.append(f"Processed directory not found: {dedupe_state.outputs['processed_dir']}")

        # Check LeanRAG is available
        if not self.config.leanrag_dir:
            errors.append("LEANRAG_DIR not configured")
        elif not (self.config.leanrag_dir / "build_graph.py").exists():
            errors.append("LeanRAG build_graph.py not found")

        # Check embedding service
        errors.extend(self.config.embedding.validate())

        return errors

    def run(self) -> dict[str, Any]:
        """Run build stage using LeanRAG."""
        dedupe_state = self.state.get_stage(Stage.DEDUPE)
        processed_dir = Path(dedupe_state.outputs["processed_dir"])

        # Run LeanRAG build_graph.py
        leanrag_dir = self.config.leanrag_dir
        build_script = leanrag_dir / "build_graph.py"

        # Determine parallelism
        num_workers = max(1, self.config.max_concurrent // 4)

        cmd = [
            sys.executable,
            str(build_script),
            "--path",
            str(processed_dir),
            "--num",
            str(num_workers),
        ]

        self.logger.info(f"Running graph build: {' '.join(cmd)}")

        env = _build_leanrag_env(self.config)
        # Force sequential embedding to work with llama-cpp server (can't handle concurrent requests)
        env["EMBEDDING_MAX_WORKERS"] = "1"

        with self.logger.timed("embedding_call", operation="graph_build_embeddings"):
            result = _run_subprocess_with_timeout(
                cmd=cmd,
                cwd=str(leanrag_dir),
                env=env,
                timeout=7200,  # 2 hour timeout
                operation_name="Graph build",
            )

            if result.stdout:
                for line in result.stdout.split("\n"):
                    if line.strip():
                        self.logger.debug(line)

            # Check for essential outputs BEFORE failing on return code
            # LeanRAG's build_graph.py may fail at MySQL step after creating all critical outputs
            all_entities_file = processed_dir / "all_entities.json"
            milvus_db_file = processed_dir / "milvus_demo.db"

            has_essential_outputs = (
                all_entities_file.exists()
                and all_entities_file.stat().st_size > 0
                and milvus_db_file.exists()
                and milvus_db_file.stat().st_size > 0
            )

            if result.returncode != 0:
                stderr_redacted = _redact_sensitive(result.stderr)
                if has_essential_outputs:
                    # MySQL or other non-critical step failed, but graph is built
                    self.logger.warning(
                        f"Graph build subprocess returned code {result.returncode}, "
                        "but essential outputs exist - treating as success"
                    )
                    if "mysql" in result.stderr.lower() or "Access denied" in result.stderr:
                        self.logger.warning(
                            "MySQL connection failed (this is expected without MySQL setup). "
                            "Vector DB (Milvus) was created successfully."
                        )
                else:
                    self.logger.error(f"Graph build failed: {stderr_redacted}")
                    raise StageError(f"Graph build failed with code {result.returncode}")

        # Verify outputs
        all_entities_file = processed_dir / "all_entities.json"
        if not all_entities_file.exists():
            raise StageError(f"All entities file not created: {all_entities_file}")

        # Load stats - file may contain multiple JSON objects (one per line for hierarchy levels)
        all_entities = []
        with open(all_entities_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        data = json.loads(line)
                        if isinstance(data, list):
                            all_entities.append(data)
                    except json.JSONDecodeError as e:
                        self.logger.warning(f"Skipping invalid JSON line: {e}")
                        continue

        layers = len(all_entities)
        total_entities = sum(len(layer) for layer in all_entities)

        self.logger.info(f"Built graph: {layers} layers, {total_entities} total entities")

        # Record stats - total_entities represents embedded entities
        self.logger.record_stat("embeddings_generated", total_entities)

        return {
            "graph_dir": str(processed_dir),
            "all_entities_file": str(all_entities_file),
            "layers": layers,
            "total_entities": total_entities,
        }


def get_runner(stage: Stage, config: PipelineConfig, state: PipelineState, logger: PipelineLogger) -> StageRunner:
    """Get the appropriate runner for a stage."""
    runners = {
        Stage.EXPORT: ExportStageRunner,
        Stage.EXTRACT: ExtractStageRunner,
        Stage.DEDUPE: DedupeStageRunner,
        Stage.BUILD: BuildStageRunner,
    }

    runner_class = runners.get(stage)
    if not runner_class:
        raise ValueError(f"No runner for stage: {stage}")

    return runner_class(config, state, logger)
