# Repository Guidelines for watercooler-cloud

## Project Overview

watercooler-cloud is a file-based collaboration protocol for agentic coding projects. It provides a Python library and MCP server for managing threaded conversations between human developers and AI agents using git-friendly markdown files.

## Project Structure & Module Organization

- **Core library**: `src/watercooler/` - Main Python package with CLI and core functionality
- **MCP server**: `src/watercooler_mcp/` - Model Context Protocol server integration
- **Tests**: `tests/` - Test suite mirroring source structure (e.g., `src/watercooler/cli.py` → `tests/test_cli.py`)
- **Documentation**: `docs/` - User and developer documentation
- **Templates**: `src/watercooler/templates/` - Bundled thread and entry templates
- **Configuration**: Root-level config files (`pyproject.toml`, etc.)

## Python Standards

- **Python Version**: 3.10, 3.11, and 3.12 (3.9+ compatibility required for core library)
- **Type Hints**: Use mypy for type checking. Always use type hints for function signatures in public interfaces
- **Package Structure**: Standard src-layout (`src/watercooler/`, `src/watercooler_mcp/`)
- **Import Style**: Use absolute imports, prefer standard library modules

## Design Principles

1. **Stdlib-only**: No external runtime dependencies in core library (watercooler package)
2. **File-based**: Git-friendly markdown format for threads
3. **Zero-config**: Works out-of-box for standard layouts
4. **CLI parity**: Drop-in replacement workflows for existing watercooler.py

## Code Style & Naming Conventions

### Python Style Guide

- Follow **PEP 8** with these tools:
  - `black` for code formatting
  - `ruff` for linting
  - `mypy` for type checking
- **Indentation**: 2 spaces (Python standard)
- **Line length**: Follow black defaults (~88-100 characters)
- **Type hints**: Required for all public function signatures

### Naming Conventions

- **Functions/methods**: `snake_case` (e.g., `init_thread`, `append_entry`)
- **Classes**: `PascalCase` (e.g., `ThreadContext`, `EntryFormatter`)
- **Constants**: `UPPER_SNAKE_CASE` (e.g., `DEFAULT_THREADS_DIR`)
- **Private**: `_leading_underscore` for internal use
- **CLI commands**: kebab-case (e.g., `init-thread`, `append-entry`)
- **Python modules**: `snake_case` (e.g., `git_sync.py`, `structured_entries.py`)
- **Agent roles**: lowercase (e.g., `planner`, `critic`, `implementer`, `tester`, `pm`, `scribe`)
- **Entry types**: PascalCase (e.g., `Note`, `Plan`, `Decision`, `PR`, `Closure`)

### Code Guidelines

1. **Type Hints**: Always use type hints for function signatures:
   ```python
   def say(
       topic: str,
       threads_dir: Path,
       agent: str,
       title: str,
       body: str,
       role: str = "implementer",
       entry_type: str = "Note"
   ) -> None:
       """Add an entry to a thread."""
   ```

2. **Docstrings**: Use Google-style docstrings:
   ```python
   def read_thread(topic: str, threads_dir: Path) -> str:
       """Read the content of a thread.

       Args:
           topic: The thread topic identifier
           threads_dir: Path to threads directory

       Returns:
           The full thread content as markdown

       Raises:
           FileNotFoundError: If thread doesn't exist
       """
   ```

3. **Error Handling**: Use specific exception types with helpful messages:
   ```python
   try:
       content = thread_path.read_text()
   except FileNotFoundError:
       raise FileNotFoundError(
           f"Thread '{topic}' not found at {thread_path}"
       )
   ```

4. **No Unused Exports**: Prefer explicit types at public boundaries

## Development Workflow

### Branch Strategy

- Create feature branches off `main`
- Branch naming: `feature/`, `fix/`, `docs/`, `refactor/`, `test/`
- Keep changes focused - separate bug fixes, features, and documentation updates

### Commit Guidelines

Follow **Conventional Commits** format:

```
<type>(<scope>): <subject>

<body>

<footer>
```

**Types:**
- `feat` - New feature
- `fix` - Bug fix
- `docs` - Documentation only
- `style` - Formatting, missing semicolons, etc.
- `refactor` - Code restructuring
- `test` - Adding tests
- `chore` - Maintenance tasks

**Examples:**
```
feat(mcp): add support for custom entry templates

- Allow users to override entry templates
- Add WATERCOOLER_ENTRY_TEMPLATE env var
- Update documentation with examples

Closes #123
```

```
fix(cli): handle missing threads directory gracefully

Previously the CLI would crash with FileNotFoundError.
Now it creates the directory automatically if missing.

Fixes #456
```

### Commit Sign-off (DCO)

Every commit must include a `Signed-off-by` line:
```
Signed-off-by: Your Name <you@example.com>
```

Add it automatically with `git commit -s` or `git commit --signoff`.

### Pull Request Process

1. **Before Starting Work**:
   - Check existing issues
   - Open an issue for major changes
   - Get feedback before starting

2. **PR Requirements**:
   - Clear summary
   - Linked issue (e.g., `Closes #123`)
   - Steps to test
   - Screenshots for UI changes (if applicable)
   - All tests pass locally
   - Documentation updated as needed

3. **CI Requirements**:
   - All GitHub Actions checks must pass
   - Tests run on Ubuntu and macOS across Python 3.10-3.12
   - Prefer squash merge with a clean, conventional title

## Testing Guidelines

### Test Organization

- Test files: `test_*.py` in `tests/` directory
- Mirror source structure: `src/watercooler/cli.py` → `tests/test_cli.py`
- Use `conftest.py` for shared pytest fixtures

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=watercooler --cov=watercooler_mcp --cov-report=html

# Run specific test file
pytest tests/test_commands.py

# Run specific test
pytest tests/test_commands.py::test_say_command

# Skip HTTP facade tests (require [http] dependencies)
pytest -m "not http"

# Run only HTTP facade tests
pytest -m http
```

### Test Coverage Goals

- **Core functionality**: 90%+ coverage
- **Edge cases**: Test error conditions
- **Integration**: Test CLI commands end-to-end

### Writing Tests

Follow this structure:
```python
def test_feature_name():
    """Test description of what this validates."""
    # Arrange
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()

    # Act
    result = say("test-topic", threads_dir, ...)

    # Assert
    assert result is None
    thread_path = threads_dir / "test-topic.md"
    assert thread_path.exists()
```

### Test Best Practices

1. One assertion concept per test
2. Descriptive test names using `test_what_when_expected` pattern
3. Use fixtures for shared setup/teardown
4. Clean up resources (temp files, env vars, etc.)
5. Test both success and failure paths
6. Isolate tests (no dependencies between tests)
7. Keep tests fast (use mocks for expensive operations)

### Timeouts

- Do not add tiny timeouts to commands or subprocesses; they are not helpful here. Use generous defaults (minutes, not seconds) for end-to-end pipelines and integration tests.

## Codex Watercooler Protocol (Session Rules)

### Purpose

Standardize how Codex (this assistant) uses Watercooler tools so entries remain attributable and contextually accurate.

### Pre-flight (Required Before Any Write: say/ack/handoff/set_status)

- Ensure Agent base is set to `Codex`.
- Ensure a clear specialization `spec` is set to match the current task (examples below).
- If either is missing/unclear, do not post; set/confirm first.

### Setting Identity

- **Preferred (cloud context)**: Call `watercooler_set_agent` with `base="Codex"` and an appropriate `spec` (e.g., `pm`, `planner-architecture`, `implementer-code`, `tester`, `security-audit`, `docs`, `ops`, or `general-purpose`).
- **Alternative (per-call)**: Supply `agent_func` parameter in format `<platform>:<model>:<role>` (e.g., `"Codex:gpt-4:implementer"`) where platform is the actual IDE/platform name, model is the exact model identifier, and role is the agent role.
- **Local context (no explicit setter)**: Still enforce the rule by selecting the matching entry Role and adding a visible `Spec: <value>` line at the top of the entry body.

### Code Path Selection

- Always set `code_path` to the root of the repository whose code or planning you are touching.
- When the scope moves to a different repository, migrate the conversation to that repo's threads before posting again.
- Never post watercooler-cloud updates while pointing at watercooler-site (or vice versa); keep the code/threads pair aligned.

### Thread Reading & Entry Access

- Use `watercooler_list_thread_entries` with `format="json"` plus `offset`/`limit` to explore large conversations safely. Feed the returned `entry_id`/index into `watercooler_get_thread_entry` or `watercooler_get_thread_entry_range` for precise retrieval.
- Request `format="markdown"` only when you need to present the entry text directly to the user; otherwise stick with JSON for easier downstream parsing.
- Reserve `watercooler_read_thread(format="json")` for full exports or analytics. For normal reading, paginate with the entry tools to avoid exceeding stdio output limits (~10 KB per response).
- Break huge responses into manageable slices (e.g., 5–10 entries per call) and cite the `entry_id` when referencing or quoting a specific entry later.

### Role Alignment

- Keep `spec` (session specialization) and Watercooler entry `Role` distinct but aligned:
  - `spec=pm` → Role `pm`
  - `planner-architecture` → Role `planner`
  - `implementer-code` → Role `implementer`
  - `tester` → Role `tester`
  - `docs` → Role `scribe`
  - `security-audit` → Role `critic`
  - `ops` → Role `pm` or `scribe`

### Default Taxonomy (Examples)

- `pm` - Project management
- `planner-architecture` - Architecture and design
- `implementer-code` - Code implementation
- `tester` - Testing and validation
- `security-audit` - Security review
- `debugger` - Debugging and troubleshooting
- `docs` - Documentation
- `ops` - Operations and deployment
- `general-purpose` - General assistance

### Entry Formatting Requirement

- Include a first-line marker in the entry body: `Spec: <spec>` to make specialization explicit in the thread record.
- This keeps the Watercooler audit trail self-describing when rendered outside MCP tools.

### Failure Policy

- If `base` and suitable `spec` are not set, Codex will block the write and prompt to set them before proceeding.

### Structured Entry Format

Each Watercooler entry includes:
- **Agent**: Who created the entry (with user tag), e.g., `Codex (user)`
- **Role**: The agent's role (`planner`, `critic`, `implementer`, `tester`, `pm`, `scribe`)
- **Type**: The kind of entry (`Note`, `Plan`, `Decision`, `PR`, `Closure`)
- **Title**: Brief summary
- **Timestamp**: UTC timestamp (ISO 8601 format)
- **Body**: The actual content (markdown supported)

### Agent Roles

- **planner**: Architecture and design decisions
- **critic**: Code review and quality assessment
- **implementer**: Feature implementation and coding
- **tester**: Test coverage and validation
- **pm**: Project management and coordination
- **scribe**: Documentation and notes

### Entry Types

- **Note**: Default type for general observations and updates
- **Plan**: Design proposals and roadmaps
- **Decision**: Architectural or technical decisions
- **PR**: Pull request related entries
- **Closure**: Thread conclusion and summary

## Branch Pairing Contract (Team Invariant)

- **Repositories**: Pair each code repo with a dedicated threads repo named `<repo>-threads`.
- **Branches**: Mirror code branches in the threads repo (same branch name).
- **Write behavior**: Before a write, ensure the threads repo is on the same-named branch; push with rebase+retry.
- **Commit footer convention** (in threads repo):
  - `Code-Repo: <org>/<repo>`
  - `Code-Branch: <branch>`
  - `Code-Commit: <short-sha>`
  - `Watercooler-Entry-ID: <ULID>`
  - `Watercooler-Topic: <topic>`
- **Authoring**: Include a visible `Spec: <value>` in the entry body and align the Role to the specialization (pm/planner/implementer/tester/docs/ops/etc.).
- **Closure**: On merge, post a Closure entry referencing the PR; optionally consolidate to `threads:main` with a brief summary.

## Security & Configuration

### Secrets Management

- **Never commit secrets**: Use environment files like `.env.local` (gitignored)
- **Document required vars**: List all required environment variables in `README.md` or `docs/ENVIRONMENT_VARS.md`
- **Validate external input**: Sanitize user-rendered content
- **Dependency review**: Run `pip-audit` regularly

### Input Validation

- Always validate external input
- Sanitize user-rendered content
- Use specific exception types for error handling
- Provide helpful error messages without exposing sensitive information

### Security Reporting

- For security vulnerabilities, email: [security@watercoolerdev.com](mailto:security@watercoolerdev.com)
- Do not create public GitHub issues for suspected vulnerabilities
- Follow the disclosure timeline in `SECURITY.md`

## Documentation Standards

### Code Documentation

- **Docstrings**: All public functions/classes must have Google-style docstrings
- **Type hints**: All function signatures must have type hints
- **Comments**: Use comments for complex logic only, not obvious code

### User Documentation

Update these files as needed:
- `docs/QUICKSTART.md` - Getting started guide
- `docs/CONFIGURATION.md` - Configuration reference
- `docs/mcp-server.md` - MCP tool reference
- `docs/TROUBLESHOOTING.md` - Common issues
- `README.md` - Project overview

### Documentation Guidelines

- **Clear and concise**: Avoid jargon
- **Examples**: Show don't just tell
- **Up-to-date**: Update docs with code changes
- **Cross-references**: Link to related docs
- **Markdown formatting**: Wrap at ~100 columns, use sentence case headings, prefer relative links

### Documentation Workflow

1. Make code changes
2. Update relevant docs in the same PR
3. Add examples if introducing new features
4. Update `ROADMAP.md` if changing project status

## Development Commands

### Installation

```bash
# Install with all development dependencies
pip install -e ".[dev,mcp]"

# Or with specific extras
pip install -e ".[dev]"  # pytest, mypy, black, ruff
pip install -e ".[mcp]"   # MCP server dependencies
```

### Code Quality

```bash
# Format code
black src/ tests/

# Lint code
ruff check src/ tests/

# Type check
mypy src/

# Run all quality checks
black src/ tests/ && ruff check src/ tests/ && mypy src/
```

### Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=watercooler --cov=watercooler_mcp --cov-report=html

# Run specific test file
pytest tests/test_commands.py
```

### CLI Usage

```bash
# Test CLI
watercooler --help

# Test MCP server
python3 -m watercooler_mcp
```

## Module Organization

- **Core library**: `src/watercooler/` - Main package
  - `agents.py` - Agent registry and canonicalization
  - `cli.py` - CLI entry point
  - `commands.py` - High-level command implementations
  - `config.py` - Configuration resolution
  - `fs.py` - File system operations
  - `header.py` - Header parsing/updating
  - `lock.py` - Advisory file locking
  - `metadata.py` - Thread metadata parsing
  - `templates/` - Built-in templates
- **MCP server**: `src/watercooler_mcp/` - MCP integration
  - `server.py` - FastMCP server implementation
  - `config.py` - MCP-specific configuration
  - `git_sync.py` - Git synchronization
  - `observability.py` - Logging and monitoring
  - `provisioning.py` - Resource provisioning

## Best Practices

1. **Keep dependencies minimal**: Core library should use stdlib-only
2. **File-based operations**: All operations should work with git-friendly markdown files
3. **Zero-config defaults**: Provide sensible defaults that work out-of-box
4. **CLI parity**: Ensure CLI commands match expected workflows
5. **Test coverage**: Maintain high test coverage for core functionality
6. **Type safety**: Use type hints and mypy for type checking
7. **Error handling**: Provide clear, actionable error messages
8. **Documentation**: Keep docs up-to-date with code changes

## Watercooler Tool Requirements

**Mandatory Tool Usage**: When addressing "threads", "watercooler threads", or "wc" (or any variation), you MUST use the watercooler-cloud MCP tools exclusively. Do not attempt to read, write, or manipulate thread files directly.

**Error Handling**: If watercooler tools are requested but:
- The tools don't exist (not available in MCP server list)
- The server is not responsive (timeout or connection errors)
- There is an unresolvable error (authentication, permissions, etc.)

Then you MUST report a clear failure message to the user explaining:
- What operation was attempted
- Why it failed (tool missing, server unresponsive, or specific error)
- What the user should check (MCP server configuration, server status, etc.)

Do not silently fail or attempt workarounds that bypass the watercooler tools.

## When Using Watercooler Tools

When Codex uses Watercooler MCP tools:

1. **Set identity first**: Call `watercooler_set_agent` with `base="Codex"` and appropriate `spec`, or supply `agent_func="<platform>:<model>:<role>"` (e.g., `"Codex:gpt-4:implementer"`) on each write call
2. **Use appropriate roles**: Match the role to the activity (planner, critic, implementer, tester, pm, scribe)
3. **Include Spec marker**: Always include `Spec: <spec>` as the first line of entry body
4. **Meaningful titles**: Use descriptive titles, not generic ones like "Update" or "Done"
5. **Choose correct types**: Use `Decision` for decisions, `Plan` for plans, `PR` for PRs, `Closure` for closures
6. **Manage ball appropriately**: Use `say` for normal back-and-forth, `ack` to keep ball, `handoff` for explicit coordination
