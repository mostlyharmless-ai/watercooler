"""Tests for visualize_graph.py script.

Tests core functionality that doesn't require pyvis/networkx dependencies.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


# Import the module - deps availability is checked via DEPS_AVAILABLE flag
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
try:
    from visualize_graph import (
        load_graph,
        get_node_color,
        get_node_size,
        get_node_label,
        get_node_title,
        get_edge_color,
        COLORS,
        SIZES,
        DEPS_AVAILABLE,
    )
except ImportError:
    # Fallback if module itself can't be imported
    DEPS_AVAILABLE = False


pytestmark = pytest.mark.skipif(
    not DEPS_AVAILABLE,
    reason="Visualization dependencies (pyvis, networkx) not installed"
)


class TestLoadGraph:
    """Tests for load_graph function."""

    def test_load_empty_directory(self, tmp_path):
        """Test loading from directory without graph files."""
        nodes, edges = load_graph(tmp_path)
        assert nodes == []
        assert edges == []

    def test_load_valid_graph(self, tmp_path):
        """Test loading valid nodes and edges."""
        nodes_file = tmp_path / "nodes.jsonl"
        edges_file = tmp_path / "edges.jsonl"

        nodes_file.write_text(
            '{"id": "thread:test", "type": "thread", "topic": "test"}\n'
            '{"id": "entry:test:1", "type": "entry"}\n'
        )
        edges_file.write_text(
            '{"source": "thread:test", "target": "entry:test:1", "type": "contains"}\n'
        )

        nodes, edges = load_graph(tmp_path)

        assert len(nodes) == 2
        assert len(edges) == 1
        assert nodes[0]["type"] == "thread"
        assert nodes[1]["type"] == "entry"
        assert edges[0]["type"] == "contains"

    def test_load_skips_empty_lines(self, tmp_path):
        """Test that empty lines are skipped."""
        nodes_file = tmp_path / "nodes.jsonl"
        nodes_file.write_text(
            '{"id": "node1"}\n'
            '\n'
            '   \n'
            '{"id": "node2"}\n'
        )

        nodes, edges = load_graph(tmp_path)
        assert len(nodes) == 2

    def test_load_handles_malformed_json(self, tmp_path, capsys):
        """Test that malformed JSON lines are skipped with warning."""
        nodes_file = tmp_path / "nodes.jsonl"
        nodes_file.write_text(
            '{"id": "valid"}\n'
            'not valid json\n'
            '{"id": "also_valid"}\n'
        )

        nodes, edges = load_graph(tmp_path)

        # Should skip the bad line
        assert len(nodes) == 2
        assert nodes[0]["id"] == "valid"
        assert nodes[1]["id"] == "also_valid"

        # Should print warning
        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "line 2" in captured.err


class TestGetNodeColor:
    """Tests for get_node_color function."""

    def test_thread_open_color(self):
        """Test color for OPEN thread."""
        node = {"type": "thread", "status": "OPEN"}
        assert get_node_color(node) == COLORS["thread"]["OPEN"]

    def test_thread_closed_color(self):
        """Test color for CLOSED thread."""
        node = {"type": "thread", "status": "CLOSED"}
        assert get_node_color(node) == COLORS["thread"]["CLOSED"]

    def test_thread_lowercase_status(self):
        """Test that lowercase status is handled."""
        node = {"type": "thread", "status": "open"}
        # Status is uppercased in get_node_color
        assert get_node_color(node) == COLORS["thread"]["OPEN"]

    def test_thread_unknown_status_fallback(self):
        """Test fallback for unknown thread status."""
        node = {"type": "thread", "status": "UNKNOWN"}
        assert get_node_color(node) == COLORS["thread"]["OPEN"]

    def test_entry_note_color(self):
        """Test color for Note entry."""
        node = {"type": "entry", "entry_type": "Note"}
        assert get_node_color(node) == COLORS["entry"]["Note"]

    def test_entry_plan_color(self):
        """Test color for Plan entry."""
        node = {"type": "entry", "entry_type": "Plan"}
        assert get_node_color(node) == COLORS["entry"]["Plan"]

    def test_entry_decision_color(self):
        """Test color for Decision entry."""
        node = {"type": "entry", "entry_type": "Decision"}
        assert get_node_color(node) == COLORS["entry"]["Decision"]

    def test_entry_unknown_type_fallback(self):
        """Test fallback for unknown entry type."""
        node = {"type": "entry", "entry_type": "Unknown"}
        assert get_node_color(node) == COLORS["entry"]["Note"]

    def test_default_type_is_entry(self):
        """Test that missing type defaults to entry."""
        node = {}
        assert get_node_color(node) == COLORS["entry"]["Note"]


class TestGetNodeSize:
    """Tests for get_node_size function."""

    def test_thread_base_size(self):
        """Test base size for thread."""
        node = {"type": "thread", "entry_count": 0}
        assert get_node_size(node) == SIZES["thread"]

    def test_thread_scales_with_entries(self):
        """Test that thread size scales with entry count."""
        node_small = {"type": "thread", "entry_count": 1}
        node_large = {"type": "thread", "entry_count": 10}

        size_small = get_node_size(node_small)
        size_large = get_node_size(node_large)

        assert size_large > size_small

    def test_thread_size_cap(self):
        """Test that thread size has a maximum cap."""
        node = {"type": "thread", "entry_count": 1000}
        size = get_node_size(node)
        # Base is 30, max additional is 30
        assert size <= SIZES["thread"] + 30

    def test_entry_size(self):
        """Test size for entry."""
        node = {"type": "entry"}
        assert get_node_size(node) == SIZES["entry"]

    def test_unknown_type_fallback(self):
        """Test fallback for unknown type."""
        node = {"type": "unknown"}
        assert get_node_size(node) == 15


class TestGetNodeLabel:
    """Tests for get_node_label function."""

    def test_thread_label(self):
        """Test label for thread."""
        node = {"type": "thread", "topic": "feature-auth", "status": "OPEN"}
        label = get_node_label(node)
        assert "feature-auth" in label
        assert "OPEN" in label

    def test_entry_with_title(self):
        """Test entry label with title."""
        node = {"type": "entry", "title": "Initial analysis"}
        assert get_node_label(node) == "Initial analysis"

    def test_entry_long_title_truncated(self):
        """Test that long titles are truncated."""
        node = {"type": "entry", "title": "A" * 50}
        label = get_node_label(node)
        assert len(label) <= 25
        assert label.endswith("...")

    def test_entry_without_title(self):
        """Test entry label fallback to entry_id."""
        node = {"type": "entry", "entry_id": "topic:1"}
        assert get_node_label(node) == "topic:1"


class TestGetNodeTitle:
    """Tests for get_node_title (tooltip) function."""

    def test_thread_tooltip_contains_metadata(self):
        """Test thread tooltip contains all metadata."""
        node = {
            "type": "thread",
            "topic": "feature-auth",
            "title": "Authentication Refactor",
            "status": "OPEN",
            "ball": "Claude",
            "entry_count": 5,
            "last_updated": "2024-01-01",
            "summary": "Summary text",
        }
        tooltip = get_node_title(node)

        assert "feature-auth" in tooltip
        assert "Authentication Refactor" in tooltip
        assert "OPEN" in tooltip
        assert "Claude" in tooltip
        assert "5" in tooltip
        assert "Summary text" in tooltip

    def test_entry_tooltip_contains_metadata(self):
        """Test entry tooltip contains all metadata."""
        node = {
            "type": "entry",
            "entry_id": "topic:1",
            "title": "Entry Title",
            "entry_type": "Note",
            "agent": "Claude",
            "role": "implementer",
            "timestamp": "2024-01-01T00:00:00Z",
            "summary": "Entry summary",
            "file_refs": ["src/main.py"],
            "pr_refs": [42],
        }
        tooltip = get_node_title(node)

        assert "topic:1" in tooltip
        assert "Entry Title" in tooltip
        assert "Note" in tooltip
        assert "Claude" in tooltip
        assert "implementer" in tooltip
        assert "Entry summary" in tooltip
        assert "src/main.py" in tooltip
        assert "#42" in tooltip

    def test_long_summary_truncated(self):
        """Test that long summaries are truncated."""
        node = {
            "type": "thread",
            "topic": "test",
            "summary": "A" * 300,
        }
        tooltip = get_node_title(node)
        assert "..." in tooltip


class TestGetEdgeColor:
    """Tests for get_edge_color function."""

    def test_contains_edge_color(self):
        """Test color for contains edge."""
        edge = {"type": "contains"}
        assert get_edge_color(edge) == COLORS["edge"]["contains"]

    def test_followed_by_edge_color(self):
        """Test color for followed_by edge."""
        edge = {"type": "followed_by"}
        assert get_edge_color(edge) == COLORS["edge"]["followed_by"]

    def test_unknown_type_fallback(self):
        """Test fallback for unknown edge type."""
        edge = {"type": "unknown"}
        assert get_edge_color(edge) == "#999999"


# Import build_networkx_graph for testing (requires networkx)
try:
    from visualize_graph import build_networkx_graph
    # Only mark available if deps are actually installed
    BUILD_GRAPH_AVAILABLE = DEPS_AVAILABLE
except ImportError:
    BUILD_GRAPH_AVAILABLE = False


@pytest.mark.skipif(
    not BUILD_GRAPH_AVAILABLE,
    reason="build_networkx_graph requires networkx"
)
class TestBuildNetworkxGraph:
    """Tests for build_networkx_graph function."""

    def test_skips_nodes_without_id(self, capsys):
        """Test that nodes without 'id' are skipped with warning."""
        nodes = [
            {"id": "node1", "type": "thread"},
            {"type": "entry"},  # Missing id
            {"id": "node2", "type": "entry"},
        ]
        edges = []

        G = build_networkx_graph(nodes, edges)

        assert len(G.nodes()) == 2
        assert "node1" in G.nodes()
        assert "node2" in G.nodes()

        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "without 'id'" in captured.err

    def test_skips_edges_without_source(self, capsys):
        """Test that edges without 'source' are skipped with warning."""
        nodes = [{"id": "n1"}, {"id": "n2"}]
        edges = [
            {"source": "n1", "target": "n2"},
            {"target": "n2"},  # Missing source
        ]

        G = build_networkx_graph(nodes, edges)

        assert len(G.edges()) == 1
        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "without 'source' or 'target'" in captured.err

    def test_skips_edges_without_target(self, capsys):
        """Test that edges without 'target' are skipped with warning."""
        nodes = [{"id": "n1"}, {"id": "n2"}]
        edges = [
            {"source": "n1", "target": "n2"},
            {"source": "n1"},  # Missing target
        ]

        G = build_networkx_graph(nodes, edges)

        assert len(G.edges()) == 1
        captured = capsys.readouterr()
        assert "Warning" in captured.err

    def test_valid_graph_builds_correctly(self):
        """Test that valid nodes and edges build correctly."""
        nodes = [
            {"id": "thread:test", "type": "thread", "topic": "test"},
            {"id": "entry:1", "type": "entry"},
        ]
        edges = [
            {"source": "thread:test", "target": "entry:1", "type": "contains"},
        ]

        G = build_networkx_graph(nodes, edges)

        assert len(G.nodes()) == 2
        assert len(G.edges()) == 1
        assert G.nodes["thread:test"]["type"] == "thread"


# Import main for integration testing (requires pyvis/networkx)
try:
    from visualize_graph import main
    # Only mark available if deps are actually installed
    MAIN_AVAILABLE = DEPS_AVAILABLE
except ImportError:
    MAIN_AVAILABLE = False


@pytest.mark.skipif(
    not MAIN_AVAILABLE,
    reason="main() requires pyvis/networkx"
)
class TestMainIntegration:
    """Integration tests for main() function."""

    def test_main_generates_html_file(self, tmp_path):
        """Test that main() generates a valid HTML file."""
        # Create test graph data
        graph_dir = tmp_path / "graph"
        graph_dir.mkdir()

        nodes_file = graph_dir / "nodes.jsonl"
        edges_file = graph_dir / "edges.jsonl"

        nodes_file.write_text(
            '{"id": "thread:test", "type": "thread", "topic": "test", "status": "OPEN"}\n'
            '{"id": "entry:test:1", "type": "entry", "title": "Test entry"}\n'
        )
        edges_file.write_text(
            '{"source": "thread:test", "target": "entry:test:1", "type": "contains"}\n'
        )

        output_file = tmp_path / "output.html"

        # Run main with test args
        with patch("sys.argv", ["visualize_graph.py", "-i", str(graph_dir), "-o", str(output_file)]):
            result = main()

        assert result == 0
        assert output_file.exists()

        # Verify HTML content
        html_content = output_file.read_text()
        assert "<html>" in html_content or "<!DOCTYPE" in html_content
        assert "vis-network" in html_content or "vis.js" in html_content

    def test_main_returns_error_for_missing_directory(self, tmp_path, capsys):
        """Test that main() returns error for non-existent directory."""
        missing_dir = tmp_path / "nonexistent"

        with patch("sys.argv", ["visualize_graph.py", "-i", str(missing_dir)]):
            result = main()

        assert result == 1
        captured = capsys.readouterr()
        assert "not found" in captured.err

    def test_main_returns_error_for_file_instead_of_dir(self, tmp_path, capsys):
        """Test that main() returns error when given a file instead of dir."""
        file_path = tmp_path / "file.txt"
        file_path.write_text("not a directory")

        with patch("sys.argv", ["visualize_graph.py", "-i", str(file_path)]):
            result = main()

        assert result == 1
        captured = capsys.readouterr()
        assert "Not a directory" in captured.err

    def test_main_returns_error_for_nonexistent_output_dir(self, tmp_path, capsys):
        """Test that main() returns error when output directory doesn't exist."""
        # Create valid input
        graph_dir = tmp_path / "graph"
        graph_dir.mkdir()
        (graph_dir / "nodes.jsonl").write_text('{"id": "t:1", "type": "thread"}\n')
        (graph_dir / "edges.jsonl").write_text("")

        # Output to nonexistent directory
        bad_output = tmp_path / "nonexistent" / "output.html"

        with patch(
            "sys.argv",
            ["visualize_graph.py", "-i", str(graph_dir), "-o", str(bad_output)],
        ):
            result = main()

        assert result == 1
        captured = capsys.readouterr()
        assert "Output directory not found" in captured.err
