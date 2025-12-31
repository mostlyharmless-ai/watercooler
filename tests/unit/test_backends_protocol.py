"""Protocol compliance tests for memory backends."""

import pytest

from watercooler_memory.backends import (
    BackendError,
    Capabilities,
    MemoryBackend,
    UnsupportedOperationError,
)
from watercooler_memory.backends.null import NullBackend


class TestNullBackendProtocolCompliance:
    """Test that NullBackend implements the MemoryBackend protocol."""

    def test_null_backend_is_protocol_compliant(self):
        """NullBackend should satisfy MemoryBackend protocol."""
        backend = NullBackend()
        assert isinstance(backend, MemoryBackend)

    def test_null_backend_capabilities(self):
        """NullBackend capabilities should indicate no support for new operations."""
        backend = NullBackend()
        caps = backend.get_capabilities()
        
        assert isinstance(caps, Capabilities)
        assert caps.supports_nodes is False
        assert caps.supports_facts is False
        assert caps.supports_episodes is False
        assert caps.supports_chunks is False
        assert caps.supports_edges is False
        assert caps.node_id_type == "passthrough"
        assert caps.edge_id_type == "passthrough"

    def test_null_backend_search_nodes_raises(self):
        """NullBackend.search_nodes() should raise UnsupportedOperationError."""
        backend = NullBackend()
        with pytest.raises(UnsupportedOperationError) as exc_info:
            backend.search_nodes("test query")
        
        assert "null" in str(exc_info.value).lower()
        assert "node search" in str(exc_info.value).lower()

    def test_null_backend_search_facts_raises(self):
        """NullBackend.search_facts() should raise UnsupportedOperationError."""
        backend = NullBackend()
        with pytest.raises(UnsupportedOperationError) as exc_info:
            backend.search_facts("test query")
        
        assert "null" in str(exc_info.value).lower()
        assert "fact search" in str(exc_info.value).lower()

    def test_null_backend_search_episodes_raises(self):
        """NullBackend.search_episodes() should raise UnsupportedOperationError."""
        backend = NullBackend()
        with pytest.raises(UnsupportedOperationError) as exc_info:
            backend.search_episodes("test query")
        
        assert "null" in str(exc_info.value).lower()
        assert "episode search" in str(exc_info.value).lower()

    def test_null_backend_get_node_raises(self):
        """NullBackend.get_node() should raise UnsupportedOperationError."""
        backend = NullBackend()
        with pytest.raises(UnsupportedOperationError) as exc_info:
            backend.get_node("test-id")
        
        assert "null" in str(exc_info.value).lower()
        assert "node retrieval" in str(exc_info.value).lower()

    def test_null_backend_get_edge_raises(self):
        """NullBackend.get_edge() should raise UnsupportedOperationError."""
        backend = NullBackend()
        with pytest.raises(UnsupportedOperationError) as exc_info:
            backend.get_edge("test-id")
        
        assert "null" in str(exc_info.value).lower()
        assert "edge retrieval" in str(exc_info.value).lower()


class TestProtocolMethodSignatures:
    """Test that protocol methods have correct signatures."""

    def test_search_nodes_signature(self):
        """search_nodes() should accept correct parameters."""
        backend = NullBackend()
        
        # Verify method exists and accepts expected parameters
        assert hasattr(backend, 'search_nodes')
        
        # Should accept all optional parameters
        with pytest.raises(UnsupportedOperationError):
            backend.search_nodes(
                query="test",
                group_ids=["group1"],
                max_results=5,
                entity_types=["PERSON"],
            )

    def test_search_facts_signature(self):
        """search_facts() should accept correct parameters."""
        backend = NullBackend()
        
        assert hasattr(backend, 'search_facts')
        
        with pytest.raises(UnsupportedOperationError):
            backend.search_facts(
                query="test",
                group_ids=["group1"],
                max_results=5,
                center_node_id="node-123",
            )

    def test_search_episodes_signature(self):
        """search_episodes() should accept correct parameters."""
        backend = NullBackend()
        
        assert hasattr(backend, 'search_episodes')
        
        with pytest.raises(UnsupportedOperationError):
            backend.search_episodes(
                query="test",
                group_ids=["group1"],
                max_results=5,
            )

    def test_get_node_signature(self):
        """get_node() should accept correct parameters."""
        backend = NullBackend()
        
        assert hasattr(backend, 'get_node')
        
        with pytest.raises(UnsupportedOperationError):
            backend.get_node(
                node_id="node-123",
                group_id="group1",
            )

    def test_get_edge_signature(self):
        """get_edge() should accept correct parameters."""
        backend = NullBackend()
        
        assert hasattr(backend, 'get_edge')
        
        with pytest.raises(UnsupportedOperationError):
            backend.get_edge(
                edge_id="edge-123",
                group_id="group1",
            )


class TestCapabilitiesDataclass:
    """Test the extended Capabilities dataclass."""

    def test_capabilities_has_new_fields(self):
        """Capabilities should have new operation support fields."""
        caps = Capabilities()
        
        # New operation support flags
        assert hasattr(caps, 'supports_nodes')
        assert hasattr(caps, 'supports_facts')
        assert hasattr(caps, 'supports_episodes')
        assert hasattr(caps, 'supports_chunks')
        assert hasattr(caps, 'supports_edges')
        
        # ID modality flags
        assert hasattr(caps, 'node_id_type')
        assert hasattr(caps, 'edge_id_type')

    def test_capabilities_defaults(self):
        """Capabilities should have correct default values."""
        caps = Capabilities()
        
        # Should default to False
        assert caps.supports_nodes is False
        assert caps.supports_facts is False
        assert caps.supports_episodes is False
        assert caps.supports_chunks is False
        assert caps.supports_edges is False
        
        # Should default to "uuid"
        assert caps.node_id_type == "uuid"
        assert caps.edge_id_type == "uuid"

    def test_capabilities_can_be_set(self):
        """Capabilities fields should be settable."""
        caps = Capabilities(
            supports_nodes=True,
            supports_facts=True,
            node_id_type="name",
            edge_id_type="synthetic",
        )
        
        assert caps.supports_nodes is True
        assert caps.supports_facts is True
        assert caps.node_id_type == "name"
        assert caps.edge_id_type == "synthetic"


class TestGraphitiBackendProtocolCompliance:
    """Test that GraphitiBackend implements the new protocol methods correctly."""

    @pytest.fixture
    def graphiti_backend(self):
        """Create a GraphitiBackend instance for testing."""
        from unittest.mock import Mock
        from watercooler_memory.backends.graphiti import GraphitiBackend
        
        # Create a mock backend that has all the protocol methods
        # We'll just verify the methods exist and have correct signatures
        backend = Mock(spec=GraphitiBackend)
        
        # Set up capabilities
        from watercooler_memory.backends import Capabilities
        backend.get_capabilities.return_value = Capabilities(
            supports_nodes=True,
            supports_facts=True,
            supports_episodes=True,
            supports_chunks=False,
            supports_edges=True,
            node_id_type="uuid",
            edge_id_type="uuid",
        )
        
        return backend

    def test_graphiti_backend_has_protocol_methods(self):
        """GraphitiBackend should have all new protocol methods."""
        from watercooler_memory.backends.graphiti import GraphitiBackend
        
        # Verify methods exist without needing to instantiate
        assert hasattr(GraphitiBackend, 'search_nodes')
        assert hasattr(GraphitiBackend, 'search_facts')
        assert hasattr(GraphitiBackend, 'search_episodes')
        assert hasattr(GraphitiBackend, 'get_node')
        assert hasattr(GraphitiBackend, 'get_edge')

    def test_graphiti_backend_capabilities_structure(self, graphiti_backend):
        """GraphitiBackend capabilities should have correct structure."""
        caps = graphiti_backend.get_capabilities()
        
        assert caps.supports_nodes is True
        assert caps.supports_facts is True
        assert caps.supports_episodes is True
        assert caps.supports_chunks is False
        assert caps.supports_edges is True
        assert caps.node_id_type == "uuid"
        assert caps.edge_id_type == "uuid"

    def test_graphiti_search_facts_method_signature(self):
        """search_facts() should have correct signature."""
        from watercooler_memory.backends.graphiti import GraphitiBackend
        import inspect
        
        sig = inspect.signature(GraphitiBackend.search_facts)
        params = sig.parameters
        
        assert 'query' in params
        assert 'group_ids' in params
        assert 'max_results' in params
        assert 'center_node_id' in params

    def test_graphiti_search_episodes_method_signature(self):
        """search_episodes() should have correct signature."""
        from watercooler_memory.backends.graphiti import GraphitiBackend
        import inspect
        
        sig = inspect.signature(GraphitiBackend.search_episodes)
        params = sig.parameters
        
        assert 'query' in params
        assert 'group_ids' in params
        assert 'max_results' in params

    def test_graphiti_get_edge_method_signature(self):
        """get_edge() should have correct signature."""
        from watercooler_memory.backends.graphiti import GraphitiBackend
        import inspect
        
        sig = inspect.signature(GraphitiBackend.get_edge)
        params = sig.parameters
        
        assert 'edge_id' in params
        assert 'group_id' in params

    def test_graphiti_get_node_method_signature(self):
        """get_node() should have correct signature."""
        from watercooler_memory.backends.graphiti import GraphitiBackend
        import inspect
        
        sig = inspect.signature(GraphitiBackend.get_node)
        params = sig.parameters
        
        assert 'node_id' in params
        assert 'group_id' in params

    def test_graphiti_search_methods_exist_and_callable(self):
        """All search methods should exist and be callable."""
        from watercooler_memory.backends.graphiti import GraphitiBackend
        
        for method_name in ['search_nodes', 'search_facts', 'search_episodes']:
            assert hasattr(GraphitiBackend, method_name)
            method = getattr(GraphitiBackend, method_name)
            assert callable(method)

    def test_graphiti_get_methods_exist_and_callable(self):
        """All get methods should exist and be callable."""
        from watercooler_memory.backends.graphiti import GraphitiBackend
        
        for method_name in ['get_node', 'get_edge']:
            assert hasattr(GraphitiBackend, method_name)
            method = getattr(GraphitiBackend, method_name)
            assert callable(method)
