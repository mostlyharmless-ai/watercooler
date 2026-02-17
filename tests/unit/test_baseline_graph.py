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
    _build_summary_messages,
    _extract_headers,
    _extract_tags,
    _strip_tags_from_summary,
    _truncate_text,
)
from watercooler.models import (
    get_model_family,
    get_model_prompt_defaults,
)


# =============================================================================
# SummarizerConfig Tests
# =============================================================================


class TestSummarizerConfig:
    """Tests for SummarizerConfig."""

    def test_defaults(self, monkeypatch, isolated_config):
        """Test default configuration values.

        Uses isolated_config fixture to avoid loading user's ~/.watercooler/config.toml.
        """
        from watercooler.config_facade import config

        # Clear env vars that might override defaults
        monkeypatch.delenv("LLM_API_BASE", raising=False)
        monkeypatch.delenv("LLM_MODEL", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.delenv("BASELINE_GRAPH_API_BASE", raising=False)
        monkeypatch.delenv("BASELINE_GRAPH_MODEL", raising=False)
        monkeypatch.delenv("BASELINE_GRAPH_API_KEY", raising=False)
        config.reset()

        summarizer_config = SummarizerConfig()
        assert summarizer_config.api_base == "http://localhost:8000/v1"  # llama-server port
        assert summarizer_config.model == "qwen3:1.7b"
        assert summarizer_config.api_key == ""  # Empty for local llama-server
        assert summarizer_config.timeout == 30.0
        assert summarizer_config.max_tokens == 256
        assert summarizer_config.extractive_max_chars == 200
        assert summarizer_config.include_headers is True
        assert summarizer_config.max_headers == 3
        assert summarizer_config.prefer_extractive is False

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

    def test_from_config_dict_empty(self, monkeypatch, isolated_config):
        """Test creating config from empty dictionary uses defaults.

        Uses isolated_config fixture to avoid loading user's ~/.watercooler/config.toml.
        """
        from watercooler.config_facade import config

        monkeypatch.delenv("LLM_API_BASE", raising=False)
        monkeypatch.delenv("LLM_MODEL", raising=False)
        monkeypatch.delenv("BASELINE_GRAPH_API_BASE", raising=False)
        monkeypatch.delenv("BASELINE_GRAPH_MODEL", raising=False)
        config.reset()

        summarizer_config = SummarizerConfig.from_config_dict({})
        assert summarizer_config.api_base == "http://localhost:8000/v1"  # llama-server port
        assert summarizer_config.model == "qwen3:1.7b"

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


# =============================================================================
# Model Family Detection Tests
# =============================================================================


class TestGetModelFamily:
    """Tests for get_model_family() function."""

    def test_qwen3_family(self):
        """Test Qwen3 model family detection."""
        assert get_model_family("qwen3:1.7b") == "qwen3"
        assert get_model_family("qwen3:30b") == "qwen3"
        assert get_model_family("Qwen3:1.7b") == "qwen3"  # Case insensitive
        assert get_model_family("QWEN3:8B") == "qwen3"

    def test_qwen2_5_family(self):
        """Test Qwen2.5 model family detection."""
        assert get_model_family("qwen2.5:3b") == "qwen2.5"
        assert get_model_family("qwen2.5:7b") == "qwen2.5"
        assert get_model_family("Qwen2.5:3b") == "qwen2.5"  # Case insensitive

    def test_llama_family(self):
        """Test Llama model family detection."""
        assert get_model_family("llama3.2:3b") == "llama"
        assert get_model_family("llama3:8b") == "llama"
        assert get_model_family("Llama3.2:3B") == "llama"  # Case insensitive

    def test_smollm2_family(self):
        """Test SmolLM2 model family detection."""
        assert get_model_family("smollm2:1.7b") == "smollm2"
        assert get_model_family("SmolLM2:360m") == "smollm2"

    def test_phi3_family(self):
        """Test Phi-3 model family detection."""
        assert get_model_family("phi3:3.8b") == "phi3"
        assert get_model_family("Phi-3:mini") == "phi3"
        assert get_model_family("phi-3:3.8b") == "phi3"

    def test_unknown_model_returns_default(self):
        """Test unknown models return 'default' family."""
        assert get_model_family("unknown-model") == "default"
        assert get_model_family("gpt-4o") == "default"
        assert get_model_family("claude-3") == "default"

    def test_empty_model_returns_default(self):
        """Test empty model name returns 'default' family."""
        assert get_model_family("") == "default"


class TestGetModelPromptDefaults:
    """Tests for get_model_prompt_defaults() function."""

    def test_qwen3_uses_no_think_prefix(self):
        """Test Qwen3 models get /no_think prefix."""
        defaults = get_model_prompt_defaults("qwen3:1.7b")
        assert defaults["prompt_prefix"] == "/no_think "
        assert defaults["system_prompt"] == ""  # No system prompt

    def test_qwen2_5_uses_system_prompt(self):
        """Test Qwen2.5 models get system prompt instead of prefix."""
        defaults = get_model_prompt_defaults("qwen2.5:3b")
        assert defaults["prompt_prefix"] == ""
        assert "summarize" in defaults["system_prompt"].lower()

    def test_llama_uses_system_prompt(self):
        """Test Llama models get system prompt."""
        defaults = get_model_prompt_defaults("llama3.2:3b")
        assert defaults["prompt_prefix"] == ""
        assert "summarize" in defaults["system_prompt"].lower()

    def test_unknown_model_uses_default(self):
        """Test unknown models get default prompting."""
        defaults = get_model_prompt_defaults("unknown-model")
        assert defaults["prompt_prefix"] == ""
        assert "summarize" in defaults["system_prompt"].lower()

    def test_returns_both_keys(self):
        """Test all models return both prompt_prefix and system_prompt keys."""
        for model in ["qwen3:1.7b", "qwen2.5:3b", "llama3.2:3b", "unknown"]:
            defaults = get_model_prompt_defaults(model)
            assert "prompt_prefix" in defaults
            assert "system_prompt" in defaults


# =============================================================================
# Message Building Tests
# =============================================================================


class TestBuildSummaryMessages:
    """Tests for _build_summary_messages() function."""

    def test_qwen3_includes_no_think_prefix(self):
        """Test Qwen3 model messages include /no_think prefix."""
        config = SummarizerConfig(model="qwen3:1.7b")
        messages = _build_summary_messages(
            entry_body="Test content",
            entry_title="Test Title",
            entry_type="Note",
            config=config,
        )

        # Should have user message with /no_think prefix
        user_msg = next(m for m in messages if m["role"] == "user")
        assert user_msg["content"].startswith("/no_think")

        # Should not have system message (Qwen3 doesn't use one)
        system_msgs = [m for m in messages if m["role"] == "system"]
        assert len(system_msgs) == 0 or system_msgs[0]["content"] == ""

    def test_qwen2_5_includes_system_prompt(self):
        """Test Qwen2.5 model messages include system prompt."""
        config = SummarizerConfig(model="qwen2.5:3b")
        messages = _build_summary_messages(
            entry_body="Test content",
            entry_title="Test Title",
            entry_type="Note",
            config=config,
        )

        # Should have system message with summarization instruction
        system_msgs = [m for m in messages if m["role"] == "system"]
        assert len(system_msgs) == 1
        assert "summarize" in system_msgs[0]["content"].lower()

        # User message should not have /no_think prefix
        user_msg = next(m for m in messages if m["role"] == "user")
        assert not user_msg["content"].startswith("/no_think")

    def test_includes_entry_context(self):
        """Test messages include entry title and type."""
        config = SummarizerConfig(model="llama3.2:3b")
        messages = _build_summary_messages(
            entry_body="Test content",
            entry_title="My Entry Title",
            entry_type="Decision",
            config=config,
        )

        user_msg = next(m for m in messages if m["role"] == "user")
        assert "My Entry Title" in user_msg["content"]
        assert "Decision" in user_msg["content"]

    def test_includes_few_shot_example(self):
        """Test messages include few-shot example when configured."""
        config = SummarizerConfig(
            model="llama3.2:3b",
            summary_example_input="Example input text",
            summary_example_output="Example output with\ntags: #example",
        )
        messages = _build_summary_messages(
            entry_body="Test content",
            entry_title=None,
            entry_type=None,
            config=config,
        )

        user_msg = next(m for m in messages if m["role"] == "user")
        assert "Example input text" in user_msg["content"]
        assert "Example output with" in user_msg["content"]

    def test_config_overrides_auto_detect(self):
        """Test explicit config values override auto-detection."""
        # Qwen3 would normally use /no_think, but explicit prefix overrides
        config = SummarizerConfig(
            model="qwen3:1.7b",
            system_prompt="Custom system prompt",
            prompt_prefix="CUSTOM_PREFIX ",  # Custom prefix overrides auto-detect
        )
        messages = _build_summary_messages(
            entry_body="Test content",
            entry_title=None,
            entry_type=None,
            config=config,
        )

        # Should use custom system prompt
        system_msgs = [m for m in messages if m["role"] == "system"]
        assert len(system_msgs) == 1
        assert system_msgs[0]["content"] == "Custom system prompt"

        # User message should have custom prefix (not auto-detected /no_think)
        user_msg = next(m for m in messages if m["role"] == "user")
        assert user_msg["content"].startswith("CUSTOM_PREFIX")
        assert not user_msg["content"].startswith("/no_think")

    def test_empty_prompt_prefix_triggers_auto_detect(self):
        """Test empty prompt_prefix means auto-detect (documented behavior)."""
        # Empty string intentionally means "auto-detect based on model"
        config = SummarizerConfig(
            model="qwen3:1.7b",
            prompt_prefix="",  # Empty = use auto-detected /no_think
        )
        messages = _build_summary_messages(
            entry_body="Test content",
            entry_title=None,
            entry_type=None,
            config=config,
        )

        # Qwen3 should get /no_think prefix via auto-detection
        user_msg = next(m for m in messages if m["role"] == "user")
        assert user_msg["content"].startswith("/no_think")

    def test_empty_body_handled(self):
        """Test empty entry body is handled gracefully."""
        config = SummarizerConfig(model="llama3.2:3b")
        messages = _build_summary_messages(
            entry_body="",
            entry_title=None,
            entry_type=None,
            config=config,
        )

        # Should still return valid messages
        assert len(messages) >= 1
        user_msg = next(m for m in messages if m["role"] == "user")
        assert user_msg["content"]  # Not empty


# =============================================================================
# Tag Extraction Tests
# =============================================================================


class TestExtractTags:
    """Tests for _extract_tags() function."""

    def test_extract_tags_from_tags_line(self):
        """Test extracting tags from 'tags: #foo #bar' format."""
        text = "Summary of the entry.\ntags: #authentication #OAuth2 #JWT"
        tags = _extract_tags(text)
        assert tags == ["authentication", "jwt", "oauth2"]

    def test_extract_tags_case_insensitive(self):
        """Test tags are normalized to lowercase."""
        text = "tags: #Authentication #OAUTH2 #jwt"
        tags = _extract_tags(text)
        assert tags == ["authentication", "jwt", "oauth2"]

    def test_extract_tags_deduplication(self):
        """Test duplicate tags are removed."""
        text = "tags: #auth #Auth #AUTH #security"
        tags = _extract_tags(text)
        assert tags == ["auth", "security"]

    def test_extract_standalone_hashtags(self):
        """Test extracting standalone hashtags when no tags: line."""
        text = "Implemented #authentication with #JWT tokens"
        tags = _extract_tags(text)
        assert tags == ["authentication", "jwt"]

    def test_extract_tags_empty_string(self):
        """Test empty string returns empty list."""
        assert _extract_tags("") == []
        assert _extract_tags(None) == []

    def test_extract_tags_no_tags(self):
        """Test text without tags returns empty list."""
        text = "Just a regular summary without any tags."
        assert _extract_tags(text) == []

    def test_extract_tags_sorted(self):
        """Test tags are returned sorted alphabetically."""
        text = "tags: #zebra #alpha #middle"
        tags = _extract_tags(text)
        assert tags == ["alpha", "middle", "zebra"]


class TestStripTagsFromSummary:
    """Tests for _strip_tags_from_summary() function."""

    def test_strip_tags_line(self):
        """Test removing tags line from summary."""
        text = "Summary of the entry.\ntags: #foo #bar #baz"
        result = _strip_tags_from_summary(text)
        assert result == "Summary of the entry."

    def test_strip_tags_preserves_content(self):
        """Test non-tag content is preserved."""
        text = "First line.\nSecond line.\ntags: #test"
        result = _strip_tags_from_summary(text)
        assert result == "First line.\nSecond line."

    def test_strip_tags_no_tags(self):
        """Test text without tags is returned unchanged."""
        text = "Summary without tags."
        result = _strip_tags_from_summary(text)
        assert result == "Summary without tags."

    def test_strip_tags_empty_string(self):
        """Test empty string returns empty string."""
        assert _strip_tags_from_summary("") == ""

    def test_strip_tags_only_tags(self):
        """Test summary that is only tags returns empty."""
        text = "tags: #only #tags"
        result = _strip_tags_from_summary(text)
        assert result == ""


class TestSummarizeThreadTagAggregation:
    """Tests for tag aggregation in summarize_thread()."""

    def test_thread_summary_aggregates_tags_from_entries(self):
        """Test that thread summary aggregates tags from entry summaries."""
        entries = [
            {
                "title": "Entry 1",
                "body": "Content 1",
                "summary": "Summary 1.\ntags: #auth #security",
            },
            {
                "title": "Entry 2",
                "body": "Content 2",
                "summary": "Summary 2.\ntags: #api #security",
            },
        ]
        config = SummarizerConfig(prefer_extractive=True)
        result = summarize_thread(entries, thread_title="Test", config=config)

        # Should contain aggregated, deduplicated tags
        assert "tags:" in result
        assert "#api" in result
        assert "#auth" in result
        assert "#security" in result

    def test_thread_summary_deduplicates_tags(self):
        """Test that duplicate tags across entries are deduplicated."""
        entries = [
            {"title": "E1", "body": "C1", "summary": "S1.\ntags: #auth #jwt"},
            {"title": "E2", "body": "C2", "summary": "S2.\ntags: #auth #oauth"},
        ]
        config = SummarizerConfig(prefer_extractive=True)
        result = summarize_thread(entries, thread_title="Test", config=config)

        # Count occurrences of #auth - should only appear once
        assert result.count("#auth") == 1

    def test_thread_summary_sorts_tags(self):
        """Test that aggregated tags are sorted alphabetically."""
        entries = [
            {"title": "E1", "body": "C1", "summary": "S1.\ntags: #zebra #alpha"},
        ]
        config = SummarizerConfig(prefer_extractive=True)
        result = summarize_thread(entries, thread_title="Test", config=config)

        # Tags should be sorted
        tags_line = [line for line in result.split("\n") if line.startswith("tags:")][0]
        assert tags_line.index("#alpha") < tags_line.index("#zebra")

    def test_thread_summary_no_tags_if_entries_have_none(self):
        """Test no tags line if entries don't have tags."""
        entries = [
            {"title": "E1", "body": "Content without tags", "summary": "Summary 1."},
            {"title": "E2", "body": "More content", "summary": "Summary 2."},
        ]
        config = SummarizerConfig(prefer_extractive=True)
        result = summarize_thread(entries, thread_title="Test", config=config)

        # Should not have a tags line
        assert "tags:" not in result
