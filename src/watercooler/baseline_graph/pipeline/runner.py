"""Baseline graph pipeline runner."""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .config import PipelineConfig
from .state import PipelineState
from .. import storage


@dataclass
class PipelineResult:
    """Result of pipeline execution."""

    success: bool
    threads_processed: int
    entries_processed: int
    nodes_created: int
    edges_created: int
    embeddings_generated: int
    duration_seconds: float
    output_dir: Path
    error: Optional[str] = None


class BaselineGraphRunner:
    """Runs the baseline graph construction pipeline."""

    def __init__(
        self,
        config: PipelineConfig,
        verbose: bool = False,
        auto_server: bool = True,
        stop_servers: bool = False,
        auto_approve: bool = False,
    ):
        self.config = config
        self.verbose = verbose
        self.auto_server = auto_server
        self.stop_servers = stop_servers
        self.auto_approve = auto_approve
        self._server_manager = None
        self._state = None
        self._changed_topics = set()  # Topics that need reprocessing

    def _log(self, msg: str) -> None:
        """Log a message."""
        print(msg, flush=True)

    def _log_verbose(self, msg: str) -> None:
        """Log a verbose message."""
        if self.verbose:
            print(f"  {msg}", flush=True)

    def _get_server_manager(self):
        """Get or create server manager."""
        if self._server_manager is None:
            try:
                from watercooler_memory.pipeline.server_manager import ServerManager
                self._server_manager = ServerManager(
                    llm_api_base=self.config.llm.api_base,
                    embedding_api_base=self.config.embedding.api_base,
                    interactive=True,
                    auto_approve=self.auto_approve,
                    verbose=self.verbose,
                )
            except ImportError:
                self._log("Warning: ServerManager not available, skipping auto-server")
                return None
        return self._server_manager

    def _ensure_servers(self) -> bool:
        """Ensure required servers are running."""
        if not self.auto_server:
            return True

        manager = self._get_server_manager()
        if manager is None:
            return True  # No manager, assume servers are external

        servers_needed = []
        if not self.config.extractive_only:
            servers_needed.append("llm")
        if not self.config.skip_embeddings:
            servers_needed.append("embedding")

        if not servers_needed:
            return True

        self._log("Checking server availability...")

        if "llm" in servers_needed and not manager.check_llm_server():
            if not manager.start_llm_server():
                return False

        if "embedding" in servers_needed and not manager.check_embedding_server():
            if not manager.start_embedding_server():
                return False

        return True

    def _stop_servers_if_needed(self) -> None:
        """Stop servers if requested."""
        if self.stop_servers and self._server_manager:
            self._log("Stopping servers...")
            self._server_manager.stop_servers()

    def _clear_cache(self) -> None:
        """Clear output directory if fresh mode."""
        if self.config.fresh and self.config.output_dir.exists():
            self._log(f"Clearing cached results: {self.config.output_dir}")
            shutil.rmtree(self.config.output_dir)

    def _state_path(self) -> Path:
        """Get path to state file."""
        return self.config.output_dir / "state.json"

    def _load_state(self) -> None:
        """Load pipeline state for incremental builds."""
        if not self.config.incremental:
            self._state = PipelineState()
            return

        if self.config.fresh:
            self._log("Fresh mode: ignoring cached state")
            self._state = PipelineState()
            return

        state_path = self._state_path()
        self._state = PipelineState.load(state_path)
        if self._state.last_run:
            self._log(f"Loaded state from {self._state.last_run}")
        else:
            self._log("No previous state found, will process all threads")

    def _save_state(self, threads: List[Any]) -> None:
        """Save pipeline state after successful run."""
        if not self.config.incremental:
            return

        # Update state for all processed threads
        current_topics = set()
        for thread in threads:
            topic = thread.topic
            current_topics.add(topic)

            # Get thread file mtime
            thread_path = self.config.threads_dir / f"{topic}.md"
            mtime = thread_path.stat().st_mtime if thread_path.exists() else 0

            # Collect entry summaries and embeddings
            entry_summaries = {}
            entry_embeddings = {}
            for entry in thread.entries:
                entry_id = entry.entry_id
                if hasattr(entry, 'summary') and entry.summary:
                    entry_summaries[entry_id] = entry.summary
                if hasattr(entry, 'embedding') and entry.embedding:
                    entry_embeddings[entry_id] = entry.embedding

            self._state.update_thread(
                topic=topic,
                mtime=mtime,
                entry_count=len(thread.entries),
                summary=thread.summary or "",
                entry_summaries=entry_summaries,
                entry_embeddings=entry_embeddings,
            )

        # Remove deleted threads from state
        removed = self._state.remove_deleted_threads(current_topics)
        if removed:
            self._log_verbose(f"Removed {len(removed)} deleted threads from state")

        # Save state
        self._state.save(self._state_path())
        self._log_verbose(f"Saved state to {self._state_path()}")

    def _parse_threads(self) -> List[Any]:
        """Parse threads from directory.

        In incremental mode, detects changed threads and applies cached
        summaries/embeddings for unchanged ones.
        """
        from ..parser import iter_threads

        self._log(f"Parsing threads from: {self.config.threads_dir}")

        # Use iter_threads with generate_summaries=False since we handle that separately
        threads = []
        changed_count = 0
        cached_count = 0

        for thread in iter_threads(
            self.config.threads_dir,
            config=None,
            generate_summaries=False,
            skip_closed=self.config.skip_closed,
        ):
            # Check if thread changed (incremental mode)
            if self.config.incremental and self._state:
                thread_path = self.config.threads_dir / f"{thread.topic}.md"
                mtime = thread_path.stat().st_mtime if thread_path.exists() else 0

                if self._state.is_thread_changed(thread.topic, mtime, len(thread.entries)):
                    # Thread changed, mark for reprocessing
                    self._changed_topics.add(thread.topic)
                    changed_count += 1
                    self._log_verbose(f"Thread changed: {thread.topic}")
                else:
                    # Apply cached data
                    cached_count += 1
                    cached_summary = self._state.get_cached_summary(thread.topic)
                    if cached_summary:
                        thread.summary = cached_summary

                    # Apply cached entry data
                    for entry in thread.entries:
                        cached_entry_summary = self._state.get_cached_entry_summary(
                            thread.topic, entry.entry_id
                        )
                        if cached_entry_summary:
                            entry.summary = cached_entry_summary

                        cached_embedding = self._state.get_cached_entry_embedding(
                            thread.topic, entry.entry_id
                        )
                        if cached_embedding:
                            entry.embedding = cached_embedding
            else:
                # Non-incremental mode: all threads need processing
                self._changed_topics.add(thread.topic)

            threads.append(thread)
            if self.config.test_limit and len(threads) >= self.config.test_limit:
                break

        if self.config.test_limit:
            self._log(f"  Limited to {len(threads)} threads (test mode)")
        elif self.config.incremental:
            self._log(f"  Found {len(threads)} threads ({changed_count} changed, {cached_count} cached)")
        else:
            self._log(f"  Found {len(threads)} threads")

        return threads

    def _summarize_entries(self, threads: List[Any]) -> None:
        """Generate summaries for entries.

        In incremental mode, only processes entries from changed threads.
        """
        # Filter to entries that need summarization
        entries_needing_summary = []
        for thread in threads:
            # Skip unchanged threads in incremental mode
            if self.config.incremental and thread.topic not in self._changed_topics:
                continue
            for entry in thread.entries:
                if not hasattr(entry, 'summary') or not entry.summary:
                    entries_needing_summary.append((thread, entry))

        total_to_process = len(entries_needing_summary)
        if total_to_process == 0:
            self._log("All entries already have summaries")
            return

        if self.config.extractive_only:
            from ..summarizer import extractive_summary

            self._log(f"Generating extractive summaries for {total_to_process} entries...")

            for i, (thread, entry) in enumerate(entries_needing_summary, 1):
                if i % 10 == 0 or i == total_to_process:
                    self._log_verbose(f"Summarizing entry {i}/{total_to_process}")

                entry.summary = extractive_summary(
                    entry.body,
                    max_chars=200,
                    include_headers=True,
                )
            return

        from concurrent.futures import ThreadPoolExecutor, as_completed
        from ..summarizer import summarize_entry, SummarizerConfig

        summarizer_config = SummarizerConfig(
            api_base=self.config.llm.api_base,
            model=self.config.llm.model,
            api_key=self.config.llm.api_key,
            timeout=self.config.llm.timeout,
            max_tokens=self.config.llm.max_tokens,
        )

        self._log(f"Generating LLM summaries for {total_to_process} entries...")

        # Extract just the entries from the (thread, entry) tuples
        entries_to_summarize = [entry for _, entry in entries_needing_summary]

        def summarize_one(entry):
            return entry, summarize_entry(
                entry.body,
                entry_title=entry.title,
                entry_type=getattr(entry, 'entry_type', None),
                config=summarizer_config,
            )

        # Parallelize with configurable workers (default 4)
        max_workers = getattr(self.config, 'llm_workers', 4)
        processed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(summarize_one, e): e for e in entries_to_summarize}
            for future in as_completed(futures):
                processed += 1
                if processed % 10 == 0 or processed == total_to_process:
                    self._log_verbose(f"Summarizing entry {processed}/{total_to_process}")
                entry, summary = future.result()
                entry.summary = summary

    def _summarize_threads(self, threads: List[Any]) -> None:
        """Generate summaries for threads based on their entries.

        Thread summaries provide a high-level overview of the discussion,
        key decisions, and outcomes. They are generated after entry summaries
        are available. In incremental mode, only processes changed threads.
        """
        # Filter to threads that need summarization
        threads_needing_summary = []
        for thread in threads:
            # Skip unchanged threads in incremental mode
            if self.config.incremental and thread.topic not in self._changed_topics:
                continue
            if not thread.summary:
                threads_needing_summary.append(thread)

        total_to_process = len(threads_needing_summary)
        if total_to_process == 0:
            self._log("All threads already have summaries")
            return

        if self.config.extractive_only:
            from ..summarizer import extractive_summary

            self._log(f"Generating extractive thread summaries for {total_to_process} threads...")

            for i, thread in enumerate(threads_needing_summary, 1):
                if i % 10 == 0 or i == total_to_process:
                    self._log_verbose(f"Summarizing thread {i}/{total_to_process}")

                # Concatenate entry summaries or titles for extractive summary
                entry_texts = []
                for entry in thread.entries[:10]:  # Limit to first 10 entries
                    if hasattr(entry, 'summary') and entry.summary:
                        entry_texts.append(f"- {entry.title}: {entry.summary[:100]}")
                    else:
                        entry_texts.append(f"- {entry.title}: {entry.body[:100]}")

                if entry_texts:
                    combined = "\n".join(entry_texts)
                    thread.summary = extractive_summary(
                        combined,
                        max_chars=300,
                        include_headers=False,
                    )
            return

        from concurrent.futures import ThreadPoolExecutor, as_completed
        from ..summarizer import summarize_thread, SummarizerConfig

        summarizer_config = SummarizerConfig(
            api_base=self.config.llm.api_base,
            model=self.config.llm.model,
            api_key=self.config.llm.api_key,
            timeout=self.config.llm.timeout,
            max_tokens=self.config.llm.max_tokens,
        )

        self._log(f"Generating LLM thread summaries for {total_to_process} threads...")

        def summarize_one(thread):
            entry_dicts = [
                {"body": e.body, "title": e.title, "type": e.entry_type}
                for e in thread.entries
            ]
            return thread, summarize_thread(
                entry_dicts,
                thread_title=thread.title,
                config=summarizer_config,
            )

        # Parallelize with configurable workers (default 4)
        max_workers = getattr(self.config, 'llm_workers', 4)
        processed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(summarize_one, t): t for t in threads_needing_summary}
            for future in as_completed(futures):
                processed += 1
                if processed % 10 == 0 or processed == total_to_process:
                    self._log_verbose(f"Summarizing thread {processed}/{total_to_process}")
                thread, summary = future.result()
                thread.summary = summary

    def _generate_embeddings(self, threads: List[Any]) -> int:
        """Generate embeddings for entries.

        In incremental mode, only processes entries from changed threads
        that don't already have embeddings.
        """
        if self.config.skip_embeddings:
            self._log("Skipping embedding generation")
            return 0

        import httpx

        # Filter to entries needing embeddings
        entries_needing_embedding = []
        for thread in threads:
            # Skip unchanged threads in incremental mode
            if self.config.incremental and thread.topic not in self._changed_topics:
                continue
            for entry in thread.entries:
                if not hasattr(entry, 'embedding') or not entry.embedding:
                    entries_needing_embedding.append(entry)

        total_to_process = len(entries_needing_embedding)
        if total_to_process == 0:
            self._log("All entries already have embeddings")
            return 0

        self._log(f"Generating embeddings for {total_to_process} entries...")

        url = f"{self.config.embedding.api_base.rstrip('/')}/embeddings"
        generated = 0

        with httpx.Client(timeout=30.0) as client:
            for i, entry in enumerate(entries_needing_embedding, 1):
                if i % 10 == 0 or i == total_to_process:
                    self._log_verbose(f"Embedding entry {i}/{total_to_process}")

                # Use summary if available, otherwise truncated body
                text = getattr(entry, 'summary', None) or entry.body[:500]

                try:
                    response = client.post(url, json={
                        "model": self.config.embedding.model,
                        "input": text,
                    })
                    response.raise_for_status()
                    data = response.json()
                    entry.embedding = data["data"][0]["embedding"]
                    generated += 1
                except Exception as e:
                    self._log_verbose(f"Warning: Failed to embed entry: {e}")
                    entry.embedding = None

        return generated

    def _export_graph(self, threads: List[Any]) -> tuple[int, int]:
        """Export threads to per-thread JSONL graph format.

        Writes per-thread format:
            threads/<topic>/meta.json     - Thread node
            threads/<topic>/entries.jsonl - Entry nodes
            threads/<topic>/edges.jsonl   - Thread-local edges
            search-index.jsonl            - Entry embeddings for cross-thread search
        """
        from ..export import (
            thread_to_node,
            entry_to_node,
            generate_edges,
            generate_cross_references,
        )

        graph_dir = storage.ensure_graph_dir(self.config.threads_dir)

        self._log(f"Exporting graph to: {graph_dir}")

        node_count = 0
        edge_count = 0
        xref_count = 0
        search_index_entries: List[Dict[str, Any]] = []

        # Collect all nodes and edges for monolithic dual-write
        all_nodes: List[Dict[str, Any]] = []
        all_edges: List[Dict[str, Any]] = []

        # Build topic lookup for cross-references
        topic_lookup = {t.topic: t for t in threads}

        for thread in threads:
            topic = thread.topic

            # Thread node (meta.json)
            thread_node = thread_to_node(thread)
            if hasattr(thread, 'embedding') and thread.embedding:
                thread_node["embedding"] = thread.embedding
            node_count += 1

            # Entry nodes
            entries_dict: Dict[str, Dict[str, Any]] = {}
            for entry in thread.entries:
                entry_node = entry_to_node(entry, thread.topic)
                if hasattr(entry, 'summary') and entry.summary:
                    entry_node["summary"] = entry.summary
                if hasattr(entry, 'embedding') and entry.embedding:
                    entry_node["embedding"] = entry.embedding
                    # Add to search index (use raw entry_id ULID, not node ID)
                    search_index_entries.append({
                        "entry_id": entry.entry_id,
                        "thread_topic": topic,
                        "embedding": entry.embedding,
                    })
                entries_dict[entry_node["id"]] = entry_node
                node_count += 1

            # Edges (contains, followed_by)
            edges_dict: Dict[str, Dict[str, Any]] = {}
            for edge in generate_edges(thread):
                edge_key = edge.get("source", "") + edge.get("target", "")
                edges_dict[edge_key] = edge
                edge_count += 1

            # Write per-thread files
            storage.write_thread_graph(graph_dir, topic, thread_node, entries_dict, edges_dict)

            # Collect for monolithic dual-write
            all_nodes.append(thread_node)
            all_nodes.extend(entries_dict.values())
            all_edges.extend(edges_dict.values())

        # Cross-reference edges (need all threads for lookup)
        # These are stored in each thread's edges file where the reference originates
        self._log_verbose("Detecting cross-references...")
        xref_by_topic: Dict[str, List[Dict[str, Any]]] = {}
        for edge in generate_cross_references(threads):
            xref_count += 1
            # Determine source topic from edge source ID
            # Edge source format is "entry:{ulid}" (set in export.py generate_cross_references)
            source_id = edge.get("source", "")
            source_topic = None

            # Extract the raw entry_id from "entry:{ulid}" format
            raw_entry_id = source_id[6:] if source_id.startswith("entry:") else source_id

            for topic, thread in topic_lookup.items():
                for entry in thread.entries:
                    entry_id = getattr(entry, 'entry_id', '')
                    # Compare raw entry IDs (ULIDs)
                    if entry_id == raw_entry_id:
                        source_topic = topic
                        break
                if source_topic:
                    break

            if source_topic:
                if source_topic not in xref_by_topic:
                    xref_by_topic[source_topic] = []
                xref_by_topic[source_topic].append(edge)

        # Append cross-references to thread edge files
        for topic, xref_edges in xref_by_topic.items():
            existing_edges = storage.load_thread_edges(graph_dir, topic)
            for edge in xref_edges:
                edge_key = edge.get("source", "") + edge.get("target", "")
                existing_edges[edge_key] = edge
                all_edges.append(edge)  # Collect for monolithic
            storage.write_thread_edges(graph_dir, topic, existing_edges)

        # Dual-write: Write monolithic format for backward compatibility
        storage.write_monolithic_graph(graph_dir, all_nodes, all_edges)
        self._log_verbose(f"  Wrote monolithic format: {len(all_nodes)} nodes, {len(all_edges)} edges")

        # Write search index
        storage.atomic_write_jsonl(graph_dir / "search-index.jsonl", search_index_entries)

        self._log(f"  Wrote {node_count} nodes, {edge_count} edges, {xref_count} cross-references")

        # Write manifest
        from datetime import datetime, timezone
        total_edges = edge_count + xref_count
        manifest = {
            "version": "2.0",  # Per-thread format
            "format": "per-thread",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_dir": str(self.config.threads_dir),
            "threads_exported": len(threads),
            "entries_exported": sum(len(t.entries) for t in threads),
            "nodes_written": node_count,
            "edges_written": total_edges,
            "cross_references": xref_count,
            "topics": {t.topic: datetime.now(timezone.utc).isoformat() for t in threads},
        }
        storage.atomic_write_json(graph_dir / "manifest.json", manifest)

        return node_count, total_edges

    def run(self) -> PipelineResult:
        """Run the full pipeline."""
        start_time = time.time()

        # Validate config
        errors = self.config.validate()
        if errors:
            return PipelineResult(
                success=False,
                threads_processed=0,
                entries_processed=0,
                nodes_created=0,
                edges_created=0,
                embeddings_generated=0,
                duration_seconds=0,
                output_dir=self.config.output_dir,
                error="; ".join(errors),
            )

        try:
            # Ensure servers are available
            if not self._ensure_servers():
                return PipelineResult(
                    success=False,
                    threads_processed=0,
                    entries_processed=0,
                    nodes_created=0,
                    edges_created=0,
                    embeddings_generated=0,
                    duration_seconds=time.time() - start_time,
                    output_dir=self.config.output_dir,
                    error="Required servers not available",
                )

            # Clear cache if fresh mode
            self._clear_cache()

            # Load state for incremental builds
            self._load_state()

            # Parse threads
            threads = self._parse_threads()
            if not threads:
                return PipelineResult(
                    success=False,
                    threads_processed=0,
                    entries_processed=0,
                    nodes_created=0,
                    edges_created=0,
                    embeddings_generated=0,
                    duration_seconds=time.time() - start_time,
                    output_dir=self.config.output_dir,
                    error="No threads found",
                )

            total_entries = sum(len(t.entries) for t in threads)

            # Generate summaries
            self._summarize_entries(threads)
            self._summarize_threads(threads)

            # Generate embeddings
            embeddings_generated = self._generate_embeddings(threads)

            # Export graph
            nodes, edges = self._export_graph(threads)

            # Save state for incremental builds
            self._save_state(threads)

            duration = time.time() - start_time
            self._log(f"\nPipeline completed in {duration:.1f}s")

            return PipelineResult(
                success=True,
                threads_processed=len(threads),
                entries_processed=total_entries,
                nodes_created=nodes,
                edges_created=edges,
                embeddings_generated=embeddings_generated,
                duration_seconds=duration,
                output_dir=self.config.output_dir,
            )

        except Exception as e:
            return PipelineResult(
                success=False,
                threads_processed=0,
                entries_processed=0,
                nodes_created=0,
                edges_created=0,
                embeddings_generated=0,
                duration_seconds=time.time() - start_time,
                output_dir=self.config.output_dir,
                error=str(e),
            )
        finally:
            self._stop_servers_if_needed()


def run_pipeline(
    threads_dir: Path,
    output_dir: Optional[Path] = None,
    test_limit: Optional[int] = None,
    fresh: bool = False,
    incremental: bool = False,
    extractive_only: bool = False,
    skip_embeddings: bool = False,
    skip_closed: bool = False,
    verbose: bool = False,
    auto_server: bool = True,
    stop_servers: bool = False,
    auto_approve: bool = False,
) -> PipelineResult:
    """Convenience function to run the pipeline.

    Args:
        threads_dir: Directory containing thread markdown files
        output_dir: Output directory for graph files (default: threads_dir/graph/baseline)
        test_limit: Limit number of threads to process (for testing)
        fresh: Clear cached results and reprocess everything
        incremental: Only process changed threads (uses state.json)
        extractive_only: Use extractive summarization (no LLM)
        skip_embeddings: Skip embedding generation
        skip_closed: Skip closed threads
        verbose: Enable verbose output
        auto_server: Automatically start required servers
        stop_servers: Stop servers after completion
        auto_approve: Auto-approve server startup prompts

    Returns:
        PipelineResult with success status and statistics
    """
    config = PipelineConfig(
        threads_dir=threads_dir,
        output_dir=output_dir,
        test_limit=test_limit,
        fresh=fresh,
        incremental=incremental,
        extractive_only=extractive_only,
        skip_embeddings=skip_embeddings,
        skip_closed=skip_closed,
    )

    runner = BaselineGraphRunner(
        config=config,
        verbose=verbose,
        auto_server=auto_server,
        stop_servers=stop_servers,
        auto_approve=auto_approve,
    )

    return runner.run()
