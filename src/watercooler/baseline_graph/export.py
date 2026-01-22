"""Export baseline graph to JSONL format.

Produces graph files suitable for loading into graph databases or
the Watercooler dashboard knowledge graph view.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .parser import ParsedThread, ParsedEntry, iter_threads
from .summarizer import SummarizerConfig

logger = logging.getLogger(__name__)

# Regex patterns for cross-reference extraction
FILE_REF_RE = re.compile(r"`([a-zA-Z0-9_/.-]+\.[a-zA-Z0-9]+)`")
PR_REF_RE = re.compile(r"#(\d+)")
COMMIT_REF_RE = re.compile(r"\b([a-fA-F0-9]{10,40})\b")  # Min 10 chars to avoid UUID/hex false positives

# Thread cross-reference patterns
# Matches: thread:topic-name, [text](topic.md), `topic-name` (when topic is known)
THREAD_REF_EXPLICIT_RE = re.compile(r"thread:([a-z0-9][-a-z0-9]*)")
THREAD_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([a-z0-9][-a-z0-9]*)\.md\)")
THREAD_BACKTICK_RE = re.compile(r"`([a-z0-9][-a-z0-9]*)`")

# Entry ID reference pattern (ULID format)
ENTRY_ID_RE = re.compile(r"\b(01[A-Z0-9]{24})\b")


def _is_safe_path(path: str) -> bool:
    """Check if a path reference is safe (no traversal or absolute paths).

    Args:
        path: Path string to validate

    Returns:
        True if path is safe, False if it contains traversal or is absolute
    """
    # Reject absolute paths
    if path.startswith("/") or (len(path) > 1 and path[1] == ":"):
        return False
    # Reject path traversal
    if ".." in path:
        return False
    return True


def _extract_file_refs(text: str) -> List[str]:
    """Extract file path references from text.

    Filters out paths with traversal sequences or absolute paths for security.
    """
    refs = FILE_REF_RE.findall(text)
    return list(set(ref for ref in refs if _is_safe_path(ref)))


def _extract_pr_refs(text: str) -> List[int]:
    """Extract PR number references from text."""
    return [int(n) for n in set(PR_REF_RE.findall(text))]


def _extract_commit_refs(text: str) -> List[str]:
    """Extract commit SHA references from text."""
    return list(set(COMMIT_REF_RE.findall(text)))


def _extract_thread_refs(text: str, known_topics: Optional[set] = None) -> List[str]:
    """Extract thread topic references from text.

    Detects references to other threads via:
    - Explicit thread:topic notation
    - Markdown links to topic.md
    - Backtick-quoted topic names (only if known_topics provided)

    Args:
        text: Text to search for references
        known_topics: Optional set of known thread topics for backtick matching

    Returns:
        List of referenced topic names
    """
    refs = set()

    # Explicit thread:topic references (only if they're known topics to avoid
    # matching example patterns like "thread:topic-name" in documentation)
    for match in THREAD_REF_EXPLICIT_RE.findall(text):
        if known_topics is None or match in known_topics:
            refs.add(match)

    # Markdown links to topic.md (validate if known_topics provided)
    for _, topic in THREAD_MD_LINK_RE.findall(text):
        if known_topics is None or topic in known_topics:
            refs.add(topic)

    # Backtick-quoted names (only if we know it's a valid topic)
    # Require minimum length to avoid false positives like "topic", "index", etc.
    if known_topics:
        for match in THREAD_BACKTICK_RE.findall(text):
            if match in known_topics and len(match) >= 10:
                refs.add(match)

    return list(refs)


def _extract_entry_refs(text: str) -> List[str]:
    """Extract entry ID references (ULIDs) from text.

    Args:
        text: Text to search for references

    Returns:
        List of referenced entry IDs
    """
    return list(set(ENTRY_ID_RE.findall(text)))


def generate_cross_references(
    threads: List[ParsedThread],
) -> Iterator[Dict[str, Any]]:
    """Generate cross-reference edges between threads and entries.

    Detects when entries reference other threads or entries and creates
    'references' edges for the knowledge graph.

    Args:
        threads: List of all parsed threads

    Yields:
        Edge dicts for cross-references
    """
    # Build lookup maps
    known_topics = {t.topic for t in threads}
    entry_to_thread = {}  # entry_id -> topic

    for thread in threads:
        for entry in thread.entries:
            entry_to_thread[entry.entry_id] = thread.topic

    # Scan all entries for cross-references
    for thread in threads:
        for entry in thread.entries:
            entry_id = f"entry:{entry.entry_id}"

            # Thread references
            thread_refs = _extract_thread_refs(entry.body, known_topics)
            for ref_topic in thread_refs:
                # Don't create self-references to own thread
                if ref_topic != thread.topic:
                    yield {
                        "source": entry_id,
                        "target": f"thread:{ref_topic}",
                        "type": "references",
                    }

            # Entry references
            entry_refs = _extract_entry_refs(entry.body)
            for ref_entry_id in entry_refs:
                # Don't create self-references
                if ref_entry_id != entry.entry_id and ref_entry_id in entry_to_thread:
                    yield {
                        "source": entry_id,
                        "target": f"entry:{ref_entry_id}",
                        "type": "references",
                    }


def thread_to_node(thread: ParsedThread) -> Dict[str, Any]:
    """Convert ParsedThread to graph node.

    Args:
        thread: Parsed thread

    Returns:
        Node dict for JSONL export
    """
    node = {
        "id": f"thread:{thread.topic}",
        "type": "thread",
        "topic": thread.topic,
        "title": thread.title,
        "status": thread.status,
        "ball": thread.ball,
        "last_updated": thread.last_updated,
        "summary": thread.summary,
        "entry_count": thread.entry_count,
    }
    # Include embedding if present (added by pipeline runner)
    if hasattr(thread, "embedding") and thread.embedding:
        node["embedding"] = thread.embedding
    return node


def entry_to_node(entry: ParsedEntry, topic: str) -> Dict[str, Any]:
    """Convert ParsedEntry to graph node.

    Args:
        entry: Parsed entry
        topic: Parent thread topic

    Returns:
        Node dict for JSONL export
    """
    node = {
        "id": f"entry:{entry.entry_id}",
        "type": "entry",
        "entry_id": entry.entry_id,
        "thread_topic": topic,
        "index": entry.index,
        "agent": entry.agent,
        "role": entry.role,
        "entry_type": entry.entry_type,
        "title": entry.title,
        "timestamp": entry.timestamp,
        "body": entry.body,
        "summary": entry.summary,
        "file_refs": _extract_file_refs(entry.body),
        "pr_refs": _extract_pr_refs(entry.body),
        "commit_refs": _extract_commit_refs(entry.body),
    }
    # Include embedding if present (added by pipeline runner)
    if hasattr(entry, "embedding") and entry.embedding:
        node["embedding"] = entry.embedding
    return node


def generate_edges(thread: ParsedThread) -> Iterator[Dict[str, Any]]:
    """Generate edges for a thread.

    Creates edges for:
    - Thread contains entries
    - Entry follows entry (sequential)
    - Cross-references between entries (if detected)

    Args:
        thread: Parsed thread

    Yields:
        Edge dicts for JSONL export
    """
    thread_id = f"thread:{thread.topic}"

    prev_entry_id = None
    for entry in thread.entries:
        entry_id = f"entry:{entry.entry_id}"

        # Thread contains entry
        yield {
            "source": thread_id,
            "target": entry_id,
            "type": "contains",
        }

        # Sequential entry relationship
        if prev_entry_id:
            yield {
                "source": prev_entry_id,
                "target": entry_id,
                "type": "followed_by",
            }

        prev_entry_id = entry_id


def export_thread_graph(
    thread: ParsedThread,
    output_dir: Path,
    append: bool = True,
) -> Tuple[int, int]:
    """Export a single thread to JSONL files.

    Args:
        thread: Parsed thread
        output_dir: Output directory for JSONL files
        append: Append to existing files (default) or overwrite

    Returns:
        Tuple of (nodes_written, edges_written)
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    nodes_file = output_dir / "nodes.jsonl"
    edges_file = output_dir / "edges.jsonl"

    mode = "a" if append else "w"
    nodes_written = 0
    edges_written = 0

    # Write nodes
    with open(nodes_file, mode, encoding="utf-8") as f:
        # Thread node
        f.write(json.dumps(thread_to_node(thread)) + "\n")
        nodes_written += 1

        # Entry nodes
        for entry in thread.entries:
            f.write(json.dumps(entry_to_node(entry, thread.topic)) + "\n")
            nodes_written += 1

    # Write edges
    with open(edges_file, mode, encoding="utf-8") as f:
        for edge in generate_edges(thread):
            f.write(json.dumps(edge) + "\n")
            edges_written += 1

    return nodes_written, edges_written


def export_all_threads(
    threads_dir: Path,
    output_dir: Path,
    config: Optional[SummarizerConfig] = None,
    skip_closed: bool = False,
    generate_embeddings: bool = False,
) -> Dict[str, Any]:
    """Export all threads to JSONL graph format.

    Args:
        threads_dir: Path to threads directory
        output_dir: Output directory for JSONL files
        config: Summarizer configuration
        skip_closed: Skip closed threads
        generate_embeddings: Generate embedding vectors for entries

    Returns:
        Export statistics
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Clear existing files
    nodes_file = output_dir / "nodes.jsonl"
    edges_file = output_dir / "edges.jsonl"

    if nodes_file.exists():
        nodes_file.unlink()
    if edges_file.exists():
        edges_file.unlink()

    # Import embedding function if needed
    embedding_func = None
    if generate_embeddings:
        try:
            from .sync import generate_embedding
            embedding_func = generate_embedding
            logger.info("Embedding generation enabled")
        except ImportError:
            logger.warning("Embedding generation requested but sync module not available")

    total_threads = 0
    total_entries = 0
    total_nodes = 0
    total_edges = 0
    total_embeddings = 0

    for thread in iter_threads(threads_dir, config, generate_summaries=True, skip_closed=skip_closed):
        # Generate embeddings for entries if enabled
        if embedding_func:
            for entry in thread.entries:
                embed_text = entry.summary if entry.summary else entry.body[:500]
                embedding = embedding_func(embed_text)
                if embedding:
                    entry.embedding = embedding
                    total_embeddings += 1

        nodes, edges = export_thread_graph(thread, output_dir, append=True)
        total_threads += 1
        total_entries += thread.entry_count
        total_nodes += nodes
        total_edges += edges
        logger.info(f"Exported thread '{thread.topic}': {nodes} nodes, {edges} edges")

    # Write manifest
    manifest = {
        "version": "1.0",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_dir": str(threads_dir),
        "threads_exported": total_threads,
        "entries_exported": total_entries,
        "nodes_written": total_nodes,
        "edges_written": total_edges,
        "embeddings_generated": total_embeddings,
        "files": {
            "nodes": "nodes.jsonl",
            "edges": "edges.jsonl",
        },
    }

    manifest_file = output_dir / "manifest.json"
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    return manifest
