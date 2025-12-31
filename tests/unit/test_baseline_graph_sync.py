"""Tests for baseline graph sync module."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from watercooler.baseline_graph.sync import (
    EmbeddingConfig,
    GraphHealthReport,
    GraphSyncState,
    _atomic_append_jsonl,
    _atomic_write_json,
    check_graph_health,
    generate_embedding,
    get_graph_sync_state,
    is_embedding_available,
    reconcile_graph,
    record_graph_sync_error,
    should_update_thread_summary,
    sync_entry_to_graph,
    sync_thread_to_graph,
)
from watercooler.baseline_graph.parser import ParsedThread, ParsedEntry
from watercooler.baseline_graph.summarizer import (
    SummarizerConfig,
    is_llm_service_available,
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def threads_dir(tmp_path: Path) -> Path:
    """Create a temporary threads directory with a sample thread."""
    threads = tmp_path / "threads"
    threads.mkdir()
    return threads


@pytest.fixture
def sample_thread(threads_dir: Path) -> Path:
    """Create a sample thread file."""
    thread_content = """# test-topic — Thread
Status: OPEN
Ball: Claude (user)
Topic: test-topic
Created: 2025-01-01T00:00:00Z

---
Entry: Claude (user) 2025-01-01T00:00:00Z
Role: planner
Type: Note
Title: First Entry

This is the first entry body.
<!-- Entry-ID: 01TEST00000000000000000001 -->

---
Entry: Claude (user) 2025-01-01T01:00:00Z
Role: implementer
Type: Note
Title: Second Entry

This is the second entry body.
<!-- Entry-ID: 01TEST00000000000000000002 -->
"""
    thread_path = threads_dir / "test-topic.md"
    thread_path.write_text(thread_content, encoding="utf-8")
    return thread_path


@pytest.fixture
def graph_dir(threads_dir: Path) -> Path:
    """Create graph output directory."""
    gd = threads_dir / "graph" / "baseline"
    gd.mkdir(parents=True)
    return gd


# ============================================================================
# Atomic File Operations Tests
# ============================================================================


def test_atomic_write_json_creates_file(tmp_path: Path):
    """Test atomic JSON write creates file correctly."""
    target = tmp_path / "test.json"
    data = {"key": "value", "number": 42}

    _atomic_write_json(target, data)

    assert target.exists()
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded == data


def test_atomic_write_json_overwrites(tmp_path: Path):
    """Test atomic JSON write overwrites existing file."""
    target = tmp_path / "test.json"
    target.write_text('{"old": "data"}', encoding="utf-8")

    _atomic_write_json(target, {"new": "data"})

    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded == {"new": "data"}


def test_atomic_append_jsonl_creates_file(tmp_path: Path):
    """Test atomic JSONL append creates file correctly."""
    target = tmp_path / "test.jsonl"
    items = [{"id": "1", "value": "a"}, {"id": "2", "value": "b"}]

    _atomic_append_jsonl(target, items)

    assert target.exists()
    lines = target.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["id"] == "1"
    assert json.loads(lines[1])["id"] == "2"


def test_atomic_append_jsonl_deduplicates(tmp_path: Path):
    """Test atomic JSONL append deduplicates by ID."""
    target = tmp_path / "test.jsonl"

    # First write
    _atomic_append_jsonl(target, [{"id": "1", "value": "a"}])

    # Second write with same ID (should update)
    _atomic_append_jsonl(target, [{"id": "1", "value": "updated"}])

    lines = target.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    assert json.loads(lines[0])["value"] == "updated"


def test_atomic_append_jsonl_merges_new(tmp_path: Path):
    """Test atomic JSONL append merges new items."""
    target = tmp_path / "test.jsonl"

    # First write
    _atomic_append_jsonl(target, [{"id": "1", "value": "a"}])

    # Second write with new ID
    _atomic_append_jsonl(target, [{"id": "2", "value": "b"}])

    lines = target.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2


# ============================================================================
# Graph Sync State Tests
# ============================================================================


def test_get_graph_sync_state_none_when_missing(threads_dir: Path):
    """Test get_graph_sync_state returns None when no state exists."""
    state = get_graph_sync_state(threads_dir, "nonexistent")
    assert state is None


def test_record_graph_sync_error_creates_state(threads_dir: Path, graph_dir: Path):
    """Test record_graph_sync_error creates error state."""
    record_graph_sync_error(
        threads_dir, "test-topic", "entry123", Exception("Test error")
    )

    state = get_graph_sync_state(threads_dir, "test-topic")
    assert state is not None
    assert state.status == "error"
    assert "Test error" in state.error_message


def test_graph_sync_state_round_trip(threads_dir: Path, graph_dir: Path):
    """Test graph sync state can be written and read."""
    # Record an error first to create state file
    record_graph_sync_error(
        threads_dir, "test-topic", "entry123", Exception("Test error")
    )

    state = get_graph_sync_state(threads_dir, "test-topic")
    assert state.status == "error"
    assert state.error_message == "Test error"


# ============================================================================
# Entry Sync Tests
# ============================================================================


def test_sync_entry_to_graph_creates_nodes(threads_dir: Path, sample_thread: Path):
    """Test sync_entry_to_graph creates nodes and edges."""
    success = sync_entry_to_graph(threads_dir, "test-topic")

    assert success

    # Check nodes file
    nodes_file = threads_dir / "graph" / "baseline" / "nodes.jsonl"
    assert nodes_file.exists()

    nodes = []
    for line in nodes_file.read_text(encoding="utf-8").strip().split("\n"):
        nodes.append(json.loads(line))

    # Should have thread node + entry node (at minimum)
    assert len(nodes) >= 2

    # Check for thread node
    thread_nodes = [n for n in nodes if n.get("type") == "thread"]
    assert len(thread_nodes) >= 1
    assert thread_nodes[0]["topic"] == "test-topic"

    # Check for entry node
    entry_nodes = [n for n in nodes if n.get("type") == "entry"]
    assert len(entry_nodes) >= 1


def test_sync_entry_to_graph_creates_edges(threads_dir: Path, sample_thread: Path):
    """Test sync_entry_to_graph creates edges."""
    sync_entry_to_graph(threads_dir, "test-topic")

    edges_file = threads_dir / "graph" / "baseline" / "edges.jsonl"
    assert edges_file.exists()

    edges = []
    for line in edges_file.read_text(encoding="utf-8").strip().split("\n"):
        edges.append(json.loads(line))

    # Should have at least one "contains" edge
    contains_edges = [e for e in edges if e.get("type") == "contains"]
    assert len(contains_edges) >= 1


def test_sync_entry_to_graph_updates_state(threads_dir: Path, sample_thread: Path):
    """Test sync_entry_to_graph updates sync state."""
    sync_entry_to_graph(threads_dir, "test-topic")

    state = get_graph_sync_state(threads_dir, "test-topic")
    assert state is not None
    assert state.status == "ok"
    assert state.last_synced_entry_id is not None


def test_sync_entry_to_graph_nonexistent_thread(threads_dir: Path):
    """Test sync_entry_to_graph returns False for nonexistent thread."""
    success = sync_entry_to_graph(threads_dir, "nonexistent")
    assert not success


def test_sync_entry_with_specific_entry_id(threads_dir: Path, sample_thread: Path):
    """Test sync_entry_to_graph with specific entry ID."""
    # First sync to create initial state
    sync_thread_to_graph(threads_dir, "test-topic")

    # Sync specific entry
    success = sync_entry_to_graph(
        threads_dir, "test-topic", entry_id="01TEST00000000000000000001"
    )

    assert success


# ============================================================================
# Thread Sync Tests
# ============================================================================


def test_sync_thread_to_graph_full_sync(threads_dir: Path, sample_thread: Path):
    """Test sync_thread_to_graph performs full sync."""
    success = sync_thread_to_graph(threads_dir, "test-topic")

    assert success

    # Check nodes
    nodes_file = threads_dir / "graph" / "baseline" / "nodes.jsonl"
    nodes = []
    for line in nodes_file.read_text(encoding="utf-8").strip().split("\n"):
        nodes.append(json.loads(line))

    # Should have 1 thread + 2 entries = 3 nodes
    assert len(nodes) == 3

    # Check state
    state = get_graph_sync_state(threads_dir, "test-topic")
    assert state.entries_synced == 2


# ============================================================================
# Health Check Tests
# ============================================================================


def test_check_graph_health_no_state(threads_dir: Path, sample_thread: Path):
    """Test check_graph_health reports stale threads when no state."""
    report = check_graph_health(threads_dir)

    assert not report.healthy
    assert report.total_threads == 1
    assert "test-topic" in report.stale_threads


def test_check_graph_health_after_sync(threads_dir: Path, sample_thread: Path):
    """Test check_graph_health reports healthy after sync."""
    sync_thread_to_graph(threads_dir, "test-topic")

    report = check_graph_health(threads_dir)

    assert report.healthy
    assert report.synced_threads == 1
    assert report.error_threads == 0
    assert len(report.stale_threads) == 0


def test_check_graph_health_with_errors(threads_dir: Path, sample_thread: Path):
    """Test check_graph_health reports error threads."""
    # Create graph dir first
    (threads_dir / "graph" / "baseline").mkdir(parents=True)

    # Record an error
    record_graph_sync_error(
        threads_dir, "test-topic", None, Exception("Sync failed")
    )

    report = check_graph_health(threads_dir)

    assert not report.healthy
    assert report.error_threads == 1
    assert "test-topic" in report.error_details


# ============================================================================
# Reconciliation Tests
# ============================================================================


def test_reconcile_graph_fixes_stale(threads_dir: Path, sample_thread: Path):
    """Test reconcile_graph fixes stale threads."""
    # Check health shows stale
    report_before = check_graph_health(threads_dir)
    assert not report_before.healthy

    # Reconcile
    results = reconcile_graph(threads_dir)

    assert results.get("test-topic") is True

    # Check health shows healthy
    report_after = check_graph_health(threads_dir)
    assert report_after.healthy


def test_reconcile_graph_specific_topics(threads_dir: Path, sample_thread: Path):
    """Test reconcile_graph for specific topics."""
    results = reconcile_graph(threads_dir, topics=["test-topic"])

    assert results == {"test-topic": True}


# ============================================================================
# Concurrency Tests
# ============================================================================


def test_concurrent_sync_operations(threads_dir: Path, sample_thread: Path):
    """Test concurrent sync operations don't corrupt JSONL."""
    results = []
    errors = []

    def sync_thread():
        try:
            success = sync_thread_to_graph(threads_dir, "test-topic")
            results.append(success)
        except Exception as e:
            errors.append(e)

    # Run multiple syncs concurrently
    threads = [threading.Thread(target=sync_thread) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All should succeed (atomic writes prevent corruption)
    assert all(results), f"Some syncs failed: {errors}"
    assert len(errors) == 0

    # Verify JSONL is valid
    nodes_file = threads_dir / "graph" / "baseline" / "nodes.jsonl"
    lines = nodes_file.read_text(encoding="utf-8").strip().split("\n")
    for line in lines:
        json.loads(line)  # Should not raise


def test_sync_failure_does_not_block(threads_dir: Path, sample_thread: Path):
    """Test that sync failure is recorded but doesn't raise."""
    # Create graph dir
    (threads_dir / "graph" / "baseline").mkdir(parents=True)

    # Mock parse_thread_file to raise
    with patch(
        "watercooler.baseline_graph.sync.parse_thread_file",
        side_effect=Exception("Parse failed"),
    ):
        success = sync_entry_to_graph(threads_dir, "test-topic")

    # Should return False, not raise
    assert not success

    # Error should be recorded
    state = get_graph_sync_state(threads_dir, "test-topic")
    assert state.status == "error"


# ============================================================================
# Manifest Tests
# ============================================================================


def test_manifest_updated_on_sync(threads_dir: Path, sample_thread: Path):
    """Test manifest is updated after sync."""
    sync_thread_to_graph(threads_dir, "test-topic")

    manifest_path = threads_dir / "graph" / "baseline" / "manifest.json"
    assert manifest_path.exists()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "schema_version" in manifest
    assert "last_updated" in manifest
    assert "test-topic" in manifest.get("topics_synced", {})


def test_manifest_preserves_other_topics(threads_dir: Path, sample_thread: Path):
    """Test manifest preserves data from other topics."""
    # Sync first topic
    sync_thread_to_graph(threads_dir, "test-topic")

    # Create another thread
    (threads_dir / "other-topic.md").write_text(
        """# other-topic — Thread
Status: OPEN
Ball: User
Topic: other-topic
Created: 2025-01-01T00:00:00Z

---
Entry: User 2025-01-01T00:00:00Z
Role: planner
Type: Note
Title: Other Entry

Body text.
<!-- Entry-ID: 01OTHER0000000000000000001 -->
""",
        encoding="utf-8",
    )

    # Sync second topic
    sync_thread_to_graph(threads_dir, "other-topic")

    # Both should be in manifest
    manifest_path = threads_dir / "graph" / "baseline" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    topics_synced = manifest.get("topics_synced", {})
    assert "test-topic" in topics_synced
    assert "other-topic" in topics_synced


# ============================================================================
# Arc Change Detection Tests
# ============================================================================


def _make_parsed_entry(
    entry_id: str,
    index: int,
    entry_type: str = "Note",
    title: str = "Test Entry",
) -> ParsedEntry:
    """Create a ParsedEntry for testing."""
    return ParsedEntry(
        entry_id=entry_id,
        index=index,
        agent="Claude",
        role="implementer",
        entry_type=entry_type,
        title=title,
        timestamp="2025-01-01T00:00:00Z",
        body="Test body content.",
        summary="",
    )


def _make_parsed_thread(
    topic: str,
    entries: list,
    status: str = "OPEN",
) -> ParsedThread:
    """Create a ParsedThread for testing."""
    return ParsedThread(
        topic=topic,
        title=f"{topic} Thread",
        status=status,
        ball="Claude",
        last_updated="2025-01-01T00:00:00Z",
        summary="",
        entries=entries,
    )


def test_should_update_summary_first_entry():
    """Test summary update triggers on first entry."""
    entry = _make_parsed_entry("01TEST001", 0)
    thread = _make_parsed_thread("test", [entry])

    assert should_update_thread_summary(thread, entry, previous_entry_count=0)


def test_should_update_summary_second_entry():
    """Test summary update triggers on second entry."""
    entries = [
        _make_parsed_entry("01TEST001", 0),
        _make_parsed_entry("01TEST002", 1),
    ]
    thread = _make_parsed_thread("test", entries)

    assert should_update_thread_summary(thread, entries[1], previous_entry_count=1)


def test_should_update_summary_third_entry():
    """Test summary update triggers on third entry."""
    entries = [
        _make_parsed_entry("01TEST001", 0),
        _make_parsed_entry("01TEST002", 1),
        _make_parsed_entry("01TEST003", 2),
    ]
    thread = _make_parsed_thread("test", entries)

    assert should_update_thread_summary(thread, entries[2], previous_entry_count=2)


def test_should_update_summary_closure_entry():
    """Test summary update triggers on Closure entry."""
    entries = [
        _make_parsed_entry("01TEST001", 0),
        _make_parsed_entry("01TEST002", 1),
        _make_parsed_entry("01TEST003", 2),
        _make_parsed_entry("01TEST004", 3),
        _make_parsed_entry("01TEST005", 4, entry_type="Closure"),
    ]
    thread = _make_parsed_thread("test", entries)

    assert should_update_thread_summary(thread, entries[4], previous_entry_count=4)


def test_should_update_summary_decision_entry():
    """Test summary update triggers on Decision entry."""
    entries = [
        _make_parsed_entry("01TEST001", 0),
        _make_parsed_entry("01TEST002", 1),
        _make_parsed_entry("01TEST003", 2),
        _make_parsed_entry("01TEST004", 3),
        _make_parsed_entry("01TEST005", 4, entry_type="Decision"),
    ]
    thread = _make_parsed_thread("test", entries)

    assert should_update_thread_summary(thread, entries[4], previous_entry_count=4)


def test_should_update_summary_plan_entry():
    """Test summary update triggers on Plan entry."""
    entries = [
        _make_parsed_entry("01TEST001", 0),
        _make_parsed_entry("01TEST002", 1),
        _make_parsed_entry("01TEST003", 2),
        _make_parsed_entry("01TEST004", 3),
        _make_parsed_entry("01TEST005", 4, entry_type="Plan"),
    ]
    thread = _make_parsed_thread("test", entries)

    assert should_update_thread_summary(thread, entries[4], previous_entry_count=4)


def test_should_update_summary_significant_growth():
    """Test summary update triggers on 50%+ growth."""
    entries = [_make_parsed_entry(f"01TEST{i:03d}", i) for i in range(6)]
    thread = _make_parsed_thread("test", entries)

    # 6 entries vs 4 previous = 50% growth
    assert should_update_thread_summary(thread, entries[5], previous_entry_count=4)


def test_should_update_summary_every_tenth():
    """Test summary update triggers every 10th entry."""
    entries = [_make_parsed_entry(f"01TEST{i:03d}", i) for i in range(10)]
    thread = _make_parsed_thread("test", entries)

    # 10th entry (index 9) should trigger
    assert should_update_thread_summary(thread, entries[9], previous_entry_count=9)


def test_should_not_update_summary_regular_note():
    """Test no summary update for regular Note in middle of thread."""
    entries = [_make_parsed_entry(f"01TEST{i:03d}", i) for i in range(5)]
    thread = _make_parsed_thread("test", entries)

    # 5th entry (index 4), not a special type, not significant growth
    assert not should_update_thread_summary(thread, entries[4], previous_entry_count=4)


# ============================================================================
# Embedding Config Tests
# ============================================================================


def test_embedding_config_defaults():
    """Test EmbeddingConfig has sensible defaults."""
    config = EmbeddingConfig()

    assert config.api_base == "http://localhost:8080/v1"
    assert config.model == "bge-m3"
    assert config.timeout == 30.0


def test_embedding_config_custom():
    """Test EmbeddingConfig accepts custom values."""
    config = EmbeddingConfig(
        api_base="http://custom:9000/v1",
        model="custom-model",
        timeout=60.0,
    )

    assert config.api_base == "http://custom:9000/v1"
    assert config.model == "custom-model"
    assert config.timeout == 60.0


def test_is_embedding_available_no_server():
    """Test is_embedding_available returns False when server unavailable.

    Uses an invalid port to guarantee connection failure.
    """
    from watercooler.baseline_graph.sync import EmbeddingConfig

    # Use invalid port to ensure no server responds
    config = EmbeddingConfig(api_base="http://localhost:1/v1")
    assert not is_embedding_available(config)


def test_is_llm_service_available_no_server():
    """Test is_llm_service_available returns False when server unavailable.

    Uses an invalid port to guarantee connection failure.
    """
    # Use invalid port to ensure no server responds
    config = SummarizerConfig(api_base="http://localhost:1/v1")
    assert not is_llm_service_available(config)


def test_generate_embedding_no_server():
    """Test generate_embedding returns None when server unavailable.

    Uses an invalid port to guarantee connection failure.
    """
    from watercooler.baseline_graph.sync import EmbeddingConfig

    # Use invalid port to ensure no server responds
    config = EmbeddingConfig(api_base="http://localhost:1/v1")
    result = generate_embedding("test text", config)
    assert result is None


# ============================================================================
# Sync with Embeddings Tests
# ============================================================================


def test_sync_entry_with_embeddings_flag(threads_dir: Path, sample_thread: Path, monkeypatch):
    """Test sync_entry_to_graph respects generate_embeddings flag."""
    # Track if embedding was attempted
    embedding_called = []

    def mock_generate_embedding(text, config=None):
        embedding_called.append(text)
        return [0.1, 0.2, 0.3]  # Mock embedding vector

    monkeypatch.setattr(
        "watercooler.baseline_graph.sync.generate_embedding",
        mock_generate_embedding,
    )
    # Mock service availability check
    monkeypatch.setattr(
        "watercooler.baseline_graph.sync.is_embedding_available",
        lambda config=None: True,
    )

    # Sync with embeddings enabled
    success = sync_entry_to_graph(
        threads_dir, "test-topic", generate_embeddings=True
    )

    assert success
    assert len(embedding_called) > 0  # Embedding was generated


def test_sync_entry_without_embeddings_flag(threads_dir: Path, sample_thread: Path, monkeypatch):
    """Test sync_entry_to_graph skips embeddings when disabled."""
    embedding_called = []

    def mock_generate_embedding(text, config=None):
        embedding_called.append(text)
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(
        "watercooler.baseline_graph.sync.generate_embedding",
        mock_generate_embedding,
    )

    # Sync with embeddings disabled (default)
    success = sync_entry_to_graph(threads_dir, "test-topic", generate_embeddings=False)

    assert success
    assert len(embedding_called) == 0  # Embedding was not generated


def test_reconcile_graph_with_embeddings(threads_dir: Path, sample_thread: Path, monkeypatch):
    """Test reconcile_graph passes generate_embeddings to sync."""
    embedding_called = []

    def mock_generate_embedding(text, config=None):
        embedding_called.append(text)
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(
        "watercooler.baseline_graph.sync.generate_embedding",
        mock_generate_embedding,
    )

    # Reconcile with embeddings enabled
    results = reconcile_graph(
        threads_dir,
        topics=["test-topic"],
        generate_embeddings=True,
    )

    assert results.get("test-topic") is True
    assert len(embedding_called) > 0


# ============================================================================
# Auto-Start Service Tests
# ============================================================================


def test_should_auto_start_services_disabled_by_default(monkeypatch):
    """Test _should_auto_start_services returns False by default."""
    from watercooler.baseline_graph.sync import _should_auto_start_services

    # Ensure env var is not set
    monkeypatch.delenv("WATERCOOLER_AUTO_START_SERVICES", raising=False)
    assert not _should_auto_start_services()


def test_should_auto_start_services_enabled_via_env(monkeypatch):
    """Test _should_auto_start_services returns True when env var set."""
    from watercooler.baseline_graph.sync import _should_auto_start_services

    for value in ["1", "true", "True", "TRUE", "yes", "YES"]:
        monkeypatch.setenv("WATERCOOLER_AUTO_START_SERVICES", value)
        assert _should_auto_start_services(), f"Expected True for {value}"


def test_should_auto_start_services_disabled_for_other_values(monkeypatch):
    """Test _should_auto_start_services returns False for non-truthy values."""
    from watercooler.baseline_graph.sync import _should_auto_start_services

    for value in ["0", "false", "no", "maybe", ""]:
        monkeypatch.setenv("WATERCOOLER_AUTO_START_SERVICES", value)
        assert not _should_auto_start_services(), f"Expected False for {value}"


def test_try_auto_start_service_disabled_returns_false(monkeypatch):
    """Test _try_auto_start_service returns False when auto-start disabled."""
    from watercooler.baseline_graph.sync import _try_auto_start_service

    monkeypatch.delenv("WATERCOOLER_AUTO_START_SERVICES", raising=False)
    assert not _try_auto_start_service("llm", "http://localhost:11434/v1")
    assert not _try_auto_start_service("embedding", "http://localhost:8080/v1")


def test_try_auto_start_service_no_server_manager(monkeypatch):
    """Test _try_auto_start_service returns False when ServerManager unavailable."""
    from watercooler.baseline_graph.sync import _try_auto_start_service

    monkeypatch.setenv("WATERCOOLER_AUTO_START_SERVICES", "true")

    # ServerManager import will fail (not installed in test env)
    # Should return False gracefully
    result = _try_auto_start_service("llm", "http://localhost:11434/v1")
    assert not result


def test_sync_skips_llm_when_unavailable(threads_dir: Path, sample_thread: Path, monkeypatch):
    """Test sync_entry_to_graph skips LLM summary when service unavailable."""
    llm_called = []

    def mock_summarize_entry(*args, **kwargs):
        llm_called.append(True)
        return "mock summary"

    monkeypatch.setattr(
        "watercooler.baseline_graph.sync.summarize_entry",
        mock_summarize_entry,
    )
    # LLM service is unavailable
    monkeypatch.setattr(
        "watercooler.baseline_graph.sync.is_llm_service_available",
        lambda config=None: False,
    )
    # Don't try auto-start
    monkeypatch.setattr(
        "watercooler.baseline_graph.sync._try_auto_start_service",
        lambda *args: False,
    )

    # Sync with summaries enabled but service unavailable
    success = sync_entry_to_graph(
        threads_dir, "test-topic", generate_summaries=True
    )

    assert success
    assert len(llm_called) == 0  # LLM was NOT called because service unavailable


def test_sync_calls_llm_when_available(threads_dir: Path, sample_thread: Path, monkeypatch):
    """Test sync_entry_to_graph calls LLM when service is available."""
    llm_called = []

    def mock_summarize_entry(*args, **kwargs):
        llm_called.append(True)
        return "mock summary"

    monkeypatch.setattr(
        "watercooler.baseline_graph.sync.summarize_entry",
        mock_summarize_entry,
    )
    # LLM service is available
    monkeypatch.setattr(
        "watercooler.baseline_graph.sync.is_llm_service_available",
        lambda config=None: True,
    )

    # Sync with summaries enabled
    success = sync_entry_to_graph(
        threads_dir, "test-topic", generate_summaries=True
    )

    assert success
    assert len(llm_called) > 0  # LLM was called


def test_sync_skips_embedding_when_unavailable(threads_dir: Path, sample_thread: Path, monkeypatch):
    """Test sync_entry_to_graph skips embedding when service unavailable."""
    embed_called = []

    def mock_generate_embedding(*args, **kwargs):
        embed_called.append(True)
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(
        "watercooler.baseline_graph.sync.generate_embedding",
        mock_generate_embedding,
    )
    # Embedding service is unavailable
    monkeypatch.setattr(
        "watercooler.baseline_graph.sync.is_embedding_available",
        lambda config=None: False,
    )
    # Don't try auto-start
    monkeypatch.setattr(
        "watercooler.baseline_graph.sync._try_auto_start_service",
        lambda *args: False,
    )

    # Sync with embeddings enabled but service unavailable
    success = sync_entry_to_graph(
        threads_dir, "test-topic", generate_embeddings=True
    )

    assert success
    assert len(embed_called) == 0  # Embedding was NOT called
