#!/usr/bin/env python3
"""Index watercooler threads and documents into LeanRAG backend.

Usage:
    # Index threads
    python3 scripts/index_leanrag.py --thread-list /path/to/threads-to-index.txt
    python3 scripts/index_leanrag.py --threads graphiti-mcp-integration memory-backend

    # Index reference documents (white papers)
    python3 scripts/index_leanrag.py --docs-dir ../Gist-threads/refs --docs-pattern "ref-*.md"

    # Index both threads and documents
    python3 scripts/index_leanrag.py --threads-dir ../threads --all --docs-dir ../refs
"""

import argparse
import os
import sys
import time
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from watercooler_memory.backends.leanrag import LeanRAGBackend, LeanRAGConfig
from watercooler_memory.backends.graphiti import _derive_database_name
from watercooler_memory.backends import CorpusPayload, ChunkPayload
from watercooler_memory.graph import MemoryGraph
from watercooler_memory.chunker import ChunkerConfig
from watercooler_memory.graph import GraphConfig
from watercooler_memory.document_ingest import ingest_directory


def load_thread_list(list_file: Path) -> list[str]:
    """Load thread filenames from list file."""
    threads = []
    with open(list_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                # Ensure .md extension
                if not line.endswith(".md"):
                    line = f"{line}.md"
                threads.append(line)
    return threads


def build_corpus(
    threads_dir: Path | None,
    thread_files: list[str],
    docs_dir: Path | None = None,
    docs_pattern: str = "*.md",
) -> CorpusPayload:
    """Build corpus from watercooler threads and/or documents."""
    threads_data = []
    entries_data = []
    chunk_nodes = []

    # Process threads if provided
    if threads_dir and thread_files:
        print(f"Building corpus from {len(thread_files)} threads...")

        # Build memory graph with watercooler preset for headers
        config = GraphConfig(chunker=ChunkerConfig.watercooler_preset())
        graph = MemoryGraph(config=config)

        for thread_file in thread_files:
            topic = thread_file.removesuffix(".md")
            print(f"  Loading {thread_file}...")
            try:
                graph.add_thread(threads_dir, topic)
            except FileNotFoundError:
                print(f"  Warning: {thread_file} not found in graph, skipping")

        # Chunk all entries using the custom watercooler chunker with headers
        print("Chunking entries...")
        chunk_nodes = graph.chunk_all_entries()
        print(f"Created {len(chunk_nodes)} chunks from {len(graph.entries)} entries")

        # Convert to canonical payload format
        threads_data = [
            {
                "id": thread.thread_id,
                "topic": thread.thread_id,
                "status": thread.status,
                "ball": thread.ball,
                "entry_count": len([e for e in graph.entries.values() if e.thread_id == thread.thread_id]),
                "title": thread.title,
            }
            for thread in graph.threads.values()
        ]

        entries_data = [
            {
                "id": entry.entry_id,
                "thread_id": entry.thread_id,
                "agent": entry.agent,
                "role": entry.role,
                "type": entry.entry_type,
                "title": entry.title,
                "body": entry.body,
                "timestamp": entry.timestamp,
                # Include chunks for this entry
                "chunks": [
                    {"text": chunk.text, "chunk_id": chunk.chunk_id, "token_count": len(chunk.text.split())}
                    for chunk in chunk_nodes
                    if chunk.entry_id == entry.entry_id
                ],
            }
            for entry in graph.entries.values()
        ]

    # Process documents if provided
    if docs_dir:
        print(f"Ingesting documents from {docs_dir} (pattern: {docs_pattern})...")
        docs, doc_chunks = ingest_directory(docs_dir, pattern=docs_pattern)
        print(f"Found {len(docs)} documents, {len(doc_chunks)} chunks")

        # Convert documents to entry format
        for doc in docs:
            doc_chunk_list = [c for c in doc_chunks if c.doc_id == doc.doc_id]
            entries_data.append({
                "id": doc.doc_id,
                "thread_id": f"doc:{Path(doc.file_path).stem}",
                "agent": doc.metadata.get("authors", "Unknown"),
                "role": "reference",
                "type": "Document",
                "title": doc.title,
                "body": "",  # Body content is in chunks
                "timestamp": doc.metadata.get("year", ""),
                "chunks": [
                    {"text": c.text, "chunk_id": c.chunk_id, "token_count": c.token_count}
                    for c in doc_chunk_list
                ],
            })

    return CorpusPayload(
        manifest_version="1.0.0",
        threads=threads_data,
        entries=entries_data,
        edges=[],
        metadata={"source": "index_leanrag.py"},
    )


def build_chunks(corpus: CorpusPayload) -> ChunkPayload:
    """Extract chunks from corpus entries."""
    all_chunks = []
    for entry in corpus.entries:
        if "chunks" in entry:
            for chunk in entry["chunks"]:
                all_chunks.append({
                    "id": chunk.get("chunk_id", chunk.get("id")),
                    "entry_id": entry["id"],
                    "text": chunk["text"],
                    "token_count": chunk.get("token_count", len(chunk["text"].split())),
                    "hash_code": chunk.get("hash_code", ""),
                })

    return ChunkPayload(
        manifest_version="1.0.0",
        chunks=all_chunks,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Index watercooler threads and documents into LeanRAG",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Index specific threads
  python3 scripts/index_leanrag.py --threads-dir ../threads --threads auth-feature api-design

  # Index all threads
  python3 scripts/index_leanrag.py --threads-dir ../threads --all

  # Index reference documents (white papers)
  python3 scripts/index_leanrag.py --docs-dir ../Gist-threads/refs --docs-pattern "ref-*.md"

  # Index both threads and documents
  python3 scripts/index_leanrag.py --threads-dir ../threads --all --docs-dir ../refs
""",
    )
    parser.add_argument("--threads-dir",
                        help="Path to threads directory")
    parser.add_argument("--code-path",
                        help="Path to code repository (for database name derivation, defaults to threads-dir without -threads suffix)")
    parser.add_argument("--thread-list", help="Path to file with thread list (one per line)")
    parser.add_argument("--threads", nargs="+", help="List of thread topics (without .md)")
    parser.add_argument("--all", action="store_true",
                        help="Index all .md thread files in threads-dir")
    parser.add_argument("--docs-dir",
                        help="Path to documents directory (white papers, reference docs)")
    parser.add_argument("--docs-pattern", default="*.md",
                        help="Glob pattern for documents (default: *.md)")
    parser.add_argument("--work-dir", help="Work directory for LeanRAG (default: ~/.watercooler/<database>)")
    parser.add_argument("--leanrag-dir", help="Path to LeanRAG repository (default: $LEANRAG_DIR or ./external/LeanRAG)")

    args = parser.parse_args()

    # Load config from unified config system (config.toml + env vars)
    try:
        config = LeanRAGConfig.from_unified()
    except Exception as e:
        print(f"Error loading config: {e}", file=sys.stderr)
        print("\nEnsure ~/.watercooler/config.toml exists with [memory.llm] section,", file=sys.stderr)
        print("or set environment variables (e.g., DEEPSEEK_API_KEY)", file=sys.stderr)
        return 1

    # Determine thread list
    threads_dir = None
    thread_files = []

    if args.all:
        if not args.threads_dir:
            print("Error: --threads-dir is required when using --all", file=sys.stderr)
            return 1
        threads_dir = Path(args.threads_dir)
        if not threads_dir.exists():
            print(f"Error: Threads directory not found: {threads_dir}", file=sys.stderr)
            return 1
        non_thread_files = {'readme.md', 'changelog.md', 'index.md', 'license.md'}
        thread_files = sorted([
            f.name for f in threads_dir.glob("*.md")
            if f.is_file() and f.name.lower() not in non_thread_files
        ])
        print(f"Discovered {len(thread_files)} thread files")
    elif args.thread_list:
        thread_list_path = Path(args.thread_list)
        if not thread_list_path.exists():
            print(f"Error: Thread list file not found: {thread_list_path}", file=sys.stderr)
            return 1
        thread_files = load_thread_list(thread_list_path)
        threads_dir = Path(args.threads_dir) if args.threads_dir else None
    elif args.threads:
        thread_files = [f"{t}.md" if not t.endswith(".md") else t for t in args.threads]
        threads_dir = Path(args.threads_dir) if args.threads_dir else None

    # Validate we have something to index
    docs_dir = Path(args.docs_dir) if args.docs_dir else None
    if not thread_files and not docs_dir:
        print("Error: Specify threads (--all, --thread-list, or --threads) and/or --docs-dir", file=sys.stderr)
        parser.print_help()
        return 1

    if threads_dir and thread_files and not threads_dir.exists():
        print(f"Error: Threads directory not found: {threads_dir}", file=sys.stderr)
        return 1

    if docs_dir and not docs_dir.exists():
        print(f"Error: Documents directory not found: {docs_dir}", file=sys.stderr)
        return 1

    # Resolve code_path for database name derivation (consistent with index_graphiti.py)
    if args.code_path:
        code_path = Path(args.code_path)
    elif threads_dir:
        # Derive code_path from threads_dir by removing -threads suffix
        threads_dir_str = str(threads_dir.resolve())
        if threads_dir_str.endswith("-threads"):
            code_path = Path(threads_dir_str.removesuffix("-threads"))
        else:
            code_path = threads_dir
    elif docs_dir:
        # Use docs_dir parent as code_path
        code_path = docs_dir.parent
    else:
        code_path = Path.cwd()

    database_name = _derive_database_name(code_path)
    print(f"Database: {database_name} (derived from {code_path.name})")
    print(f"FalkorDB graph: {database_name}")

    # Determine LeanRAG directory
    if args.leanrag_dir:
        leanrag_dir = Path(args.leanrag_dir)
    elif "LEANRAG_DIR" in os.environ:
        leanrag_dir = Path(os.environ["LEANRAG_DIR"])
    else:
        leanrag_dir = Path(__file__).parent.parent / "external" / "LeanRAG"

    if not leanrag_dir.exists():
        print(f"Error: LeanRAG directory not found: {leanrag_dir}", file=sys.stderr)
        print("Specify with --leanrag-dir or set LEANRAG_DIR environment variable", file=sys.stderr)
        return 1

    # Update config with work_dir and leanrag_path (work_dir basename = database name)
    work_dir = Path(args.work_dir) if args.work_dir else Path.home() / ".watercooler" / database_name
    work_dir.mkdir(parents=True, exist_ok=True)

    config.work_dir = work_dir
    config.leanrag_path = leanrag_dir

    print(f"Config: LLM={config.llm_model}, Embedding={config.embedding_model}")
    backend = LeanRAGBackend(config)

    # Check health
    print("Checking LeanRAG backend health...")
    health = backend.healthcheck()
    if not health.ok:
        print(f"Error: Backend health check failed: {health.details}", file=sys.stderr)
        print("\nMake sure FalkorDB is running: docker run -d -p 6379:6379 falkordb/falkordb:latest", file=sys.stderr)
        return 1
    print(f"✓ Backend healthy: {health.details}")

    # Track timing for each step
    timings = {}
    total_start = time.perf_counter()

    # Build corpus from threads and/or documents
    step_start = time.perf_counter()
    corpus = build_corpus(
        threads_dir,
        thread_files,
        docs_dir=docs_dir,
        docs_pattern=args.docs_pattern,
    )
    timings["corpus"] = time.perf_counter() - step_start
    print(f"\n✓ Built corpus: {len(corpus.threads)} threads, {len(corpus.entries)} entries ({timings['corpus']:.1f}s)")

    # Step 1: Prepare (export to LeanRAG format)
    print("\nStep 1: Preparing corpus for LeanRAG...")
    step_start = time.perf_counter()
    prepare_result = backend.prepare(corpus)
    timings["prepare"] = time.perf_counter() - step_start
    print(f"✓ Prepared {prepare_result.prepared_count} entries ({timings['prepare']:.1f}s)")

    # Step 2: Build chunks
    print("\nStep 2: Building chunk payload...")
    step_start = time.perf_counter()
    chunks = build_chunks(corpus)
    timings["chunks"] = time.perf_counter() - step_start
    print(f"✓ Built {len(chunks.chunks)} chunks ({timings['chunks']:.1f}s)")

    # Step 3: Index (triple extraction + hierarchical graph building)
    # Graph name in FalkorDB is the work_dir basename (same as database_name)
    graph_name = work_dir.name
    print(f"\nStep 3: Indexing into LeanRAG (FalkorDB graph: {graph_name})...")
    print("  3a. Triple extraction: LLM extracts entities and relationships from each chunk")
    print("  3b. Graph building: Clustering, embeddings, and FalkorDB insertion")
    print(f"  Processing {len(chunks.chunks)} chunks - this may take several minutes...")
    print()

    # Progress tracking state
    last_stage = [None]
    last_step = [None]
    stage_start = [time.perf_counter()]
    last_extraction_report = [0]

    def progress_callback(stage: str, step: str, current: int, total: int) -> None:
        """Print progress updates during indexing."""
        # Track stage transitions
        if stage != last_stage[0]:
            if last_stage[0] is not None:
                elapsed = time.perf_counter() - stage_start[0]
                print(f"\n  ✓ {last_stage[0]} complete ({elapsed:.1f}s)")
            last_stage[0] = stage
            stage_start[0] = time.perf_counter()
            last_extraction_report[0] = 0
            if stage == "triple_extraction":
                print(f"  → Triple extraction: processing {total} chunks...", flush=True)
            elif stage == "graph_building":
                print(f"  → Graph building: 5 steps...")

        # Progress updates during triple extraction
        if stage == "triple_extraction" and step == "processing":
            elapsed = time.perf_counter() - stage_start[0]
            # Only report if we've made progress
            if current > last_extraction_report[0]:
                last_extraction_report[0] = current
                pct = (current / total * 100) if total > 0 else 0
                rate = current / elapsed if elapsed > 0 else 0
                eta = (total - current) / rate if rate > 0 else 0
                print(f"    {current}/{total} chunks (~{pct:.0f}%) | {elapsed:.0f}s elapsed | ETA: {eta:.0f}s", flush=True)

        # Track step transitions within graph_building
        if stage == "graph_building" and step != last_step[0] and step not in ("starting", "complete"):
            last_step[0] = step
            print(f"    [{current}/5] {step}...", flush=True)

    step_start = time.perf_counter()
    index_result = backend.index(chunks, progress_callback=progress_callback)
    timings["index"] = time.perf_counter() - step_start

    # Final stage completion
    if last_stage[0] is not None:
        elapsed = time.perf_counter() - stage_start[0]
        print(f"  ✓ {last_stage[0]} complete ({elapsed:.1f}s)")

    print(f"\n✓ Indexed {index_result.indexed_count} chunks into LeanRAG graph ({timings['index']:.1f}s)")

    # Summary
    total_elapsed = time.perf_counter() - total_start
    print(f"\n{'─' * 50}")
    print(f"✅ Indexing complete! Total time: {total_elapsed:.1f}s")
    print(f"\nTiming breakdown:")
    for step_name, elapsed in timings.items():
        pct = (elapsed / total_elapsed) * 100
        print(f"  {step_name:15} {elapsed:6.1f}s ({pct:4.1f}%)")
    print(f"\nFalkorDB graph: {graph_name}")
    print(f"Work directory: {work_dir}")
    print("\nYou can now query via MCP tools:")
    print(f'  watercooler_smart_query(query="your question", code_path="{code_path}")')

    return 0


if __name__ == "__main__":
    sys.exit(main())
