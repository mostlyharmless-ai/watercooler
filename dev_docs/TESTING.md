# Testing Strategy

This document outlines the testing strategy for the Watercooler project, including the Remote MCP deployment components.

## Test Organization

Tests are organized in the `tests/` directory with the following structure:

```
tests/
├── conftest.py           # Shared pytest fixtures and configuration
├── fixtures/             # Shared test data (e.g., sample threads)
├── unit/                 # Unit tests (fast, isolated, no external deps)
└── integration/          # Integration tests (CLI, MCP, external services)
```

- **Unit tests** (`tests/unit/`) - Fast, isolated tests for individual functions/classes
- **Integration tests** (`tests/integration/`) - CLI tests, smoke tests, tests requiring infrastructure

## Test Categories

### 1. Core Watercooler Tests

**Command-line Interface Tests** (`tests/integration/`):
- `test_cli.py` - General CLI functionality
- `test_cli_init_thread.py` - Thread initialization
- `test_cli_say_ack.py` - Say and acknowledge operations
- `test_cli_handoff.py` - Agent handoff operations
- `test_cli_setters.py` - Status and ball setters
- `test_cli_list.py` - Thread listing
- `test_cli_reindex_search.py` - Search and reindex
- `test_cli_unlock.py` - Lock management
- `test_cli_web_export.py` - Web export functionality

**Core Functionality Tests** (`tests/unit/`):
- `test_agents.py` - Agent management
- `test_fs.py` - Filesystem operations
- `test_header.py` - Thread header parsing
- `test_templates.py` - Template rendering
- `test_lock.py` - File locking mechanisms
- `test_structured_entries.py` - Entry data structures
- `test_observability.py` - Logging and monitoring

**Git Synchronization Tests:**
- `tests/unit/test_git_sync.py` - Unit tests for git sync operations
- `tests/integration/test_git_sync_integration.py` - Integration tests for git workflows

**MCP Tests:**
- `tests/unit/test_mcp_directory_creation.py` - MCP directory management
- `tests/integration/test_smoke.py` - Basic smoke tests

### 2. Remote MCP Tests

**Authentication Tests (`test_http_facade_auth.py`):**

Tests for the HTTP facade authentication flow, ensuring secure access control:

- **Health endpoint accessibility** - Public endpoints work without auth
- **Auth header validation** - Requests require valid X-Internal-Auth header
- **Secret verification** - Correct/incorrect secrets properly handled
- **Required headers** - X-User-Id and X-Project-Id headers enforced
- **Identity extraction** - Headers properly parsed and stored in request state
- **Agent name defaults** - Missing X-Agent-Name defaults to "Agent"
- **Dev mode behavior** - Auth checks skipped when ALLOW_DEV_MODE=true

**ACL Enforcement Tests (`test_http_facade_acl.py`):**

Tests for project-level isolation and access control:

- **Directory derivation** - Correct per-user/per-project paths created
- **User isolation** - Different users get separate directories
- **Project isolation** - Same user with different projects separated
- **Path consistency** - Same user/project always gets same directory
- **Directory hierarchy** - Correct nested structure (base_root/user/project)
- **Idempotency** - Safe handling of existing directories
- **Internal auth verification** - Unit tests for secret validation logic
- **End-to-end isolation** - Full request flow with multiple users/projects

## Running Tests

### Run All Tests

```bash
pytest
```

### Run Specific Test Categories

**Unit tests only (fast):**
```bash
pytest tests/unit/
```

**Integration tests only:**
```bash
pytest tests/integration/
```

**CLI tests:**
```bash
pytest tests/integration/test_cli*.py
```

**Git sync tests:**
```bash
pytest tests/unit/test_git_sync.py tests/integration/test_git_sync_integration.py
```

### Run with Coverage

```bash
pytest --cov=watercooler --cov=watercooler_mcp --cov-report=html
```

View coverage report: `open htmlcov/index.html`

### Run Specific Tests

```bash
# Run a specific test file
pytest tests/unit/test_header.py

# Run a specific test class
pytest tests/integration/test_cli.py::TestCliHelp

# Run a specific test method
pytest tests/unit/test_agents.py::test_canonicalize_agent_name

# Run tests matching a pattern
pytest -k "auth"

# Skip tests requiring external infrastructure
pytest -m "not integration_falkor and not integration_leanrag_llm"

# Run only HTTP facade tests (requires: pip install -e ".[http]")
pytest -m http
```

## Test Environment

### Environment Variables

Tests may set these environment variables:

- `INTERNAL_AUTH_SECRET` - Auth secret for testing (cleaned up after tests)
- `ALLOW_DEV_MODE` - Enable dev mode for tests (skips auth checks)
- `BASE_THREADS_ROOT` - Temporary directory for test threads

Tests use fixtures to manage environment variables and ensure proper cleanup.

### Temporary Directories

Most tests use `pytest` fixtures (`tmp_path`, `tempfile.TemporaryDirectory`) to create isolated temporary directories. These are automatically cleaned up after tests.

## CI/CD Integration

Tests run automatically on GitHub Actions for:

- **Pull requests** - All tests must pass before merge
- **Push to main** - Regression testing on main branch
- **Multiple Python versions** - 3.10, 3.11, 3.12
- **Multiple platforms** - ubuntu-latest, macos-latest

### CI Configuration

See `.github/workflows/ci.yml` for the complete CI setup.

**Test matrix:**
```yaml
strategy:
  matrix:
    os: [ubuntu-latest, macos-latest]
    python-version: ['3.10', '3.11', '3.12']
```

## Writing New Tests

### Test Structure

Follow this pattern for new test files:

```python
"""Module docstring describing what is being tested."""

import pytest
from module_under_test import function_to_test


@pytest.fixture
def my_fixture():
    """Fixture docstring."""
    # Setup
    resource = create_resource()
    yield resource
    # Cleanup
    cleanup_resource(resource)


class TestFeatureName:
    """Test class for a specific feature."""

    def test_expected_behavior(self, my_fixture):
        """Test docstring describing what is verified."""
        result = function_to_test(my_fixture)
        assert result == expected_value

    def test_error_condition(self):
        """Test docstring for error case."""
        with pytest.raises(ExpectedException):
            function_to_test(invalid_input)
```

### Best Practices

1. **One assertion concept per test** - Tests should verify one behavior
2. **Descriptive test names** - Use `test_what_when_expected` pattern
3. **Use fixtures** - Share setup/teardown code via pytest fixtures
4. **Clean up resources** - Always clean up temp files, env vars, etc.
5. **Test both success and failure** - Cover happy paths and error cases
6. **Isolate tests** - Tests should not depend on each other
7. **Fast tests** - Keep tests fast by using mocks for expensive operations

### Remote MCP Testing Guidelines

When testing Remote MCP components:

1. **Auth testing** - Always test both authenticated and unauthenticated paths
2. **Isolation testing** - Verify per-user and per-project isolation
3. **Header validation** - Test all required and optional headers
4. **Dev vs Prod modes** - Test behavior in both development and production modes
5. **Secret management** - Never commit actual secrets; use fixtures

## Current Test Coverage

As of the Remote MCP implementation (PR #6):

- **Core watercooler** - ✅ Well-covered (20+ test files)
- **Git sync** - ✅ Unit and integration tests
- **HTTP facade auth** - ✅ Integration tests (new)
- **HTTP facade ACL** - ✅ Unit and integration tests (new)
- **Cloudflare Worker** - ⚠️ No automated tests (manual testing only)

### Coverage Gaps

**High Priority:**
1. Cloudflare Worker unit tests (TypeScript)
2. End-to-end tests with real OAuth flow
3. SSE streaming tests

**Medium Priority:**
1. Load testing for concurrent users
2. Security penetration testing
3. Performance benchmarks

## Manual Testing

For components without automated tests (e.g., Cloudflare Worker), use manual testing:

### Worker Manual Testing

See `cloudflare-worker/scripts/test-security.sh` for security testing:

```bash
cd cloudflare-worker/scripts
./test-security.sh <worker-url>
```

This tests:
- Unauthorized access rejection
- Auth header validation
- Project ACL enforcement
- SSE connection with OAuth

### Local Development Testing

Test the HTTP facade locally:

```bash
# Start facade in dev mode
export ALLOW_DEV_MODE=true
export BASE_THREADS_ROOT=/tmp/watercooler-test
python -m watercooler_mcp.http_facade

# In another terminal, test endpoints
curl http://localhost:8000/health
curl -X POST http://localhost:8000/mcp/watercooler_health \
  -H "X-User-Id: gh:testuser" \
  -H "X-Project-Id: test-project"
```

## Troubleshooting Tests

### Common Issues

**Import errors:**
```bash
# Ensure package is installed in editable mode
pip install -e .
```

**Fixture not found:**
```bash
# Check conftest.py is in tests/ directory
ls tests/conftest.py
```

**Environment variable pollution:**
```python
# Always clean up in fixtures
try:
    os.environ["MY_VAR"] = "value"
    yield
finally:
    os.environ.pop("MY_VAR", None)
```

**Temp directory conflicts:**
```python
# Use pytest's tmp_path fixture instead of hardcoded paths
def test_something(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
```

## Future Improvements

1. **Worker tests** - Add Jest/Vitest tests for Cloudflare Worker TypeScript
2. **E2E tests** - Full OAuth flow with browser automation
3. **Load tests** - Concurrent user simulation with k6 or locust
4. **Security tests** - Automated security scanning in CI
5. **Contract tests** - API contract testing between Worker and Facade
6. **Mutation tests** - Test the quality of tests themselves

## Resources

- [pytest documentation](https://docs.pytest.org/)
- [FastAPI testing](https://fastapi.tiangolo.com/tutorial/testing/)
- [GitHub Actions pytest](https://docs.github.com/en/actions/automating-builds-and-tests/building-and-testing-python)
