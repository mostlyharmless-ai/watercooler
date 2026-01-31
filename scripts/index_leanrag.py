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
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from watercooler_memory.backends.leanrag import LeanRAGBackend, LeanRAGConfig
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
            thread_path = threads_dir / thread_file
            if thread_path.exists():
                print(f"  Loading {thread_file}...")
                graph.add_thread(thread_path)
            else:
                print(f"  Warning: {thread_file} not found, skipping")

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
    parser.add_argument("--thread-list", help="Path to file with thread list (one per line)")
    parser.add_argument("--threads", nargs="+", help="List of thread topics (without .md)")
    parser.add_argument("--all", action="store_true",
                        help="Index all .md thread files in threads-dir")
    parser.add_argument("--docs-dir",
                        help="Path to documents directory (white papers, reference docs)")
    parser.add_argument("--docs-pattern", default="*.md",
                        help="Glob pattern for documents (default: *.md)")
    parser.add_argument("--work-dir", help="Work directory for LeanRAG (default: ~/.watercooler/leanrag)")
    parser.add_argument("--leanrag-dir", help="Path to LeanRAG repository (default: $LEANRAG_DIR or ./external/LeanRAG)")

    args = parser.parse_args()

    # Check for DeepSeek API key (or other LLM)
    if "DEEPSEEK_API_KEY" not in os.environ:
        print("Error: DEEPSEEK_API_KEY environment variable not set", file=sys.stderr)
        print("Export your DeepSeek API key: export DEEPSEEK_API_KEY=sk-...", file=sys.stderr)
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

    # Set up LeanRAG backend
    work_dir = Path(args.work_dir) if args.work_dir else Path.home() / ".watercooler" / "leanrag"
    work_dir.mkdir(parents=True, exist_ok=True)

    config = LeanRAGConfig(work_dir=work_dir, leanrag_path=leanrag_dir)
    backend = LeanRAGBackend(config)

    # Check health
    print("Checking LeanRAG backend health...")
    health = backend.healthcheck()
    if not health.ok:
        print(f"Error: Backend health check failed: {health.details}", file=sys.stderr)
        print("\nMake sure FalkorDB is running: docker run -d -p 6379:6379 falkordb/falkordb:latest", file=sys.stderr)
        return 1
    print(f"✓ Backend healthy: {health.details}")

    # Build corpus from threads and/or documents
    corpus = build_corpus(
        threads_dir,
        thread_files,
        docs_dir=docs_dir,
        docs_pattern=args.docs_pattern,
    )
    print(f"\n✓ Built corpus: {len(corpus.threads)} threads, {len(corpus.entries)} entries")

    # Step 1: Prepare (entity extraction)
    print("\nStep 1: Preparing (entity/relation extraction)...")
    print("  This step uses LLM to extract entities and relationships from chunks.")
    print("  With 3 threads, expect ~5-15 minutes depending on chunk count.")
    prepare_result = backend.prepare(corpus)
    print(f"✓ Prepared {prepare_result.prepared_count} chunks")

    # Step 2: Build chunks
    print("\nStep 2: Building chunks...")
    chunks = build_chunks(corpus)
    print(f"✓ Built {len(chunks.chunks)} chunks")

    # Step 3: Index (build hierarchical graph)
    print("\nStep 3: Building hierarchical knowledge graph...")
    print("  This step performs clustering and builds the graph in FalkorDB.")
    index_result = backend.index(chunks)
    print(f"✓ Indexed {index_result.indexed_count} chunks into LeanRAG graph")

    print(f"\n✅ Indexing complete! Work directory: {work_dir}")
    print("\nYou can now query via Phase 2 backend:")
    print('  backend.search_nodes(query="your question", max_results=10)')
    print('  backend.search_facts(query="your question", max_results=10)')

    return 0


if __name__ == "__main__":
    sys.exit(main())
