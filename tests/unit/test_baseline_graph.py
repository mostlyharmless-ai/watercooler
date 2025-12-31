"""Tests for baseline_graph module.

Tests cover:
- SummarizerConfig: env var loading, config dict, defaults
- Extractive summarization: truncation, headers, edge cases
- Parser: thread parsing, entry extraction
- Export: JSONL generation and loading
- Error handling: JSON parsing errors with line numbers
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from watercooler.baseline_graph import (
    SummarizerConfig,
    extractive_summary,
    summarize_entry,
    summarize_thread,
    create_summarizer_config,
    ParsedEntry,
    ParsedThread,
    parse_thread_file,
    iter_threads,
    parse_all_threads,
    get_thread_stats,
    export_thread_graph,
    export_all_threads,
    load_nodes,
    load_edges,
    load_graph,
)
from watercooler.baseline_graph.export import (
    thread_to_node,
    entry_to_node,
    generate_edges,
    _extract_file_refs,
    _extract_pr_refs,
    _extract_commit_refs,
)
from watercooler.baseline_graph.summarizer import (
    _extract_headers,
    _truncate_text,
)


# =============================================================================
# SummarizerConfig Tests
# =============================================================================


class TestSummarizerConfig:
    """Tests for SummarizerConfig."""

    def test_defaults(self):
        """Test default configuration values."""
        config = SummarizerConfig()
        assert config.api_base == "http://localhost:11434/v1"
        assert config.model == "llama3.2:3b"
        assert config.api_key == "ollama"
        assert config.timeout == 30.0
        assert config.max_tokens == 256
        assert config.extractive_max_chars == 200
        assert config.include_headers is True
        assert config.max_headers == 3
        assert config.prefer_extractive is False

    def test_from_config_dict(self):
        """Test creating config from dictionary."""
        config_dict = {
            "llm": {
                "api_base": "http://custom:8080/v1",
                "model": "custom-model",
                "timeout": 60.0,
            },
            "extractive": {
                "max_chars": 300,
                "include_headers": False,
            },
            "prefer_extractive": True,
        }
        config = SummarizerConfig.from_config_dict(config_dict)
        assert config.api_base == "http://custom:8080/v1"
        assert config.model == "custom-model"
        assert config.timeout == 60.0
        assert config.extractive_max_chars == 300
        assert config.include_headers is False
        assert config.prefer_extractive is True

    def test_from_config_dict_empty(self):
        """Test creating config from empty dictionary uses defaults."""
        config = SummarizerConfig.from_config_dict({})
        assert config.api_base == "http://localhost:11434/v1"
        assert config.model == "llama3.2:3b"

    def test_from_env(self):
        """Test creating config from environment variables."""
        env_vars = {
            "BASELINE_GRAPH_API_BASE": "http://env:9090/v1",
            "BASELINE_GRAPH_MODEL": "env-model",
            "BASELINE_GRAPH_API_KEY": "env-key",
            "BASELINE_GRAPH_TIMEOUT": "45.0",
            "BASELINE_GRAPH_MAX_TOKENS": "512",
            "BASELINE_GRAPH_EXTRACTIVE_ONLY": "true",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            config = SummarizerConfig.from_env()
            assert config.api_base == "http://env:9090/v1"
            assert config.model == "env-model"
            assert config.api_key == "env-key"
            assert config.timeout == 45.0
            assert config.max_tokens == 512
            assert config.prefer_extractive is True

    def test_from_env_empty_string_uses_default(self):
        """Test that empty env vars fall back to defaults."""
        env_vars = {
            "BASELINE_GRAPH_TIMEOUT": "",
            "BASELINE_GRAPH_MAX_TOKENS": "",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            config = SummarizerConfig.from_env()
            # Empty strings should use defaults via `or` operator
            assert config.timeout == 30.0
            assert config.max_tokens == 256

    def test_from_env_extractive_only_variations(self):
        """Test various truthy values for extractive-only mode."""
        for val in ["1", "true", "yes", "TRUE", "Yes"]:
            with patch.dict(os.environ, {"BASELINE_GRAPH_EXTRACTIVE_ONLY": val}):
                config = SummarizerConfig.from_env()
                assert config.prefer_extractive is True, f"Failed for value: {val}"

        for val in ["0", "false", "no", ""]:
            with patch.dict(os.environ, {"BASELINE_GRAPH_EXTRACTIVE_ONLY": val}):
                config = SummarizerConfig.from_env()
                assert config.prefer_extractive is False, f"Failed for value: {val}"


# =============================================================================
# Extractive Summarization Tests
# =============================================================================


class TestExtractiveSummarization:
    """Tests for extractive summarization functions."""

    def test_extract_headers(self):
        """Test markdown header extraction."""
        text = """# First Header
Some content
## Second Header
More content
### Third Header
Even more
#### Fourth Header
This should be ignored
"""
        headers = _extract_headers(text, max_headers=3)
        assert headers == ["First Header", "Second Header", "Third Header"]

    def test_extract_headers_empty(self):
        """Test header extraction from text without headers."""
        text = "No headers here, just plain text."
        headers = _extract_headers(text)
        assert headers == []

    def test_truncate_text_short(self):
        """Test that short text is not truncated."""
        text = "Short text"
        result = _truncate_text(text, max_chars=100)
        assert result == "Short text"

    def test_truncate_text_sentence_boundary(self):
        """Test truncation at sentence boundary."""
        text = "First sentence. Second sentence. Third sentence is very long."
        result = _truncate_text(text, max_chars=40)
        assert result == "First sentence. Second sentence."

    def test_truncate_text_word_boundary(self):
        """Test truncation at word boundary when no sentence break."""
        text = "This is a very long sentence without any periods until the end"
        result = _truncate_text(text, max_chars=30)
        assert result.endswith("...")
        assert " " not in result[-4:]  # Should break at word

    def test_extractive_summary_basic(self):
        """Test basic extractive summary."""
        text = "This is some content that should be summarized."
        result = extractive_summary(text, max_chars=100)
        assert "This is some content" in result

    def test_extractive_summary_with_headers(self):
        """Test extractive summary includes headers."""
        text = """# Authentication
## Login Flow
This is the content about login."""
        result = extractive_summary(text, include_headers=True, max_headers=2)
        assert "Topics:" in result
        assert "Authentication" in result
        assert "Login Flow" in result

    def test_extractive_summary_without_headers(self):
        """Test extractive summary without headers."""
        text = """# Header
Content here."""
        result = extractive_summary(text, include_headers=False)
        assert "Topics:" not in result
        assert "Content here" in result

    def test_extractive_summary_empty(self):
        """Test extractive summary with empty input."""
        assert extractive_summary("") == ""
        assert extractive_summary("   ") == ""


class TestSummarizeEntry:
    """Tests for entry summarization."""

    def test_summarize_entry_short_text_extractive(self):
        """Test that short text uses extractive summary."""
        config = SummarizerConfig(extractive_max_chars=200)
        text = "Short entry body"
        result = summarize_entry(text, config=config)
        assert result == "Short entry body"

    def test_summarize_entry_extractive_mode(self):
        """Test forced extractive mode."""
        config = SummarizerConfig(prefer_extractive=True)
        text = "A" * 500  # Long text
        result = summarize_entry(text, config=config)
        # Should use extractive, not LLM
        assert len(result) <= config.extractive_max_chars + 50  # Some buffer for headers


class TestSummarizeThread:
    """Tests for thread summarization."""

    def test_summarize_thread_empty(self):
        """Test summarizing empty thread."""
        result = summarize_thread([])
        assert result == ""

    def test_summarize_thread_extractive(self):
        """Test thread summarization in extractive mode."""
        entries = [
            {"body": "First entry content", "title": "Entry 1", "type": "Note"},
            {"body": "Second entry content", "title": "Entry 2", "type": "Note"},
        ]
        config = SummarizerConfig(prefer_extractive=True)
        result = summarize_thread(entries, config=config)
        assert "Entry 1" in result or "First entry" in result


# =============================================================================
# Parser Tests
# =============================================================================


class TestParsedDataclasses:
    """Tests for ParsedEntry and ParsedThread dataclasses."""

    def test_parsed_entry_creation(self):
        """Test ParsedEntry dataclass."""
        entry = ParsedEntry(
            entry_id="topic:1",
            index=1,
            agent="Claude",
            role="implementer",
            entry_type="Note",
            title="Test Entry",
            timestamp="2024-01-01T00:00:00Z",
            body="Entry body",
            summary="Entry summary",
        )
        assert entry.entry_id == "topic:1"
        assert entry.index == 1
        assert entry.agent == "Claude"

    def test_parsed_thread_creation(self):
        """Test ParsedThread dataclass."""
        entry = ParsedEntry(
            entry_id="topic:1",
            index=1,
            agent=None,
            role=None,
            entry_type=None,
            title=None,
            timestamp=None,
            body="Body",
            summary="Summary",
        )
        thread = ParsedThread(
            topic="test-topic",
            title="Test Thread",
            status="OPEN",
            ball="Claude",
            last_updated="2024-01-01",
            summary="Thread summary",
            entries=[entry],
        )
        assert thread.topic == "test-topic"
        assert thread.entry_count == 1

    def test_parsed_thread_entry_count(self):
        """Test entry_count property."""
        thread = ParsedThread(
            topic="test",
            title="Test",
            status="OPEN",
            ball="",
            last_updated="",
            summary="",
            entries=[],
        )
        assert thread.entry_count == 0


class TestParseThreadFile:
    """Tests for parse_thread_file function."""

    def test_parse_nonexistent_file(self, tmp_path):
        """Test parsing nonexistent file returns None."""
        result = parse_thread_file(tmp_path / "nonexistent.md")
        assert result is None

    def test_parse_basic_thread(self, tmp_path):
        """Test parsing a basic thread file."""
        thread_file = tmp_path / "test-topic.md"
        thread_file.write_text("""# Test Thread
Status: OPEN
Ball: Claude
Last-Updated: 2024-01-01

---

## Entry 1

Entry content here.
""")
        config = SummarizerConfig(prefer_extractive=True)
        result = parse_thread_file(thread_file, config=config)

        assert result is not None
        assert result.topic == "test-topic"
        assert result.title == "Test Thread"
        assert result.status.upper() == "OPEN"  # Status may be lowercase

    def test_parse_thread_no_summaries(self, tmp_path):
        """Test parsing without generating summaries."""
        thread_file = tmp_path / "test.md"
        thread_file.write_text("""# Test
Status: OPEN
Ball: User
Last-Updated: 2024-01-01

---

## Entry

Content.
""")
        result = parse_thread_file(thread_file, generate_summaries=False)

        assert result is not None
        assert result.summary == ""


class TestIterThreads:
    """Tests for iter_threads function."""

    def test_iter_nonexistent_directory(self, tmp_path):
        """Test iterating over nonexistent directory."""
        result = list(iter_threads(tmp_path / "nonexistent"))
        assert result == []

    def test_iter_empty_directory(self, tmp_path):
        """Test iterating over empty directory."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        result = list(iter_threads(threads_dir))
        assert result == []

    def test_iter_skips_index(self, tmp_path):
        """Test that index.md is skipped."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        (threads_dir / "index.md").write_text("# Index\n")
        (threads_dir / "topic.md").write_text("""# Topic
Status: OPEN
Ball: User
Last-Updated: 2024-01-01

---

## Entry

Content.
""")
        config = SummarizerConfig(prefer_extractive=True)
        result = list(iter_threads(threads_dir, config=config))
        assert len(result) == 1
        assert result[0].topic == "topic"

    def test_iter_skip_closed(self, tmp_path):
        """Test skipping closed threads."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        (threads_dir / "open.md").write_text("""# Open Thread
Status: OPEN
Ball: User
Last-Updated: 2024-01-01
---
## Entry
Content.
""")
        (threads_dir / "closed.md").write_text("""# Closed Thread
Status: CLOSED
Ball: User
Last-Updated: 2024-01-01
---
## Entry
Content.
""")

        config = SummarizerConfig(prefer_extractive=True)
        result = list(iter_threads(threads_dir, config=config, skip_closed=True))
        assert len(result) == 1
        assert result[0].topic == "open"


class TestGetThreadStats:
    """Tests for get_thread_stats function."""

    def test_stats_nonexistent_directory(self, tmp_path):
        """Test stats for nonexistent directory."""
        result = get_thread_stats(tmp_path / "nonexistent")
        assert "error" in result

    def test_stats_empty_directory(self, tmp_path):
        """Test stats for empty directory."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        result = get_thread_stats(threads_dir)
        assert result["total_threads"] == 0
        assert result["total_entries"] == 0


# =============================================================================
# Export Tests
# =============================================================================


class TestRefExtraction:
    """Tests for reference extraction functions."""

    def test_extract_file_refs(self):
        """Test file reference extraction."""
        text = "Check `src/main.py` and `tests/test_main.py` for details."
        refs = _extract_file_refs(text)
        assert "src/main.py" in refs
        assert "tests/test_main.py" in refs

    def test_extract_file_refs_deduplication(self):
        """Test that duplicate file refs are deduplicated."""
        text = "`file.py` and `file.py` again"
        refs = _extract_file_refs(text)
        assert refs == ["file.py"]

    def test_extract_pr_refs(self):
        """Test PR reference extraction."""
        text = "See #123 and #456 for more info."
        refs = _extract_pr_refs(text)
        assert 123 in refs
        assert 456 in refs

    def test_extract_commit_refs(self):
        """Test commit SHA extraction (min 10 chars to avoid UUID/hex false positives)."""
        text = "Fixed in abc1234567 and def5678901234567890."
        refs = _extract_commit_refs(text)
        assert "abc1234567" in refs  # 10 chars - matches
        assert "def5678901234567890" in refs  # 20 chars - matches
        # 9-char strings should NOT match (too short)
        text2 = "Not a commit: abc123456"  # 9 chars
        refs2 = _extract_commit_refs(text2)
        assert "abc123456" not in refs2


class TestNodeConversion:
    """Tests for node conversion functions."""

    def test_thread_to_node(self):
        """Test converting ParsedThread to node dict."""
        thread = ParsedThread(
            topic="test-topic",
            title="Test Thread",
            status="OPEN",
            ball="Claude",
            last_updated="2024-01-01",
            summary="Thread summary",
            entries=[],
        )
        node = thread_to_node(thread)

        assert node["id"] == "thread:test-topic"
        assert node["type"] == "thread"
        assert node["topic"] == "test-topic"
        assert node["title"] == "Test Thread"
        assert node["status"] == "OPEN"
        assert node["summary"] == "Thread summary"

    def test_entry_to_node(self):
        """Test converting ParsedEntry to node dict."""
        entry = ParsedEntry(
            entry_id="topic:1",
            index=1,
            agent="Claude",
            role="implementer",
            entry_type="Note",
            title="Test Entry",
            timestamp="2024-01-01T00:00:00Z",
            body="Body with `file.py` and #42",
            summary="Summary",
        )
        node = entry_to_node(entry, "test-topic")

        assert node["id"] == "entry:topic:1"
        assert node["type"] == "entry"
        assert node["thread_topic"] == "test-topic"
        assert "file.py" in node["file_refs"]
        assert 42 in node["pr_refs"]


class TestEdgeGeneration:
    """Tests for edge generation."""

    def test_generate_edges_empty(self):
        """Test edge generation for thread with no entries."""
        thread = ParsedThread(
            topic="test",
            title="Test",
            status="OPEN",
            ball="",
            last_updated="",
            summary="",
            entries=[],
        )
        edges = list(generate_edges(thread))
        assert edges == []

    def test_generate_edges_single_entry(self):
        """Test edge generation for thread with one entry."""
        entry = ParsedEntry(
            entry_id="test:1",
            index=1,
            agent=None,
            role=None,
            entry_type=None,
            title=None,
            timestamp=None,
            body="",
            summary="",
        )
        thread = ParsedThread(
            topic="test",
            title="Test",
            status="OPEN",
            ball="",
            last_updated="",
            summary="",
            entries=[entry],
        )
        edges = list(generate_edges(thread))

        assert len(edges) == 1
        assert edges[0]["source"] == "thread:test"
        assert edges[0]["target"] == "entry:test:1"
        assert edges[0]["type"] == "contains"

    def test_generate_edges_multiple_entries(self):
        """Test edge generation with sequential entries."""
        entries = [
            ParsedEntry(
                entry_id=f"test:{i}",
                index=i,
                agent=None,
                role=None,
                entry_type=None,
                title=None,
                timestamp=None,
                body="",
                summary="",
            )
            for i in range(3)
        ]
        thread = ParsedThread(
            topic="test",
            title="Test",
            status="OPEN",
            ball="",
            last_updated="",
            summary="",
            entries=entries,
        )
        edges = list(generate_edges(thread))

        # 3 contains + 2 followed_by = 5 edges
        assert len(edges) == 5

        # Check followed_by edges
        followed_by = [e for e in edges if e["type"] == "followed_by"]
        assert len(followed_by) == 2


class TestExportAndLoad:
    """Tests for export and load functions."""

    def test_export_thread_graph(self, tmp_path):
        """Test exporting a single thread."""
        entry = ParsedEntry(
            entry_id="test:1",
            index=1,
            agent="Claude",
            role="implementer",
            entry_type="Note",
            title="Entry",
            timestamp="2024-01-01T00:00:00Z",
            body="Content",
            summary="Summary",
        )
        thread = ParsedThread(
            topic="test",
            title="Test Thread",
            status="OPEN",
            ball="Claude",
            last_updated="2024-01-01",
            summary="Thread summary",
            entries=[entry],
        )

        output_dir = tmp_path / "graph"
        nodes, edges = export_thread_graph(thread, output_dir)

        assert nodes == 2  # 1 thread + 1 entry
        assert edges == 1  # 1 contains edge
        assert (output_dir / "nodes.jsonl").exists()
        assert (output_dir / "edges.jsonl").exists()

    def test_load_nodes(self, tmp_path):
        """Test loading nodes from JSONL."""
        nodes_file = tmp_path / "nodes.jsonl"
        nodes_file.write_text(
            '{"id": "node1", "type": "thread"}\n'
            '{"id": "node2", "type": "entry"}\n'
        )

        nodes = list(load_nodes(nodes_file))
        assert len(nodes) == 2
        assert nodes[0]["id"] == "node1"
        assert nodes[1]["id"] == "node2"

    def test_load_nodes_skips_empty_lines(self, tmp_path):
        """Test that empty lines are skipped."""
        nodes_file = tmp_path / "nodes.jsonl"
        nodes_file.write_text(
            '{"id": "node1"}\n'
            '\n'
            '   \n'
            '{"id": "node2"}\n'
        )

        nodes = list(load_nodes(nodes_file))
        assert len(nodes) == 2

    def test_load_edges(self, tmp_path):
        """Test loading edges from JSONL."""
        edges_file = tmp_path / "edges.jsonl"
        edges_file.write_text(
            '{"source": "a", "target": "b", "type": "contains"}\n'
        )

        edges = list(load_edges(edges_file))
        assert len(edges) == 1
        assert edges[0]["source"] == "a"

    def test_load_graph(self, tmp_path):
        """Test loading complete graph."""
        nodes_file = tmp_path / "nodes.jsonl"
        edges_file = tmp_path / "edges.jsonl"

        nodes_file.write_text('{"id": "n1"}\n')
        edges_file.write_text('{"source": "n1", "target": "n2"}\n')

        nodes, edges = load_graph(tmp_path)
        assert len(nodes) == 1
        assert len(edges) == 1


class TestJSONErrorHandling:
    """Tests for JSON parsing error handling."""

    def test_load_nodes_invalid_json(self, tmp_path):
        """Test that invalid JSON raises error with line number."""
        nodes_file = tmp_path / "nodes.jsonl"
        nodes_file.write_text(
            '{"id": "valid"}\n'
            'invalid json line\n'
            '{"id": "another"}\n'
        )

        with pytest.raises(json.JSONDecodeError) as exc_info:
            list(load_nodes(nodes_file))

        # Should include line number in error
        assert "line 2" in str(exc_info.value)

    def test_load_edges_invalid_json(self, tmp_path):
        """Test that invalid JSON in edges raises error with line number."""
        edges_file = tmp_path / "edges.jsonl"
        edges_file.write_text(
            '{"source": "a", "target": "b"}\n'
            '{"broken: json}\n'
        )

        with pytest.raises(json.JSONDecodeError) as exc_info:
            list(load_edges(edges_file))

        assert "line 2" in str(exc_info.value)

    def test_load_nodes_includes_filename(self, tmp_path):
        """Test that error includes filename."""
        nodes_file = tmp_path / "my_nodes.jsonl"
        nodes_file.write_text('not json\n')

        with pytest.raises(json.JSONDecodeError) as exc_info:
            list(load_nodes(nodes_file))

        assert "my_nodes.jsonl" in str(exc_info.value)


class TestExportAllThreads:
    """Tests for export_all_threads function."""

    def test_export_all_threads(self, tmp_path):
        """Test exporting all threads from directory."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # Create test thread
        (threads_dir / "test-topic.md").write_text("""# Test Topic
Status: OPEN
Ball: Claude
Last-Updated: 2024-01-01

---

## Entry 1

Content here.
""")

        output_dir = tmp_path / "graph"
        config = SummarizerConfig(prefer_extractive=True)
        manifest = export_all_threads(threads_dir, output_dir, config=config)

        assert manifest["threads_exported"] == 1
        assert (output_dir / "nodes.jsonl").exists()
        assert (output_dir / "edges.jsonl").exists()
        assert (output_dir / "manifest.json").exists()

    def test_export_clears_existing_files(self, tmp_path):
        """Test that export clears existing files before writing."""
        output_dir = tmp_path / "graph"
        output_dir.mkdir()

        # Create existing files with old data
        (output_dir / "nodes.jsonl").write_text('{"old": "data"}\n')
        (output_dir / "edges.jsonl").write_text('{"old": "edge"}\n')

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        # Create a thread so files get re-created
        (threads_dir / "test.md").write_text("""# Test
Status: OPEN
Ball: User
Last-Updated: 2024-01-01
---
## Entry
Content.
""")

        config = SummarizerConfig(prefer_extractive=True)
        export_all_threads(threads_dir, output_dir, config=config)

        # Old data should be gone, replaced with new thread data
        nodes_content = (output_dir / "nodes.jsonl").read_text()
        assert '{"old": "data"}' not in nodes_content
        assert "thread:test" in nodes_content  # New data present
