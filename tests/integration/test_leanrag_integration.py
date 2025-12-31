#!/usr/bin/env python3
"""Integration test for LeanRAG backend with real graph data.

This script verifies that the LeanRAG backend implementation can successfully
query the existing graph database created from watercooler threads.
"""

from pathlib import Path
from watercooler_memory.backends.leanrag import LeanRAGBackend, LeanRAGConfig


def test_leanrag_query_integration():
    """Test LeanRAG backend can query existing graph data."""
    
    # Use existing test artifacts directory
    work_dir = Path("tests/test-artifacts/leanrag-work")
    
    if not work_dir.exists():
        print(f"❌ Work directory not found: {work_dir}")
        print("   Run LeanRAG pipeline first to create the graph.")
        return False
    
    # Check required files exist
    required_files = ["threads_chunk.json", "entity.jsonl", "relation.jsonl"]
    for file in required_files:
        if not (work_dir / file).exists():
            print(f"❌ Required file missing: {file}")
            return False
    
    print("✓ Found required LeanRAG data files")
    
    # Create backend instance
    config = LeanRAGConfig(
        work_dir=work_dir,
        leanrag_path=Path("external/LeanRAG"),
        embedding_api_base="http://localhost:8080",  # BGE-M3 server
        embedding_model="BAAI/bge-m3",
    )
    
    backend = LeanRAGBackend(config)
    print("✓ LeanRAG backend initialized")
    
    # Test 1: Healthcheck
    print("\n" + "="*60)
    print("Test 1: Healthcheck")
    print("="*60)
    health = backend.healthcheck()
    print(f"Health status: {'✓ OK' if health.ok else '✗ FAIL'}")
    print(f"Details: {health.details}")
    
    if not health.ok:
        print("❌ Healthcheck failed")
        return False
    
    # Test 2: Search nodes
    print("\n" + "="*60)
    print("Test 2: Search Nodes (Entity Search)")
    print("="*60)
    try:
        nodes = backend.search_nodes(
            query="authentication OAuth2",
            max_results=5
        )
        print(f"✓ Found {len(nodes)} nodes")
        
        if nodes:
            print("\nTop results:")
            for i, node in enumerate(nodes[:3], 1):
                print(f"\n  {i}. {node.get('name', node['id'])}")
                print(f"     ID: {node['id']}")
                print(f"     Summary: {node.get('summary', 'N/A')[:100]}...")
                print(f"     Backend: {node.get('backend', 'N/A')}")
                print(f"     Score: {node.get('score', 'N/A')}")
                if 'metadata' in node:
                    print(f"     Parent: {node['metadata'].get('parent', 'N/A')}")
                    print(f"     Level: {node['metadata'].get('level', 'N/A')}")
        else:
            print("  No results found")
    except Exception as e:
        print(f"❌ search_nodes failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Test 3: Search facts
    print("\n" + "="*60)
    print("Test 3: Search Facts (Relationship Search)")
    print("="*60)
    try:
        facts = backend.search_facts(
            query="implements uses",
            max_results=5
        )
        print(f"✓ Found {len(facts)} facts/relationships")
        
        if facts:
            print("\nTop results:")
            for i, fact in enumerate(facts[:3], 1):
                print(f"\n  {i}. {fact['id']}")
                print(f"     Fact: {fact.get('summary', 'N/A')}")
                print(f"     Source: {fact.get('source_node_id', 'N/A')}")
                print(f"     Target: {fact.get('target_node_id', 'N/A')}")
                print(f"     Backend: {fact.get('backend', 'N/A')}")
                print(f"     Score: {fact.get('score', 'N/A')}")
        else:
            print("  No results found")
    except Exception as e:
        print(f"❌ search_facts failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Test 4: Get node by ID (if we found any nodes above)
    if nodes and len(nodes) > 0:
        print("\n" + "="*60)
        print("Test 4: Get Node by ID")
        print("="*60)
        try:
            node_id = nodes[0]['id']
            print(f"Looking up node: {node_id}")
            
            node = backend.get_node(node_id)
            
            if node:
                print(f"✓ Retrieved node: {node.get('name', node['id'])}")
                print(f"  Summary: {node.get('summary', 'N/A')[:100]}...")
                print(f"  Backend: {node.get('backend', 'N/A')}")
                if 'metadata' in node:
                    print(f"  Parent: {node['metadata'].get('parent', 'N/A')}")
                    print(f"  Level: {node['metadata'].get('level', 'N/A')}")
                    print(f"  Degree: {node['metadata'].get('degree', 'N/A')}")
            else:
                print(f"❌ Node not found: {node_id}")
                return False
        except Exception as e:
            print(f"❌ get_node failed: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    # Test 5: Get edge by ID (if we found any facts above)
    if facts and len(facts) > 0:
        print("\n" + "="*60)
        print("Test 5: Get Edge by ID")
        print("="*60)
        try:
            edge_id = facts[0]['id']
            print(f"Looking up edge: {edge_id}")
            
            edge = backend.get_edge(edge_id)
            
            if edge:
                print(f"✓ Retrieved edge: {edge['id']}")
                print(f"  Fact: {edge.get('summary', 'N/A')}")
                print(f"  Source: {edge.get('source_node_id', 'N/A')}")
                print(f"  Target: {edge.get('target_node_id', 'N/A')}")
                print(f"  Backend: {edge.get('backend', 'N/A')}")
                print(f"  Score: {edge.get('score', 'N/A')}")
            else:
                print(f"❌ Edge not found: {edge_id}")
                return False
        except Exception as e:
            print(f"❌ get_edge failed: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    # Test 6: Test UUID rejection for get_node
    print("\n" + "="*60)
    print("Test 6: Verify UUID Rejection")
    print("="*60)
    try:
        from watercooler_memory.backends import IdNotSupportedError
        
        uuid_id = "01KCVY8C4TYG742H69YN375DB1"
        print(f"Attempting to get node with UUID: {uuid_id}")
        
        try:
            backend.get_node(uuid_id)
            print("❌ Should have raised IdNotSupportedError")
            return False
        except IdNotSupportedError as e:
            print(f"✓ Correctly rejected UUID: {str(e)[:100]}...")
    except Exception as e:
        print(f"❌ UUID rejection test failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Test 7: Test malformed edge ID rejection
    print("\n" + "="*60)
    print("Test 7: Verify Malformed Edge ID Rejection")
    print("="*60)
    try:
        from watercooler_memory.backends import IdNotSupportedError
        
        bad_id = "NO_SEPARATOR"
        print(f"Attempting to get edge with malformed ID: {bad_id}")
        
        try:
            backend.get_edge(bad_id)
            print("❌ Should have raised IdNotSupportedError")
            return False
        except IdNotSupportedError as e:
            print(f"✓ Correctly rejected malformed ID: {str(e)[:100]}...")
    except Exception as e:
        print(f"❌ Malformed ID rejection test failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Test 8: Test search_episodes raises UnsupportedOperationError
    print("\n" + "="*60)
    print("Test 8: Verify Episodes Unsupported")
    print("="*60)
    try:
        from watercooler_memory.backends import UnsupportedOperationError
        
        print("Attempting to search episodes...")
        
        try:
            backend.search_episodes(query="test")
            print("❌ Should have raised UnsupportedOperationError")
            return False
        except UnsupportedOperationError as e:
            print(f"✓ Correctly raised UnsupportedOperationError: {str(e)[:100]}...")
    except Exception as e:
        print(f"❌ Episodes unsupported test failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    print("\n" + "="*60)
    print("✅ ALL TESTS PASSED")
    print("="*60)
    print(f"\nLeanRAG backend successfully queried graph at: {work_dir}")
    print("Backend is ready for Phase 2 PR.")
    
    return True


if __name__ == "__main__":
    import sys
    success = test_leanrag_query_integration()
    sys.exit(0 if success else 1)
